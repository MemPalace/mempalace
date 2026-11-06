"""The owner can widen an existing agent connection's read wings without a new token."""

from tests._commonplace_helpers import (
    functional_worker_transport,  # noqa: F401
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_agent_connections import (
    create,
    fresh as fresh_fixture,
    http,
    seed_memory,
    tool,
)

fresh = fresh_fixture


def _grant(hub, fresh, connection_id, wings):
    return http(
        hub,
        "PATCH",
        f"/api/connections/{connection_id}",
        cookie=fresh.cookie,
        body={"read_wings": wings},
    )


def test_agent_scoped_to_one_wing_cannot_read_another_project_wing(hub, fresh):
    hidden = seed_memory(fresh, "GRANT_LATER_CANARY project decisions", "project_b")
    seed_memory(fresh, "GRANT_SCOPE_CANARY project decisions", "project_a")
    result = create(hub, fresh, read_wings=["project_a"])
    principal = hub.db.authenticate_token(result["token"])
    assert not principal.policy.can_read_wing("project_b")
    assert tool(hub, result["token"], "memory_get", id=hidden)[0]


def test_owner_grants_existing_agent_read_access_to_a_project_wing(hub, fresh):
    granted = seed_memory(fresh, "GRANTED_WING_CANARY project decisions", "project_b")
    seed_memory(fresh, "FIRST_WING_CANARY project decisions", "project_a")
    result = create(hub, fresh, read_wings=["project_a"])
    token, connection_id = result["token"], result["connection"]["id"]

    status, _, _ = _grant(hub, fresh, connection_id, ["project_a", "project_b"])

    assert status == 200
    principal = hub.db.authenticate_token(token)
    assert principal is not None and principal.connection_id == connection_id
    assert principal.policy.can_read_wing("project_a")
    assert principal.policy.can_read_wing("project_b")
    assert not principal.policy.can_read_wing("private.notes")
    assert not tool(hub, token, "memory_get", id=granted)[0]


def test_wing_grant_refuses_private_and_unknown_wings(hub, fresh):
    seed_memory(fresh, "REFUSE_CANARY project decisions", "project_a")
    result = create(hub, fresh, read_wings=["project_a"])
    connection_id = result["connection"]["id"]

    for wings in (["private.notes"], ["missing_wing"], ["project_*"], []):
        assert _grant(hub, fresh, connection_id, wings)[0] == 400

    principal = hub.db.authenticate_token(result["token"])
    assert principal.policy.read_wings == ("project_a",)


def test_agents_screen_renders_and_saves_existing_read_access():
    import shutil
    import subprocess
    from pathlib import Path

    script = r"""
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
const url = (text) => `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`;
const apiSource = await readFile("commonplace/web/static/api.js", "utf8");
const source = await readFile("commonplace/web/static/agents.js", "utf8");
const agents = await import(url(source.replace('"./api.js"', JSON.stringify(url(apiSource)))));
const connection = { id: "conn-one", name: "Agent <one>", auth: "pat", client: "codex", read_wings: ["project_a"] };
const html = agents.agentsView({ connections: [connection], wings: ["project_a", "project_b"] });
assert.ok(html.includes('data-action="agent-wings"'), "Existing agents need a read access action");
assert.ok(html.includes('value="project_b"'), "Owner must be able to pick a project wing");
assert.match(html, /value="project_a"[^>]*checked/);
assert.ok(html.includes("Agent &lt;one&gt;"));
assert.ok(!agents.agentsView({ connections: [{ ...connection, revoked_at: "2026-01-01" }], wings: ["project_a"] }).includes('data-action="agent-wings"'));
const list = {}, parent = { value: "" }, status = {};
const access = { querySelectorAll: () => [{ value: "project_a" }, { value: "project_b" }] };
const section = { dataset: {}, isConnected: true, querySelectorAll: () => [], querySelector: (selector) => selector === "[data-agent-list]" ? list : parent };
const button = { dataset: { action: "agent-wings", agentId: "conn-one" }, closest: (selector) => selector === ".agents-settings" ? section : access };
const requests = [];
globalThis.fetch = async (path, options) => {
  requests.push({ path, options });
  return { ok: true, status: 200, json: async () => options.method === "PATCH" ? { connection: { ...connection, read_wings: ["project_a", "project_b"] } } : { connections: [{ ...connection, read_wings: ["project_a", "project_b"] }], wings: ["project_a", "project_b"] } };
};
await agents.agentAction(button);
assert.equal(requests[0].path, "/api/connections/conn-one");
assert.equal(requests[0].options.method, "PATCH");
assert.deepEqual(JSON.parse(requests[0].options.body), { read_wings: ["project_a", "project_b"] });
assert.ok(list.innerHTML.includes('value="project_b" checked'));
assert.equal(section.dataset.busy, undefined);
"""
    result = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_wing_update_preserves_other_authority_and_is_audited(hub, fresh):
    import json

    seed_memory(fresh, "POLICY_CANARY", "project_a")
    seed_memory(fresh, "POLICY_CANARY", "project_b")
    created = create(hub, fresh, read_wings=["project_a"])
    cid = created["connection"]["id"]
    before = dict(hub.db.connection(cid, fresh.workspace))
    status, _, payload = _grant(hub, fresh, cid, ["wing_project_b", "project_b"])
    assert status == 200
    assert payload["connection"]["read_wings"] == ["project_b"]
    assert "token" not in payload
    after = dict(hub.db.connection(cid, fresh.workspace))
    assert {k: v for k, v in before.items() if k != "policy"} == {
        k: v for k, v in after.items() if k != "policy"
    }
    old_policy, new_policy = json.loads(before["policy"]), json.loads(after["policy"])
    assert {k: v for k, v in old_policy.items() if k != "read_wings"} == {
        k: v for k, v in new_policy.items() if k != "read_wings"
    }
    principal = hub.db.authenticate_token(created["token"])
    assert not principal.policy.can_write_wing("project_b")
    assert not principal.policy.can_read_wing("project_a")
    entries = hub.db.audit_entries(fresh.workspace)
    update = next(row for row in entries if row["action"] == "connection.read_wings")
    assert update["user_id"] == fresh.user and update["connection_id"] == cid
    assert update["decision"] == "ok"


def test_wing_update_requires_owner_session_valid_body_and_live_local_agent(hub, fresh):
    from commonplace import sessions
    from tests.test_commonplace_agent_connections import signup

    seed_memory(fresh, "GUARDS_CANARY", "project_a")
    created = create(hub, fresh, read_wings=["project_a"])
    cid, token = created["connection"]["id"], created["token"]
    path = f"/api/connections/{cid}"
    body = {"read_wings": ["project_a"]}
    member = hub.db.create_user("wing-member@example.test")
    hub.db.add_member(fresh.workspace, member, "member", actor=fresh.user)
    link = sessions.create_login_link(hub.db, "wing-member@example.test", fresh.workspace)
    cookie = f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, link)}"
    assert http(hub, "PATCH", path, body=body)[0] == 401
    assert http(hub, "PATCH", path, token=token, body=body)[0] == 403
    assert http(hub, "PATCH", path, cookie=cookie, body=body)[0] == 403
    assert (
        http(hub, "PATCH", path, cookie=fresh.cookie, body=body, Origin="https://other.example")[0]
        == 403
    )
    assert (
        http(hub, "PATCH", path, cookie=fresh.cookie, body=body, **{"Content-Type": "text/plain"})[
            0
        ]
        == 403
    )
    for invalid in (
        {},
        {**body, "write_wings": ["project_a"]},
        {"read_wings": "project_a"},
        {"read_wings": [None]},
        {"read_wings": ["private_notes"]},
    ):
        assert http(hub, "PATCH", path, cookie=fresh.cookie, body=invalid)[0] == 400
    other = signup(hub, "wing-other@example.test")
    assert http(hub, "PATCH", path, cookie=other.cookie, body=body)[0] == 404
    hub.db.revoke(cid, actor=fresh.user, workspace_ref=fresh.workspace)
    assert _grant(hub, fresh, cid, ["project_a"])[0] == 409


def test_agent_listing_offers_only_existing_nonprivate_wings(hub, fresh):
    seed_memory(fresh, "LIST_CANARY", "project_a")
    seed_memory(fresh, "PRIVATE_CANARY", "private.notes")
    created = create(hub, fresh, read_wings=["project_a"])
    status, _, payload = http(hub, "GET", cookie=fresh.cookie)
    assert status == 200
    assert "project_a" in payload.get("wings", [])
    assert "private_notes" not in payload["wings"]
    assert "private.notes" not in payload["wings"]
    row = next(c for c in payload["connections"] if c["id"] == created["connection"]["id"])
    assert row["read_wings"] == ["project_a"]
