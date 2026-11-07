"""Self-service PATs through real HTTP and MCP in a newly signed-up workspace."""

import json
import logging
import re
import shutil
import subprocess
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import sessions, urls
from commonplace.boards import BoardService
from commonplace.extensions import ToolContext
from commonplace.rules import RuleService, checks, render
from commonplace.social import accounts
from commonplace.social.providers import Profile
from tests._commonplace_helpers import (
    functional_worker_transport,  # noqa: F401
    hub_fixture,  # noqa: F401
)


def http(hub, method, path="/api/connections", *, cookie=None, token=None, body=None, **headers):
    headers = {"Content-Type": "application/json", **headers}
    if cookie:
        headers["Cookie"] = cookie
    if token:
        headers["Authorization"] = f"Bearer {token}"
    connection = HTTPConnection("127.0.0.1", hub.port, timeout=3)
    try:
        connection.request(method, path, None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), json.loads(raw) if raw else None
    finally:
        connection.close()


def signup(hub, email):
    user = accounts.resolve(hub.db, "test", Profile(email, "New person", (email,)), signup=True)
    workspace = sessions.home_workspace(hub.db, user)
    link = sessions.create_login_link(hub.db, email, workspace)
    status, headers, _ = http(hub, "GET", f"/app/login?t={link}")
    assert status == 302
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    person = sessions.person_principal(hub.db, user, workspace)
    assert not hub.db.user(user)["is_admin"]
    assert hub.db.workspace(workspace)["personal_of"] == user
    return SimpleNamespace(
        user=user, workspace=workspace, cookie=cookie, ctx=ToolContext(person, hub.gateway)
    )


@pytest.fixture
def fresh(hub, config, monkeypatch):
    person = signup(hub, "fresh@example.test")
    person.directory = Path(hub.supervisor.workspace_dir(person.workspace))
    monkeypatch.setattr(config, "_palace_path_override", str(person.directory / "palace"))
    monkeypatch.setattr(urls, "AGENT_URL", "https://cmmn.dev")
    return person


def create(hub, fresh, name="my-agent", **body):
    status, headers, payload = http(hub, "POST", cookie=fresh.cookie, body={"name": name, **body})
    assert status == 201
    assert headers["Cache-Control"] == "no-store"
    assert payload["token"].startswith("cmmn_") and payload["mcp_url"] == "https://cmmn.dev/mcp"
    return payload


def tool(hub, token, name, **arguments):
    result = hub.rpc(token, "tools/call", {"name": name, "arguments": arguments})
    assert "error" not in result
    return result["result"].get("isError", False), result["result"]["content"][0]["text"]


def seed_memory(fresh, text, wing):
    return fresh.ctx.memory(
        "mempalace_add_drawer", {"content": text, "wing": wing, "room": "notes"}
    )["drawer_id"]


def test_fresh_user_creates_connects_and_claims_without_admin(hub, fresh):
    result = create(hub, fresh, client="claude-code")
    token, connection = result["token"], result["connection"]
    principal = hub.db.authenticate_token(token)
    assert principal.workspace_id == fresh.workspace and principal.user_id == fresh.user
    assert principal.parent_agent is None
    assert principal.policy.tools == "slim" and principal.policy.trust == "untrusted"
    assert principal.policy.write_wing == "inbox"
    assert not principal.policy.has_scope("admin")
    assert not principal.policy.can_read_wing("private.notes")
    assert not principal.policy.can_write_wing("shared")
    lifetime = hub.db.connection(connection["id"], fresh.workspace)
    from datetime import datetime

    assert (
        datetime.fromisoformat(lifetime["expires_at"])
        - datetime.fromisoformat(lifetime["created_at"])
    ).days == 90
    initialized = hub.rpc(
        token,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "acceptance", "version": "1"},
        },
    )
    assert "session_start" in initialized["result"]["instructions"]
    assert {"memory_save", "rules_publish", "task_add", "task_take_next"} <= hub.tool_names(token)
    assert hub.raw(token, {"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
    failed, text = tool(hub, token, "task_add", title="First task")
    assert not failed
    number = int(re.search(r"#(\d+)", text)[1])
    BoardService(fresh.ctx).task_approve(number)
    failed, text = tool(hub, token, "task_take_next", task=number)
    assert not failed
    card = BoardService(fresh.ctx).task_show(number)
    assert card["assignee"] == "conn:my-agent" and card["claim_until"]
    assert card["proposal_status"] is None


@pytest.mark.usefixtures("functional_worker_transport")
def test_initialize_uses_canonical_rules_and_memory_and_writes_stay_proposals(
    hub, fresh, monkeypatch
):
    monkeypatch.setattr(checks, "monotonic", lambda: 0.0)
    memory = "Workspace decisions: keep these exact words. Ω\n  Preserve spacing."
    seed_memory(fresh, memory, "shared")
    seed_memory(fresh, "PRIVATE_STARTUP_CANARY", "private.notes")
    rules = RuleService(fresh.ctx)
    rule = "Keep all workspace decisions verbatim."
    rules.create("*", rule, "instruction")
    token = create(hub, fresh)["token"]
    instructions = hub.rpc(token, "initialize", {})["result"]["instructions"]
    assert render.quote(rule) in instructions and render.quote(memory) in instructions
    assert "PRIVATE_STARTUP_CANARY" not in instructions
    failed, text = tool(hub, token, "memory_save", text="AGENT_MEMORY_CANARY", theme="shared")
    assert not failed and text.startswith("saved ")
    drawer = fresh.ctx.memory("mempalace_get_drawer", {"drawer_id": text.split()[1]})
    assert drawer["metadata"]["wing"] == "inbox"
    failed, text = tool(
        hub,
        token,
        "rules_publish",
        agent="*",
        rules=[{"key": "agent-proposal", "text": "PROPOSED_RULE_CANARY", "kind": "instruction"}],
    )
    assert not failed and "proposed" in text
    proposal = next(row for row in rules.list_rules()["rules"] if row["key"] == "agent-proposal")
    assert proposal["status"] == "proposed"
    instructions = hub.rpc(token, "initialize", {})["result"]["instructions"]
    assert "PROPOSED_RULE_CANARY" not in instructions


def test_rotation_preserves_actor_claims_and_policy_then_revocation_is_immediate(
    hub, fresh, caplog
):
    caplog.set_level(logging.DEBUG, logger="commonplace")
    created = create(hub, fresh)
    cid, old = created["connection"]["id"], created["token"]
    failed, text = tool(hub, old, "task_add", title="Keep my claim", claim=True)
    assert not failed
    number = int(re.search(r"#(\d+)", text)[1])
    BoardService(fresh.ctx).task_approve(number)
    assert not tool(hub, old, "task_take_next", task=number)[0]
    policy = hub.db.connection(cid, fresh.workspace)["policy"]
    path = f"/api/connections/{cid}"
    status, headers, rotated = http(hub, "POST", path + "/rotate", cookie=fresh.cookie, body={})
    assert status == 200 and headers["Cache-Control"] == "no-store"
    new = rotated["token"]
    assert new != old and rotated["connection"]["id"] == cid
    assert hub.db.connection(cid, fresh.workspace)["policy"] == policy
    assert hub.raw(old, {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 401
    assert not tool(hub, new, "task_heartbeat", task=1)[0]
    status, _, listed = http(hub, "GET", cookie=fresh.cookie)
    assert status == 200
    assert listed["connections"][0]["id"] == cid
    assert http(hub, "DELETE", path, cookie=fresh.cookie)[0] == 200
    assert http(hub, "DELETE", path, cookie=fresh.cookie)[0] == 200
    assert hub.raw(new, {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 401
    assert http(hub, "POST", path + "/rotate", cookie=fresh.cookie, body={})[0] == 409
    durable = json.dumps(
        {
            "connections": [dict(row) for row in hub.db.connections()],
            "audit": [dict(row) for row in hub.db.audit_entries()],
        }
    )
    for secret in (old, new):
        assert (
            secret not in durable and secret not in json.dumps(listed) and secret not in caplog.text
        )
    assert {row["action"] for row in hub.db.audit_entries()} >= {
        "token.create",
        "token.rotate",
        "token.revoke",
    }


def test_second_user_cannot_list_rotate_revoke_or_read_the_first_users_data(hub, fresh):
    created = create(hub, fresh)
    cid, first = created["connection"]["id"], created["token"]
    assert not tool(hub, first, "task_add", title="FIRST_USER_CARD_CANARY")[0]
    BoardService(fresh.ctx).task_approve(1)
    other = signup(hub, "second@example.test")
    second = create(hub, other)["token"]
    _, _, listed = http(hub, "GET", cookie=other.cookie)
    assert len(listed["connections"]) == 1 and listed["connections"][0]["id"] != cid
    assert (
        http(hub, "POST", f"/api/connections/{cid}/rotate", cookie=other.cookie, body={})[0] == 404
    )
    assert http(hub, "DELETE", f"/api/connections/{cid}", cookie=other.cookie)[0] == 404
    assert http(hub, "POST", cookie=other.cookie, body={"name": "child", "parent": cid})[0] == 400
    assert "FIRST_USER_CARD_CANARY" not in tool(hub, second, "task_list")[1]
    assert not tool(hub, first, "task_take_next", task=1)[0]


def test_child_proposals_still_need_parent_approval_after_parent_rotation(hub, fresh):
    parent = create(hub, fresh, name="parent")
    assert not tool(hub, parent["token"], "task_add", title="Parent work", claim=True)[0]
    child = create(hub, fresh, name="child", parent=parent["connection"]["id"])
    failed, text = tool(hub, child["token"], "task_add", title="Child proposal", claim=True)
    assert not failed
    number = int(re.search(r"#(\d+)", text)[1])
    proposal = BoardService(fresh.ctx).task_show(number)
    assert proposal["proposal_status"] == "pending"
    assert proposal["proposed_to"] == "conn:parent" and proposal["status"] == "backlog"
    assert tool(hub, child["token"], "task_take_next", task=number)[0]
    assert tool(hub, child["token"], "task_approve", task=number)[0]
    _, _, rotated = http(
        hub,
        "POST",
        f"/api/connections/{parent['connection']['id']}/rotate",
        cookie=fresh.cookie,
        body={},
    )
    assert not tool(hub, rotated["token"], "task_approve", task=number, reason="synthetic reason")[
        0
    ]
    assert tool(hub, child["token"], "task_take_next", task=number)[0]
    assert BoardService(fresh.ctx).task_show(number)["approved_by"] is None
    BoardService(fresh.ctx).task_approve(number)
    assert not tool(hub, child["token"], "task_take_next", task=number)[0]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": []},
        {"name": "a b"},
        {"name": "x", "client": None},
        {"name": "x", "parent": None},
        {"name": "x", "trust": "trusted"},
        {"name": "x", "user_id": "another"},
        {"name": "x", "workspace": "another"},
        {"name": "x", "read_wings": []},
        {"name": "x", "read_wings": "shared"},
        {"name": "x", "read_wings": [None]},
        {"name": "x", "read_wings": ["*"]},
        {"name": "x", "read_wings": ["private.notes"]},
    ],
)
def test_invalid_requests_never_create_connections(hub, fresh, body):
    assert http(hub, "POST", cookie=fresh.cookie, body=body)[0] == 400
    assert hub.db.connections(fresh.workspace) == []


def test_duplicate_names_and_inactive_parents_are_refused(hub, fresh):
    parent = create(hub, fresh)
    assert http(hub, "POST", cookie=fresh.cookie, body={"name": "my-agent"})[0] == 400
    hub.db.revoke(parent["connection"]["id"], actor=fresh.user, workspace_ref=fresh.workspace)
    assert (
        http(
            hub,
            "POST",
            cookie=fresh.cookie,
            body={"name": "child", "parent": parent["connection"]["id"]},
        )[0]
        == 400
    )


def test_only_owner_sessions_manage_agents_and_csrf_guard_applies(hub, fresh):
    created = create(hub, fresh)
    cid = created["connection"]["id"]
    mutations = [
        ("POST", "/api/connections", {"name": "new"}),
        ("POST", f"/api/connections/{cid}/rotate", {}),
        ("DELETE", f"/api/connections/{cid}", None),
    ]
    member = hub.db.create_user("member@example.test")
    hub.db.add_member(fresh.workspace, member, "member", actor=fresh.user)
    link = sessions.create_login_link(hub.db, "member@example.test", fresh.workspace)
    cookie = f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, link)}"
    for method, path, body in mutations:
        assert http(hub, method, path, body=body)[0] == 401
        assert http(hub, method, path, token=created["token"], body=body)[0] == 403
        assert http(hub, method, path, cookie=cookie, body=body)[0] == 403
        assert (
            http(
                hub, method, path, cookie=fresh.cookie, body=body, Origin="https://foreign.example"
            )[0]
            == 403
        )
    assert hub.db.authenticate_token(created["token"])


def test_explicit_wing_scope_does_not_read_other_workspace_wings(hub, fresh):
    visible = seed_memory(fresh, "SCOPED_VISIBLE_CANARY workspace decisions", "project_a")
    hidden = seed_memory(fresh, "SCOPED_HIDDEN_CANARY workspace decisions", "project_b")
    result = create(hub, fresh, read_wings=["project_a"])
    principal = hub.db.authenticate_token(result["token"])
    assert principal.policy.read_wings == ("project_a",)
    assert not principal.policy.can_read_wing("shared") and not principal.policy.can_read_wing(
        "project_b"
    )
    assert not tool(hub, result["token"], "memory_get", id=visible)[0]
    assert tool(hub, result["token"], "memory_get", id=hidden)[0]
    failed, text = tool(hub, result["token"], "memory_search", query="workspace decisions")
    assert not failed and "SCOPED_VISIBLE_CANARY" in text and "SCOPED_HIDDEN_CANARY" not in text
    assert (
        http(
            hub,
            "POST",
            cookie=fresh.cookie,
            body={"name": "unknown", "read_wings": ["missing_wing"]},
        )[0]
        == 400
    )
    other = signup(hub, "scoped-second@example.test")

    # A wing in another workspace does not qualify as a known local wing.
    def no_wings(principal, name, arguments):
        assert principal.workspace_id == other.workspace and name == "mempalace_list_wings"
        return {"wings": {}}

    original = hub.gateway.call_memory_tool
    hub.gateway.call_memory_tool = no_wings
    try:
        assert (
            http(
                hub,
                "POST",
                cookie=other.cookie,
                body={"name": "intruder", "read_wings": ["project_a"]},
            )[0]
            == 400
        )
    finally:
        hub.gateway.call_memory_tool = original


def test_client_configs_use_the_native_bearer_formats():
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node is required to verify the shipped client config generator")
    source = Path("commonplace/web/static/agents.js").read_text()
    # Execute the actual generator without a DOM or browser.
    start, end = source.index("export function clientConfig"), source.index("const destinations")
    script = (
        source[start:end].replace("export function", "function")
        + '\nprocess.stdout.write(JSON.stringify(["claude-code", "codex", "cursor", "generic"].map(client => clientConfig(client, "https://cmmn.dev/mcp", "cmmn_EXAMPLE"))));'
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    claude, codex, cursor, generic = json.loads(result.stdout)
    assert json.loads(claude)["mcpServers"]["commonplace"] == {
        "type": "http",
        "url": "https://cmmn.dev/mcp",
        "headers": {"Authorization": "Bearer cmmn_EXAMPLE"},
    }
    assert "type" not in json.loads(cursor)["mcpServers"]["commonplace"]
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    server = tomllib.loads(codex)["mcp_servers"]["commonplace"]
    assert server == {
        "url": "https://cmmn.dev/mcp",
        "http_headers": {"Authorization": "Bearer cmmn_EXAMPLE"},
    }
    assert json.loads(generic) == {
        "url": "https://cmmn.dev/mcp",
        "headers": {"Authorization": "Bearer cmmn_EXAMPLE"},
    }
