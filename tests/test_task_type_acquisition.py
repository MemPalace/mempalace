"""Legacy typed rows exercise acquisition boundaries on both base and head.

SQL creates legacy fixtures only: base has no public type-writing API. All
acquisition, update and replacement actions under test use production methods.
"""

import pytest

from commonplace.extensions import ToolError
from commonplace.runner import predicate
from tests.test_commonplace_boards import env as env, hub_fixture as hub_fixture, service


@pytest.fixture
def legacy(hub, env):
    _, agent, person, cookie, owner = env
    cards = [owner.task_add(title, board="work") for title in ("Wrong first", "Code", "Plain")]
    with owner.store.transaction() as conn:
        if "task_type" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
            conn.execute("ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS connection_task_types (connection_id TEXT NOT NULL, task_type TEXT NOT NULL, PRIMARY KEY(connection_id,task_type))"
        )
        for card, label in zip(cards, ["review", "code", ""]):
            conn.execute("UPDATE tasks SET task_type=? WHERE id=?", (label, card["number"]))
    return agent, person, owner, cards


def refuse(call):
    try:
        call()
    except ToolError as exc:
        assert "task type" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("task type guard did not refuse acquisition")


@pytest.mark.parametrize("operation", ["explicit", "update", "auto"])
def test_legacy_unmapped_acquisition(hub, legacy, operation):
    agent, _, _, cards = legacy
    worker = service(hub, agent)
    if operation == "auto":
        result = worker.task_take_next(board="work")
        assert result["number"] == cards[2]["number"], "auto must skip typed cards without grants"
    elif operation == "explicit":
        refuse(lambda: worker.task_take_next(task=cards[1]["number"]))
    else:
        refuse(lambda: worker.task_update(cards[1]["number"], status="doing"))


def test_legacy_wrong_first_skip(hub, legacy):
    agent, _, owner, cards = legacy
    with owner.store.transaction() as conn:
        conn.execute("INSERT INTO connection_task_types VALUES (?, 'code')", (agent.connection_id,))
    result = service(hub, agent).task_take_next(board="work")
    assert result["number"] == cards[1]["number"], "only exact allowed label is eligible"


@pytest.mark.parametrize("corruption", ["type", "mapping", "missing-table"])
def test_legacy_corruption_refuses(hub, legacy, corruption):
    agent, _, owner, cards = legacy
    with owner.store.transaction() as conn:
        if corruption == "type":
            conn.execute("UPDATE tasks SET task_type='Invalid' WHERE id=?", (cards[1]["number"],))
        elif corruption == "mapping":
            conn.execute(
                "INSERT INTO connection_task_types VALUES (?, 'Invalid')", (agent.connection_id,)
            )
        else:
            conn.execute("DROP TABLE connection_task_types")
    # Reuse already-open service: initialization must not repair the corruption fixture.
    worker = service(hub, agent)
    if corruption == "missing-table":
        with owner.store.transaction() as conn:
            conn.execute("DROP TABLE IF EXISTS connection_task_types")
    refuse(lambda: worker.task_take_next(task=cards[1]["number"]))


@pytest.mark.parametrize("allowed", [False, True])
def test_legacy_runner_exact_reason(hub, legacy, allowed):
    agent, _, owner, cards = legacy
    if allowed:
        with owner.store.transaction() as conn:
            conn.execute(
                "INSERT INTO connection_task_types VALUES (?, 'code')", (agent.connection_id,)
            )
    with owner.store.connection() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (cards[1]["number"],)).fetchone()
        board = conn.execute("SELECT * FROM boards WHERE slug='work'").fetchone()
    # Runner worker's single write wing is part of existing admission authority.
    token = hub.token(
        "runner-worker",
        profile="full",
        scopes=["memory:read", "memory:write"],
        read_wings=["shared"],
        write_wing="shared",
        trust="trusted",
    )
    worker = hub.gateway.authenticate(f"Bearer {token}")
    if allowed:
        with owner.store.transaction() as conn:
            conn.execute(
                "INSERT INTO connection_task_types VALUES (?, 'code')", (worker.connection_id,)
            )
    machine = {"workspace_id": hub.workspace, "worker_connection_id": worker.connection_id}
    if allowed:
        predicate.startable(hub.gateway, machine, row, board)
    else:
        try:
            predicate.startable(hub.gateway, machine, row, board)
        except predicate.Refused as exc:
            assert exc.reason == "task_type_not_allowed"
        else:
            raise AssertionError("runner must refuse unmapped typed work")


def test_legacy_person_and_untyped_unchanged(hub, legacy):
    agent, person, owner, cards = legacy
    assert owner.task_take_next(task=cards[0]["number"])["assignee"] == person.actor
    assert (
        service(hub, agent).task_take_next(task=cards[2]["number"])["number"] == cards[2]["number"]
    )


def test_legacy_rollback_expiry(hub, legacy):
    agent, _, owner, cards = legacy
    held = owner.task_add("Expired other card", board="work", status="doing", assignee=agent.actor)
    with owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (held["number"],)
        )
    with owner.store.connection() as conn:
        before = [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
        events = [tuple(r) for r in conn.execute("SELECT * FROM task_events ORDER BY id")]
    refuse(lambda: service(hub, agent).task_take_next(task=cards[1]["number"]))
    with owner.store.connection() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")] == before
        assert [tuple(r) for r in conn.execute("SELECT * FROM task_events ORDER BY id")] == events


@pytest.mark.parametrize("holder", ["ungranted-agent", "granted-agent", "person"])
def test_owner_update_checks_resulting_holder(hub, env, holder):
    _, agent, person, cookie, owner = env
    from tests.test_commonplace_task_types import configure

    assignee = person.actor if holder == "person" else agent.actor
    card = owner.task_add("Reserved typed work", board="work", task_type="code")
    # Legacy todo reservations predate immediate owner-assignment claims.
    with owner.store.transaction() as conn:
        conn.execute("UPDATE tasks SET assignee=? WHERE id=?", (assignee, card["number"]))
    if holder == "granted-agent":
        configure(hub, cookie, agent, ["code"])
    if holder == "ungranted-agent":
        refuse(lambda: owner.task_update(card["number"], status="doing"))
        current = owner.task_show(card["number"])
        assert current["status"] == "todo" and current["assignee"] == agent.actor
    else:
        result = owner.task_update(card["number"], status="doing")
        assert result["status"] == "doing" and result["assignee"] == assignee
        assert (result["claim_until"] is None) == (holder == "person")


@pytest.mark.parametrize("on", [False, True])
def test_owner_handoff_checks_typed_assignment_grant(hub, env, on):
    from commonplace.projects import orchestration
    from tests.test_commonplace_task_types import configure

    _, agent, person, cookie, owner = env
    orchestration.set_enabled(hub.db, person, on)
    card = owner.task_add("Typed assignment", board="work", task_type="code")
    before = owner.task_show(card["number"])
    refuse(lambda: owner.task_handoff(card["number"], agent.actor, "Please do this"))
    assert owner.task_show(card["number"]) == before
    configure(hub, cookie, agent, ["code"])
    held = owner.task_handoff(card["number"], agent.actor, "Please do this")
    assert held["status"] == "doing" and held["assignee"] == agent.actor
    assert held["claim_until"] and held["status_line"].startswith("Start blocked:")
    assert any(event["kind"] == "claim" for event in owner.task_show(card["number"])["events"])


def test_existing_typed_holder_take_preserves_claim_after_revocation(hub, env):
    from tests.test_commonplace_task_types import configure

    _, agent, _, cookie, owner = env
    card = owner.task_add("Keep working", board="work", task_type="code")
    configure(hub, cookie, agent, ["code"])
    worker = service(hub, agent)
    held = worker.task_take_next(task=card["number"])
    configure(hub, cookie, agent, [])
    try:
        repeated = worker.task_take_next(task=card["number"])
    except ToolError:
        raise AssertionError("existing holders must keep their claim after grant removal") from None
    assert repeated == held


def test_automatic_take_combines_type_and_blocker_filters(hub, env):
    from commonplace.projects import orchestration
    from tests.test_commonplace_task_types import configure

    _, agent, person, cookie, owner = env
    orchestration.set_enabled(hub.db, person, True)
    dependency = owner.task_add("Dependency", board="work", status="backlog")
    owner.task_add("Wrong type", board="work", task_type="review")
    blocked = owner.task_add(
        "Blocked code", board="work", task_type="code", blocked_by=[dependency["number"]]
    )
    ready = owner.task_add("Ready code", board="work", task_type="code")
    configure(hub, cookie, agent, ["code"])
    worker = service(hub, agent)
    assert worker.task_take_next(board="work")["number"] == ready["number"]
    worker.task_update(ready["number"], status="done")
    assert worker.task_take_next(board="work") is None
    owner.task_update(blocked["number"], blocked_by=[])
    assert worker.task_take_next(board="work")["number"] == blocked["number"]


@pytest.mark.parametrize("route", ["create", "handoff"])
@pytest.mark.parametrize("reset_binding", [False, True])
def test_typed_assignment_configured_start_keeps_binding_fence(hub, env, route, reset_binding):
    from commonplace.runner import registry
    from tests._phase_b_helpers import World, cap, seed
    from tests.test_commonplace_task_types import configure

    _, _, _, cookie, _ = env
    w = World(hub)
    seed(w)
    cap(w, 1)
    w.configure(max_agents=1)
    worker = w.db.authenticate_connection(w.machine()["worker_connection_id"])
    configure(hub, cookie, worker, ["code"])
    if route == "create":
        assigned = w.boards.task_add(
            "Typed configured work", board="acme", assignee=worker.actor, task_type="code"
        )
    else:
        card = w.boards.task_add("Typed configured work", board="acme", task_type="code")
        assigned = w.boards.task_handoff(card["number"], worker.actor, "Please do this")
    assert assigned["status"] == "doing" and assigned["claim_until"]
    assert assigned["status_line"].startswith("Start queued:")
    (job,) = w.all_jobs()
    assert job["state"] == "queued" and job["config_version"] is not None
    assert job["binding_epoch"] == w.machine()["binding_epoch"]
    leased = w.lease()
    if reset_binding:
        registry.patch(w.db, w.owner, w.machine_id, {"topics": ["other"]})
        assert w.machine_call("starting", {"lease_token": leased["lease_token"]})[0] == 409
        (stored,) = w.jobs_for(assigned["number"])
        assert stored["state"] == "cancelled" and stored["starting_at"] is None
    else:
        assert w.machine_call("starting", {"lease_token": leased["lease_token"]})[0] == 200
        assert (
            service(hub, worker).task_take_next(task=assigned["number"])["assignee"] == worker.actor
        )
