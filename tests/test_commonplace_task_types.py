"""Task types slice 1: production card and owner configuration boundaries."""

import pytest

from commonplace.boards.store import BoardStore
from commonplace.boards.tools import line
from commonplace.extensions import ToolError
from tests.test_commonplace_boards import api, env as env, hub_fixture as hub_fixture, service


def create(hub, cookie, **fields):
    status, card = api(
        hub, cookie, "POST", "/api/boards/work/cards", {"title": "Exact words", **fields}
    )
    assert status == 201, (status, card)
    return card


def configure(hub, cookie, agent, labels):
    status, result = api(
        hub,
        cookie,
        "POST",
        f"/api/task-types/connections/{agent.connection_id}",
        {"allowed_types": labels},
    )
    assert status == 200, (status, result)
    assert result == {"allowed_types": sorted(set(labels))}


def denied(call):
    try:
        call()
    except ToolError as exc:
        assert "task type" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("typed acquisition must be refused")


def test_defaults_migration_and_untyped_take(hub, env):
    _, agent, _, _, owner = env
    card = owner.task_add("Exact words", board="work")
    assert card.get("task_type") == ""
    before = line(card)
    BoardStore(owner.ctx.workspace_dir())
    current = owner.task_show(card["number"])
    assert current["body"] == card["body"] and current["number"] == card["number"]
    assert line(current) == before and "type:" not in before
    worker = service(hub, agent)
    assert worker.task_take_next(task=card["number"])["task_type"] == ""
    worker.task_update(card["number"], status="done")
    next_card = owner.task_add("Next", board="work")
    assert worker.task_take_next(board="work")["number"] == next_card["number"]


@pytest.mark.parametrize("value", [None, "", "code"])
@pytest.mark.parametrize("operation", ["create", "update"])
def test_role_owner_agent_cannot_supply_type(hub, env, value, operation):
    token, agent, _, _, _ = env
    assert agent.role == "owner" and not agent.person
    if operation == "create":
        status, result = api(
            hub, token, "POST", "/api/boards/work/cards", {"title": "Agent", "task_type": value}
        )
    else:
        card = service(hub, agent).task_add("Own card", board="work")
        status, result = api(
            hub,
            token,
            "PATCH",
            f"/api/cards/{card['number']}",
            {"task_type": value, "version": card["version"]},
        )
    assert status == 403 and "owner" in result.get("error", ""), (status, result)


@pytest.mark.parametrize(
    "value,valid",
    [
        ("a", True),
        ("a" * 48, True),
        ("a" * 49, False),
        ("Code", False),
        ("a_b", False),
        (None, False),
        ([], False),
        ("", True),
    ],
)
def test_owner_slug_validation(hub, env, value, valid):
    _, _, _, cookie, _ = env
    status, card = api(
        hub, cookie, "POST", "/api/boards/work/cards", {"title": "Typed", "task_type": value}
    )
    assert status == (201 if valid else 403), (status, card)
    if valid:
        assert card["task_type"] == value


def test_mapping_canonical_replace_and_clear(hub, env):
    _, agent, _, cookie, _ = env
    configure(hub, cookie, agent, ["review", "code", "code"])
    path = f"/api/task-types/connections/{agent.connection_id}"
    assert api(hub, cookie, "GET", path) == (200, {"allowed_types": ["code", "review"]})
    configure(hub, cookie, agent, [])
    assert api(hub, cookie, "GET", path) == (200, {"allowed_types": []})


@pytest.mark.parametrize(
    "labels", [None, "code", [None], ["Bad"], ["x" * 49], [f"t{i}" for i in range(129)]]
)
def test_mapping_invalid_is_atomic(hub, env, labels):
    _, agent, _, cookie, _ = env
    configure(hub, cookie, agent, ["code"])
    path = f"/api/task-types/connections/{agent.connection_id}"
    assert api(hub, cookie, "POST", path, {"allowed_types": labels})[0] == 400
    assert api(hub, cookie, "GET", path)[1] == {"allowed_types": ["code"]}


def test_mapping_maximum_and_owner_only_exact_id(hub, env):
    token, agent, _, cookie, _ = env
    configure(hub, cookie, agent, [f"t{i}" for i in range(128)] * 2)
    path = f"/api/task-types/connections/{agent.connection_id}"
    assert api(hub, token, "GET", path)[0] == 403
    assert api(hub, token, "POST", path, {"allowed_types": []})[0] == 403
    assert api(hub, cookie, "GET", "/api/task-types/connections/agent")[0] == 404
    assert api(hub, cookie, "GET", path + "/extra")[0] == 404
    hub.db.revoke(agent.connection_id, actor=hub.admin, workspace_ref=hub.workspace)
    assert api(hub, cookie, "GET", path)[0] == 404


@pytest.mark.parametrize("operation", ["explicit", "auto", "update", "person-assign"])
def test_all_doing_writers_check_actual_holder(hub, env, operation):
    _, agent, _, cookie, owner = env
    card = create(hub, cookie, task_type="code")
    worker = service(hub, agent)
    if operation == "auto":
        assert worker.task_take_next(board="work") is None
        configure(hub, cookie, agent, ["code"])
        assert worker.task_take_next(board="work")["number"] == card["number"]
        return
    if operation == "person-assign":
        denied(
            lambda: owner.task_add(
                "Assigned", board="work", status="doing", assignee=agent.actor, task_type="code"
            )
        )
        configure(hub, cookie, agent, ["code"])
        assert (
            owner.task_add(
                "Allowed", board="work", claim=True, assignee=agent.actor, task_type="code"
            )["assignee"]
            == agent.actor
        )
    else:
        acquire = (
            (lambda: worker.task_take_next(task=card["number"]))
            if operation == "explicit"
            else (lambda: worker.task_update(card["number"], status="doing"))
        )
        denied(acquire)
        configure(hub, cookie, agent, ["code"])
        assert acquire()["status"] == "doing"


def test_skip_wrong_first_and_holder_grace(hub, env):
    _, agent, _, cookie, owner = env
    first = create(hub, cookie, task_type="review")
    second = create(hub, cookie, task_type="code")
    configure(hub, cookie, agent, ["code"])
    worker = service(hub, agent)
    held = worker.task_take_next(board="work")
    assert held["number"] == second["number"]
    configure(hub, cookie, agent, [])
    changed = owner.task_update(held["number"], task_type="review")
    assert changed["assignee"] == held["assignee"] and changed["claim_until"] == held["claim_until"]
    assert worker.task_update(held["number"], status_line="Still working")["status"] == "doing"
    assert worker.task_update(held["number"], status="done")["status"] == "done"
    denied(lambda: worker.task_take_next(task=first["number"]))


def test_same_call_type_and_person_exemption(hub, env):
    _, agent, _, cookie, owner = env
    card = create(hub, cookie, task_type="code")
    # A person holds their own card without a connection grant.
    result = owner.task_update(card["number"], task_type="review", status="doing")
    assert result["task_type"] == "review" and result["assignee"] == owner.principal.actor
    configure(hub, cookie, agent, ["code"])
    plain = owner.task_add("Untyped", board="work")
    assert owner.task_update(plain["number"], task_type="code")["task_type"] == "code"
    assert owner.task_update(plain["number"], task_type="")["task_type"] == ""


def test_explicit_denial_rolls_back_expiry(hub, env):
    _, agent, _, cookie, owner = env
    card = create(hub, cookie, task_type="code")
    expired = owner.task_add("Expired", board="work", status="doing", assignee=agent.actor)
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (expired["number"],)
        )
    with owner.store.connection() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
        events = [tuple(r) for r in conn.execute("SELECT * FROM task_events ORDER BY id")]
    denied(lambda: service(hub, agent).task_take_next(task=card["number"]))
    with owner.store.connection() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")] == before
        assert [tuple(r) for r in conn.execute("SELECT * FROM task_events ORDER BY id")] == events


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_configuration_member_foreign_and_dead(hub, env, method):
    from commonplace import sessions
    from tests.runner_harness import http

    _, agent, _, cookie, _ = env
    user = hub.db.create_user("member-type@example.com")
    hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
    login = sessions.redeem_login_link(
        hub.db, sessions.create_login_link(hub.db, "member-type@example.com")
    )
    member_cookie = {"Cookie": f"{sessions.COOKIE}={login}"}
    path = f"/api/task-types/connections/{agent.connection_id}"
    assert (
        api(hub, member_cookie, method, path, {"allowed_types": []} if method == "POST" else None)[
            0
        ]
        == 403
    )
    foreign = hub.db.create_workspace("foreign-types", hub.admin)
    token = hub.token(
        "foreign-type",
        workspace=foreign,
        profile="full",
        trust="trusted",
        read_wings=["*"],
        write_wings=["*"],
    )
    foreign_agent = hub.gateway.authenticate(f"Bearer {token}")
    assert (
        api(
            hub,
            cookie,
            method,
            f"/api/task-types/connections/{foreign_agent.connection_id}",
            {"allowed_types": []} if method == "POST" else None,
        )[0]
        == 404
    )
    assert (
        http(
            hub.port,
            "POST",
            path,
            cookie=cookie["Cookie"],
            origin="https://wrong.example",
            body={"allowed_types": []},
        )[0]
        == 403
    )
    with hub.db._lock:
        hub.db._conn.execute(
            "UPDATE connections SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (agent.connection_id,),
        )
    assert (
        api(hub, cookie, method, path, {"allowed_types": []} if method == "POST" else None)[0]
        == 404
    )


def test_get_does_not_create_database(hub, env):
    from pathlib import Path

    _, _, _, cookie, _ = env
    # A second workspace is deliberately left without a boards store.
    workspace = hub.db.create_workspace("no-type-store", hub.admin)
    token = hub.token(
        "no-store-agent",
        workspace=workspace,
        profile="full",
        trust="trusted",
        read_wings=["*"],
        write_wings=["*"],
    )
    agent = hub.gateway.authenticate(f"Bearer {token}")
    from commonplace import sessions
    from commonplace.web import task_types

    person = sessions.person_principal(hub.db, hub.admin, workspace)

    class Handler:
        command = "GET"
        path = f"/api/task-types/connections/{agent.connection_id}"

    from unittest.mock import patch

    with (
        patch.object(task_types, "require_principal", return_value=person),
        patch.object(task_types, "send_json") as send,
    ):
        routes = {}
        from types import SimpleNamespace

        gateway = SimpleNamespace(
            db=hub.db,
            workspace_dir=hub.gateway.workspace_dir,
            add_prefix_route=lambda method, path, handler: routes.update({method: handler}),
        )
        task_types.install(gateway)
        routes["GET"](Handler())
        assert send.call_count == 1
        assert send.call_args.args[1:] == (200, {"allowed_types": []})
    assert not (Path(hub.gateway.workspace_dir(workspace)) / "boards.sqlite3").exists()


def test_rotation_new_id_and_reservation(hub, env):
    _, agent, _, cookie, owner = env
    configure(hub, cookie, agent, ["code"])
    card = create(hub, cookie, task_type="code", assignee=agent.actor)
    assert card["status"] == "doing"
    card = owner.task_handoff(card["number"], agent.actor, "Return to the worker's queue")
    assert card["status"] == "todo" and card["claim_until"] is None
    hub.db.rotate_token(agent.connection_id, actor=hub.admin, workspace_ref=hub.workspace)
    assert service(hub, agent).task_take_next(task=card["number"])["status"] == "doing"
    fresh = hub.gateway.authenticate(f"Bearer {hub.owner_token('new-agent')}")
    other = create(hub, cookie, task_type="code")
    denied(lambda: service(hub, fresh).task_take_next(task=other["number"]))
    hub.db.revoke(agent.connection_id, actor=hub.admin, workspace_ref=hub.workspace)
    assert hub.db.authenticate_connection(agent.connection_id) is None


def test_type_edit_version_and_member_person(hub, env):
    from commonplace import sessions
    from commonplace.boards.service import VersionClash

    _, _, _, cookie, owner = env
    card = create(hub, cookie, task_type="code")
    with pytest.raises(VersionClash):
        owner.task_update(card["number"], task_type="review", version=card["version"] + 1)
    user = hub.db.create_user("person-types@example.com")
    hub.db.add_member(hub.workspace, user, "member", actor=hub.admin)
    member = service(hub, sessions.person_principal(hub.db, user, hub.workspace))
    with pytest.raises(ToolError, match="owner"):
        member.task_update(card["number"], task_type="review")
    assert member.task_take_next(task=card["number"])["assignee"] == member.principal.actor


def test_same_call_clear_new_acquisition_and_seed(hub, env):
    _, _, _, cookie, owner = env
    card = create(hub, cookie, task_type="code")
    assert owner.task_update(card["number"], status="doing", task_type="")["task_type"] == ""
    assert (
        owner.seed_card(board="work", title="Imported typed", status="doing", task_type="code")[
            "task_type"
        ]
        == "code"
    )


def test_mcp_presence_and_typed_rendering(hub, env):
    from tests.test_commonplace_boards import mcp

    token, _, _, cookie, owner = env
    card = create(hub, cookie, task_type="code")
    assert "type:code" in line(card)
    for value in (None, "", "code"):
        ok, text = mcp(hub, token, "task_add", title="Agent type", board="work", task_type=value)
        assert not ok and "owner" in text, text
    ok, text = mcp(hub, token, "task_show", task=card["number"])
    assert ok and "type:code" in text
    assert owner.task_add("Without field", board="work")["task_type"] == ""


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.parametrize("entry", ["status", "claim", "self-todo"])
def test_typed_creation_final_holder_on_off(hub, env, on, entry):
    from commonplace.projects import orchestration

    _, agent, person, cookie, owner = env
    orchestration.set_enabled(hub.db, person, on)
    fields = {"assignee": agent.actor}
    if entry == "status":
        fields["status"] = "doing"
    elif entry == "claim":
        fields["claim"] = True
    # Default todo assignments claim immediately, just like explicit doing/claim.
    denied(lambda: owner.task_add("Denied", board="work", task_type="code", **fields))
    configure(hub, cookie, agent, ["code"])
    held = owner.task_add("Allowed", board="work", task_type="code", **fields)
    assert held["status"] == "doing" and held["assignee"] == agent.actor


@pytest.mark.parametrize("order", ["claim-first", "remove-first", "retype-first"])
def test_replacement_order_and_holder_renewal(hub, env, order):
    _, agent, _, cookie, owner = env
    card = create(hub, cookie, task_type="code")
    configure(hub, cookie, agent, ["code"])
    worker = service(hub, agent)
    if order == "claim-first":
        held = worker.task_take_next(task=card["number"])
        configure(hub, cookie, agent, [])
        owner.task_update(card["number"], task_type="review")
        assert worker.task_heartbeat(card["number"])["assignee"] == held["assignee"]
        assert worker.task_comment(card["number"], text="Continuing")
        assert (
            worker.task_update(card["number"], body="Exact edited words")["body"]
            == "Exact edited words"
        )
    else:
        if order == "remove-first":
            configure(hub, cookie, agent, [])
        else:
            owner.task_update(card["number"], task_type="review")
        denied(lambda: worker.task_take_next(task=card["number"]))


@pytest.mark.parametrize("inactive", ["disabled", "membership"])
def test_configuration_connection_liveness(hub, env, inactive):
    _, agent, _, cookie, _ = env
    with hub.db._lock:
        if inactive == "disabled":
            hub.db._conn.execute(
                "UPDATE users SET disabled_at='2026-01-01T00:00:00Z' WHERE id=?", (agent.user_id,)
            )
        else:
            # Corruption fixture: normal FK writes cannot orphan a live connection.
            hub.db._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                hub.db._conn.execute(
                    "DELETE FROM memberships WHERE workspace_id=? AND user_id=?",
                    (hub.workspace, agent.user_id),
                )
            finally:
                hub.db._conn.execute("PRAGMA foreign_keys=ON")
    # Exercise the handler using an independent live owner principal so the target's
    # disabled membership cannot turn the request into an authentication failure.
    from types import SimpleNamespace
    from unittest.mock import patch

    from commonplace.web import task_types

    _, _, person, _, _ = env
    routes = {}
    gateway = SimpleNamespace(
        db=hub.db,
        workspace_dir=hub.gateway.workspace_dir,
        add_prefix_route=lambda method, path, handler: routes.update({method: handler}),
    )
    task_types.install(gateway)
    h = SimpleNamespace(command="GET", path=f"/api/task-types/connections/{agent.connection_id}")
    with (
        patch.object(task_types, "require_principal", return_value=person),
        patch.object(task_types, "send_json") as send,
    ):
        routes["GET"](h)
        assert send.call_args.args[1:] == (404, {"error": "Connection not found"})


def test_configuration_json_and_storage_guards(hub, env, monkeypatch):
    import http.client

    from commonplace import plans
    from tests.test_commonplace_plans import fill

    _, agent, _, cookie, _ = env
    path = f"/api/task-types/connections/{agent.connection_id}"
    conn = http.client.HTTPConnection("127.0.0.1", hub.port, timeout=10)
    try:
        conn.request("POST", path, "{", {**cookie, "Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 400
        response.read()
    finally:
        conn.close()
    assert (
        api(hub, cookie, "POST", path, {"allowed_types": [], "workspace": hub.workspace})[0] == 400
    )
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    plans.set_plan(hub.db, hub.admin, "free", actor=hub.admin)
    fill(hub)
    assert api(hub, cookie, "POST", path, {"allowed_types": []})[0] == 507


def test_legacy_schema_migration_preserves_hold(hub, env):
    _, agent, _, _, owner = env
    held = owner.task_add(
        "Verbatim legacy", board="work", body="  exact\nwords", status="doing", assignee=agent.actor
    )
    with owner.store.transaction() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "task_type" in columns:
            conn.execute("ALTER TABLE tasks DROP COLUMN task_type")
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (held["number"],)).fetchone())
    for _ in range(2):
        BoardStore(owner.ctx.workspace_dir())
    with owner.store.connection() as conn:
        after = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (held["number"],)).fetchone())
        assert after.get("task_type") == "", (
            "additive migration must restore the empty type default"
        )
        assert {key: after[key] for key in before} == before


@pytest.mark.parametrize("body", ["null", "42", "[]", '["allowed_types"]'])
def test_configuration_rejects_non_object_json(hub, env, body):
    import http.client

    _, agent, _, cookie, _ = env
    configure(hub, cookie, agent, ["code"])
    path = f"/api/task-types/connections/{agent.connection_id}"
    conn = http.client.HTTPConnection("localhost", hub.port, timeout=10)
    try:
        conn.request("POST", path, body, {**cookie, "Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 400
        response.read()
    finally:
        conn.close()
    assert api(hub, cookie, "GET", path) == (200, {"allowed_types": ["code"]})
