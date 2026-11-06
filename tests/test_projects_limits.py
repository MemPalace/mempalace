"""Project concurrency limits and atomic admission of synthetic agents."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext, ToolError
from tests._projects_helpers import projects_hub_fixture  # noqa: F401


def service(h, token):
    principal = h.gateway.authenticate(f"Bearer {token}")
    return BoardService(ToolContext(principal=principal, gateway=h.gateway))


def setup_agents(h):
    assert h.create_board(h.owner, "alpha")[0] == 201
    first = service(h, h.a1)
    second = service(h, h.hub.owner_token("a2"))
    cards = []
    for title in ("First task", "Second task", "Third task"):
        status, body, _ = h.request(h.owner, "POST", "/api/boards/alpha/cards", {"title": title})
        assert status == 201
        cards.append(body["number"])
    return first, second, cards


def seed_cap(agent, cap):
    # Seed the persisted setting independently so admission REDs reach both callers
    # even on a version without the HTTP configuration route.
    with agent.store.transaction() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS project_limits ("
            "board_id INTEGER PRIMARY KEY REFERENCES boards(id), max_agents INTEGER)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO project_limits VALUES "
            "((SELECT id FROM boards WHERE slug='alpha'), ?)",
            (cap,),
        )


def test_r01_owner_limits_roundtrip(projects_hub):
    h = projects_hub
    assert h.create_board(h.owner, "alpha")[0] == 201
    status, body, _ = h.request(h.owner, "PUT", "/api/projects/alpha/limits", {"max_agents": 1})
    assert status == 200  # RED
    assert "limits" in body
    assert body["limits"] == {"max_agents": 1}
    assert h.project(h.owner, "alpha")[1]["limits"] == {"max_agents": 1}
    assert "memory" in body
    assert body["memory"]["wing"] == "alpha"
    for value in (20, None):
        assert (
            h.request(h.owner, "PUT", "/api/projects/alpha/limits", {"max_agents": value})[0] == 200
        )
        assert h.project(h.owner, "alpha")[1]["limits"] == {"max_agents": value}


def test_r02_default_limits(projects_hub):
    h = projects_hub
    status, body = h.project(h.owner, "work")
    assert status == 200
    assert "limits" in body  # RED
    assert body["limits"] == {"max_agents": None}


def test_r03_agents_working_distinct(projects_hub):
    h = projects_hub
    first, second, cards = setup_agents(h)
    first.task_take_next(board="alpha", task=cards[0])
    first.task_take_next(board="alpha", task=cards[1])
    second.task_take_next(board="alpha", task=cards[2])
    status, resp, _ = h.get(h.owner, "/api/boards")
    assert status == 200
    assert "boards" in resp
    assert resp["boards"]
    assert "agents_working" in resp["boards"][0]  # RED
    assert next(b for b in resp["boards"] if b["slug"] == "alpha")["agents_working"] == 2
    second.task_update(cards[2], status="backlog")
    assert h.board("alpha")["agents_working"] == 1


@pytest.mark.parametrize("operation", ["take", "update"])
def test_r04_second_agent_refused(projects_hub, operation):
    first, second, cards = setup_agents(projects_hub)
    seed_cap(first, 1)
    first.task_take_next(board="alpha", task=cards[0])
    try:
        if operation == "take":
            second.task_take_next(board="alpha", task=cards[1])
        else:
            second.task_update(cards[1], status="doing")
    except ToolError as exc:
        refusal = str(exc)
    else:
        refusal = None
    assert refusal is not None  # RED
    assert refusal == (
        "no: alpha already has its maximum of 1 agents working; the card waits in Todo"
    )
    assert second.task_show(cards[1])["status"] == "todo"
    assert first.task_take_next(board="alpha", task=cards[2])["status"] == "doing"


@pytest.mark.parametrize("operation", ["take", "update"])
def test_r05_two_callers_one_slot(projects_hub, operation):
    first, second, cards = setup_agents(projects_hub)
    seed_cap(first, 1)
    start = Barrier(2)

    def attempt(agent, number):
        start.wait(timeout=10)
        try:
            if operation == "take":
                agent.task_take_next(board="alpha", task=number)
            else:
                agent.task_update(number, status="doing")
            return True
        except ToolError as exc:
            assert "maximum of 1 agents working" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(attempt, agent, number) for agent, number in zip((first, second), cards)
        ]
        results = [future.result(timeout=20) for future in futures]
    assert sum(results) == 1  # RED
    assert sorted(first.task_show(n)["status"] for n in cards[:2]) == ["doing", "todo"]


def test_g01_unset_allows_both_agents(projects_hub):
    first, second, cards = setup_agents(projects_hub)
    assert first.task_take_next(board="alpha", task=cards[0])["status"] == "doing"
    assert second.task_update(cards[1], status="doing")["status"] == "doing"


def test_r06_only_owner_configures(projects_hub):
    h = projects_hub
    for auth in (h.member, h.a1):
        assert h.request(auth, "PUT", "/api/projects/work/limits", {"max_agents": 1})[0] == 403


def test_r07_invalid_limits_leave_setting_untouched(projects_hub):
    h = projects_hub
    # The existence assertion keeps configuration guards valid on the base.
    status, _, _ = h.request(h.owner, "PUT", "/api/projects/work/limits", {"max_agents": 2})
    assert status == 200  # RED
    for body in (
        {},
        {"max_agents": True},
        {"max_agents": 0},
        {"max_agents": 21},
        {"max_agents": 1.5},
        {"max_agents": "2"},
        {"max_agents": 2, "extra": 1},
        [],
    ):
        assert h.request(h.owner, "PUT", "/api/projects/work/limits", body)[0] == 400
        assert h.project(h.owner, "work")[1]["limits"] == {"max_agents": 2}
    assert h.request(h.owner, "PUT", "/api/projects/missing/limits", {"max_agents": 1})[0] == 404


@pytest.mark.parametrize(
    "creation", [{"claim": True}, {"status": "doing"}, {"status": "todo", "assignee": "self"}]
)
def test_r08_creation_into_doing_refused(projects_hub, creation):
    first, second, cards = setup_agents(projects_hub)
    seed_cap(first, 1)
    first.task_take_next(board="alpha", task=cards[0])
    with first.store.connection() as conn:
        before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    try:
        second.task_add(title="Another task", board="alpha", **creation)
    except ToolError as exc:
        refusal = str(exc)
    else:
        refusal = None
    with first.store.connection() as conn:
        after = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    assert (refusal, after) == (
        "no: alpha already has its maximum of 1 agents working; the card waits in Todo",
        before,
    )  # RED


@pytest.mark.parametrize("status", ["backlog", "todo"])
def test_g02_nonworking_creation_allowed_at_cap(projects_hub, status):
    first, second, cards = setup_agents(projects_hub)
    seed_cap(first, 1)
    first.task_take_next(board="alpha", task=cards[0])
    created = second.task_add(title="Waiting task", board="alpha", status=status)
    assert created["status"] == status
    assert second.task_show(created["number"])["status"] == status


@pytest.mark.parametrize("creator", ["parent", "owner"])
def test_r09_creation_for_child_refused(projects_hub, creator):
    from commonplace import sessions

    h = projects_hub
    parent, _, cards = setup_agents(h)
    seed_cap(parent, 1)
    parent.task_take_next(board="alpha", task=cards[0])
    child_id, _ = h.db.create_token(
        workspace_ref=h.workspace,
        user_ref=h.admin,
        actor=h.admin,
        name="child-worker",
        client="test",
        parent=parent.principal.connection_id,
        policy=parent.principal.policy,
    )
    caller = parent
    if creator == "owner":
        principal = sessions.person_principal(h.db, h.admin, h.workspace)
        caller = BoardService(ToolContext(principal=principal, gateway=h.gateway))
    with parent.store.connection() as conn:
        before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    try:
        caller.task_add(
            title="Child task", board="alpha", status="doing", assignee=f"conn:{child_id}"
        )
    except ToolError as exc:
        refusal = str(exc)
    else:
        refusal = None
    with parent.store.connection() as conn:
        after = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    assert (refusal, after) == (
        "no: alpha already has its maximum of 1 agents working; the card waits in Todo",
        before,
    )  # RED


def test_g03_working_parent_can_create_claim(projects_hub):
    parent, _, cards = setup_agents(projects_hub)
    seed_cap(parent, 1)
    parent.task_take_next(board="alpha", task=cards[0])
    created = parent.task_add(title="Another parent task", board="alpha", claim=True)
    assert created["status"] == "doing"
    assert created["assignee"] == parent.principal.actor
