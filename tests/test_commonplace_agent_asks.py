"""Generic agent ask routing and narrowly authorized withdrawal, via public APIs."""

import pytest

from commonplace import sessions
from commonplace.extensions import ToolError
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    REVIEW_QUESTION,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_board_authority import fresh as fresh_fixture
from tests.test_commonplace_boards import api, mcp, service
from tests.test_commonplace_subagent_asks import make_coordinator_unavailable, seat

fresh = fresh_fixture


def open_ask(caller, route="new", **kwargs):
    args = dict(question=REVIEW_QUESTION, options=REVIEW_OPTIONS, context=REVIEW_CONTEXT)
    args.update(kwargs)
    if route == "new":
        return caller.ask(board="work", **args)
    card = caller.task_add(board="work", title="Draft change awaiting checks", claim=True)
    if route == "review":
        return caller.task_update(card["number"], status="review", **args)
    return caller.ask(task=card["number"], **args)


def ask_numbers(caller):
    return {c["number"] for c in caller.needs_me()["asks"]}


@pytest.mark.parametrize("route", ["new", "card", "review"])
@pytest.mark.parametrize("requester", ["orchestrator", "independent"])
def test_card339_routine_agent_ask_reaches_coordinator(fresh, route, requester):
    orch = seat(fresh)
    caller = orch if requester == "orchestrator" else fresh.connect("independent")
    asked = open_ask(caller.work, route)
    number = asked["number"]
    assert number not in ask_numbers(fresh.owner)
    assert number in ask_numbers(orch.work)
    assert asked["ask_to"] == orch.principal.actor
    assert asked["return_to"] == caller.principal.actor
    assert asked["review_kind"] == "decision"
    item = next(c for c in orch.work.needs_me()["asks"] if c["number"] == number)
    assert item["actions"] == ["withdraw" if requester == "orchestrator" else "answer", "escalate"]


@pytest.mark.parametrize("route", ["new", "card", "review"])
def test_card339_board_without_coordinator_keeps_owner_route(fresh, route):
    asked = open_ask(fresh.connect("worker").work, route)
    assert asked["ask_to"] == fresh.owner.principal.actor
    assert asked["number"] in ask_numbers(fresh.owner)


def test_card339_person_handoff_survives_technical_status_line(fresh):
    seat(fresh)
    worker = fresh.connect("worker")
    card = worker.work.task_add(
        board="work", title="Person authorization needed", body=REVIEW_CONTEXT, claim=True
    )
    worker.work.task_update(card["number"], status_line="Waiting for draft checks and gate")
    asked = worker.work.task_handoff(
        card["number"],
        fresh.owner.principal.actor,
        "Only a person can authorize spending: which budget should be used?",
        options=REVIEW_OPTIONS,
        context=REVIEW_CONTEXT,
    )
    assert asked["number"] in ask_numbers(fresh.owner)
    assert asked["status_line"] == "Waiting for draft checks and gate"


@pytest.mark.parametrize("route", ["new", "card", "review"])
@pytest.mark.parametrize("status", ["todo", "doing"])
@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_card339_coordinator_withdraws_own_ask(hub, fresh, route, status, surface):
    orch = seat(fresh)
    asked = open_ask(orch.work, route)
    before = fresh.owner.task_show(asked["number"])
    args = {"status": status, "reason": "Checks are still running; retain the draft."}
    if surface == "mcp":
        ok, output = mcp(hub, orch.token, "task_update", task=asked["number"], **args)
        assert ok, output
    else:
        code, output = api(
            hub,
            orch.token,
            "PATCH",
            f"/api/cards/{asked['number']}",
            {"version": asked["version"], **args},
        )
        assert code == 200, output
    current = fresh.owner.task_show(asked["number"])
    assert current["status"] == status
    assert all(current[k] is None for k in ("ask", "ask_to", "return_to"))
    assert current["assignee"] == orch.principal.actor
    assert bool(current["claim_until"]) == (status == "doing")
    assert current["events"][: len(before["events"])] == before["events"]
    release = next(e for e in current["events"] if e["kind"] == "release")
    assert (release["text"], release["reason"], release["target"]) == (
        REVIEW_QUESTION,
        args["reason"],
        before["ask_to"],
    )
    assert current["number"] not in ask_numbers(fresh.owner)
    assert current["number"] not in ask_numbers(orch.work)


@pytest.mark.parametrize("actor", ["other-agent", "member-agent", "member-person", "other-board"])
@pytest.mark.parametrize("operation", ["withdraw-todo", "withdraw-doing", "answer"])
def test_card339_unrelated_actors_cannot_withdraw_or_answer(hub, fresh, actor, operation):
    orch = seat(fresh)
    asked = open_ask(orch.work)
    if actor.startswith("member"):
        user = hub.db.create_user("member-agent-asks@example.test")
        hub.db.add_member(fresh.workspace, user, "member", actor=fresh.user)
        caller = (
            service(hub, sessions.person_principal(hub.db, user, fresh.workspace))
            if actor == "member-person"
            else fresh.connect("member", user_ref=user).work
        )
    else:
        other = fresh.connect(actor)
        caller = other.work
        if actor == "other-board":
            fresh.owner.board_add(slug="second", wing="shared")
            fresh.owner.board_set("second", other.principal.actor)
    before = fresh.owner.task_show(asked["number"])
    with pytest.raises(ToolError):
        if operation == "answer":
            caller.decide(asked["number"], choice=REVIEW_OPTIONS[0])
        else:
            caller.task_update(asked["number"], status=operation.split("-")[1])
    assert fresh.owner.task_show(asked["number"]) == before


@pytest.mark.parametrize("status", ["todo", "doing"])
def test_card339_coordinator_cannot_withdraw_another_requesters_ask(fresh, status):
    orch = seat(fresh)
    asked = open_ask(fresh.connect("requester").work)
    before = fresh.owner.task_show(asked["number"])
    with pytest.raises(ToolError):
        orch.work.task_update(asked["number"], status=status)
    assert fresh.owner.task_show(asked["number"]) == before


@pytest.mark.parametrize(
    "unavailable", ["unset", "replaced", "revoked", "readonly", "hidden", "no-decide", "no-needs"]
)
def test_card339_withdrawal_rechecks_current_authority(hub, fresh, unavailable):
    orch = seat(fresh)
    asked = open_ask(orch.work)
    make_coordinator_unavailable(hub, fresh, orch, unavailable)
    before = fresh.owner.task_show(asked["number"])
    with pytest.raises(ToolError):
        orch.work.task_update(asked["number"], status="todo")
    assert fresh.owner.task_show(asked["number"]) == before
    assert asked["number"] in ask_numbers(fresh.owner)


def test_card339_new_coordinator_cannot_withdraw_ask_to_previous_coordinator(fresh):
    seat(fresh)
    requester = fresh.connect("next-coordinator")
    asked = open_ask(requester.work)
    fresh.owner.board_set("work", requester.principal.actor)
    with pytest.raises(ToolError):
        requester.work.task_update(asked["number"], status="todo")


@pytest.mark.parametrize("field", ["review_kind", "assignee"])
def test_card339_legacy_review_authorization_stays_closed(fresh, field):
    orch = seat(fresh)
    asked = open_ask(orch.work)
    # Historical shapes must not gain withdrawal authority from their recipient alone.
    with fresh.owner.store.transaction() as conn:
        conn.execute(
            f"UPDATE tasks SET {field}=? WHERE id=?",
            (
                "completion" if field == "review_kind" else fresh.owner.principal.actor,
                asked["number"],
            ),
        )
    with pytest.raises(ToolError):
        orch.work.task_update(asked["number"], status="todo")


@pytest.mark.parametrize("route", ["new", "card", "review"])
def test_person_reason_targets_owner_and_is_audited(fresh, route):
    orch = seat(fresh)
    reason = "Only a person may authorize this spending."
    asked = open_ask(orch.work, route, person_reason=reason)
    assert asked["ask_to"] == fresh.owner.principal.actor
    assert asked["number"] in ask_numbers(fresh.owner)
    assert reason in str(fresh.owner.task_show(asked["number"])["events"])
    for operation in ("todo", "doing", "done", "answer"):
        with pytest.raises(ToolError):
            if operation == "answer":
                orch.work.decide(asked["number"], choice=REVIEW_OPTIONS[0])
            else:
                orch.work.task_update(asked["number"], status=operation)


@pytest.mark.parametrize("route", ["new", "card", "review"])
@pytest.mark.parametrize("reason", ["", "   ", 7, "x" * 501])
def test_invalid_person_reason_changes_nothing(fresh, route, reason):
    orch = seat(fresh)
    with pytest.raises(ToolError, match="person_reason"):
        open_ask(orch.work, route, person_reason=reason)
    assert not ask_numbers(fresh.owner)
    assert not ask_numbers(orch.work)


def test_coordinator_must_withdraw_or_escalate_own_ask(fresh):
    orch = seat(fresh)
    asked = open_ask(orch.work)
    with pytest.raises(ToolError, match="own ask"):
        orch.work.decide(asked["number"], choice=REVIEW_OPTIONS[0])
    with pytest.raises(ToolError):
        orch.work.task_update(asked["number"], status="done")
    escalated = orch.work.decide(asked["number"], then="escalate", reply="Person approval needed.")
    assert escalated["number"] in ask_numbers(fresh.owner)
    with pytest.raises(ToolError):
        orch.work.task_update(asked["number"], status="todo")


@pytest.mark.parametrize("tool", ["ask", "task_update"])
def test_person_reason_is_supported_through_mcp(hub, fresh, tool):
    orch = seat(fresh)
    args = dict(
        question=REVIEW_QUESTION,
        options=REVIEW_OPTIONS,
        context=REVIEW_CONTEXT,
        person_reason="A person must authorize spending.",
    )
    if tool == "task_update":
        card = orch.work.task_add(board="work", title="Review request", claim=True)
        args.update(task=card["number"], status="review")
    else:
        args["board"] = "work"
    ok, output = mcp(hub, orch.token, tool, **args)
    assert ok, output
    assert len(ask_numbers(fresh.owner)) == 1


def test_existing_open_asks_are_unchanged_when_service_reopens(hub, fresh):
    worker = fresh.connect("worker")
    asked = open_ask(worker.work)
    before = fresh.owner.task_show(asked["number"])
    seat(fresh)
    restarted = service(hub, fresh.owner.principal)
    assert restarted.task_show(asked["number"]) == before
    assert asked["number"] in ask_numbers(restarted)
