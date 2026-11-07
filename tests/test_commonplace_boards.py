"""Boards through the production gateway, real worker fixture and SQLite."""

import http.client
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from commonplace import events, sessions
from commonplace.boards.service import BoardService, needs_me
from commonplace.boards.store import BoardStore
from commonplace.extensions import ToolContext, ToolError
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    REVIEW_QUESTION,
    hub_fixture,  # noqa: F401
)

CARD_KEYS = set(
    "task_type escalated_to review_context claim_expired_at claim_expired agent_parent proposal_status proposed_to approved_by can_approve review_kind number parent board title body status assignee claim_until ask ask_options ask_to return_to flags repo branch pr_url pr_number pr_state pr_head pr_reported_at checks status_line heartbeat_at labels links version created_by created_at updated_at position need message_id restricted".split()
)


def api(hub, auth, method, path, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if isinstance(auth, dict):
        headers.update(auth)
    elif auth:
        headers["Authorization"] = f"Bearer {auth}"
    try:
        connection.request(
            method, path, json.dumps(body).encode() if body is not None else None, headers
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def mcp(hub, token, tool_name, **args):
    response = hub.rpc(token, "tools/call", {"name": tool_name, "arguments": args})
    if "error" in response:
        return False, response["error"]["message"]
    return not response["result"].get("isError", False), response["result"]["content"][0]["text"]


def service(hub, principal):
    return BoardService(ToolContext(principal=principal, gateway=hub.gateway))


@pytest.fixture
def env(hub):
    token = hub.owner_token("agent")
    principal = hub.gateway.authenticate(f"Bearer {token}")
    person = sessions.person_principal(hub.db, hub.admin, hub.workspace)
    session = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, "owner@example.com")
    )
    cookie = {"Cookie": f"{sessions.COOKIE}={session}"}
    owner = service(hub, person)
    owner.board_add(slug="work", wing="shared", repo="https://github.com/140er/mempalace.git")
    return token, principal, person, cookie, owner


@pytest.fixture(params=[False, True], ids=["standard-agent", "member-agent"])
def standard_agent(hub, request):
    user = hub.admin
    if request.param:
        user = hub.db.create_user("member@example.com")
        hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
    token = hub.token(
        "standard-worker",
        user=user,
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["shared"],
        trust="standard",
    )
    return token, hub.gateway.authenticate(f"Bearer {token}")


def test_registration_real_worker_and_profiles(hub, env):
    token, _agent, _person, _cookie, _owner = env
    names = hub.tool_names(token)
    assert {"board_add", "task_take_next", "ask", "decide", "needs_me"} <= names
    slim = hub.token(
        "slim",
        profile="slim",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wing="inbox",
        trust="untrusted",
    )
    names = hub.tool_names(slim)
    assert {"board_list", "task_list", "task_add", "task_show", "task_comment"} <= names
    assert not {"board_add", "decide"} & names
    assert hub.supervisor.get(hub.workspace) is not None


def test_card_shape_metadata_and_routes(hub, env):
    token, agent, _person, cookie, _owner = env
    status, created = api(
        hub,
        token,
        "POST",
        "/api/boards/work/cards",
        {"title": "Exact\nwords", "body": "  keep all words\n", "labels": ["backend"]},
    )
    assert status == 201 and set(created) == CARD_KEYS | {"url"}
    assert created["created_by"] == agent.actor
    assert created["repo"] == "github.com/140er/mempalace"
    number = created["number"]
    assert mcp(hub, token, "task_take_next", task=number)[0]
    status, current = api(hub, token, "GET", f"/api/cards/{number}")
    status, updated = api(
        hub,
        token,
        "PATCH",
        f"/api/cards/{number}",
        {
            "version": current["version"],
            "branch": "feat/work",
            "pr": 12,
            "checks": "passing",
            "status_line": "Testing",
        },
    )
    assert status == 200 and set(updated) == CARD_KEYS | {"url"}
    assert updated["pr_url"] == "https://github.com/140er/mempalace/pull/12"
    assert updated["heartbeat_at"] and updated["claim_until"]
    assert updated["body"] == "  keep all words\n" and updated["labels"] == ["backend"]
    ok, output = mcp(hub, token, "task_show", task=number)
    assert ok and all(value in output for value in ("feat/work", "pull/12", "passing", "Testing"))
    assert "wing" not in output and "palace" not in output.replace("mempalace", "")
    assert api(hub, None, "GET", "/api/boards")[0] == 401
    assert api(hub, cookie, "GET", "/api/boards/work/cards")[1]["cards"][0]["number"] == number


MOVES = [
    ("backlog", "todo", "update", True),
    ("backlog", "doing", "update", True),
    ("backlog", "doing", "take", True),
    ("todo", "doing", "update", True),
    ("todo", "doing", "take", True),
    ("doing", "todo", "update", False),
    ("doing", "todo", "handoff", True),
    ("doing", "review", "update", True),
    ("doing", "review", "handoff", True),
    ("doing", "done", "update", True),
    ("review", "todo", "update", False),
    ("review", "done", "update", False),
    ("review", "backlog", "update", False),
    ("review", "done", "decide", False),
    ("todo", "review", "handoff", True),
    ("todo", "review", "update", True),
    ("todo", "done", "update", False),
    ("done", "todo", "update", True),
    ("todo", "backlog", "update", True),
    ("doing", "backlog", "update", True),
]


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("old,new,operation,allowed", MOVES)
def test_agent_move_rules(hub, env, surface, old, new, operation, allowed):
    token, agent, _person, _cookie, owner = env
    card = (
        service(hub, agent).ask(
            REVIEW_QUESTION,
            board="work",
            context=REVIEW_CONTEXT,
            options=REVIEW_OPTIONS,
        )
        if old == "review" and operation == "update"
        else owner.seed_card(board="work", title="move", status=old)
    )
    number = card["number"]
    if old == "doing":
        with owner.store.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET assignee=?,claim_until='2099-01-01T00:00:00Z' WHERE id=?",
                (agent.actor, number),
            )
    if surface == "mcp" or operation == "take":
        if operation == "take":
            ok, _text = mcp(hub, token, "task_take_next", task=number)
        elif operation == "handoff":
            ok, _text = mcp(
                hub,
                token,
                "task_handoff",
                task=number,
                to="owner@example.com" if new == "review" else "anyone",
                note="Verbatim note Which approach should we take?",
                context=REVIEW_CONTEXT,
                options=REVIEW_OPTIONS if new == "review" else None,
            )
        elif operation == "decide":
            ok, _text = mcp(hub, token, "decide", task=number, choice="yes", then="done")
        else:
            ok, _text = mcp(
                hub,
                token,
                "task_update",
                task=number,
                status=new,
                **(
                    {
                        "question": REVIEW_QUESTION,
                        "options": REVIEW_OPTIONS,
                        "context": REVIEW_CONTEXT,
                    }
                    if new == "review"
                    else {}
                ),
            )
    else:
        current = owner.task_show(number)
        if operation == "handoff":
            status, _body = api(
                hub,
                token,
                "POST",
                f"/api/cards/{number}/handoff",
                {
                    "to": "owner@example.com" if new == "review" else "anyone",
                    "note": "Verbatim note Which approach should we take?",
                    "context": REVIEW_CONTEXT,
                    "options": REVIEW_OPTIONS if new == "review" else None,
                },
            )
        elif operation == "decide":
            status, _body = api(
                hub,
                token,
                "POST",
                f"/api/cards/{number}/decide",
                {"version": current["version"], "choice": "yes", "then": "done"},
            )
        else:
            status, _body = api(
                hub,
                token,
                "PATCH",
                f"/api/cards/{number}",
                {
                    "version": current["version"],
                    "status": new,
                    **(
                        {
                            "question": REVIEW_QUESTION,
                            "options": REVIEW_OPTIONS,
                            "context": REVIEW_CONTEXT,
                        }
                        if new == "review"
                        else {}
                    ),
                },
            )
        ok = status == 200
    assert ok is allowed
    assert owner.task_show(number)["status"] == (new if allowed else old)
    if allowed and operation == "update":
        with owner.store.connection() as conn:
            event = conn.execute(
                "SELECT * FROM task_events WHERE task_id=? AND kind='move' ORDER BY id DESC",
                (number,),
            ).fetchone()
        assert event["stamp_connection"] == agent.actor.removeprefix("conn:")
        assert (event["from_status"], event["to_status"]) == (old, new)
        emitted = [
            e
            for e in events.after(hub.db, hub.workspace)
            if e["kind"] == "card.moved" and e["data"]["number"] == number
        ]
        assert emitted[-1]["data"]["from"] == old
        assert emitted[-1]["data"]["to"] == new


@pytest.mark.parametrize(
    "old,new",
    [
        ("backlog", "todo"),
        ("todo", "doing"),
        ("doing", "todo"),
        ("doing", "review"),
        ("doing", "done"),
        ("todo", "review"),
        ("done", "todo"),
        ("todo", "backlog"),
        ("doing", "backlog"),
        ("done", "backlog"),
    ],
)
def test_person_moves_rest(hub, env, old, new):
    _token, _agent, person, cookie, owner = env
    created = owner.seed_card(board="work", title="Person move", status=old)
    status, current = api(
        hub,
        cookie,
        "PATCH",
        f"/api/cards/{created['number']}",
        {
            "version": created["version"],
            "status": new,
            **(
                {"question": REVIEW_QUESTION, "options": REVIEW_OPTIONS, "context": REVIEW_CONTEXT}
                if new == "review"
                else {}
            ),
        },
    )
    assert status == 200 and current["status"] == new
    if new == "doing":
        assert current["assignee"] == person.actor and current["claim_until"] is None


def test_review_only_decide_and_done_is_never_deleted(hub, env):
    _token, _agent, _person, cookie, owner = env
    created = owner.ask(
        "Proceed? Which approach should we take?",
        options=["yes", "no"],
        board="work",
        context=REVIEW_CONTEXT,
    )
    assert (
        api(
            hub,
            cookie,
            "PATCH",
            f"/api/cards/{created['number']}",
            {"version": created["version"], "status": "todo"},
        )[0]
        == 403
    )
    status, current = api(
        hub,
        cookie,
        "POST",
        f"/api/cards/{created['number']}/decide",
        {"version": created["version"], "choice": "yes", "then": "done"},
    )
    assert status == 200 and current["status"] == "done"
    assert api(hub, cookie, "DELETE", f"/api/cards/{created['number']}")[0] == 404
    with owner.store.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            conn.execute("DELETE FROM tasks WHERE id=?", (created["number"],))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM task_events WHERE task_id=?", (created["number"],))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE task_events SET text='replaced' WHERE task_id=?", (created["number"],)
            )


def board_agent(hub, *, trust="trusted", member=False, **policy):
    user = hub.admin
    if member:
        user = hub.db.create_user("worker@example.com")
        hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
    options = {
        "profile": "full",
        "scopes": ["memory:read", "memory:write"],
        "read_wings": ["shared"],
        "write_wings": ["shared"],
        **policy,
    }
    token = hub.token("worker", user=user, trust=trust, **options)
    principal = hub.gateway.authenticate(f"Bearer {token}")
    return token, principal, service(hub, principal)


def assert_card_event(hub, owner, number, actor, kind, old, new, trust="trusted"):
    with owner.store.connection() as conn:
        logged = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (number,)
        ).fetchone()
    assert logged["kind"] == kind
    assert (logged["from_status"], logged["to_status"]) == (old, new)
    assert logged["stamp_connection"] == actor.removeprefix("conn:")
    assert logged["stamp_trust"] == trust
    emitted = [
        event for event in events.after(hub.db, hub.workspace) if event["subject"] == f"#{number}"
    ][-1]
    assert (
        emitted["kind"]
        == {
            "claim": "card.claimed",
            "move": "card.moved",
            "edit": "card.updated",
        }[kind]
    )
    assert emitted["actor"] == actor
    assert (emitted["data"]["from"], emitted["data"]["to"]) == (old, new)


@pytest.mark.parametrize("member", [False, True])
@pytest.mark.parametrize("reserved", [False, True])
@pytest.mark.parametrize(
    "path", ["promote-mcp", "promote-rest", "take-mcp", "take-service", "claim-mcp", "claim-rest"]
)
def test_trusted_agent_picks_explicit_backlog(hub, env, member, reserved, path):
    _token, _agent, _person, _cookie, owner = env
    token, principal, worker = board_agent(hub, member=member)
    created = owner.task_add(
        board="work",
        title="Selected work",
        status="backlog",
        assignee=principal.actor if reserved else None,
    )
    number = created["number"]
    status = "todo" if path.startswith("promote") else "doing"
    if path == "take-service":
        worker.task_take_next(task=f"#{number}", board="work")
    elif path.endswith("rest"):
        code, _result = api(
            hub,
            token,
            "PATCH",
            f"/api/cards/{number}",
            {"version": created["version"], "status": status},
        )
        assert code == 200
    else:
        arguments = {} if path == "take-mcp" else {"status": status}
        assert mcp(
            hub,
            token,
            "task_take_next" if path == "take-mcp" else "task_update",
            task=number,
            **arguments,
        )[0]
    current = owner.task_show(number)
    assert current["status"] == status and current["version"] == created["version"] + 1
    assert current["assignee"] == (principal.actor if status == "doing" or reserved else None)
    assert bool(current["claim_until"]) == (status == "doing")
    assert_card_event(
        hub,
        owner,
        number,
        principal.actor,
        "claim" if path.startswith("take") else "move",
        "backlog",
        status,
    )
    if status == "todo":
        if reserved:
            assert service(hub, _agent).task_take_next(board="work") is None
        worker.task_take_next(task=number)
    worker.task_update(
        number,
        status="review",
        question=REVIEW_QUESTION,
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    refusal = (
        "only a workspace owner" if member else "only a signed-in workspace owner can answer this"
    )
    with pytest.raises(ToolError, match=refusal):
        worker.decide(number, reply="Approved", then="done")
    with pytest.raises(ToolError, match="waiting on a person"):
        worker.task_update(number, status="done")
    owner.decide(number, reply="Approved", then="done")
    assert owner.task_show(number)["status"] == "done"
    decision = owner.task_show(number)["events"][-1]
    assert (decision["kind"], decision["actor"]) == ("decide", owner.principal.actor)


@pytest.mark.parametrize("member", [False, True])
@pytest.mark.parametrize("operation", ["promote", "claim", "take"])
@pytest.mark.parametrize(
    "policy",
    [
        {"trust": "standard"},
        {"trust": "untrusted", "write_wing": "inbox"},
        {"write_wing": "inbox"},
        {"write_wing": "wing_inbox"},
        {"scopes": ["memory:read"]},
        {"write_wings": ["another"]},
    ],
)
def test_backlog_pickup_requires_trust_and_board_write(hub, env, member, operation, policy):
    _token, _agent, _person, _cookie, owner = env
    token, _principal, worker = board_agent(hub, member=member, **policy)
    created = owner.task_add(board="work", title="Protected backlog", status="backlog")
    number = created["number"]
    before = owner.task_show(number)
    emitted = events.after(hub.db, hub.workspace)
    name = "task_take_next" if operation == "take" else "task_update"
    args = {} if operation == "take" else {"status": "todo" if operation == "promote" else "doing"}
    with pytest.raises(ToolError):
        getattr(worker, name)(task=number, **args)
    assert not mcp(hub, token, name, task=number, **args)[0]
    assert owner.task_show(number) == before
    assert events.after(hub.db, hub.workspace) == emitted


@pytest.mark.parametrize("operation", ["promote", "claim", "take"])
@pytest.mark.parametrize(
    "origin",
    [
        {"trust": "untrusted", "write_wing": "shared"},
        {"trust": "trusted", "write_wing": "inbox"},
        {"trust": "trusted", "write_wing": "wing_inbox"},
    ],
)
def test_restricted_origin_backlog_requires_person_release(hub, env, origin, operation):
    trusted_token, agent, person, _cookie, owner = env
    token = hub.token(
        "submitter",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        **origin,
    )
    code, created = api(hub, token, "POST", "/api/boards/work/cards", {"title": "Needs triage"})
    assert code == 201 and created["status"] == "backlog"
    number = created["number"]
    # Reproduce a pre-migration restricted submission, rather than a new default card.
    with owner.store.transaction() as conn:
        conn.execute("UPDATE tasks SET status='backlog',restricted_origin=1 WHERE id=?", (number,))
    with owner.store.connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (number,)).fetchone()
        assert row["restricted_origin"] == 1 and row["stamp_trust"] == origin["trust"]
    # A later policy upgrade must not erase the recorded submission restriction.
    with hub.db._lock:
        hub.db._conn.execute(
            "UPDATE connections SET policy=? WHERE workspace_id=? AND name='submitter'",
            (agent.policy.to_json(), hub.workspace),
        )
    before = owner.task_show(number)
    emitted = events.after(hub.db, hub.workspace)
    worker = service(hub, agent)
    name = "task_take_next" if operation == "take" else "task_update"
    args = {} if operation == "take" else {"status": "todo" if operation == "promote" else "doing"}
    with pytest.raises(ToolError):
        getattr(worker, name)(task=number, **args)
    assert not mcp(hub, trusted_token, name, task=number, **args)[0]
    assert owner.task_show(number) == before
    assert events.after(hub.db, hub.workspace) == emitted
    assert all(
        number not in {c["number"] for c in owner.needs_me()[bucket]}
        for bucket in ("asks", "done_review", "backlog_from_agents", "ready_to_merge", "stale")
    )
    _token, standard, picker = board_agent(hub, trust="standard")
    assert picker.task_take_next(board="work") is None
    assert worker.task_take_next(board="work") is None
    promoted = owner.task_update(number, status="todo")
    assert promoted["status"] == "todo" and promoted["assignee"] is None
    logged = owner.task_show(number)["events"][-1]
    assert (logged["kind"], logged["actor"]) == (
        "move",
        person.actor,
    )
    event = events.after(hub.db, hub.workspace)[-1]
    assert (event["kind"], event["actor"]) == ("card.moved", person.actor)
    assert (event["data"]["from"], event["data"]["to"]) == ("backlog", "todo")
    taken = picker.task_take_next(board="work")
    assert taken["number"] == number and taken["assignee"] == standard.actor


@pytest.mark.parametrize("trust", ["trusted", "standard"])
def test_trusted_agent_can_pick_up_unrestricted_agent_submission(hub, env, trust):
    _token, agent, _person, _cookie, owner = env
    _token, _principal, submitter = board_agent(hub, trust=trust)
    created = submitter.task_add(board="work", title="Selected agent work", status="backlog")
    with owner.store.connection() as conn:
        assert (
            conn.execute(
                "SELECT restricted_origin FROM tasks WHERE id=?", (created["number"],)
            ).fetchone()[0]
            == 0
        )
    service(hub, agent).task_take_next(task=created["number"])
    assert_card_event(hub, owner, created["number"], agent.actor, "claim", "backlog", "doing")


def test_legacy_backlog_origin_migration_resolves_current_connection_policy(hub, env):
    _token, agent, _person, _cookie, owner = env
    worker = service(hub, agent)
    backlog = {}
    cases = [
        ("trusted", {"trust": "trusted"}, "trusted", False),
        ("standard", {"trust": "standard"}, "standard", False),
        ("inbox", {"trust": "trusted", "write_wing": "inbox"}, "trusted", True),
        ("wing-inbox", {"trust": "trusted", "write_wing": "wing_inbox"}, "trusted", True),
        ("untrusted-now", {"trust": "untrusted", "write_wing": "shared"}, "trusted", True),
        ("untrusted-stamp", {"trust": "trusted"}, "untrusted", True),
        ("missing", None, "trusted", True),
        ("revoked", {"trust": "trusted"}, "trusted", True),
        ("mismatched-user", {"trust": "trusted"}, "trusted", True),
        ("newer-connection", {"trust": "trusted"}, "trusted", True),
    ]
    for name, policy, trust, restricted in cases:
        connection = f"legacy-{name}"
        if policy is not None:
            hub.token(connection, profile="full", read_wings=["shared"], **policy)
        if name == "revoked":
            hub.db.revoke(connection, actor=hub.admin, workspace_ref=hub.workspace)
        created = owner.task_add(board="work", title=f"Legacy {name}", status="backlog")
        with owner.store.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET stamp_trust=?,stamp_connection=?,created_by=? WHERE id=?",
                (trust, connection, f"conn:{connection}", created["number"]),
            )
            if name == "mismatched-user":
                conn.execute(
                    "UPDATE tasks SET stamp_user='unknown-user' WHERE id=?", (created["number"],)
                )
            if name == "newer-connection":
                conn.execute(
                    "UPDATE tasks SET created_at='2000-01-01T00:00:00Z' WHERE id=?",
                    (created["number"],),
                )
        backlog[created["number"]] = restricted
    person_card = owner.task_add(board="work", title="Person selected", status="backlog")
    ready = worker.task_add(board="work", title="Already released")
    held = worker.task_add(board="work", title="Legacy claim")
    worker.task_take_next(task=held["number"])
    with owner.store.transaction() as conn:
        conn.execute("ALTER TABLE tasks DROP COLUMN restricted_origin")
        conn.execute("ALTER TABLE tasks DROP COLUMN claim_from")
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (held["number"],)
        )
    migrated = BoardStore(owner.ctx.workspace_dir())
    with migrated.connection() as conn:
        assert (
            conn.execute("SELECT count(*) FROM tasks WHERE restricted_origin IS NULL").fetchone()[0]
            == len(cases) - 1
        )
    # A store opened without connection policy leaves unresolved rows protected.
    with pytest.raises(ToolError, match="not available"):
        worker.task_take_next(task=next(iter(backlog)))
    worker = service(hub, agent)
    with migrated.connection() as conn:
        assert {
            r["id"]: bool(r["restricted_origin"])
            for r in conn.execute("SELECT id,restricted_origin FROM tasks WHERE status='backlog'")
            if r["id"] in backlog
        } == backlog
    for number, restricted in backlog.items():
        if not restricted:
            worker.task_update(number, status="todo")
            assert worker.task_take_next(task=number)["status"] == "doing"
            continue
        for status in ("todo", "doing"):
            with pytest.raises(ToolError, match="person|owner release"):
                worker.task_update(number, status=status)
        with pytest.raises(ToolError, match="not available"):
            worker.task_take_next(task=number)
    assert worker.task_take_next(task=person_card["number"])["status"] == "doing"
    assert worker.task_take_next(task=ready["number"])["status"] == "doing"
    assert owner.task_show(held["number"])["status"] == "doing"
    released = next(number for number, restricted in backlog.items() if restricted)
    owner.task_update(released, status="todo")
    BoardStore(owner.ctx.workspace_dir())
    assert worker.task_take_next(task=released)["status"] == "doing"


@pytest.mark.parametrize("field", ["ask_to", "return_to", "assignee", "other_agent"])
def test_agent_cannot_pick_up_backlog_waiting_on_someone(hub, env, field):
    _token, _agent, person, _cookie, owner = env
    _token, _principal, worker = board_agent(hub)
    created = owner.task_add(board="work", title="Wait for a decision", status="backlog")
    column = "assignee" if field == "other_agent" else field
    value = "conn:other" if field == "other_agent" else person.actor
    with owner.store.transaction() as conn:
        conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, created["number"]))
    before = owner.task_show(created["number"])
    emitted = events.after(hub.db, hub.workspace)
    for status in ("todo", "doing", "review", "done"):
        with pytest.raises(ToolError):
            worker.task_update(created["number"], status=status)
    with pytest.raises(ToolError, match="card is not available to take"):
        worker.task_take_next(task=created["number"])
    assert owner.task_show(created["number"]) == before
    assert events.after(hub.db, hub.workspace) == emitted


def test_pending_person_ask_cannot_be_promoted_or_taken(hub, env):
    _token, agent, _person, _cookie, owner = env
    waiting = owner.ask(
        "Choose first Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    worker = service(hub, agent)
    before = owner.task_show(waiting["number"])
    for status in ("todo", "doing"):
        with pytest.raises(ToolError):
            worker.task_update(waiting["number"], status=status)
    with pytest.raises(ToolError, match="card is not available to take"):
        worker.task_take_next(task=waiting["number"])
    assert owner.task_show(waiting["number"]) == before
    assert waiting["number"] in {item["number"] for item in owner.needs_me()["asks"]}


def test_take_next_stays_todo_only_and_skips_person_decisions(hub, env):
    _token, agent, person, _cookie, owner = env
    worker = service(hub, agent)
    backlog = owner.task_add(board="work", title="Choose explicitly", status="backlog")
    waiting = owner.task_add(board="work", title="Needs a person")
    with owner.store.transaction() as conn:
        conn.execute("UPDATE tasks SET ask_to=? WHERE id=?", (person.actor, waiting["number"]))
    assert worker.task_take_next(board="work") is None
    with pytest.raises(ToolError, match="card is not available to take"):
        worker.task_take_next(task=waiting["number"])
    ready = owner.task_add(board="work", title="Ready")
    assert worker.task_take_next(board="work")["number"] == ready["number"]
    assert owner.task_show(backlog["number"])["status"] == "backlog"
    assert worker.task_take_next(board="work") is None
    with pytest.raises(ToolError, match="card is not available to take"):
        owner.task_take_next(task=backlog["number"])
    assert owner.task_take_next(task=waiting["number"])["assignee"] == person.actor


@pytest.mark.parametrize("with_status", [False, True])
def test_trusted_agent_annotates_done_without_reopening(hub, env, with_status):
    _token, _agent, _person, _cookie, owner = env
    token, principal, worker = board_agent(hub, member=True)
    created = owner.seed_card(board="work", title="Completed", status="done")
    number = created["number"]
    with owner.store.connection() as conn:
        done_at = conn.execute("SELECT done_at FROM tasks WHERE id=?", (number,)).fetchone()[0]
    args = {"status": "done"} if with_status else {}
    assert mcp(
        hub,
        token,
        "task_update",
        task=number,
        lesson="Keep the exact lesson.",
        pr="https://example.com/project/pull/42",
        **args,
    )[0]
    current = owner.task_show(number)
    assert current["status"] == "done" and current["assignee"] is None
    assert current["pr_url"] == "https://example.com/project/pull/42"
    assert current["links"] == 1
    drawer = current["linked"][0]["drawer_id"]
    assert_card_event(hub, owner, number, principal.actor, "edit", "done", "done")
    before = owner.task_show(number)
    emitted = events.after(hub.db, hub.workspace)
    with pytest.raises(ToolError, match="completed cards are frozen"):
        worker.task_update(number, unlink=drawer, **args)
    assert not mcp(hub, token, "task_update", task=number, unlink=drawer, **args)[0]
    assert owner.task_show(number) == before
    assert events.after(hub.db, hub.workspace) == emitted
    saved, _ = hub.call(
        token, "mempalace_add_drawer", wing="shared", room="lessons", content="Additional evidence"
    )
    worker.task_update(number, link=saved["drawer_id"])
    assert owner.task_show(number)["links"] == 2
    with owner.store.connection() as conn:
        assert (
            conn.execute("SELECT done_at FROM tasks WHERE id=?", (number,)).fetchone()[0] == done_at
        )
        kinds = [
            row[0]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (number,)
            )
        ]
    assert kinds[-4:] == ["link", "edit", "link", "edit"]


@pytest.mark.parametrize(
    "policy",
    [
        {"trust": "standard"},
        {"trust": "untrusted", "write_wing": "inbox"},
        {"write_wing": "inbox"},
        {"write_wing": "wing_inbox"},
        {"write_wings": ["another"]},
        {"scopes": ["memory:read"]},
    ],
)
def test_done_annotations_require_trusted_board_writer(hub, env, policy):
    _token, _agent, _person, _cookie, owner = env
    _token, _principal, worker = board_agent(hub, **policy)
    created = owner.seed_card(board="work", title="Completed", status="done")
    before = owner.task_show(created["number"])
    for args in (
        {"lesson": "No Which approach should we take?"},
        {"pr": 3},
        {"link": "missing"},
        {"unlink": "missing"},
    ):
        with pytest.raises(ToolError):
            worker.task_update(created["number"], **args)
    assert owner.task_show(created["number"]) == before


@pytest.mark.parametrize(
    "args",
    [
        {"title": "Changed"},
        {"body": "Changed"},
        {"labels": ["changed"]},
        {"checks": "passing"},
        {"status_line": "Changed"},
        {"branch": "changed"},
        {"repo": "example.com/changed"},
        {"callstack": None},
        {"unlink": "memory"},
        {"lesson": "New", "title": "Changed"},
        {"pr": 2, "status": "todo"},
    ],
)
def test_done_annotations_do_not_allow_other_edits(hub, env, args):
    _token, agent, _person, _cookie, owner = env
    created = owner.seed_card(board="work", title="Completed", status="done")
    before = owner.task_show(created["number"])
    with pytest.raises(ToolError, match="completed cards are frozen"):
        service(hub, agent).task_update(created["number"], **args)
    assert owner.task_show(created["number"]) == before


@pytest.mark.parametrize(
    "policy,member_allowed",
    [
        ({}, True),
        ({"trust": "standard"}, False),
        ({"trust": "untrusted", "write_wing": "inbox"}, False),
        ({"write_wing": "inbox"}, False),
        ({"write_wing": "wing_inbox"}, False),
    ],
)
@pytest.mark.parametrize("member", [False, True])
def test_needs_me_keeps_owner_access_and_adds_trusted_members(
    hub, env, policy, member_allowed, member
):
    _token, agent, _person, _cookie, owner = env
    asked = service(hub, agent).ask(
        "Keep waiting Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    token, principal, worker = board_agent(hub, member=member, scopes=["memory:read"], **policy)
    allowed = not member or member_allowed
    addressed = owner.ask(
        "Member question Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    owner.board_add(slug="hidden", wing="private.hidden")
    hidden = owner.ask(
        "HIDDEN-QUEUE-CONTENT Which approach should we take?",
        board="hidden",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET ask_to=? WHERE id IN (?,?)",
            (f"user:{principal.user_id}", addressed["number"], hidden["number"]),
        )
    before = owner.task_show(asked["number"])
    assert ("needs_me" in hub.tool_names(token)) is allowed
    for format in ("list", "digest"):
        ok, output = mcp(hub, token, "needs_me", format=format)
        assert ok is allowed
        status, result = api(
            hub, token, "GET", "/api/needs-me" + ("/digest" if format == "digest" else "")
        )
        assert status == (200 if allowed else 403)
        assert "HIDDEN-QUEUE-CONTENT Which approach should we take?" not in output + json.dumps(
            result
        )
        if allowed:
            assert "Member question Which approach should we take?" in output
            assert ("Keep waiting Which approach should we take?" in output) is (not member)
            assert result["count"] == (1 if member else 2)
    if allowed:
        assert {item["number"] for item in worker.needs_me()["asks"]} == (
            {addressed["number"]} if member else {asked["number"], addressed["number"]}
        )
    if member and allowed:
        assert "board_set" not in hub.tool_names(token)
        with pytest.raises(ToolError, match="only a workspace owner"):
            worker.board_set("work", coordinator=principal.actor)
        with pytest.raises(ToolError, match="only a workspace owner"):
            worker.decide(asked["number"], reply="No Which approach should we take?")
    if not allowed:
        with pytest.raises(ToolError, match="only a workspace owner"):
            worker.needs_me()
    assert owner.task_show(asked["number"]) == before
    assert asked["number"] in {item["number"] for item in owner.needs_me()["asks"]}


def test_needs_me_member_filters_asks_by_user_and_other_buckets_by_wing(hub, env, monkeypatch):
    _token, agent, _person, _cookie, owner = env
    worker = service(hub, agent)
    _token, member, reader = board_agent(hub, member=True, scopes=["memory:read"])
    worker.ask(
        "Owner decision Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    addressed = worker.ask(
        "Member decision Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    done = worker.ask(
        REVIEW_QUESTION,
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    worker.task_add(board="work", title="Agent backlog", status="backlog")
    ready = worker.task_add(board="work", title="Ready and stale")
    worker.task_take_next(task=ready["number"])
    ready = worker.task_update(ready["number"], pr=8, checks="passing")
    with owner.store.transaction() as conn:
        conn.execute("UPDATE tasks SET review_kind='completion' WHERE id=?", (done["number"],))
        conn.execute(
            "UPDATE tasks SET ask_to=? WHERE id IN (?,?)",
            (f"user:{member.user_id}", addressed["number"], done["number"]),
        )
    owner.board_add(slug="hidden", wing="private.hidden")
    worker.task_add(board="hidden", title="Hidden backlog", status="backlog")
    from tests.test_commonplace_needs_me_human_only import clock

    clock(monkeypatch, ready["claim_until"])
    result = reader.needs_me()
    assert {
        key: {c["number"] for c in result[key]}
        for key in result
        if key not in ("count", "approvals_truncated")
    } == {
        "asks": {addressed["number"]},
        "done_review": {done["number"]},
        "backlog_from_agents": set(),
        "ready_to_merge": set(),
        "stale": set(),
        "plans": set(),
        "approvals": set(),
    }
    assert result["count"] == 2
    assert all(item["actions"] == [] for key in ("asks", "done_review") for item in result[key])


@pytest.mark.parametrize("scopes", [[], ["memory:write"], ["memory:read"]])
def test_needs_me_member_list_and_digest_require_memory_read(hub, env, scopes):
    _token, _agent, _person, _cookie, owner = env
    token, principal, reader = board_agent(hub, member=True, scopes=scopes)
    asked = owner.ask(
        "Member read permission Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET ask_to=? WHERE id=?",
            (f"user:{principal.user_id}", asked["number"]),
        )
    allowed = "memory:read" in scopes
    assert ("needs_me" in hub.tool_names(token)) is allowed
    for format in ("list", "digest"):
        assert mcp(hub, token, "needs_me", format=format)[0] is allowed
        if allowed:
            assert reader.needs_me(format=format)["count"] == 1
        else:
            with pytest.raises(ToolError, match="only a workspace owner"):
                reader.needs_me(format=format)
    for path in (
        "/api/needs-me",
        "/api/needs-me/digest",
        "/api/needs-me/digest?format=text",
        "/api/needs-me/digest?format=ssml",
    ):
        if "format=" in path and allowed:
            # The shared JSON helper covers the JSON list and digest responses.
            continue
        code, result = api(hub, token, "GET", path)
        assert code == (200 if allowed else 403)
        if allowed:
            assert result["count"] == 1
        else:
            assert "Member read permission Which approach should we take?" not in json.dumps(result)


@pytest.mark.parametrize("trust", ["trusted", "standard"])
@pytest.mark.parametrize("destination", ["todo", "done"])
@pytest.mark.parametrize("pickup", ["explicit", "automatic"])
def test_person_answer_return_marker_does_not_block_released_cards(
    hub, env, trust, destination, pickup
):
    _token, _agent, owner_person, _cookie, owner = env
    user = hub.db.create_user("handoff-source@example.com")
    hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
    person = sessions.person_principal(hub.db, user, hub.workspace)
    requester = service(hub, person)
    login = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, "handoff-source@example.com")
    )
    cookie = {"Cookie": f"{sessions.COOKIE}={login}"}
    _token, principal, worker = board_agent(hub, trust=trust)
    created = requester.task_add(board="work", title="Person released work")
    number = created["number"]
    requester.task_handoff(
        number,
        to=owner_person.actor,
        note="Can I continue? Which approach should we take?",
        options=["yes", "no"],
        context=REVIEW_CONTEXT,
    )
    returned = owner.decide(number, choice="yes", then="return")
    assert returned["assignee"] == person.actor and returned["return_to"] == person.actor
    with pytest.raises(ToolError, match="not available"):
        worker.task_take_next(task=number)
    assert worker.task_take_next(board="work") is None
    code, held = api(
        hub,
        cookie,
        "PATCH",
        f"/api/cards/{number}",
        {"version": returned["version"], "status": "doing"},
    )
    assert code == 200 and held["assignee"] == person.actor and held["claim_until"] is None
    code, released = api(
        hub,
        cookie,
        "PATCH",
        f"/api/cards/{number}",
        {"version": held["version"], "status": destination},
    )
    assert code == 200 and released["assignee"] is None
    assert released["return_to"] == person.actor and released["ask_to"] is None
    assert released["ask"] is None
    if destination == "done":
        if trust == "trusted":
            worker.task_update(number, lesson="Keep the answered decision")
            assert_card_event(hub, owner, number, principal.actor, "edit", "done", "done")
            link = worker.ctx.memory(
                "mempalace_add_drawer",
                {"content": "Additional evidence", "wing": "shared", "room": "general"},
            )["drawer_id"]
            worker.task_update(number, link=link)
            assert owner.task_show(number)["links"] == 2
            assert_card_event(hub, owner, number, principal.actor, "edit", "done", "done")
        else:
            with pytest.raises(ToolError, match="completed cards are frozen"):
                worker.task_update(number, lesson="Still trusted-only")
        worker.task_update(number, status="todo")
        assert_card_event(
            hub,
            owner,
            number,
            principal.actor,
            "move",
            "done",
            "todo",
            trust,
        )
    taken = (
        worker.task_take_next(task=number)
        if pickup == "explicit"
        else worker.task_take_next(board="work")
    )
    assert taken["number"] == number and taken["assignee"] == principal.actor
    assert taken["status"] == "doing" and taken["claim_until"]
    assert_card_event(hub, owner, number, principal.actor, "claim", "todo", "doing", trust)


def test_take_next_thread_race_separate_connections(hub, env):
    _token, _agent, _person, _cookie, owner = env
    for number in range(12):
        owner.task_add(title=f"race {number}", board="work")
    tokens = [hub.owner_token(f"racer-{i}") for i in range(4)]
    principals = [hub.gateway.authenticate(f"Bearer {token}") for token in tokens]
    gate = threading.Barrier(4)

    def take(principal):
        worker = service(hub, principal)
        gate.wait(timeout=5)
        result = []
        while current := worker.task_take_next(board="work"):
            result.append(current["number"])
        return result

    with ThreadPoolExecutor(max_workers=4) as executor:
        taken = [n for batch in executor.map(take, principals) for n in batch]
    assert len(taken) == len(set(taken)) == 12
    assert len(owner.task_list(board="work", status="doing")) == 12


@pytest.mark.parametrize("pickup", ["take", "update"])
def test_expired_backlog_claim_keeps_holder_and_lane_with_history(hub, env, pickup):
    _token, agent, _person, _cookie, owner = env
    worker = service(hub, agent)
    created = owner.task_add(board="work", title="Explicit selection", status="backlog")
    number = created["number"]
    if pickup == "take":
        worker.task_take_next(task=number)
    else:
        worker.task_update(number, status="doing")
    worker.task_comment(number, "Still working")
    with owner.store.transaction() as conn:
        assert (
            conn.execute("SELECT claim_from FROM tasks WHERE id=?", (number,)).fetchone()[0]
            == "backlog"
        )
        conn.execute("UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (number,))
    current = owner.task_show(number)
    assert current["status"] == "doing" and current["assignee"] == agent.actor
    assert current["claim_expired"] and current["claim_until"]
    expired = [e for e in current["events"] if e["kind"] == "expire"]
    assert len(expired) == 1 and expired[0]["actor"] == "system"
    with owner.store.connection() as conn:
        logged = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind='expire'", (number,)
        ).fetchone()
        assert (logged["from_status"], logged["to_status"], logged["target"]) == (
            "doing",
            "doing",
            agent.actor,
        )
    event = events.after(hub.db, hub.workspace)[-1]
    assert (event["kind"], event["actor"]) == ("card.expired", "system")
    assert (event["data"]["from"], event["data"]["to"], event["data"]["target"]) == (
        "doing",
        "doing",
        agent.actor,
    )
    assert worker.task_take_next(board="work") is None
    worker.task_heartbeat(number)
    renewed = owner.task_show(number)
    assert renewed["status"] == "doing" and not renewed["claim_expired"]


def test_lazy_expiry_and_change_counter(hub, env):
    token, _agent, _person, _cookie, owner = env
    card = owner.task_add(title="Expiry", board="work")
    mcp(hub, token, "task_take_next", task=card["number"])
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (card["number"],)
        )
    changes = owner.store.changes
    owner.board_list()
    current = owner.task_show(card["number"])
    assert current["status"] == "doing" and current["assignee"] == "conn:agent"
    assert len([e for e in current["events"] if e["kind"] == "expire"]) == 1
    assert owner.store.changes > changes
    changes = owner.store.changes
    owner.board_list()
    assert owner.store.changes == changes
    assert len([e for e in events.after(hub.db, hub.workspace) if e["kind"] == "card.expired"]) == 1


def test_ask_decide_return_priority_and_decision_line(hub, env):
    token, agent, person, cookie, owner = env
    first = owner.task_add(title="Earlier free card", board="work")
    card = owner.task_add(title="Needs a decision", board="work")
    assert mcp(
        hub,
        token,
        "ask",
        task=card["number"],
        question="1 year or 90 days? Which approach should we take?",
        options=["1y", "90d"],
        context=REVIEW_CONTEXT,
    )[0]
    current = owner.task_show(card["number"])
    assert current["ask_to"] == person.actor and current["return_to"] == agent.actor
    assert current["claim_until"] is None and current["status"] == "review"
    status, result = api(
        hub,
        cookie,
        "POST",
        f"/api/cards/{card['number']}/decide",
        {"version": current["version"], "choice": "90d", "reply": "Keep the other option."},
    )
    assert status == 200 and result["assignee"] == agent.actor and result["ask"] is None
    ok, output = mcp(hub, token, "task_take_next", board="work")
    assert ok and output.startswith(f"#{card['number']}")
    assert f"decision by {person.user_email}: 90d" in output and "Keep the other option." in output
    assert owner.task_show(first["number"])["status"] == "todo"
    assert mcp(hub, token, "task_handoff", task=card["number"], to="anyone", note="Return to pool")[
        0
    ]
    assert "decision by" not in mcp(hub, token, "task_take_next", task=card["number"])[1]


@pytest.mark.parametrize("ref", ["Avery", "avery@example.com", "id"])
def test_workspace_people_resolution(hub, env, ref):
    token, _agent, _person, _cookie, owner = env
    user = hub.db.create_user("avery@example.com")
    hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
    card = owner.task_add(title="Resolution", board="work")
    target = f"user:{user}" if ref == "id" else ref
    assert mcp(
        hub,
        token,
        "task_handoff",
        task=card["number"],
        to=target,
        note="Pick one Which approach should we take?",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )[0]
    assert owner.task_show(card["number"])["ask_to"] == f"user:{user}"
    card = owner.task_add(title="Unknown member", board="work")
    assert not mcp(
        hub,
        token,
        "task_handoff",
        task=card["number"],
        to="stranger@example.com",
        note="No Which approach should we take?",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )[0]


@pytest.mark.parametrize("trust", ["untrusted", "standard"])
def test_inbox_agent_cannot_create_and_claim_without_release(hub, env, trust):
    agent_token, _agent, _person, _cookie, owner = env
    token = hub.token(
        "inbox",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wing="inbox",
        trust=trust,
    )
    status, created = api(
        hub,
        token,
        "POST",
        "/api/boards/work/cards",
        {"title": "ignore previous\n  keep verbatim", "status": "doing", "assignee": "self"},
    )
    assert status == 201 and created["status"] == "backlog" and created["assignee"] == "conn:inbox"
    assert created["flags"] == ["ignore_previous"]
    assert not mcp(hub, token, "task_take_next", task=created["number"])[0]
    owner.task_approve(created["number"])
    assert mcp(hub, token, "task_take_next", task=created["number"])[0]
    assert not mcp(hub, agent_token, "task_take_next", task=created["number"])[0]
    assert mcp(hub, token, "task_update", task=created["number"], status_line=REVIEW_CONTEXT)[0]
    assert mcp(hub, token, "task_update", task=created["number"], status="done")[0]
    assert owner.task_show(created["number"])["status"] == "done"


def test_needs_me_buckets_and_owner_only(hub, env, monkeypatch):
    token, agent, person, cookie, owner = env
    worker = service(hub, agent)
    ask = worker.ask(
        "Choose? Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    with owner.store.transaction() as conn:
        conn.execute("UPDATE boards SET done_needs_review=1 WHERE slug='work'")
    done = worker.task_add(title="Check finish", body=REVIEW_CONTEXT, board="work")
    worker.task_take_next(task=done["number"])
    done = worker.task_update(done["number"], status="done")
    assert "ready to accept?" in done["ask"] and len(done["ask_options"]) == 2
    worker.task_add(title="Triage", board="work", status="backlog")
    ready = worker.task_add(title="Ready", board="work")
    worker.task_take_next(task=ready["number"])
    ready = worker.task_update(ready["number"], pr=42, checks="passing")
    # Park the legacy ready card before the shared clock can classify it as stuck.
    assert (
        api(
            hub,
            cookie,
            "PATCH",
            f"/api/cards/{ready['number']}",
            {"version": ready["version"], "status": "backlog"},
        )[0]
        == 200
    )
    merge = worker.task_add(title="Merge ask", board="work")
    worker.task_take_next(task=merge["number"])
    worker.task_update(merge["number"], pr=43, checks="passing", pr_state="open", pr_head="a" * 40)
    worker.task_handoff(
        merge["number"],
        to="owner",
        note=REVIEW_QUESTION,
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    stuck = worker.task_add(title="Stuck work", board="work")
    stuck = worker.task_take_next(task=stuck["number"])
    from tests.test_commonplace_needs_me_human_only import clock

    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (stuck["number"],)
        )
    stuck = owner.task_show(stuck["number"])
    clock(monkeypatch, stuck["claim_until"])
    result = needs_me(owner.store, person)
    assert {c["number"] for c in result["asks"]} == {ask["number"], merge["number"]}
    assert {c["number"] for c in result["done_review"]} == {done["number"]}
    assert result["backlog_from_agents"] == []
    assert {c["number"] for c in result["ready_to_merge"]} == {merge["number"]}
    assert {c["number"] for c in result["stale"]} == {stuck["number"]}
    assert result["count"] == 4
    assert all(
        set(c) == CARD_KEYS | {"why", "actions"}
        for bucket in result.values()
        if isinstance(bucket, list)
        for c in bucket
    )
    assert mcp(hub, token, "needs_me")[0]
    member = hub.db.create_user("member@example.com")
    hub.db.add_member(hub.workspace, member, "member", actor=hub.admin)
    member_token = hub.token(
        "member",
        user=member,
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["shared"],
        trust="standard",
    )
    assert not {"decide", "needs_me"} & hub.tool_names(member_token)
    assert api(hub, member_token, "GET", "/api/needs-me")[0] == 403


def test_forbidden_board_canary_every_surface(hub, env):
    _token, _agent, _person, _cookie, owner = env
    owner.board_add(slug="hidden-canary", wing="private.secret", repo="github.com/secret/canary")
    secret = owner.seed_card(
        board="hidden-canary", title="CANARY-TITLE", body="CANARY-BODY", status="review"
    )
    owner.task_comment(secret["number"], "CANARY-COMMENT")
    scoped = hub.token(
        "scoped-owner",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["shared"],
        trust="trusted",
    )
    forbidden = (
        "hidden-canary",
        "CANARY-TITLE",
        "CANARY-BODY",
        "CANARY-COMMENT",
        "private.secret",
        "secret/canary",
    )
    operations = [
        ("board_list", {}),
        ("task_list", {}),
        ("task_list", {"mine": True}),
        ("task_list", {"board": "hidden-canary"}),
        ("board_add", {"slug": "hidden-canary", "wing": "shared"}),
        ("task_add", {"title": "attempt", "board": "hidden-canary"}),
        ("task_show", {"task": secret["number"]}),
        ("task_comment", {"task": secret["number"], "text": "attempt"}),
        ("task_take_next", {"task": secret["number"]}),
        ("task_take_next", {"board": "hidden-canary"}),
        ("task_update", {"task": secret["number"], "status": "done"}),
        ("task_handoff", {"task": secret["number"], "to": "owner@example.com", "note": "attempt"}),
        ("ask", {"task": secret["number"], "question": "attempt"}),
        ("ask", {"board": "hidden-canary", "question": "attempt"}),
        ("decide", {"task": secret["number"], "reply": "attempt"}),
        ("needs_me", {}),
    ]
    for name, args in operations:
        _ok, output = mcp(hub, scoped, name, **args)
        assert not any(value in output for value in forbidden), (name, output)
    n = secret["number"]
    requests = [
        ("GET", "/api/boards", None),
        ("GET", "/api/needs-me", None),
        ("GET", "/api/boards/hidden-canary/cards", None),
        ("POST", "/api/boards", {"slug": "hidden-canary", "wing": "shared"}),
        ("POST", "/api/boards/hidden-canary/cards", {"title": "attempt"}),
        ("GET", f"/api/cards/{n}", None),
        ("PATCH", f"/api/cards/{n}", {"version": -1, "status": "done"}),
        ("POST", f"/api/cards/{n}/comments", {"text": "attempt"}),
        ("POST", f"/api/cards/{n}/handoff", {"to": "owner@example.com", "note": "attempt"}),
        ("POST", f"/api/cards/{n}/decide", {"version": -1, "reply": "attempt"}),
    ]
    for method, path, body in requests:
        status, result = api(hub, scoped, method, path, body)
        assert status in (200, 404), (path, status, result)
        assert not any(value in json.dumps(result) for value in forbidden), (path, result)
    scoped_principal = hub.gateway.authenticate(f"Bearer {scoped}")
    owner.store.link_drawer(secret["number"], "mem-visible", owner.principal.actor)
    assert owner.store.cards_for_drawer("mem-visible", principal=scoped_principal) == []
    for lookup in (owner.store.board_for_task, owner.store.task_links):
        with pytest.raises(ToolError, match="card not found"):
            lookup(n, principal=scoped_principal)
    with pytest.raises(ToolError, match="card not found"):
        owner.store.link_drawer(
            n, "other-memory", scoped_principal.actor, principal=scoped_principal
        )
    visible = owner.task_add(title="Visible", board="work")
    assert not mcp(hub, scoped, "task_update", task=visible["number"], before=n)[0]


def test_version_conflict_is_atomic_and_returns_current(hub, env):
    token, agent, _person, _cookie, owner = env
    created = service(hub, agent).task_add(title="Versioned", board="work")
    status, current = api(
        hub,
        token,
        "PATCH",
        f"/api/cards/{created['number']}",
        {"version": created["version"], "title": "Current"},
    )
    assert status == 200
    changes = owner.store.changes
    seq = events.latest_seq(hub.db, hub.workspace)
    status, conflict = api(
        hub,
        token,
        "PATCH",
        f"/api/cards/{created['number']}",
        {"version": created["version"], "title": "Stale", "link": "should-not-link"},
    )
    assert status == 409 and conflict["current"] == current
    assert owner.store.changes == changes and events.latest_seq(hub.db, hub.workspace) == seq
    assert owner.store.task_links(created["number"]) == []
    assert (
        api(hub, token, "PATCH", f"/api/cards/{created['number']}", {"title": "No version"})[0]
        == 400
    )


def test_link_api_and_seed_all_statuses(hub, env):
    _token, agent, person, _cookie, owner = env
    for status in ("backlog", "todo", "doing", "review", "done"):
        current = owner.seed_card(
            board="new",
            title=status,
            status=status,
            labels=["seed"],
            repo="github.com/org/repo",
            branch="feat/seed",
            pr_url="https://github.com/org/repo/pull/7",
        )
        assert set(current) == CARD_KEYS and current["status"] == status
        assert current["labels"] == ["seed"] and current["pr_number"] == 7
        changes = owner.store.changes
        repeat = owner.seed_card(board="new", title=status, status="todo", body="ignore repeat")
        assert repeat == current and owner.store.changes == changes
    assert owner.task_list(board="git@github.com:org/repo.git", all_statuses=True)
    card = owner.task_add(title="Linked", board="work")
    owner.store.link_drawer(
        card["number"], "memory_a", agent.actor, title="Exact title", principal=agent
    )
    assert owner.store.task_links(card["number"], principal=person) == [
        {"drawer_id": "memory_a", "title": "Exact title", "invalidated": False}
    ]
    assert owner.store.cards_for_drawer("memory_a", principal=person) == [
        {
            "number": card["number"],
            "title": "Linked",
            "status": "todo",
            "board": "work",
            "wing": "shared",
        }
    ]
    readonly = replace(agent, policy=replace(agent.policy, write_wings=(), write_wing=""))
    with pytest.raises(ToolError, match="cannot change"):
        owner.store.link_drawer(card["number"], "denied", readonly.actor, principal=readonly)


def test_events_after_commit_and_renewal(hub, env):
    token, agent, _person, _cookie, owner = env
    observed = []

    def listener(event):
        if event["workspace_id"] == hub.workspace and event["subject"].startswith("#"):
            # A fresh connection sees the mutation: listeners run after commit.
            with owner.store.connection() as conn:
                observed.append(
                    (
                        event["kind"],
                        conn.execute(
                            "SELECT status FROM tasks WHERE id=?", (int(event["subject"][1:]),)
                        ).fetchone()[0],
                    )
                )

    events.add_listener(listener)
    try:
        current = owner.task_add(title="Renew", board="work")
        mcp(hub, token, "task_take_next", task=current["number"])
        with owner.store.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET claim_until='2099-01-01T00:00:00Z' WHERE id=?",
                (current["number"],),
            )
        before = owner.task_show(current["number"])
        assert mcp(hub, token, "task_comment", task=current["number"], text="Exact progress")[0]
        after = owner.task_show(current["number"])
        assert (
            after["claim_until"] != before["claim_until"]
            and after["version"] == before["version"] + 1
        )
        assert ("card.created", "todo") in observed and ("card.claimed", "doing") in observed
        assert ("card.commented", "doing") in observed
        comment = after["events"][-1]
        assert comment["actor"] == agent.actor and comment["text"] == "Exact progress"
    finally:
        events.remove_listener(listener)


def test_save_lesson_uses_real_memory_worker(hub, env):
    token, _agent, _person, _cookie, owner = env
    current = owner.task_add(title="Lesson", board="work")
    assert mcp(hub, token, "task_take_next", task=current["number"])[0]
    ok, result = mcp(
        hub,
        token,
        "task_update",
        task=current["number"],
        status="done",
        lesson="  Exact lesson\nsecond line",
    )
    assert ok, result
    linked = owner.store.task_links(current["number"])
    assert len(linked) == 1
    memory = hub.gateway.call_memory_tool(
        owner.principal, "mempalace_get_drawer", {"drawer_id": linked[0]["drawer_id"]}
    )
    assert "Exact lesson" in json.dumps(memory)


def test_board_autocreate_write_scope_and_reserved_card(hub, env):
    token, agent, _person, _cookie, owner = env
    assert mcp(hub, token, "task_add", board="auto-created", title="Automatic")[0]
    limited = hub.token(
        "read-only",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wings=["different"],
        trust="standard",
    )
    assert mcp(hub, limited, "task_add", board="work", title="Own work")[0]
    assert not mcp(hub, limited, "task_add", board="no-auto", title="Denied")[0]
    # Reservations require an active workspace connection.
    hub.owner_token("someone-else")
    card = owner.task_add(title="Reserved", board="work", assignee="someone-else")
    assert not mcp(hub, token, "task_take_next", task=card["number"])[0]
    worker = service(hub, agent)
    card = worker.task_add(title="Held", board="work")
    worker.task_take_next(task=card["number"])
    other = hub.owner_token("other")
    assert not mcp(hub, other, "task_update", task=card["number"], title="Steal")[0]
    assert not mcp(hub, other, "task_handoff", task=card["number"], to="anyone", note="Steal")[0]
    assert mcp(hub, other, "task_comment", task=card["number"], text="Allowed comment")[0]


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("reserved_for", ["other-agent", "owner", "returned-to-agent"])
def test_handoff_respects_todo_reservations(hub, env, standard_agent, surface, reserved_for):
    _token, agent, person, _cookie, owner = env
    token, _principal = standard_agent
    assert mcp(hub, token, "task_add", board="work", title="Can write to this board")[0]
    if reserved_for == "returned-to-agent":
        card = service(hub, agent).ask(
            "Continue? Which approach should we take?",
            board="work",
            context=REVIEW_CONTEXT,
            options=REVIEW_OPTIONS,
        )
        card = owner.decide(
            card["number"], reply="Continue Which approach should we take?", then="return"
        )
    else:
        # Agent reservations remain todo; owner assignments claim immediately.
        creator = service(hub, hub.db.authenticate_token(hub.owner_token("reservation-author")))
        card = creator.task_add(
            title="Reserved",
            board="work",
            assignee=person.actor if reserved_for == "owner" else agent.actor,
        )
    number = card["number"]
    before = owner.task_show(number)
    assert before["status"] == "todo"
    if surface == "mcp":
        ok, refused = mcp(
            hub,
            token,
            "ask",
            task=number,
            question="May I finish? Which approach should we take?",
            context=REVIEW_CONTEXT,
            options=REVIEW_OPTIONS,
        )
        assert not ok and "reserved for another connection" in refused
        assert owner.task_show(number) == before
    for name, method, suffix, arguments in (
        (
            "task_handoff",
            "POST",
            "/handoff",
            {"to": "owner", "note": "May I finish? Which approach should we take?"},
        ),
        ("task_update", "PATCH", "", {"status": "done"}),
    ):
        if surface == "mcp":
            ok, refused = mcp(hub, token, name, task=number, **arguments)
            assert not ok
        else:
            code, refused = api(
                hub,
                token,
                method,
                f"/api/cards/{number}{suffix}",
                {"version": before["version"], **arguments},
            )
            assert code == 403
        assert "reserved for another connection" in str(refused)
        assert owner.task_show(number) == before


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("reserved", [False, True], ids=["free", "self-reserved"])
def test_handoff_accepts_free_and_self_reserved_todo(hub, env, standard_agent, surface, reserved):
    _token, _agent, person, _cookie, owner = env
    token, principal = standard_agent
    card = owner.task_add(
        title="Available", board="work", assignee=principal.actor if reserved else None
    )
    number = card["number"]
    if surface == "mcp":
        assert mcp(
            hub,
            token,
            "task_handoff",
            task=number,
            to="owner",
            note="Ready? Which approach should we take?",
            context=REVIEW_CONTEXT,
            options=REVIEW_OPTIONS,
        )[0]
    else:
        code, _result = api(
            hub,
            token,
            "POST",
            f"/api/cards/{number}/handoff",
            {
                "version": card["version"],
                "to": "owner",
                "note": "Ready? Which approach should we take?",
                "context": REVIEW_CONTEXT,
                "options": REVIEW_OPTIONS,
            },
        )
        assert code == 200
    current = owner.task_show(number)
    assert current["status"] == "review" and current["assignee"] is None
    assert (
        current["ask"] == "Ready? Which approach should we take?"
        and current["ask_to"] == person.actor
    )
    assert current["return_to"] == principal.actor


def test_expired_version_conflict_returns_committed_card(hub, env):
    token, _agent, _person, _cookie, owner = env
    created = owner.task_add(title="Expired version", board="work")
    assert mcp(hub, token, "task_take_next", task=created["number"])[0]
    current = owner.task_show(created["number"])
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (created["number"],)
        )
    status, conflict = api(
        hub,
        token,
        "PATCH",
        f"/api/cards/{created['number']}",
        {"version": current["version"], "title": "Should not apply"},
    )
    assert status == 409 and conflict["current"]["status"] == "doing"
    actual = owner.task_show(created["number"])
    assert {key: actual[key] for key in CARD_KEYS} == {
        k: v for k, v in conflict["current"].items() if k != "url"
    }
    assert actual["title"] == "Expired version"


def test_memory_link_renews_holder_and_never_needs_the_hold(hub, env):
    token, agent, _person, _cookie, owner = env
    created = owner.task_add(title="Linked while doing", board="work")
    assert mcp(hub, token, "task_take_next", task=created["number"])[0]
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET claim_until='2099-01-01T00:00:00Z' WHERE id=?", (created["number"],)
        )
    owner.store.link_drawer(created["number"], "memory-holder", agent.actor, principal=agent)
    assert owner.task_show(created["number"])["claim_until"] != "2099-01-01T00:00:00Z"
    other = hub.gateway.authenticate(f"Bearer {hub.owner_token('other-linker')}")
    owner.store.link_drawer(created["number"], "memory-other", other.actor, principal=other)
    assert owner.task_show(created["number"])["status"] == "doing"
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (created["number"],)
        )
    owner.store.link_drawer(created["number"], "memory-after-expiry", other.actor, principal=other)
    assert owner.task_show(created["number"])["status"] == "doing"


def test_an_agent_never_answers_its_own_question(hub, env):
    token, _agent, _person, _cookie, owner = env
    ok, text = mcp(
        hub,
        token,
        "ask",
        question="Ship it? Which approach should we take?",
        options=["yes", "no"],
        board="work",
        context=REVIEW_CONTEXT,
    )
    assert ok, text
    number = int(text.split()[0].lstrip("#"))
    card = owner.task_show(number)
    assert card["status"] == "review"
    ok, text = mcp(hub, token, "decide", task=number, choice="yes")
    assert not ok and "signed-in workspace owner" in text
    assert owner.task_show(number)["status"] == "review"


def test_reverse_links_include_authoritative_board_and_wing(hub, env):
    _token, agent, person, _cookie, owner = env
    owner.board_add(slug="restricted", wing="private.reverse-links")
    owner.board_add(slug="invalid-theme", wing="shared")
    allowed = owner.task_add(title="Allowed link", board="work")
    denied = owner.task_add(title="Restricted link", board="restricted")
    invalid = owner.task_add(title="Missing theme", board="invalid-theme")
    for item in (allowed, denied, invalid):
        owner.store.link_drawer(item["number"], "reverse-link-memory", person.actor)
    with owner.store.transaction() as conn:
        conn.execute("UPDATE boards SET wing='' WHERE slug='invalid-theme'")
    raw = owner.store.cards_for_drawer("reverse-link-memory")
    assert {item["number"]: (item["board"], item["wing"]) for item in raw} == {
        allowed["number"]: ("work", "shared"),
        denied["number"]: ("restricted", "private.reverse-links"),
        invalid["number"]: ("invalid-theme", ""),
    }
    scoped = replace(agent, policy=replace(agent.policy, read_wings=("shared",)))
    scoped_cards = owner.store.cards_for_drawer("reverse-link-memory", principal=scoped)
    assert [item["number"] for item in scoped_cards] == [allowed["number"]]
    owner_cards = owner.store.cards_for_drawer("reverse-link-memory", principal=person)
    assert {item["number"] for item in owner_cards} == {allowed["number"], denied["number"]}
    # Match the memory producer's fail-closed filter, including for owners.
    assert owner_cards == [
        item for item in raw if person.policy.can_read_wing(item.get("wing", ""))
    ]
    assert not person.policy.can_read_wing({}.get("wing", ""))


def test_cards_carry_short_links_and_people_read_as_names(hub, env):
    token, _agent, _person, cookie, owner = env
    ok, text = mcp(
        hub,
        token,
        "ask",
        question="Ship? Which approach should we take?",
        options=["yes", "no"],
        board="work",
        context=REVIEW_CONTEXT,
    )
    assert ok, text
    assert "owner@example.com" in text and "user:" not in text
    assert "/s/" in text
    number = int(text.split()[0].lstrip("#"))
    status, card = api(hub, cookie, "GET", f"/api/cards/{number}")
    assert status == 200 and "/s/" in card["url"]
    status, needs = api(hub, cookie, "GET", "/api/needs-me")
    assert all("url" in c for c in needs["asks"])


def test_handoff_to_a_person_by_name_or_owner(hub, env):
    token, _agent, _person, _cookie, owner = env
    hub.db._conn.execute("UPDATE users SET name='Avery' WHERE id=?", (hub.admin,))
    for to in ("avery", "owner"):
        card = owner.task_add(title=f"hand to {to}", board="work")
        assert mcp(hub, token, "task_take_next", task=card["number"])[0]
        ok, text = mcp(
            hub,
            token,
            "task_handoff",
            task=card["number"],
            to=to,
            note="Pick one Which approach should we take?",
            context=REVIEW_CONTEXT,
            options=["a", "b"],
        )
        assert ok, text
        assert owner.task_show(card["number"])["status"] == "review"


@pytest.mark.parametrize(
    "field,value", [("title", "Changed"), ("body", "Changed"), ("labels", ["changed"])]
)
@pytest.mark.parametrize("status", ["backlog", "todo", "doing", "review", "done"])
def test_agent_cannot_edit_another_persons_card(hub, env, field, value, status):
    _token, agent, _person, _cookie, owner = env
    created = owner.seed_card(board="work", title="Original", body="Original body", status=status)
    worker = service(hub, agent)
    for args in ({field: value}, {field: value, "status": "backlog"}):
        with pytest.raises(ToolError):
            worker.task_update(created["number"], **args)
    actual = owner.task_show(created["number"])
    assert actual[field] == created[field] and actual["status"] == status
    changed = owner.task_update(created["number"], **{field: value})
    assert changed[field] == value


@pytest.mark.parametrize("status", ["backlog", "todo", "doing", "done"])
def test_agent_edits_only_own_unclaimed_or_held_cards(hub, env, status):
    _token, agent, _person, _cookie, owner = env
    worker = service(hub, agent)
    creator = owner if status == "doing" else worker
    created = creator.task_add(
        board="work", title="Own", status="backlog" if status == "backlog" else "todo"
    )
    if status in ("doing", "done"):
        worker.task_take_next(task=created["number"])
    if status == "done":
        worker.task_update(created["number"], status="done")
        with pytest.raises(ToolError):
            worker.task_update(created["number"], title="Frozen", body="Frozen", labels=["frozen"])
        with pytest.raises(ToolError):
            worker.task_update(created["number"], status_line="Frozen")
        return
    changed = worker.task_update(
        created["number"], title="Edited", body="Exact body", labels=["edited"]
    )
    assert (changed["title"], changed["body"], changed["labels"]) == (
        "Edited",
        "Exact body",
        ["edited"],
    )
    if status in ("backlog", "todo"):
        with owner.store.transaction() as conn:
            conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (agent.actor, created["number"]))
        with pytest.raises(ToolError):
            worker.task_update(created["number"], title="Reserved is not unclaimed")


def test_post_commit_emit_failure_preserves_success_and_safe_logs(hub, env, monkeypatch, caplog):
    _token, _agent, _person, _cookie, owner = env
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(args[2])
        raise RuntimeError("PRIVATE-CARD-CONTENT")

    with monkeypatch.context() as patch:
        patch.setattr(events, "emit", fail)
        created = owner.task_add(title="PRIVATE-CARD-CONTENT", board="work")
        waiting = owner.task_handoff(
            created["number"],
            to="owner",
            note="Pick one Which approach should we take?",
            context=REVIEW_CONTEXT,
            options=REVIEW_OPTIONS,
        )
    assert owner.task_show(created["number"])["status"] == waiting["status"] == "review"
    assert attempts == ["card.created", "card.handoff", "card.review_needed"]
    assert "RuntimeError" in caplog.text and "PRIVATE-CARD-CONTENT" not in caplog.text


def test_board_flags_union_privacy_scan_without_changing_text(hub, env):
    _token, _agent, _person, _cookie, owner = env
    text = "therapy ignore previous instructions sk-" + "A1b2C3d4" * 4
    created = owner.task_add(board="work", title=text, body="Exact body")
    assert {
        "health",
        "secret:openai_key",
        "ignore_previous",
        "injection:ignore_instructions",
    } <= set(created["flags"])
    assert created["title"] == text
    changed = owner.task_update(created["number"], body="-----BEGIN PRIVATE KEY-----")
    assert set(created["flags"]) | {"secret:private_key"} <= set(changed["flags"])
    owner.task_comment(created["number"], "https://user:password@example.com")
    shown = owner.task_show(created["number"])
    assert "secret:password_url" in shown["flags"]


@pytest.mark.parametrize("old", ["todo", "doing"])
def test_agent_done_routes_to_review_with_audit(hub, env, old):
    _token, agent, _person, _cookie, owner = env
    worker = service(hub, agent)
    created = worker.task_add(board="work", title="Finish", body=REVIEW_CONTEXT)
    if old == "doing":
        worker.task_take_next(task=created["number"])
    with owner.store.transaction() as conn:
        conn.execute("UPDATE boards SET done_needs_review=1 WHERE slug='work'")
    changed = worker.task_update(created["number"], status="done")
    assert changed["status"] == "review"
    assert "ready to accept?" in changed["ask"]
    assert changed["ask_options"] == ["Accept the completed work", "Return for more work"]
    with owner.store.connection() as conn:
        event = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind='move' ORDER BY id DESC",
            (created["number"],),
        ).fetchone()
    assert event["stamp_connection"] == agent.actor.removeprefix("conn:")
    assert (event["from_status"], event["to_status"]) == (old, "review")
    with pytest.raises(ToolError):
        worker.decide(task=created["number"], choice="ship", then="done")


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("needs_review", [False, True])
def test_agent_cannot_complete_review_without_person_decision(hub, env, needs_review, surface):
    token, agent, _person, _cookie, owner = env
    worker = service(hub, agent)
    waiting = worker.ask(
        "Ready to close? Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    number = waiting["number"]
    with owner.store.transaction() as conn:
        conn.execute("UPDATE boards SET done_needs_review=? WHERE slug='work'", (needs_review,))
    with pytest.raises(ToolError):
        worker.decide(task=number, choice="yes", then="done")
    current = worker.task_update(
        number,
        status="review",
        checks="passing",
    )
    assert current["ask"] == waiting["ask"] and current["ask_to"] == waiting["ask_to"]
    before = owner.task_show(number)
    if surface == "mcp":
        assert not mcp(hub, token, "task_update", task=number, status="done")[0]
    else:
        code, _ = api(
            hub,
            token,
            "PATCH",
            f"/api/cards/{number}",
            {"version": current["version"], "status": "done"},
        )
        assert code == 403
    assert owner.task_show(number) == before
    assert number in {card["number"] for card in owner.needs_me()["asks"]}


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("needs_review", [False, True])
@pytest.mark.parametrize("member", [False, True], ids=["other-owner-agent", "member-agent"])
@pytest.mark.parametrize("status", ["done", "todo"])
def test_other_agents_cannot_move_review(hub, env, surface, needs_review, member, status):
    _token, agent, _person, _cookie, owner = env
    if member:
        user = hub.db.create_user("member@example.com")
        hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
        token = hub.token(
            "member",
            user=user,
            profile="full",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared"],
            write_wings=["shared"],
            trust="standard",
        )
    else:
        token = hub.owner_token("other")
    assert mcp(hub, token, "task_add", board="work", title="Can write to this board")[0]
    with owner.store.transaction() as conn:
        conn.execute("UPDATE boards SET done_needs_review=? WHERE slug='work'", (needs_review,))
    for asker in (service(hub, agent), owner):
        waiting = asker.ask(
            "Keep this question? Which approach should we take?",
            options=["yes", "no"],
            board="work",
            context=REVIEW_CONTEXT,
        )
        number = waiting["number"]
        before = owner.task_show(number)
        if surface == "mcp":
            ok, refused = mcp(hub, token, "task_update", task=number, status=status)
            assert not ok
        else:
            code, refused = api(
                hub,
                token,
                "PATCH",
                f"/api/cards/{number}",
                {"version": before["version"], "status": status},
            )
            assert code == 403
        assert "card is waiting on a person" in str(refused)
        assert owner.task_show(number) == before
        assert number in {card["number"] for card in owner.needs_me()["asks"]}


def review_worker(hub, *, member=False, **policy):
    user = hub.admin
    if member:
        user = hub.db.create_user("reviewer@example.com")
        hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
    token = hub.token(
        "review-worker",
        user=user,
        **{
            "profile": "full",
            "scopes": ["memory:read", "memory:write"],
            "read_wings": ["shared"],
            "write_wings": ["shared"],
            "trust": "trusted",
            **policy,
        },
    )
    principal = hub.gateway.authenticate(f"Bearer {token}")
    return token, principal, service(hub, principal)


def assert_review_event(hub, owner, number, actor, status):
    kind = "edit" if status == "review" else "move"
    with owner.store.connection() as conn:
        event = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (number,)
        ).fetchone()
    assert event["kind"] == kind
    assert (event["from_status"], event["to_status"]) == ("review", status)
    assert event["stamp_connection"] == actor.removeprefix("conn:")
    assert event["stamp_trust"] == "trusted"
    emitted = [
        event for event in events.after(hub.db, hub.workspace) if event["subject"] == f"#{number}"
    ][-1]
    assert emitted["kind"] == ("card.updated" if status == "review" else "card.moved")
    assert emitted["actor"] == actor
    assert (emitted["data"]["from"], emitted["data"]["to"]) == ("review", status)


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("member", [False, True])
@pytest.mark.parametrize("relation", ["requester", "assignee"])
def test_trusted_review_edit_keeps_person_ask(hub, env, surface, member, relation):
    _token, agent, _person, _cookie, owner = env
    token, principal, worker = review_worker(hub, member=member)
    requester = worker if relation == "requester" else service(hub, agent)
    waiting = requester.ask(
        "Keep this question? Which approach should we take?",
        options=["yes", "no"],
        board="work",
        context=REVIEW_CONTEXT,
    )
    number = waiting["number"]
    if relation == "assignee":
        with owner.store.transaction() as conn:
            conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (principal.actor, number))
    before = owner.task_show(number)
    drawer = worker.ctx.memory(
        "mempalace_add_drawer", {"content": "Review evidence", "wing": "shared", "room": "general"}
    )["drawer_id"]
    args = {
        "title": "Revised",
        "body": "Exact feedback\n",
        "checks": "passing",
        "labels": ["revised"],
        "link": drawer,
    }
    if surface == "mcp":
        assert mcp(hub, token, "task_update", task=number, **args)[0]
    else:
        assert (
            api(
                hub, token, "PATCH", f"/api/cards/{number}", {"version": before["version"], **args}
            )[0]
            == 200
        )
    current = owner.task_show(number)
    assert (current["title"], current["body"], current["checks"], current["labels"]) == (
        "Revised",
        "Exact feedback\n",
        "passing",
        ["revised"],
    )
    assert current["links"] == 1 and current["status"] == "review"
    for key in ("ask", "ask_options", "ask_to", "return_to", "assignee"):
        assert current[key] == before[key]
    assert number in {item["number"] for item in owner.needs_me()["asks"]}
    assert_review_event(hub, owner, number, principal.actor, "review")
    assert json.loads(current["events"][-1]["text"]) == {
        "title": before["title"],
        "body": before["body"],
        "checks": before["checks"],
        "labels": before["labels"],
        "links": [],
    }
    worker.task_update(number, unlink=drawer)
    unlinked = owner.task_show(number)
    assert unlinked["links"] == 0
    assert json.loads(unlinked["events"][-1]["text"]) == {"links": [drawer]}
    assert_review_event(hub, owner, number, principal.actor, "review")
    if relation == "requester":
        with pytest.raises(ToolError):
            worker.decide(number, reply="Approved", then="done")


@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_review_edit_history_preserves_previous_values_and_visible_links(hub, env, surface):
    _token, _agent, _person, _cookie, owner = env
    token, principal, worker = review_worker(hub, member=True)
    original = {
        "title": 'Original "title"',
        "body": "  Original body\n日本語\n",
        "labels": ["  original label  "],
    }
    created = worker.task_add(board="work", **original)
    number = created["number"]
    worker.task_take_next(task=number)
    old_link, new_link = [
        worker.ctx.memory(
            "mempalace_add_drawer", {"content": text, "wing": "shared", "room": "general"}
        )["drawer_id"]
        for text in ("Old evidence", "New evidence")
    ]
    hidden = owner.ctx.memory(
        "mempalace_add_drawer",
        {"content": "HIDDEN-LINK-MARKER", "wing": "private.hidden", "room": "general"},
    )["drawer_id"]
    worker.task_update(number, checks="failing", link=old_link)
    owner.task_update(number, link=hidden)
    waiting = worker.ask(
        "Keep this ask Which approach should we take?",
        task=number,
        options=["yes", "no"],
        context=REVIEW_CONTEXT,
    )
    args = {
        "title": "Revised title",
        "body": "Revised body",
        "checks": "passing",
        "labels": ["revised"],
        "link": new_link,
        "unlink": old_link,
    }
    if surface == "mcp":
        assert mcp(hub, token, "task_update", task=number, **args)[0]
    else:
        assert (
            api(
                hub, token, "PATCH", f"/api/cards/{number}", {"version": waiting["version"], **args}
            )[0]
            == 200
        )
    current = worker.task_show(number)
    previous = json.loads(current["events"][-1]["text"])
    assert previous == {**original, "checks": "failing", "links": [old_link]}
    assert {link["drawer_id"] for link in current["linked"]} == {new_link}
    assert current["ask"] == waiting["ask"] and current["version"] == waiting["version"] + 1
    assert hidden not in json.dumps(previous)
    assert "HIDDEN-LINK-MARKER" not in mcp(hub, token, "task_show", task=number)[1]
    assert_review_event(hub, owner, number, principal.actor, "review")


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("removed", [False, True])
def test_review_history_hides_other_editors_private_links(hub, env, surface, removed):
    _token, _agent, _person, _cookie, owner = env
    token, _principal, worker = review_worker(hub, member=True)
    waiting = worker.ask(
        "Keep this ask Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    number = waiting["number"]
    private, shared = [
        owner.ctx.memory(
            "mempalace_add_drawer", {"content": text, "wing": wing, "room": "general"}
        )["drawer_id"]
        for text, wing in (("Private evidence", "private.hidden"), ("Shared evidence", "shared"))
    ]
    owner.task_update(number, link=private)
    owner.task_update(number, link=shared)
    owner.task_update(number, unlink=shared)
    if removed:
        owner.task_update(number, unlink=private)
    with owner.store.connection() as conn:
        stored = [
            row["text"]
            for row in conn.execute(
                "SELECT text FROM task_events WHERE task_id=? AND kind='edit'", (number,)
            )
        ]
    assert any(private in text for text in stored if text)
    if surface == "mcp":
        ok, text = mcp(hub, token, "task_show", task=number)
        assert ok
    else:
        code, current = api(hub, token, "GET", f"/api/cards/{number}")
        assert code == 200
        text = json.dumps(current)
    assert private not in text and shared in text
    history = worker.task_show(number)["events"]
    snapshots = [json.loads(event["text"]) for event in history if event["kind"] == "edit"]
    assert any(shared in snapshot.get("links", []) for snapshot in snapshots)
    assert all(private not in snapshot.get("links", []) for snapshot in snapshots)
    assert owner.task_show(number)["ask"] == waiting["ask"]
    with owner.store.connection() as conn:
        assert stored == [
            row["text"]
            for row in conn.execute(
                "SELECT text FROM task_events WHERE task_id=? AND kind='edit'", (number,)
            )
        ]


def test_stale_person_decide_after_review_edit_is_a_version_clash(hub, env):
    relay_token, _relay, person, cookie, owner = env
    _token, principal, requester = review_worker(hub, member=True)
    waiting = requester.ask(
        "Approve this version? Which approach should we take?",
        options=["yes", "no"],
        board="work",
        context=REVIEW_CONTEXT,
    )
    number = waiting["number"]
    schema = next(
        tool["inputSchema"]
        for tool in hub.rpc(relay_token, "tools/list")["result"]["tools"]
        if tool["name"] == "decide"
    )
    assert schema["properties"]["version"] == {"type": "integer"}
    assert "version" not in schema.get("required", [])
    requester.task_update(number, body="Feedback after the owner read the card")
    current = owner.task_show(number)
    emitted = events.after(hub.db, hub.workspace)
    code, conflict = api(
        hub,
        cookie,
        "POST",
        f"/api/cards/{number}/decide",
        {"version": waiting["version"], "choice": "yes"},
    )
    assert code == 409 and conflict["current"]["version"] == current["version"]
    assert owner.task_show(number) == current
    assert events.after(hub.db, hub.workspace) == emitted
    assert not mcp(
        hub, relay_token, "decide", task=number, version=current["version"], choice="yes"
    )[0]
    assert (
        api(
            hub,
            cookie,
            "POST",
            f"/api/cards/{number}/decide",
            {"version": current["version"], "choice": "yes"},
        )[0]
        == 200
    )
    answered = owner.task_show(number)
    assert answered["status"] == "todo" and answered["assignee"] == principal.actor
    assert answered["ask"] is None
    assert answered["events"][-1]["kind"] == "decide"
    event = events.after(hub.db, hub.workspace)[-1]
    assert event["kind"] == "card.decided" and event["actor"] == person.actor


def test_public_review_creation_is_person_directed_and_agent_handoff_stays_todo(hub, env):
    _token, agent, person, _cookie, _owner = env
    worker = service(hub, agent)
    hub.owner_token("review-recipient")
    asked = worker.ask(
        "Person decision Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    work = worker.task_add(title="Work to review", board="work")
    worker.task_take_next(task=work["number"])
    reviewed = worker.task_update(
        work["number"],
        status="review",
        question=REVIEW_QUESTION,
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    handed = worker.task_add(title="Hand to an agent", board="work")
    worker.task_take_next(task=handed["number"])
    moved = worker.task_handoff(
        handed["number"],
        to="conn:review-recipient",
        note="Continue Which approach should we take?",
    )
    for card in (asked, reviewed):
        assert card["status"] == "review" and card["ask_to"] == person.actor
        assert card["assignee"] is None
    assert moved["status"] == "todo" and moved["ask_to"] is None


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("relation", ["requester", "assignee", "assignee-other-agent"])
@pytest.mark.parametrize("status", ["todo", "doing"])
@pytest.mark.parametrize(
    "ask_text,reason",
    [
        ("Review?", None),
        ("Done?", "Needs edits"),
        ("Agent question\n exact", "Exact\nfeedback"),
    ],
)
def test_trusted_review_return_withdraws_agent_ask(
    hub, env, surface, relation, status, ask_text, reason
):
    _token, agent, _person, _cookie, owner = env
    token, principal, worker = review_worker(hub, member=True)
    requester = worker if relation == "requester" else service(hub, agent)
    ask_text += " Which approach should we take?"
    waiting = requester.ask(ask_text, board="work", context=REVIEW_CONTEXT, options=REVIEW_OPTIONS)
    number = waiting["number"]
    recipient = agent.actor if relation == "requester" else principal.actor
    if relation == "assignee-other-agent":
        peer = hub.owner_token("review-recipient")
        recipient = hub.gateway.authenticate(f"Bearer {peer}").actor
    # Agent-directed reviews have no public creation path yet.
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET ask_to=?,assignee=? WHERE id=?",
            (
                recipient,
                principal.actor if relation != "requester" else None,
                number,
            ),
        )
    before = owner.task_show(number)
    args = {"status": status, "title": "After feedback", "checks": "pending"}
    if reason is not None:
        args["reason"] = reason
    if surface == "mcp":
        assert mcp(hub, token, "task_update", task=number, **args)[0]
    else:
        assert (
            api(
                hub, token, "PATCH", f"/api/cards/{number}", {"version": before["version"], **args}
            )[0]
            == 200
        )
    current = owner.task_show(number)
    assert current["status"] == status and current["title"] == "After feedback"
    assert current["checks"] == "pending" and current["return_to"] is None
    assert current["ask"] is None and current["ask_to"] is None
    assert current["assignee"] == principal.actor
    assert bool(current["claim_until"]) == (status == "doing")
    assert_review_event(hub, owner, number, principal.actor, status)
    with owner.store.connection() as conn:
        released = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind='release'", (number,)
        ).fetchall()
        assert (
            conn.execute(
                "SELECT count(*) FROM task_events WHERE task_id=? AND kind='decide'", (number,)
            ).fetchone()[0]
            == 0
        )
    assert len(released) == 1
    assert (released[0]["text"], released[0]["reason"], released[0]["target"]) == (
        ask_text,
        reason or None,
        before["ask_to"],
    )
    assert (released[0]["from_status"], released[0]["to_status"]) == ("review", status)
    assert released[0]["stamp_connection"] == principal.connection
    assert released[0]["created_at"] and released[0]["stamp_trust"] == "trusted"
    assert json.loads(released[0]["options"]) == before["ask_options"]
    withdrawn = [
        event
        for event in events.after(hub.db, hub.workspace)
        if event["kind"] == "card.ask_withdrawn" and event["subject"] == f"#{number}"
    ]
    assert len(withdrawn) == 1 and withdrawn[0]["actor"] == principal.actor
    assert (withdrawn[0]["data"]["from"], withdrawn[0]["data"]["to"]) == ("review", status)
    assert withdrawn[0]["data"]["target"] == before["ask_to"]
    assert withdrawn[0]["data"]["status"] == status and withdrawn[0]["data"]["ask_to"] is None
    logged = [event for event in current["events"] if event["kind"] == "release"]
    assert logged[0]["text"] == ask_text and logged[0]["reason"] == (reason or None)
    if reason:
        assert reason in mcp(hub, token, "task_show", task=number)[1].replace("\\n", "\n")


def test_review_withdrawal_empty_reason_is_null(hub, env):
    _token, agent, _person, _cookie, owner = env
    _token, _principal, worker = review_worker(hub)
    waiting = worker.ask(
        "Future agent review Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    number = waiting["number"]
    with owner.store.transaction() as conn:
        conn.execute("UPDATE tasks SET ask_to=? WHERE id=?", (agent.actor, number))
    worker.task_update(number, status="todo", reason="")
    with owner.store.connection() as conn:
        event = conn.execute(
            "SELECT reason FROM task_events WHERE task_id=? AND kind='release'", (number,)
        ).fetchone()
    assert event["reason"] is None
    assert [e for e in owner.task_show(number)["events"] if e["kind"] == "release"][0][
        "reason"
    ] is None


@pytest.mark.parametrize("field", ["ask_to", "default_recipient", "return_to", "assignee"])
@pytest.mark.parametrize("status", ["todo", "doing"])
def test_review_return_cannot_clear_a_pending_person_decision(hub, env, field, status):
    _token, _agent, person, _cookie, owner = env
    token, principal, worker = review_worker(hub)
    waiting = worker.ask(
        "Still waiting Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    number = waiting["number"]
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET ask_to=?,assignee=? WHERE id=?",
            (principal.actor, principal.actor, number),
        )
        conn.execute(
            f"UPDATE tasks SET {'ask_to' if field == 'default_recipient' else field}=? WHERE id=?",
            (None if field == "default_recipient" else person.actor, number),
        )
    before = owner.task_show(number)
    emitted = events.after(hub.db, hub.workspace)
    ok, refusal = mcp(hub, token, "task_update", task=number, status=status)
    assert not ok and "waiting on a person, they must answer via decide" in refusal
    assert (
        api(
            hub,
            token,
            "PATCH",
            f"/api/cards/{number}",
            {"version": before["version"], "status": status},
        )[0]
        == 403
    )
    assert owner.task_show(number) == before
    assert events.after(hub.db, hub.workspace) == emitted
    if field in ("ask_to", "default_recipient"):
        assert number in {item["number"] for item in owner.needs_me()["asks"]}


@pytest.mark.parametrize(
    "args",
    [
        {"title": "No Which approach should we take?"},
        {"body": "No Which approach should we take?"},
        {"checks": "passing"},
        {"labels": ["no"]},
        {"link": "missing"},
        {"unlink": "missing"},
        {"status": "todo"},
        {"status": "doing"},
        {"status": "done"},
    ],
)
def test_unrelated_agent_cannot_edit_bounce_or_complete_review(hub, env, args):
    _token, agent, _person, _cookie, owner = env
    token, _principal, _worker = review_worker(hub)
    waiting = service(hub, agent).ask(
        "Someone else's question Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    number = waiting["number"]
    with owner.store.transaction() as conn:
        conn.execute("UPDATE tasks SET ask_to=? WHERE id=?", (agent.actor, number))
    before = owner.task_show(number)
    emitted = events.after(hub.db, hub.workspace)
    assert not mcp(hub, token, "task_update", task=number, **args)[0]
    assert (
        api(hub, token, "PATCH", f"/api/cards/{number}", {"version": before["version"], **args})[0]
        == 403
    )
    assert owner.task_show(number) == before
    assert events.after(hub.db, hub.workspace) == emitted


def test_review_assignee_cannot_complete_another_requesters_card(hub, env):
    _token, agent, _person, _cookie, owner = env
    token, principal, _worker = review_worker(hub)
    waiting = service(hub, agent).ask(
        "Requester's decision Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    with owner.store.transaction() as conn:
        conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (principal.actor, waiting["number"]))
    before = owner.task_show(waiting["number"])
    assert not mcp(hub, token, "task_update", task=waiting["number"], status="done")[0]
    assert owner.task_show(waiting["number"]) == before


@pytest.mark.parametrize(
    "policy",
    [
        {"trust": "standard"},
        {"trust": "untrusted", "write_wing": "inbox"},
        {"write_wing": "inbox"},
        {"write_wing": "wing_inbox"},
        {"write_wings": ["another"]},
        {"scopes": ["memory:read"]},
    ],
)
def test_review_feedback_requires_trusted_board_writer(hub, env, policy):
    _token, agent, _person, _cookie, owner = env
    token, principal, worker = review_worker(hub, **policy)
    waiting = service(hub, agent).ask(
        "Feedback Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET return_to=?,ask_to=? WHERE id=?",
            (principal.actor, agent.actor, waiting["number"]),
        )
    before = owner.task_show(waiting["number"])
    for args in (
        {"title": "No Which approach should we take?"},
        {"checks": "passing"},
        {"status": "todo"},
        {"status": "doing"},
    ):
        if not principal.policy.has_scope("memory:write"):
            with pytest.raises(ToolError):
                worker.task_update(waiting["number"], **args)
            assert not mcp(hub, token, "task_update", task=waiting["number"], **args)[0]
        else:
            worker.task_update(waiting["number"], **args)
            ok, result = mcp(hub, token, "task_update", task=waiting["number"], **args)
            assert ok, result
    if not principal.policy.has_scope("memory:write"):
        assert owner.task_show(waiting["number"]) == before
    else:
        assert owner.task_show(waiting["number"])["status"] == "doing"


@pytest.mark.parametrize(
    "args",
    [
        {"branch": "changed"},
        {"repo": "example.com/repo"},
        {"pr": 1},
        {"status_line": "changed"},
        {"before": 1},
        {"status": "backlog"},
        {"status": "done", "title": "changed"},
    ],
)
def test_review_feedback_keeps_other_update_fields_blocked(hub, env, args):
    _token, _agent, _person, _cookie, owner = env
    _token, _principal, worker = review_worker(hub)
    waiting = worker.ask(
        "Pending Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    before = owner.task_show(waiting["number"])
    with pytest.raises(ToolError, match="card is waiting on a person"):
        worker.task_update(waiting["number"], **args)
    assert owner.task_show(waiting["number"]) == before


@pytest.mark.parametrize("reason", [None, 12, [], {"text": "no"}])
def test_review_withdrawal_validates_reason_before_mutation(hub, env, reason):
    _token, agent, _person, _cookie, owner = env
    token, _principal, worker = review_worker(hub)
    waiting = worker.ask(
        "Agent feedback Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    with owner.store.transaction() as conn:
        conn.execute("UPDATE tasks SET ask_to=? WHERE id=?", (agent.actor, waiting["number"]))
    before = owner.task_show(waiting["number"])
    emitted = events.after(hub.db, hub.workspace)
    assert not mcp(hub, token, "task_update", task=waiting["number"], status="todo", reason=reason)[
        0
    ]
    with pytest.raises(ToolError, match="reason must be text"):
        worker.task_update(waiting["number"], status="todo", reason=reason)
    assert owner.task_show(waiting["number"]) == before
    assert events.after(hub.db, hub.workspace) == emitted


def test_withdrawal_reason_migrates_existing_history_and_preserves_invariant(hub, env):
    _token, agent, _person, _cookie, owner = env
    _token, principal, worker = review_worker(hub)
    waiting = worker.ask(
        "  Original ask\n Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    number = waiting["number"]
    with owner.store.transaction() as conn:
        conn.execute("UPDATE tasks SET ask_to=? WHERE id=?", (agent.actor, number))
        conn.execute("ALTER TABLE task_events DROP COLUMN reason")
        original = [dict(row) for row in conn.execute("SELECT * FROM task_events ORDER BY id")]
    restarted = service(hub, principal)
    with owner.store.connection() as conn:
        migrated = [dict(row) for row in conn.execute("SELECT * FROM task_events ORDER BY id")]
    assert [
        {key: value for key, value in row.items() if key != "reason"} for row in migrated
    ] == original
    assert all(row["reason"] is None for row in migrated)
    restarted.task_update(number, status="todo", reason="  Exact reason\n")
    with owner.store.connection() as conn:
        event = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind='release'", (number,)
        ).fetchone()
        assert (event["text"], event["reason"]) == (
            "  Original ask\n Which approach should we take?",
            "  Exact reason\n",
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute("UPDATE tasks SET ask='cannot remain open' WHERE id=?", (number,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM task_events WHERE id=?", (event["id"],))


def test_reason_is_only_available_on_review_returns(hub, env):
    _token, _agent, _person, _cookie, owner = env
    _token, _principal, worker = review_worker(hub)
    waiting = worker.ask(
        "Pending Which approach should we take?",
        board="work",
        context=REVIEW_CONTEXT,
        options=REVIEW_OPTIONS,
    )
    before = owner.task_show(waiting["number"])
    with pytest.raises(ToolError, match="unknown update field"):
        worker.task_update(waiting["number"], checks="passing", reason="No return")
    assert owner.task_show(waiting["number"]) == before
