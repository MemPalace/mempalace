"""Owner assignment claims work and either starts its worker or explains why not."""

import pytest

from commonplace.boards.service import BoardService
from commonplace.extensions import ToolError
from commonplace.projects import orchestration
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._phase_b_helpers import World, cap, seed
from tests.test_commonplace_board_authority import fresh as fresh


def test_r5_manual_assignment_is_claimed_with_visible_start_reason(fresh):
    orch = fresh.connect("orchestrator", trust="standard")
    fresh.owner.board_set("work", orch.principal.actor)
    assignee = fresh.connect("assignee")
    card = fresh.owner.task_add(board="work", title="Do this")
    moved = fresh.owner.task_handoff(card["number"], assignee.principal.actor, "Please do this")
    assert (moved["status"], moved["assignee"]) == ("doing", assignee.principal.actor)
    assert moved["claim_until"]
    assert "orchestration_disabled" in moved["status_line"]
    assert fresh.owner.board_list()[0]["agents_working"] == 1
    assert orch.work.task_take_next(board="work") is None
    assert assignee.work.task_take_next(task=card["number"])["status"] == "doing"


@pytest.mark.parametrize("route", ["create", "handoff"])
def test_assignment_starts_configured_assignee_through_orchestrator(hub, route):
    w = World(hub)
    seed(w)
    cap(w, 1)
    w.configure(max_agents=1)
    worker = w.db.authenticate_connection(w.machine()["worker_connection_id"])
    if route == "create":
        assigned = w.boards.task_add("Assigned work", board="acme", assignee=worker.actor)
    else:
        assigned = w.boards.task_handoff(w.card(), worker.actor, "Please do this")
    assert assigned["status"] == "doing"
    rows = w.all_jobs()
    assert len(rows) == 1 and rows[0]["state"] == "queued"
    assert rows[0]["config_version"] is not None
    assert rows[0]["requested_by"] == "user:" + w.owner.user_id
    assert assigned["status_line"].startswith("Start queued:")
    payload = w.lease()
    assert payload["task_number"] == assigned["number"]
    assert w.machine_call("starting", {"lease_token": payload["lease_token"]})[0] == 200
    held = BoardService(w.ctx(worker)).task_take_next(task=assigned["number"])
    assert held["status"] == "doing" and held["assignee"] == worker.actor


@pytest.mark.parametrize("on", [False, True])
def test_assignment_obeys_project_limit_without_reserving_card(hub, on):
    w = World(hub)
    seed(w, on=on)
    cap(w, 1)
    BoardService(w.ctx(w.a)).task_take_next(task=w.card())
    card = w.card()
    with pytest.raises(ToolError, match="project_limit|maximum"):
        w.boards.task_handoff(card, w.b.actor, "Please do this")
    shown = w.boards.task_show(card)
    assert shown["status"] == "todo" and shown["assignee"] is None
    assert not w.all_jobs()


@pytest.mark.parametrize("route", ["create", "handoff"])
def test_assignment_refuses_busy_agent(hub, route):
    w = World(hub)
    seed(w)
    BoardService(w.ctx(w.a)).task_take_next(task=w.card())
    with pytest.raises(orchestration.AdmissionRefused, match="agent_busy"):
        if route == "create":
            w.boards.task_add("Busy assignment", board="acme", assignee=w.a.actor)
        else:
            w.boards.task_handoff(w.card(), w.a.actor, "Please do this")


@pytest.mark.parametrize(
    "configured,on,reason",
    [
        (True, False, "orchestration_disabled"),
        (False, True, "not_configured"),
        (True, True, "assignee_not_runner_worker"),
    ],
)
def test_unavailable_start_reason_is_persisted(hub, configured, on, reason):
    w = World(hub)
    seed(w, on=on)
    if configured:
        w.configure()
    assigned = w.boards.task_add("Assigned work", board="acme", assignee=w.a.actor)
    assert assigned["status"] == "doing"
    assert reason in assigned["status_line"]
    assert reason in w.boards.task_show(assigned["number"])["status_line"]
    assert not w.all_jobs()


def test_assignment_does_not_release_restricted_origin(fresh):
    coordinator = fresh.connect("coordinator")
    fresh.owner.board_set("work", coordinator.principal.actor)
    restricted = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    assignee = fresh.connect("assignee")
    card = restricted.work.task_add(board="work", title="Restricted submission")
    with pytest.raises(ToolError):
        fresh.owner.task_handoff(card["number"], assignee.principal.actor, "Please do this")
    shown = fresh.owner.task_show(card["number"])
    assert shown["status"] == "backlog" and shown["approved_by"] is None


def test_assignment_counts_queued_starts_at_admission(hub):
    w = World(hub)
    seed(w)
    cap(w, 1)
    w.person_start(w.card())
    with pytest.raises(orchestration.AdmissionRefused, match="project_limit"):
        w.boards.task_handoff(w.card(), w.a.actor, "Please do this")
    assert len(w.all_jobs()) == 1


def test_reassign_claimed_card_returns_to_todo_without_start(hub):
    w = World(hub)
    seed(w)
    w.configure()
    worker = w.db.authenticate_connection(w.machine()["worker_connection_id"])
    card = w.card()
    BoardService(w.ctx(w.a)).task_take_next(task=card)
    moved = w.boards.task_handoff(card, worker.actor, "Retry with another worker")
    assert (moved["status"], moved["assignee"]) == ("todo", worker.actor)
    assert moved["claim_until"] is None
    assert not w.all_jobs()
    claimed = BoardService(w.ctx(worker)).task_take_next(task=card)
    assert claimed["status"] == "doing"
