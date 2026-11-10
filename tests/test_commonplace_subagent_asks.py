"""Replay subagent questions through the live board coordinator before the owner."""

import json

import pytest

from commonplace.extensions import ToolError
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    REVIEW_QUESTION,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_board_authority import fresh as fresh_fixture

fresh = fresh_fixture


def seat(fresh):
    orch = fresh.connect("orchestrator:work", trust="standard")
    fresh.owner.board_set("work", orch.principal.actor)
    return orch


def question(fresh, orch):
    sub = fresh.connect("sub", parent=orch.id)
    card = fresh.owner.task_add(board="work", title="Sub work", assignee=sub.principal.actor)
    sub.work.task_take_next(task=card["number"])
    asked = sub.work.ask(
        REVIEW_QUESTION, options=REVIEW_OPTIONS, task=card["number"], context=REVIEW_CONTEXT
    )
    return sub, asked


def test_r3_sub_question_reaches_orchestrator_before_owner(fresh):
    orch = seat(fresh)
    sub, asked = question(fresh, orch)
    number = asked["number"]
    assert number not in {c["number"] for c in fresh.owner.needs_me()["asks"]}
    assert asked["ask_to"] == orch.principal.actor
    assert number in {c["number"] for c in orch.work.needs_me()["asks"]}
    answered = orch.work.decide(number, choice=REVIEW_OPTIONS[0], version=asked["version"])
    assert answered["status"] == "todo" and answered["assignee"] == sub.principal.actor
    held = sub.work.task_take_next(task=number)
    assert held["decision"]["actor"] == orch.principal.actor
    assert held["decision"]["choice"] == REVIEW_OPTIONS[0]


def test_orchestrator_can_escalate_with_a_reason_and_preserve_question(fresh):
    orch = seat(fresh)
    sub, asked = question(fresh, orch)
    number = asked["number"]
    for reason in (None, "", "   "):
        with pytest.raises(ToolError):
            orch.work.decide(number, then="escalate", reply=reason)
        assert fresh.owner.task_show(number)["version"] == asked["version"]
    reason = "Only the owner can authorize this spending decision."
    escalated = orch.work.decide(number, then="escalate", reply=reason, version=asked["version"])
    assert escalated["status"] == "review"
    assert escalated["ask_to"] == fresh.owner.principal.actor
    assert escalated["return_to"] == sub.principal.actor
    assert (escalated["ask"], escalated["ask_options"], escalated["review_context"]) == (
        REVIEW_QUESTION,
        REVIEW_OPTIONS,
        REVIEW_CONTEXT,
    )
    assert fresh.owner.task_show(number)["events"][-1]["reason"] == reason
    assert number in {c["number"] for c in fresh.owner.needs_me()["asks"]}
    with pytest.raises(ToolError):
        orch.work.decide(number, choice=REVIEW_OPTIONS[0])
    answered = fresh.owner.decide(number, choice=REVIEW_OPTIONS[0])
    assert answered["assignee"] == sub.principal.actor


def make_coordinator_unavailable(hub, fresh, orch, unavailable):
    if unavailable == "unset":
        fresh.owner.board_set("work", None)
    elif unavailable == "replaced":
        replacement = fresh.connect("replacement", trust="standard")
        fresh.owner.board_set("work", replacement.principal.actor)
    elif unavailable == "revoked":
        hub.db.revoke(orch.id, actor=fresh.user, workspace_ref=fresh.workspace)
    elif unavailable == "expired":
        hub.db._conn.execute("UPDATE connections SET expires_at='2000' WHERE id=?", (orch.id,))
    elif unavailable == "idle":
        hub.db._conn.execute(
            "UPDATE connections SET client='commonplace-orchestrator',last_used_at='2000-01-01T00:00:00Z' WHERE id=?",
            (orch.id,),
        )
    elif unavailable == "hidden":
        hub.db.set_connection_read_wings(
            orch.id, ["elsewhere"], actor=fresh.user, workspace_ref=fresh.workspace
        )
    else:
        # No public helper changes scope, trust or tool allowlists in place.
        policy = json.loads(hub.db.connection(orch.id, fresh.workspace)["policy"])
        if unavailable == "readonly":
            policy["scopes"] = ["memory:read"]
        elif unavailable == "restricted":
            policy["trust"] = "untrusted"
        else:
            missing = "needs_me" if unavailable == "no-needs" else "decide"
            policy["tools"] = sorted(orch.principal.policy.tool_names() - {missing})
        hub.db._conn.execute(
            "UPDATE connections SET policy=? WHERE id=?", (json.dumps(policy), orch.id)
        )


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize(
    "unavailable",
    [
        "unset",
        "revoked",
        "expired",
        "idle",
        "readonly",
        "hidden",
        "restricted",
        "no-decide",
        "replaced",
    ],
)
def test_no_live_coordinator_keeps_owner_route(hub, fresh, unavailable, when):
    orch = seat(fresh)
    sub = fresh.connect("sub", parent=orch.id)
    card = fresh.owner.task_add(board="work", title="Sub work", assignee=sub.principal.actor)
    sub.work.task_take_next(task=card["number"])
    if when == "after":
        asked = sub.work.ask(
            REVIEW_QUESTION, options=REVIEW_OPTIONS, task=card["number"], context=REVIEW_CONTEXT
        )
        assert asked["ask_to"] == orch.principal.actor
        assert asked["number"] not in {c["number"] for c in fresh.owner.needs_me()["asks"]}
    make_coordinator_unavailable(hub, fresh, orch, unavailable)
    if when == "before":
        asked = sub.work.ask(
            REVIEW_QUESTION, options=REVIEW_OPTIONS, task=card["number"], context=REVIEW_CONTEXT
        )
        expected = "conn:replacement" if unavailable == "replaced" else fresh.owner.principal.actor
        assert asked["ask_to"] == expected
        if unavailable == "replaced":
            assert asked["number"] not in {c["number"] for c in fresh.owner.needs_me()["asks"]}
            return
    before = fresh.owner.task_show(asked["number"])
    assert asked["number"] in {c["number"] for c in fresh.owner.needs_me()["asks"]}
    # Falling back is a projection; reading the queue never rewrites the question or audit.
    assert fresh.owner.task_show(asked["number"]) == before
    answered = fresh.owner.decide(asked["number"], choice=REVIEW_OPTIONS[0])
    assert answered["assignee"] == sub.principal.actor


def test_person_asks_keep_owner_route(fresh):
    orch = seat(fresh)
    for caller in (fresh.owner,):
        asked = caller.ask(
            REVIEW_QUESTION, options=REVIEW_OPTIONS, board="work", context=REVIEW_CONTEXT
        )
        assert asked["ask_to"] == fresh.owner.principal.actor
        with pytest.raises(ToolError):
            orch.work.decide(asked["number"], choice=REVIEW_OPTIONS[0])


def test_only_addressed_live_coordinator_can_decide(hub, fresh):
    orch = seat(fresh)
    _sub, asked = question(fresh, orch)
    unrelated = fresh.connect("unrelated")
    with pytest.raises(ToolError):
        unrelated.work.decide(asked["number"], choice=REVIEW_OPTIONS[0])
    replacement = fresh.connect("replacement", trust="standard")
    fresh.owner.board_set("work", replacement.principal.actor)
    for caller in (orch.work, replacement.work):
        with pytest.raises(ToolError):
            caller.decide(asked["number"], choice=REVIEW_OPTIONS[0])


def test_standard_coordinator_can_answer_via_mcp(hub, fresh):
    orch = seat(fresh)
    sub, asked = question(fresh, orch)
    assert {"decide", "needs_me"} <= hub.tool_names(orch.token)
    response = hub.rpc(
        orch.token,
        "tools/call",
        {
            "name": "decide",
            "arguments": {
                "task": asked["number"],
                "choice": REVIEW_OPTIONS[0],
                "version": asked["version"],
            },
        },
    )
    assert not response["result"].get("isError"), response
    assert (
        sub.work.task_take_next(task=asked["number"])["decision"]["actor"] == orch.principal.actor
    )


def test_subagent_requires_released_card_before_asking(fresh):
    orch = seat(fresh)
    sub = fresh.connect("sub", parent=orch.id)
    with pytest.raises(ToolError, match="proposal"):
        sub.work.ask(REVIEW_QUESTION, options=REVIEW_OPTIONS, board="work", context=REVIEW_CONTEXT)
    proposal = sub.work.task_add(board="work", title="Sub work")
    assert proposal["status"] == "backlog"
    assert proposal["proposal_status"] == "pending"
    with pytest.raises(ToolError):
        sub.work.ask(
            REVIEW_QUESTION, options=REVIEW_OPTIONS, task=proposal["number"], context=REVIEW_CONTEXT
        )
    released = orch.work.task_approve(proposal["number"], reason="Proceed with this work.")
    assert released["status"] == "todo"
    sub.work.task_take_next(task=proposal["number"])
    asked = sub.work.ask(
        REVIEW_QUESTION, options=REVIEW_OPTIONS, task=proposal["number"], context=REVIEW_CONTEXT
    )
    assert asked["ask_to"] == orch.principal.actor
    assert asked["number"] not in {c["number"] for c in fresh.owner.needs_me()["asks"]}
    answered = orch.work.decide(asked["number"], reply="Use the existing approach.")
    assert answered["assignee"] == sub.principal.actor


def test_subagent_without_coordinator_still_needs_a_proposal_first(fresh):
    parent = fresh.connect("parent")
    sub = fresh.connect("sub", parent=parent.id)
    with pytest.raises(ToolError, match="proposal"):
        sub.work.ask(REVIEW_QUESTION, options=REVIEW_OPTIONS, board="work", context=REVIEW_CONTEXT)


def test_standard_member_coordinator_receives_only_its_own_questions(hub, fresh):
    member = hub.db.create_user("coordinator@example.test")
    hub.db.add_member(fresh.workspace, member, "member", actor=fresh.user)
    orch = fresh.connect("member-coordinator", user_ref=member, trust="standard")
    fresh.owner.board_set("work", orch.principal.actor)
    sub, asked = question(fresh, orch)
    other = fresh.owner.ask(
        REVIEW_QUESTION, options=REVIEW_OPTIONS, board="work", context=REVIEW_CONTEXT
    )
    groups = orch.work.needs_me()
    assert {c["number"] for c in groups["asks"]} == {asked["number"]}
    assert groups["asks"][0]["actions"] == ["answer", "escalate"]
    assert "asks you" in groups["asks"][0]["why"]
    assert {"decide", "needs_me"} <= hub.tool_names(orch.token)
    with pytest.raises(ToolError):
        orch.work.decide(other["number"], choice=REVIEW_OPTIONS[0])
    response = hub.rpc(
        orch.token,
        "tools/call",
        {
            "name": "decide",
            "arguments": {
                "task": asked["number"],
                "then": "escalate",
                "reply": "The owner must authorize this change.",
            },
        },
    )
    assert not response["result"].get("isError"), response
    assert asked["number"] in {c["number"] for c in fresh.owner.needs_me()["asks"]}
    assert fresh.owner.task_show(asked["number"])["return_to"] == sub.principal.actor
