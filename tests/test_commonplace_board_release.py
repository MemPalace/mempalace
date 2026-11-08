"""Restricted-origin replay paths require an audited coordinator release."""

import pytest

from commonplace.extensions import ToolError
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api, mcp


def seat(fresh):
    coordinator = fresh.connect("orchestrator", trust="standard")
    fresh.owner.board_set("work", coordinator.principal.actor)
    return coordinator


@pytest.mark.parametrize("creation", [{}, {"claim": True}, {"status": "doing"}])
def test_restricted_origin_stays_inactive_without_release(hub, fresh, creation):
    seat(fresh)
    creator = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    card = creator.work.task_add(board="work", title="Restricted idea", **creation)
    assert card["status"] == "backlog" and card["claim_until"] is None
    assert card["approved_by"] is None
    ok, reply = mcp(hub, creator.token, "task_take_next", task=card["number"])
    assert not ok, reply
    assert creator.work.task_take_next(board="work") is None


def test_coordinator_releases_restricted_nonproposal(hub, fresh):
    coordinator = seat(fresh)
    creator = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    card = creator.work.task_add(board="work", title="Restricted idea")
    status, released = api(
        hub,
        coordinator.token,
        "POST",
        f"/api/cards/{card['number']}/approve",
        {"reason": "Ready for the planned work"},
    )
    assert status == 200, released
    assert released["status"] == "todo"
    assert released["proposal_status"] is None
    assert released["approved_by"] == coordinator.principal.actor
    assert creator.work.task_take_next(task=card["number"])["status"] == "doing"
    event = fresh.owner.task_show(card["number"])["events"][-2]
    assert event["kind"] == "decide" and event["actor"] == coordinator.principal.actor


@pytest.mark.parametrize("origin", ["subagent", "restricted"])
def test_owner_cannot_release_while_coordinator_is_live(hub, fresh, origin):
    coordinator = seat(fresh)
    creator = (
        fresh.connect("sub", parent=coordinator.id)
        if origin == "subagent"
        else fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    )
    card = creator.work.task_add(board="work", title="Follow-up")
    before = fresh.owner.task_show(card["number"])
    status, reply = api(hub, fresh.cookie, "POST", f"/api/cards/{card['number']}/approve", {})
    assert status == 403, reply
    assert fresh.owner.task_show(card["number"]) == before


@pytest.mark.parametrize("coordinator_state", ["none", "revoked", "restricted"])
@pytest.mark.parametrize("origin", ["subagent", "restricted"])
def test_owner_releases_without_live_coordinator(hub, fresh, coordinator_state, origin):
    parent = fresh.connect("parent")
    if coordinator_state != "none":
        coordinator = seat(fresh)
        if coordinator_state == "revoked":
            hub.db.revoke(coordinator.id, actor=fresh.user, workspace_ref=fresh.workspace)
        else:
            hub.db._conn.execute(
                "UPDATE connections SET policy=json_set(policy,'$.trust','untrusted') WHERE id=?",
                (coordinator.id,),
            )
    creator = (
        fresh.connect("sub", parent=parent.id)
        if origin == "subagent"
        else fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    )
    card = creator.work.task_add(board="work", title="Follow-up")
    status, released = api(
        hub,
        fresh.cookie,
        "POST",
        f"/api/cards/{card['number']}/approve",
        {"reason": "Independent owner recovery"},
    )
    assert status == 200, released
    assert released["approved_by"] == fresh.owner.principal.actor
    if origin == "subagent":
        assert released["proposal_status"] == "pending" and released["status"] == "backlog"
        reviewed = fresh.owner.task_approve(card["number"], reason="Separate proposal review")
        assert reviewed["proposal_status"] == "approved" and reviewed["status"] == "todo"
    assert creator.work.task_take_next(task=card["number"])["status"] == "doing"


def test_parent_proposal_approval_does_not_replace_coordinator_release(hub, fresh):
    coordinator = seat(fresh)
    parent = fresh.connect("parent")
    child = fresh.connect("child", parent=parent.id)
    card = child.work.task_add(board="work", title="Child follow-up")
    approved = parent.work.task_approve(card["number"], reason="The proposal is sound")
    assert approved["proposal_status"] == "approved"
    assert approved["status"] == "backlog" and approved["approved_by"] is None
    ok, reply = mcp(hub, child.token, "task_take_next", task=card["number"])
    assert not ok, reply
    released = coordinator.work.task_approve(card["number"], reason="Release the planned work")
    assert released["approved_by"] == coordinator.principal.actor
    assert child.work.task_take_next(task=card["number"])["status"] == "doing"


@pytest.mark.parametrize("actor", ["creator", "receiver", "owner"])
@pytest.mark.parametrize("route", ["take", "move"])
def test_legacy_unreleased_todo_cannot_bypass_coordinator(hub, fresh, actor, route):
    seat(fresh)
    creator = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    receiver = fresh.connect("receiver")
    card = creator.work.task_add(board="work", title="Legacy restricted work")
    with creator.work.store.transaction() as conn:
        conn.execute("UPDATE tasks SET status='todo',assignee=NULL WHERE id=?", (card["number"],))
    before = fresh.owner.task_show(card["number"])
    auth = (
        fresh.cookie
        if actor == "owner"
        else (creator.token if actor == "creator" else receiver.token)
    )
    if route == "move":
        status, reply = api(
            hub,
            auth,
            "PATCH",
            f"/api/cards/{card['number']}",
            {"status": "doing", "version": before["version"]},
        )
        assert status == 403, reply
    elif actor == "owner":
        refused = False
        try:
            fresh.owner.task_take_next(task=card["number"])
        except ToolError:
            refused = True
        assert refused
    else:
        who = creator if actor == "creator" else receiver
        ok, reply = mcp(hub, who.token, "task_take_next", task=card["number"])
        assert not ok, reply
    assert fresh.owner.task_show(card["number"]) == before


def test_coordinator_can_discover_release_and_owner_cannot(hub, fresh):
    coordinator = seat(fresh)
    creator = fresh.connect("restricted", trust="untrusted", write_wing="inbox")
    card = creator.work.task_add(board="work", title="Release this idea")
    assert not fresh.owner.task_show(card["number"])["can_approve"]
    assert coordinator.work.task_show(card["number"])["can_approve"]
    assert card["number"] in {
        item["number"] for item in coordinator.work.approvals_for_me()["items"]
    }
