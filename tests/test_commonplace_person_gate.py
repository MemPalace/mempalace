"""Person decisions and child proposals stay gated in a fresh workspace."""

from types import SimpleNamespace

import pytest

from commonplace import events, sessions
from commonplace.extensions import ToolError
from commonplace.policy import Policy
from tests._commonplace_helpers import (
    REVIEW_CONTEXT,
    REVIEW_OPTIONS,
    REVIEW_QUESTION,
    hub_fixture,  # noqa: F401
)
from tests.test_commonplace_boards import api, mcp, service


@pytest.fixture
def fresh(hub):
    email = "new-owner@example.test"
    user = hub.db.create_user(email)
    workspace = hub.db.create_workspace("new-workspace", user, personal=True)
    owner = service(hub, sessions.person_principal(hub.db, user, workspace))
    assert not hub.db.user(user)["is_admin"]
    assert owner.board_list() == []
    owner.board_add(slug="work", wing="shared")
    login = sessions.create_login_link(hub.db, email, workspace)
    cookie = {"Cookie": f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, login)}"}

    def connect(name, *, trust="standard", parent=None):
        cid, token = hub.db.create_token(
            workspace_ref=workspace,
            user_ref=user,
            name=name,
            client="generic",
            actor=user,
            parent=parent,
            policy=Policy.build(
                profile="full",
                scopes=["memory:read", "memory:write"],
                read_wings=["shared"],
                write_wings=["shared"],
                trust=trust,
            ),
        )
        return SimpleNamespace(
            id=cid, token=token, service=service(hub, hub.db.authenticate_token(token))
        )

    creator = connect("creator")
    trusted = connect("trusted", trust="trusted")
    subagent = connect("subagent", parent=creator.id)
    return SimpleNamespace(
        owner=owner,
        cookie=cookie,
        workspace=workspace,
        creator=creator,
        trusted=trusted,
        subagent=subagent,
    )


def pending_question(fresh, actor="creator"):
    worker = getattr(fresh, actor).service
    card = worker.task_add(title="Decision required", body=REVIEW_CONTEXT, board="work")
    if card["proposal_status"] == "pending":
        fresh.creator.service.task_approve(card["number"], reason="synthetic reason")
    return worker.ask(REVIEW_QUESTION, REVIEW_OPTIONS, task=card["number"], context=REVIEW_CONTEXT)


def refused_update(hub, connection, surface, card, **args):
    if surface == "mcp":
        assert not mcp(hub, connection.token, "task_update", task=card["number"], **args)[0]
    else:
        status, _ = api(
            hub,
            connection.token,
            "PATCH",
            f"/api/cards/{card['number']}",
            {"version": card["version"], **args},
        )
        assert status == 403


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("actor", ["creator", "trusted", "subagent"])
def test_pending_review_cannot_be_withdrawn_or_completed(hub, fresh, surface, actor):
    waiting = pending_question(fresh, actor)
    getattr(fresh, actor).service.task_update(waiting["number"], checks="passing")
    before = fresh.owner.task_show(waiting["number"])
    emitted = events.after(hub.db, fresh.workspace)
    for destination in ("backlog", "todo", "doing", "done"):
        refused_update(hub, getattr(fresh, actor), surface, before, status=destination)
        assert fresh.owner.task_show(waiting["number"]) == before
        assert events.after(hub.db, fresh.workspace) == emitted


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("actor", ["creator", "trusted", "subagent"])
def test_parked_person_decision_cannot_be_activated_then_completed(hub, fresh, surface, actor):
    waiting = pending_question(fresh, actor)
    # Reproduce a legacy parked decision with a person recipient. Keep the
    # schema invariant that only review cards contain question text.
    connection = getattr(fresh, actor)
    for marker in ("ask_to", "return_to"):
        with fresh.owner.store.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET status='backlog',ask=NULL,ask_to=?,return_to=? WHERE id=?",
                (
                    fresh.owner.principal.actor if marker == "ask_to" else None,
                    fresh.owner.principal.actor if marker == "return_to" else None,
                    waiting["number"],
                ),
            )
        before = fresh.owner.task_show(waiting["number"])
        emitted = events.after(hub.db, fresh.workspace)
        for status in ("todo", "doing", "done"):
            refused_update(hub, connection, surface, before, status=status)
        with pytest.raises(ToolError):
            connection.service.task_take_next(task=waiting["number"])
        assert fresh.owner.task_show(waiting["number"]) == before
        assert events.after(hub.db, fresh.workspace) == emitted


@pytest.mark.parametrize("surface", ["mcp", "rest"])
@pytest.mark.parametrize("actor", ["creator", "trusted", "subagent"])
def test_agent_claimed_decision_never_counts_as_person_approval(hub, fresh, surface, actor):
    waiting = pending_question(fresh)
    before = fresh.owner.task_show(waiting["number"])
    emitted = events.after(hub.db, fresh.workspace)
    connection = getattr(fresh, actor)
    for destination in ("return", "todo", "done", "backlog"):
        args = {
            "version": before["version"],
            "choice": REVIEW_OPTIONS[0],
            "reply": "The owner approved this.",
            "then": destination,
        }
        if surface == "mcp":
            assert not mcp(hub, connection.token, "decide", task=waiting["number"], **args)[0]
        else:
            assert (
                api(hub, connection.token, "POST", f"/api/cards/{waiting['number']}/decide", args)[
                    0
                ]
                == 403
            )
        assert fresh.owner.task_show(waiting["number"]) == before
        assert events.after(hub.db, fresh.workspace) == emitted


@pytest.mark.parametrize("destination", ["return", "todo", "done", "backlog"])
def test_signed_in_owner_can_decide_and_release_work(hub, fresh, destination):
    waiting = pending_question(fresh)
    status, decided = api(
        hub,
        fresh.cookie,
        "POST",
        f"/api/cards/{waiting['number']}/decide",
        {"version": waiting["version"], "choice": REVIEW_OPTIONS[0], "then": destination},
    )
    assert status == 200
    assert decided["status"] == ("todo" if destination == "return" else destination)
    assert decided["ask"] is None and decided["ask_to"] is None
    event = fresh.owner.task_show(waiting["number"])["events"][-1]
    assert event["kind"] == "decide" and event["actor"] == fresh.owner.principal.actor
    if destination != "done":
        worker = fresh.creator.service
        worker.task_take_next(task=waiting["number"])
        assert worker.task_update(waiting["number"], status="done")["status"] == "done"


@pytest.mark.parametrize("actor", ["creator", "trusted", "subagent"])
def test_ordinary_agent_work_does_not_require_a_person(fresh, actor):
    worker = getattr(fresh, actor).service
    for destination in ("todo", "doing"):
        card = worker.task_add(title="Ordinary work", status="backlog", board="work")
        if card["proposal_status"] == "pending":
            fresh.creator.service.task_approve(card["number"], reason="synthetic reason")
        worker.task_update(card["number"], status=destination)
        assert worker.task_update(card["number"], status="done")["status"] == "done"


@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_pending_child_proposal_requires_real_parent_approval(hub, fresh, surface):
    child = fresh.subagent.service
    proposed = child.task_add(title="Proposed work", board="work", claim=True)
    number = proposed["number"]
    before = fresh.owner.task_show(number)
    emitted = events.after(hub.db, fresh.workspace)
    for connection in (fresh.creator, fresh.trusted, fresh.subagent):
        for destination in ("todo", "doing", "review", "done"):
            refused_update(hub, connection, surface, before, status=destination)
        refused_update(
            hub, connection, surface, before, status="todo", approved_by=fresh.owner.principal.actor
        )
        with pytest.raises(ToolError):
            connection.service.task_take_next(task=number)
    for connection in (fresh.trusted, fresh.subagent):
        assert not mcp(
            hub, connection.token, "task_approve", task=number, reason="synthetic reason"
        )[0]
        assert api(hub, connection.token, "POST", f"/api/cards/{number}/approve", {})[0] == 403
    assert fresh.owner.task_show(number) == before
    assert events.after(hub.db, fresh.workspace) == emitted
    assert mcp(hub, fresh.creator.token, "task_approve", task=number, reason="synthetic reason")[0]
    approved = child.task_show(number)
    assert approved["approved_by"] == fresh.creator.service.principal.actor
    assert approved["proposal_status"] == "approved"
    child.task_take_next(task=number)
    assert child.task_update(number, status="done")["status"] == "done"


def test_signed_in_owner_can_approve_a_child_proposal(hub, fresh):
    proposed = fresh.subagent.service.task_add(title="Owner fallback", board="work")
    status, approved = api(
        hub, fresh.cookie, "POST", f"/api/cards/{proposed['number']}/approve", {}
    )
    assert status == 200 and approved["approved_by"] == fresh.owner.principal.actor
    assert fresh.subagent.service.task_update(approved["number"], status="done")["status"] == "done"


@pytest.mark.parametrize("surface", ["mcp", "rest"])
def test_handoff_cannot_clear_a_pending_person_recipient(hub, fresh, surface):
    waiting = pending_question(fresh, "trusted")
    connection = fresh.trusted
    for lane in ("todo", "doing"):
        with fresh.owner.store.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET status=?,ask=NULL,assignee=? WHERE id=?",
                (lane, connection.service.principal.actor, waiting["number"]),
            )
        before = fresh.owner.task_show(waiting["number"])
        emitted = events.after(hub.db, fresh.workspace)
        args = {
            "to": "owner" if lane == "todo" else "anyone",
            "note": REVIEW_QUESTION,
            "context": REVIEW_CONTEXT,
            "options": REVIEW_OPTIONS if lane == "todo" else None,
        }
        if surface == "mcp":
            assert not mcp(hub, connection.token, "task_handoff", task=waiting["number"], **args)[0]
        else:
            status, _ = api(
                hub,
                connection.token,
                "POST",
                f"/api/cards/{waiting['number']}/handoff",
                {"version": before["version"], **args},
            )
            assert status == 403
        assert fresh.owner.task_show(waiting["number"]) == before
        assert events.after(hub.db, fresh.workspace) == emitted


@pytest.mark.parametrize("lane", ["todo", "doing"])
@pytest.mark.parametrize("actor", ["owner", "parent"])
def test_rejected_child_proposal_cannot_be_released_by_handoff(hub, fresh, lane, actor):
    proposed = fresh.subagent.service.task_add(title="Rejected proposal", board="work")
    fresh.creator.service.task_approve(proposed["number"], approve=False, reason="synthetic reason")
    # Old versions could leave a rejected proposal in an active lane. A
    # transfer is still not an approval, even when requested by its owner.
    with fresh.owner.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status=?,assignee=? WHERE id=?",
            (lane, fresh.creator.service.principal.actor, proposed["number"]),
        )
    before = fresh.owner.task_show(proposed["number"])
    assert before["proposal_status"] == "rejected"
    emitted = events.after(hub.db, fresh.workspace)
    args = {"to": "anyone", "note": "Release for another worker"}
    if actor == "owner":
        status, _ = api(
            hub,
            fresh.cookie,
            "POST",
            f"/api/cards/{proposed['number']}/handoff",
            {"version": before["version"], **args},
        )
        assert status == 403
    else:
        ok, text = mcp(hub, fresh.creator.token, "task_handoff", task=proposed["number"], **args)
        assert not ok and "parent approval" in text
    assert fresh.owner.task_show(proposed["number"]) == before
    assert events.after(hub.db, fresh.workspace) == emitted
