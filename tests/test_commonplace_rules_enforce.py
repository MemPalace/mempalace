"""Opt-in write freshness on a new account, through real gateway dispatch/routes."""

import ast
import copy
import inspect
import json
import sqlite3
import threading
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import events, extensions, memory_tools, sessions, webhooks
from commonplace.boards import BoardService
from commonplace.control_db import ControlDB
from commonplace.extensions import ToolContext
from commonplace.gateway import Gateway
from commonplace.handoff import wiring
from commonplace.policy import GATEWAY_NEVER, Policy
from commonplace.rules import RuleService, checks, enforce
from commonplace.rules.service import effective_rules
from mempalace.tool_capabilities import TOOL_CAPABILITIES
from tests.test_handoff_api import HANDOFF_BODY, _Data, _Recall, _Service, _Settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("COMMONPLACE_RULES_CHECK", "COMMONPLACE_RULES_INJECT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    user = db.create_user("new-owner@example.test")
    workspace = db.create_workspace("new-workspace", user)
    directory = tmp_path / "workspace"
    gateway = Gateway(db, SimpleNamespace(root=tmp_path, workspace_dir=lambda _: directory))
    worker_calls = []

    def worker(_principal, request):
        worker_calls.append(request)
        if request["method"] == "tools/list":
            result = {"tools": []}
        elif request["method"] == "initialize":
            result = {"instructions": "Original worker instructions."}
        else:
            result = {"content": [{"type": "text", "text": '{"ok":true}'}]}
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}

    gateway.mediator.workers = SimpleNamespace(post=worker)

    def agent(name="builder", **overrides):
        values = dict(
            profile="full",
            scopes=[
                "memory:read",
                "memory:write",
                "kg:read",
                "kg:write",
                "handoff:write",
                "threads:read",
            ],
            read_wings=["*"],
            write_wings=["*"],
            trust="trusted",
        )
        values.update(overrides)
        _, token = db.create_token(
            workspace_ref=workspace,
            user_ref=user,
            name=name,
            client="test",
            policy=Policy.build(**values),
            actor=user,
        )
        return token, gateway.authenticate("Bearer " + token)

    token, principal = agent()
    person = sessions.person_principal(db, user, workspace)
    ctx = ToolContext(principal, gateway)
    owner = RuleService(ToolContext(person, gateway))
    try:
        yield SimpleNamespace(
            db=db,
            gateway=gateway,
            workspace=workspace,
            user=user,
            directory=directory,
            token=token,
            principal=principal,
            person=person,
            ctx=ctx,
            owner=owner,
            rules=RuleService(ctx),
            agent=agent,
            worker_calls=worker_calls,
        )
    finally:
        webhooks.stop(gateway)
        db.close()


def call(env, name, *, principal=None, **args):
    return env.gateway.mediator.handle(
        principal or env.principal,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )


def text(response):
    return response["result"]["content"][0]["text"]


def refresh(env, principal=None):
    response = call(env, "rules_get", principal=principal, effective=True, format="md")
    assert not response["result"].get("isError"), response
    return int(text(response).splitlines()[-1].split()[1])


def setup(env, mode="enforce"):
    owner_board = BoardService(env.owner.ctx)
    owner_board.board_add(slug="work", wing="shared")
    card = BoardService(env.ctx).task_add(board="work", title="Original", claim=True)
    env.owner.create("*", 'Keep every word.\n"Quoted" @reference', "instruction")
    env.owner.set_check("*", mode)
    return card


def last_audit(env):
    return next(row for row in env.db.audit_entries() if row["action"] == "tool.call")


@pytest.fixture
def http(env, tmp_path, monkeypatch):
    # The real routes/authorization use in-memory handoff services and a memory
    # sink, keeping this file independent of model/embedding/launcher runtimes.
    ctx = wiring.install(env.gateway, tmp_path / "handoff")
    original_service = ctx.service
    calls = []
    data = _Data(env.workspace, "other-workspace", calls)
    data.records["hof_local"] = replace(
        data.records["hof_local"], connection_id=env.principal.connection_id
    )
    ctx.handoff_db, ctx.settings = data, _Settings(data, calls)
    ctx.service, ctx.recall = (
        _Service(data, calls),
        _Recall(env.workspace, "other-workspace", calls),
    )
    monkeypatch.setattr(memory_tools, "_save", lambda _ctx, args: ("memory", args["text"]))
    monkeypatch.setattr(memory_tools, "_file_fact", lambda _ctx, _args: {"triple_id": 1})
    server = env.gateway.build_server("127.0.0.1", 0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()

    def request(method, path, body=None, seq=None, *, person=False):
        headers = {"Content-Type": "application/json"}
        if person:
            session = sessions.redeem_login_link(
                env.db,
                sessions.create_login_link(
                    env.db,
                    env.person.user_email,
                    env.workspace,
                ),
            )
            headers["Cookie"] = f"{sessions.COOKIE}={session}"
        else:
            headers["Authorization"] = "Bearer " + env.token
        if seq is not None:
            headers["Commonplace-Rules-Seq"] = str(seq)
        conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            conn.close()

    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        ctx.service = original_service
        ctx.close()


def test_fresh_account_bootstraps_and_legacy_off_clients_keep_working(env):
    assert not env.directory.exists()
    response = call(env, "task_add", title="First task", claim=True, rules_seq={"old": "ignored"})
    assert not response["result"].get("isError"), response
    assert BoardService(env.ctx).task_show(1)["title"] == "First task"
    assert not (env.directory / "rules.sqlite3").exists()
    env.owner.create("*", "Accepted rule", "limit")
    assert not call(env, "task_update", task=1, title="Legacy update")["result"].get("isError")
    assert last_audit(env)["reason"] == ""


def test_mcp_stale_refresh_success_without_mutation_and_provenance(env, http):
    setup(env)

    def rpc(name, **args):
        status, response, _ = http(
            "POST",
            "/mcp",
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        )
        assert status == 200
        return response

    seq = int(text(rpc("rules_get", effective=True, format="md")).splitlines()[-1].split()[1])
    accepted = env.owner.create("*", "New approved rule", "limit")
    before = events.latest_seq(env.db, env.workspace)
    response = rpc("task_update", task=1, title="Forbidden", rules_seq=seq)
    assert response["result"]["isError"] and text(response) == enforce.MESSAGES["stale"]
    assert "rules_get format=md effective=true" in text(response)
    assert BoardService(env.ctx).task_show(1)["title"] == "Original"
    assert events.latest_seq(env.db, env.workspace) == before
    assert last_audit(env)["reason"] == "rules_seq stale; transport=mcp"
    read = rpc("rules_get", effective=True, format="md")
    assert '- "Keep every word.\\n\\"Quoted\\" \\u0040reference"' in text(read)
    current = int(text(read).splitlines()[-1].split()[1])
    assert current != seq
    response = rpc("task_update", task=1, title="Updated", rules_seq=str(current))
    assert not response["result"].get("isError")
    assert BoardService(env.ctx).task_show(1)["title"] == "Updated"
    row = next(r for r in env.rules.rules_get()["rules"] if r["id"] == accepted["id"])
    assert row["source"] == "owner" and row["updated_by"] == env.person.actor


@pytest.mark.parametrize("value", [None, True, -1, 1.5, {}, [], "abc", "-1", " 123", "１２３"])
def test_invalid_versions_rejected_before_write(env, value):
    setup(env)
    refresh(env)
    response = call(env, "task_update", task=1, title="Forbidden", rules_seq=value)
    assert response["result"]["isError"] and text(response) == enforce.MESSAGES["missing"]
    assert BoardService(env.ctx).task_show(1)["title"] == "Original"


def test_initialize_does_not_serve_but_session_start_refreshes(env, monkeypatch):
    setup(env)
    from commonplace import session

    monkeypatch.setattr(session, "_memories", lambda *_: 'Memory data: "quoted memory"')
    current = effective_rules(env.ctx)["rules_seq"]
    response = env.gateway.mediator.handle(env.principal, {"id": 2, "method": "initialize"})
    instructions = response["result"]["instructions"]
    assert "Automatically call session_start" in instructions
    assert "Original worker instructions." in instructions
    assert "Keep every word." in instructions
    response = call(env, "task_update", task=1, title="Forbidden", rules_seq=current)
    assert text(response) == enforce.MESSAGES["not served"]
    response = call(env, "session_start")
    assert 'Memory data: "quoted memory"' in text(response)
    assert f"rules_seq {current}" in text(response)
    assert not call(env, "task_update", task=1, title="After session", rules_seq=current)[
        "result"
    ].get("isError")


def test_schema_modes_copy_registry_and_do_not_change_people_or_read_tools(env):
    setup(env, "off")
    originals = {
        name: copy.deepcopy(tool.input_schema) for name, tool in extensions.REGISTRY.items()
    }

    def listing(principal=None):
        return env.gateway.mediator.handle(
            principal or env.principal, {"id": 3, "method": "tools/list"}
        )["result"]["tools"]

    off = listing()
    person_off = listing(env.person)
    off_by_name = {tool["name"]: tool for tool in off}
    for mode in ("warn", "enforce"):
        env.owner.set_check("*", mode)
        agent_tools = listing()
        assert {tool["name"] for tool in agent_tools} == off_by_name.keys()
        for tool in agent_tools:
            name, schema = tool["name"], tool["inputSchema"]
            if name not in enforce.CHECKED_WRITES:
                assert tool == off_by_name[name]
            assert ("rules_seq" in schema.get("properties", {})) == (name in enforce.CHECKED_WRITES)
            assert ("rules_seq" in schema.get("required", [])) == (
                mode == "enforce" and name in enforce.CHECKED_WRITES and name != "ask"
            )
            if name in enforce.CHECKED_WRITES:
                schema["properties"]["rules_seq"]["oneOf"][0]["type"] = "changed"
        assert {name: tool.input_schema for name, tool in extensions.REGISTRY.items()} == originals
        assert listing(env.person) == person_off
    env.owner.set_check(env.principal.actor, "off")
    assert listing() == off


def test_warn_old_clients_keep_json_and_notice_separate(env):
    setup(env, "warn")
    response = call(env, "space_post", board="work", text="Exact words")
    blocks = response["result"]["content"]
    assert json.loads(blocks[0]["text"])["text"] == "Exact words"
    assert len(blocks) == 2 and "rules_get" in blocks[1]["text"]
    assert not response["result"].get("isError")
    assert last_audit(env)["reason"] == "rules_seq missing (warn); transport=mcp"


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_local_denial_keeps_exception_reason_with_reentrant_audit_lock(env, mode):
    setup(env, mode)
    args = {"rules_seq": refresh(env)} if mode == "enforce" else {}
    with env.db._lock:
        response = call(env, "task_update", task=999999, title="Missing card", **args)
        entry = last_audit(env)
    assert response["result"]["isError"]
    assert entry["decision"] == "deny"
    assert entry["reason"] == text(response)[:200]
    assert "rules_seq" not in entry["reason"]


def test_rest_notice_is_cleared_after_failure_before_headers(env):
    setup(env, "warn")
    handler = SimpleNamespace(headers={})
    with pytest.raises(RuntimeError, match="before headers"):
        with enforce.rest_write(env.ctx, handler, "task_comment", {}):
            assert "needs rules_seq" in handler._rules_notice
            raise RuntimeError("Response failed before headers")
    assert not handler._rules_notice
    env.owner.set_check("*", "off")
    with enforce.rest_write(env.ctx, handler, "task_comment", {}):
        assert not handler._rules_notice


def test_people_internal_calls_and_heartbeat_exempt_owner_tokens_checked(env):
    setup(env)
    assert call(env, "task_update", task=1, title="Agent denied")["result"]["isError"]
    assert not call(env, "task_update", principal=env.person, task=1, title="Person edit")[
        "result"
    ].get("isError")
    with checks.internal_call():
        assert not call(env, "task_update", task=1, title="Internal edit")["result"].get("isError")
    assert call(env, "task_update", task=1, title="Still denied")["result"]["isError"]
    assert not call(env, "task_heartbeat", task=1)["result"].get("isError")


def test_served_state_is_connection_scoped_and_default_reads_do_not_serve(env):
    setup(env)
    current = effective_rules(env.ctx)["rules_seq"]
    call(env, "rules_get")
    call(env, "rules_changed", since=0)
    assert (
        text(call(env, "task_update", task=1, title="No read", rules_seq=current))
        == enforce.MESSAGES["not served"]
    )
    refresh(env)
    _, other = env.agent("other")
    other_ctx = ToolContext(other, env.gateway)
    other_seq = effective_rules(other_ctx)["rules_seq"]
    assert (
        text(
            call(env, "task_comment", principal=other, task=1, text="No read", rules_seq=other_seq)
        )
        == enforce.MESSAGES["not served"]
    )


@pytest.mark.parametrize("name", sorted(enforce.CHECKED_WRITES))
def test_every_checked_mcp_tool_rejects_before_dispatch(env, name, monkeypatch):
    setup(env)
    if name == "handoff_start":
        monkeypatch.setitem(
            extensions.REGISTRY, name, replace(extensions.get(name), available=None)
        )
    before = events.latest_seq(env.db, env.workspace)
    response = call(env, name)
    assert text(response) == enforce.MESSAGES["missing"]
    assert response["result"]["isError"]
    assert events.latest_seq(env.db, env.workspace) == before


def test_hidden_owner_tools_keep_authorization_refusal_before_freshness(env):
    setup(env)
    member = env.db.create_user("member@example.test")
    env.db.add_member(env.workspace, member, "member", actor=env.user)
    _, token = env.db.create_token(
        workspace_ref=env.workspace,
        user_ref=member,
        name="member",
        client="test",
        policy=env.principal.policy,
        actor=env.user,
    )
    principal = env.gateway.authenticate("Bearer " + token)
    response = call(env, "board_set", principal=principal, board="work")
    assert response["error"]["message"] == "Unknown tool: board_set"


def test_overcap_ask_recovery_after_trim(env):
    setup(env)
    rows = [env.owner.create("*", str(i) + "\u200b" * 990, "instruction") for i in range(5)]
    response = call(env, "rules_get", effective=True, format="md")
    assert "rules_seq too_large" in text(response)
    # Check precedes argument validation; use valid ask arguments to reach the owner.
    args = dict(
        question="Should this rule set be trimmed now?",
        context="The current rules do not fit in one reply.",
        options=["Trim the rules", "Keep the rules"],
        task=1,
    )
    response = call(env, "ask", rules_seq={"malformed": True}, **args)
    assert not response["result"].get("isError"), response
    assert text(call(env, "task_comment", task=1, text="Blocked")) == enforce.MESSAGES["too_large"]
    for row in rows[-2:]:
        env.owner.action(row["id"], row["version"], "retire")
    assert text(call(env, "task_comment", task=1, text="Read again")) == enforce.MESSAGES["missing"]
    assert not call(env, "task_comment", task=1, text="Recovered", rules_seq=refresh(env))[
        "result"
    ].get("isError")


@pytest.mark.parametrize("mode", ["off", "warn", "enforce"])
def test_lost_store_guard_in_every_mode_without_recreation(env, mode):
    setup(env, mode)
    if mode != "off":
        refresh(env)
    else:
        call(env, "rules_get", effective=True)
    path = env.directory / "rules.sqlite3"
    path.unlink()
    assert not call(env, "task_comment", task=1, text="Fail open")["result"].get("isError")
    assert not path.exists()
    if mode != "off":
        suffix = " (warn)" if mode == "warn" else ""
        assert last_audit(env)["reason"] == f"rules_seq unchecked{suffix}; transport=mcp"
    assert any(row["kind"] == "rule.store_lost" for row in events.after(env.db, env.workspace))


def test_unknown_store_alarm_after_six_unchecked_writes(env, monkeypatch):
    setup(env)
    monkeypatch.setattr(
        checks,
        "reconcile",
        lambda *_a, **_k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    for _ in range(7):
        response = call(env, "task_comment", task=1, text="No notice on unknown state")
        assert not response["result"].get("isError")
        assert len(response["result"]["content"]) == 1
    alarms = [row for row in events.after(env.db, env.workspace) if row["kind"] == "rule.unchecked"]
    assert len(alarms) == 1 and alarms[0]["subject"] == env.principal.connection_id


def test_rules_get_unavailable_policy_warns_instead_of_deadlocking(env):
    setup(env)
    _, principal = env.agent("writer-only", tools=["task_comment"])
    response = call(env, "task_comment", principal=principal, task=1, text="Can keep working")
    assert not response["result"].get("isError")
    assert "cannot read rules" in response["result"]["content"][1]["text"]


def test_checked_and_exempt_inventory_covers_all_gateway_writes():
    extensions.load_modules()
    writes = {name for name, tool in extensions.REGISTRY.items() if tool.kind == "write"}
    writes |= {
        name
        for name, (kind, _) in TOOL_CAPABILITIES.items()
        if kind != "read" and name not in GATEWAY_NEVER
    }
    assert writes == enforce.CHECKED_WRITES | enforce.EXEMPT_WRITES.keys()
    assert not enforce.CHECKED_WRITES & enforce.EXEMPT_WRITES.keys()
    assert all(enforce.EXEMPT_WRITES.values())
    documentation = Path("docs/design/RULES.md").read_text()
    assert all(f"`{name}`" in documentation for name in enforce.EXEMPT_WRITES)


@pytest.mark.parametrize(
    "route", [r for r in enforce.REST_ROUTES if r.kind == "checked"], ids=lambda r: r.tool
)
def test_checked_rest_shapes_refuse_then_accept_fresh_header(env, http, route):
    card = setup(env)
    bodies = {
        "task_update": {"version": card["version"], "title": "Changed"},
        "task_comment": {"text": "Comment"},
        "task_handoff": {
            "to": env.person.actor,
            "note": "Should this work proceed now or wait?",
            "context": "The implementation is ready for an owner decision.",
            "options": ["Proceed now", "Wait for more information"],
        },
        "decide": {"version": card["version"], "reply": "Proceed"},
        "task_approve": {"approve": True},
        "task_add": {"title": "New card"},
        "board_add": {"slug": "new", "wing": "shared"},
        "board_set": {"coordinator": None},
        "space_post": {"text": "Chat"},
        "memory_save": {"text": "Exact memory", "theme": "shared"},
        "fact_save": {"subject": "Avery", "predicate": "likes", "object": "tea"},
        "relaunch": {},
    }
    if route.tool == "decide":
        card = BoardService(env.owner.ctx).ask(
            "Should this change proceed now?",
            task=1,
            context="The implementation is ready for a decision.",
            options=["Proceed", "Wait"],
        )
        bodies["decide"]["version"] = card["version"]
    if route.tool == "task_approve":
        _, token = env.db.create_token(
            workspace_ref=env.workspace,
            user_ref=env.user,
            name="child",
            client="test",
            policy=env.principal.policy,
            actor=env.user,
            parent=env.principal.connection_id,
        )
        child = env.gateway.authenticate("Bearer " + token)
        card = BoardService(ToolContext(child, env.gateway)).task_add(
            board="work", title="Proposal"
        )
    path = (
        route.path.replace("<n>", str(card["number"]))
        .replace("<slug>", "work")
        .replace("<id>", "hof_local")
    )
    seq = refresh(env)
    owner_board = BoardService(env.owner.ctx)
    original_card = owner_board.task_show(card["number"])
    before = events.latest_seq(env.db, env.workspace)
    body = {**bodies[route.tool], "rules_seq": seq}
    status, response, _ = http(route.method, path, body)
    assert status == 428 and response == {
        "error": enforce.MESSAGES["missing"],
        "rules_refresh": True,
    }
    assert owner_board.task_show(card["number"]) == original_card
    assert events.latest_seq(env.db, env.workspace) == before
    assert last_audit(env)["reason"] == "rules_seq missing; transport=rest"
    status, response, headers = http(route.method, path, body, seq)
    if route.tool == "decide":
        assert status == 403 and response == {
            "error": "only a signed-in workspace owner can answer this"
        }
        assert owner_board.task_show(card["number"]) == original_card
        assert events.latest_seq(env.db, env.workspace) == before
        denied = last_audit(env)
        assert denied["decision"] == "deny" and denied["tool"] == "decide"
        assert denied["reason"] == "transport=rest"
        assert denied["connection_id"] == env.principal.connection_id
        assert denied["auth"] == env.principal.auth

        status, response, headers = http(route.method, path, bodies["decide"], person=True)
        assert status == 200 and response["status"] == "todo"
        assert response["ask"] is None and response["ask_to"] is None
        decision = owner_board.task_show(card["number"])["events"][-1]
        assert decision["kind"] == "decide" and decision["actor"] == env.person.actor
        accepted = last_audit(env)
        assert accepted["tool"] == "decide" and accepted["reason"] == "transport=rest"
        assert accepted["workspace_id"] == env.person.workspace_id
        assert accepted["user_id"] == env.person.user_id
        assert accepted["connection_id"] == env.person.connection_id
        assert accepted["auth"] == env.person.auth
    assert status in (200, 201), response
    assert "Commonplace-Rules-Notice" not in headers
    assert last_audit(env)["decision"] == "ok"


def test_rest_stale_refresh_success_and_exact_callstack_exemption(env, http):
    card = setup(env)
    seq = refresh(env)
    env.owner.create("*", "Changed after serving", "limit")
    body = {"version": card["version"], "title": "Updated", "rules_seq": seq}
    status, response, _ = http("PATCH", "/api/cards/1", body, seq)
    assert status == 428 and response["error"] == enforce.MESSAGES["stale"]
    assert BoardService(env.ctx).task_show(1)["title"] == "Original"
    assert http("PATCH", "/api/cards/1", body, refresh(env))[0] == 200
    assert BoardService(env.ctx).task_show(1)["title"] == "Updated"
    version = BoardService(env.ctx).task_show(1)["version"]
    assert http("PATCH", "/api/cards/1", {"version": version, "callstack": None})[0] == 200
    for extra in ({"title": "Cannot bypass"}, {"task": 1}, {"status": "todo"}):
        assert (
            http("PATCH", "/api/cards/1", {"version": version, "callstack": None, **extra})[0]
            == 428
        )
    assert http("POST", "/api/cards/1/heartbeat", {})[0] == 200


def test_rest_warn_notice_off_legacy_and_people(env, http):
    setup(env, "warn")
    status, response, headers = http("POST", "/api/boards/work/chat", {"text": "Exact words"})
    assert status == 201 and response["text"] == "Exact words"
    assert "needs rules_seq" in headers["Commonplace-Rules-Notice"]
    assert "Commonplace-Rules-Notice" not in http("GET", "/api/boards")[2]
    env.owner.set_check("*", "enforce")
    assert http("POST", "/api/boards/work/chat", {"text": "Person"}, person=True)[0] == 201
    env.owner.set_check("*", "off")
    assert http("POST", "/api/boards/work/chat", {"text": "Legacy"})[0] == 201
    assert http("POST", "/api/handoff", HANDOFF_BODY)[0] == 200


def _write_branches(function):
    """Count method branches, expanding action dictionaries and action tuples."""
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            methods = [
                n
                for n in ast.walk(node.test)
                if isinstance(n, ast.Compare)
                and isinstance(n.left, (ast.Name, ast.Attribute))
                and (
                    getattr(n.left, "id", None) == "method"
                    or getattr(n.left, "attr", None) == "command"
                )
                and any(
                    isinstance(c, ast.Constant) and c.value in {"POST", "PATCH", "PUT", "DELETE"}
                    for c in n.comparators
                )
            ]
            for _ in methods:
                actions = [
                    n
                    for n in ast.walk(node.test)
                    if isinstance(n, ast.Compare)
                    and isinstance(n.ops[0], ast.In)
                    and isinstance(n.comparators[0], ast.Tuple)
                ]
                dictionaries = [
                    n.value
                    for n in node.body
                    if isinstance(n, ast.Assign)
                    and isinstance(n.value, ast.Dict)
                    and any(isinstance(t, ast.Name) and t.id == "handlers" for t in n.targets)
                ]
                count += (
                    len(actions[0].comparators[0].elts)
                    if actions
                    else len(dictionaries[0].keys)
                    if dictionaries
                    else 1
                )
        # Method-to-tool dispatch maps and the (method, action) allow-list.
        if isinstance(node, ast.Dict):
            count += sum(
                isinstance(k, ast.Constant) and k.value in {"POST", "PATCH", "PUT", "DELETE"}
                for k in node.keys
            )
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:
            count += int(
                isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value in {"POST", "PATCH", "PUT", "DELETE"}
            )
    return count


def test_rest_route_inventory_matches_ast_write_branches():
    from commonplace.boards import rest
    from commonplace.handoff import api as handoff_api

    functions = {
        "boards.rest": (rest.dispatch, rest._space_route, rest._card_route),
        "memory_tools": (memory_tools.install,),
        "handoff.api": (
            handoff_api._API.dispatch,
            handoff_api._API.threads,
            handoff_api._API.settings,
        ),
        "webhooks": (webhooks._rest,),
    }
    for module, dispatchers in functions.items():
        count = sum(_write_branches(f) for f in dispatchers)
        # The fact writer is registered directly, rather than a method branch.
        if module == "memory_tools":
            tree = ast.parse(inspect.getsource(memory_tools.install))
            count += sum(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "add_route"
                and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == "POST"
                for n in ast.walk(tree)
            )
        if module == "handoff.api":
            registrations = ast.parse(inspect.getsource(handoff_api.register))
            count += sum(
                isinstance(n, ast.Tuple)
                and len(n.elts) == 2
                and all(isinstance(e, ast.Constant) for e in n.elts)
                and tuple(e.value for e in n.elts)
                in {
                    ("POST", "/api/handoff"),
                    ("DELETE", "/api/settings/handoff"),
                }
                for n in ast.walk(registrations)
            )
        routes = [r for r in enforce.REST_ROUTES if r.module == module]
        assert count == len(routes), (module, count, routes)
    assert len({(r.method, r.path) for r in enforce.REST_ROUTES}) == len(enforce.REST_ROUTES)


@pytest.mark.parametrize(
    "route", [r for r in enforce.REST_ROUTES if r.kind != "checked"], ids=lambda r: r.path
)
def test_exempt_rest_shapes_preserve_their_routes(env, http, route, monkeypatch):
    setup(env)
    bodies = {
        "/api/handoff": HANDOFF_BODY,
        "/api/handoffs/<id>/successor": {"agent_ref": "agent:new"},
        "/api/handoffs/<id>/launch-failed": {"error": "Synthetic exit"},
        "/api/threads/<id>/recall": {"query": "tea"},
        "/api/settings/handoff": {"scope": "task", "ref": "#142", "enabled": True},
        "/api/webhooks": {"url": "https://example.test/hook"},
    }
    path = route.path.replace("<n>", "1").replace(
        "<id>", "thr_local" if "threads" in route.path else "hof_local"
    )
    if route.method == "DELETE" and "settings" in path:
        path += "?scope=task&ref=%23142"
    if route.module == "webhooks":
        monkeypatch.setattr(
            webhooks, "add", lambda *_a, **_k: {"id": "hook", "secret": "synthetic"}
        )
        monkeypatch.setattr(webhooks, "remove", lambda *_a, **_k: None)
        monkeypatch.setattr(webhooks, "test_webhook", lambda *_a, **_k: "delivery")
    status, response, headers = http(
        route.method, path, {**bodies.get(route.path, {}), "rules_seq": "ignored"}
    )
    assert status in (200, 201, 202), response
    assert "Commonplace-Rules-Notice" not in headers
