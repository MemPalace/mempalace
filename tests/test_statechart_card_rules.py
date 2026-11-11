"""One proof per card rule from the card-lifecycle statechart replay (#233).

The replay found these rules broken on an older develop. Each test walks the
shortest breaking path from that replay and asserts the rule now holds.
"""

from commonplace import events, webhooks
from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext
from commonplace.runner import jobs, predicate, registry
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    REVIEW_QUESTION,
    hub_fixture,  # noqa: F401
)
from tests._projects_helpers import projects_hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api
from tests.test_projects_phase_b import trust, trust_setup
from tests.test_runner_jobs import started_capacity as started_capacity


def seat(fresh):
    orch = fresh.connect("orchestrator:work", trust="standard")
    fresh.owner.board_set("work", orch.principal.actor)
    return orch


def owner_queue(fresh):
    groups = fresh.owner.needs_me()
    return {c["number"] for v in groups.values() if isinstance(v, list) for c in v}


# R1: a card from a restricted agent only becomes active after the orchestrator releases it.
def test_r1_restricted_origin_waits_for_orchestrator_release(fresh):
    orch = seat(fresh)
    restricted = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    card = restricted.work.task_add(board="work", title="Restricted idea")
    assert card["status"] == "backlog" and card["approved_by"] is None
    assert restricted.work.task_take_next(board="work") is None
    released = orch.work.task_approve(card["number"], approve=True, reason="Planned work")
    assert released["status"] == "todo"
    assert released["approved_by"] == orch.principal.actor
    assert restricted.work.task_take_next(task=card["number"])["status"] == "doing"


# R1 (second path): the owner does not release a subagent proposal past a live orchestrator.
def test_r1_owner_does_not_release_past_live_orchestrator(hub, fresh):
    orch = seat(fresh)
    sub = fresh.connect("sub", parent=orch.id)
    card = sub.work.task_add(board="work", title="Sub follow-up")
    assert card["status"] == "backlog" and card["proposal_status"] == "pending"
    status, reply = api(hub, fresh.cookie, "POST", f"/api/cards/{card['number']}/approve", {})
    assert status == 403, reply
    shown = fresh.owner.task_show(card["number"])
    assert shown["status"] == "backlog" and shown["approved_by"] is None
    released = orch.work.task_approve(card["number"], approve=True, reason="Planned work")
    assert released["approved_by"] == orch.principal.actor


# R3: a subagent question goes to its live orchestrator, not to the owner's needs-me.
def test_r3_sub_question_goes_to_live_orchestrator(fresh):
    orch = seat(fresh)
    sub = fresh.connect("sub", parent=orch.id)
    card = sub.work.task_add(board="work", title="Sub work")
    orch.work.task_approve(card["number"], approve=True, reason="Planned work")
    sub.work.task_take_next(task=card["number"])
    asked = sub.work.ask(
        REVIEW_QUESTION, options=REVIEW_OPTIONS, task=card["number"], context=REVIEW_CONTEXT
    )
    assert asked["ask_to"] == orch.principal.actor
    assert card["number"] not in owner_queue(fresh)
    assert card["number"] in {c["number"] for c in orch.work.needs_me()["asks"]}
    answered = orch.work.decide(card["number"], choice=REVIEW_OPTIONS[0])
    assert answered["status"] == "todo" and answered["assignee"] == sub.principal.actor


# R4: the owner is only offered an approval for a card that is also in needs-me.
def test_r4_pending_proposal_offers_owner_no_approval_past_live_orchestrator(fresh):
    orch = seat(fresh)
    sub = fresh.connect("sub", parent=orch.id)
    card = sub.work.task_add(board="work", title="Sub follow-up")
    assert card["number"] not in owner_queue(fresh)
    shown = fresh.owner.task_show(card["number"])
    # The owner may release a restricted-origin card by hand (the card panel's Release), so the
    # card offers it; the proposal itself stays with the orchestrator and is not in needs-me.
    assert shown["awaiting_release"] is True and shown["can_approve"] is True
    assert shown["proposal_status"] == "pending"


# R4 (second path): a plan the owner sees as awaiting review is in the owner's needs-me.
def test_r4_plan_label_is_in_needs_me(fresh):
    orch = seat(fresh)
    card = orch.work.task_add(board="work", title="Plan the work", labels=["plan"], claim=True)
    assert card["status"] == "doing"
    plans = {c["number"]: c for c in fresh.owner.needs_me()["plans"]}
    assert card["number"] in plans
    assert "approve" in plans[card["number"]]["actions"]


# R5: a manually assigned card counts as claimed and shows what happened to its start.
def test_r5_manual_assignment_is_claimed(fresh):
    orch = seat(fresh)
    other = fresh.connect("existing-agent")
    card = fresh.owner.task_add(board="work", title="Do this")
    moved = fresh.owner.task_handoff(card["number"], other.principal.actor, "Please do this")
    assert (moved["status"], moved["assignee"]) == ("doing", other.principal.actor)
    assert moved["claim_until"] and moved["status_line"]
    assert orch.work.task_take_next(board="work") is None


# R6: the seat orchestrator can request agent starts once the owner has trusted it.
def test_r6_trusted_seat_orchestrator_may_request_starts(projects_hub):
    h = projects_hub
    m = trust_setup(h)
    seat_id = h.seats()[0]["connection_id"]

    def may_request():
        machine = registry.get_machine(h.db, h.workspace, m["machine_id"])
        return predicate.requester_ok(h.gateway, h.db.authenticate_connection(seat_id), machine)

    assert may_request() is False
    assert trust(h, {"confirm": True, "machines": [m["machine_id"]]})[0] == 200
    assert may_request() is True
    assert trust(h, {"trusted": False})[0] == 200
    assert may_request() is False


# R6b: a started agent counts toward the machine limit for as long as it works its card.
def test_r6b_started_agent_counts_past_an_hour(started_capacity):
    store, machine, first, _ = started_capacity
    with store.connection() as conn:
        row = conn.execute(
            "SELECT state,started_at FROM runner_jobs WHERE id=?", (first["id"],)
        ).fetchone()
    assert row["state"] == "started"
    assert row["started_at"] <= jobs._after(jobs._now(), -jobs.STARTED_HOLD_SECONDS)
    assert jobs.lease(store, machine) is None


# R7: a worker finishing a card wakes the orchestrator through the recommended seat webhook.
def test_r7_done_reaches_recommended_seat_webhook(projects_hub):
    h = projects_hub
    status, connected = h.connect(h.owner, "work")
    assert status == 201
    recommended = connected["setup"]["webhook_add"]
    added = h.mcp(connected["token"], "webhook_add", url="http://127.0.0.1:9/hook", **recommended)
    assert added[1]["ok"], added
    with h.db._lock:
        subscription = dict(
            h.db._conn.execute(
                "SELECT workspace_id, kinds, board, waiting_on, owner_actor FROM webhooks "
                "WHERE connection_id=?",
                (connected["connection"]["id"],),
            ).fetchone()
        )

    def board_service(token):
        principal = h.gateway.authenticate("Bearer " + token)
        return BoardService(ToolContext(principal=principal, gateway=h.gateway))

    worker = board_service(h.a1)
    card = board_service(connected["token"]).task_add(board="work", title="Some work")
    worker.task_take_next(task=card["number"])
    before = h.seq()
    worker.task_update(card["number"], status="done")
    moved = next(
        e
        for e in events.after(h.db, h.workspace, before, h.seq())
        if e["kind"] == "card.moved" and e["data"]["number"] == card["number"]
    )
    assert moved["data"]["to"] == "done"
    assert webhooks._matches(subscription, moved)
