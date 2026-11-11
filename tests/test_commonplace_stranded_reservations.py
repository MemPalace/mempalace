"""Reserved work can be released or cancelled without granting self-approval."""

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api
from tests.test_commonplace_origin_holder_release import edit, raw, snapshot


def reserved(fresh, *, trust="untrusted"):
    creator = fresh.connect(
        "creator", trust=trust, write_wing="inbox" if trust == "untrusted" else "shared"
    )
    coordinator = fresh.connect("coordinator", trust="standard")
    fresh.owner.board_set("work", coordinator.principal.actor)
    card = creator.work.task_add(
        board="work", title="Reserved follow-up", assignee=coordinator.principal.actor
    )
    return creator, coordinator, card["number"]


def test_coordinator_releases_own_reservation(hub, fresh):
    _, coordinator, number = reserved(fresh)
    before = raw(fresh.owner, number)
    status, card = api(
        hub,
        coordinator.token,
        "POST",
        f"/api/cards/{number}/approve",
        {"reason": "Independent origin checked"},
    )
    assert status == 200, card
    assert card["approved_by"] == coordinator.principal.actor
    assert card["assignee"] == coordinator.principal.actor
    assert card["status"] == "todo" and card["claim_until"] is None
    after = raw(fresh.owner, number)
    for key in ("created_by", "stamp_user", "stamp_connection", "restricted_origin"):
        assert after[key] == before[key]
    assert coordinator.work.task_take_next(task=number)["status"] == "doing"


@pytest.mark.parametrize("release", ["current", "lapsed"])
def test_creator_cancels_own_reservation_after_handoff(hub, fresh, release):
    creator, coordinator, number = reserved(fresh)
    coordinator.work.task_approve(number, reason="Independent origin checked")
    coordinator.work.task_take_next(task=number)
    handed = coordinator.work.task_handoff(
        number, creator.principal.actor, "Return this work to its creator"
    )
    assert handed["status"] == "todo" and handed["assignee"] == creator.principal.actor
    if release == "lapsed":
        hub.db.revoke(coordinator.id, actor=fresh.user, workspace_ref=fresh.workspace)
    before = raw(fresh.owner, number)
    status, card = api(
        hub,
        creator.token,
        "PATCH",
        f"/api/cards/{number}",
        {"status": "backlog", "version": before["version"]},
    )
    assert status == 200, card
    assert card["status"] == "backlog" and card["assignee"] is None
    assert card["claim_until"] is None
    after = raw(fresh.owner, number)
    for key in ("created_by", "stamp_user", "stamp_connection", "restricted_origin", "approved_by"):
        assert after[key] == before[key]
    assert fresh.owner.task_show(number)["events"][-1]["kind"] == "move"
    if release == "lapsed":
        status, reply = api(
            hub,
            creator.token,
            "PATCH",
            f"/api/cards/{number}",
            {"status": "doing", "version": after["version"]},
        )
        assert status == 403, reply
        assert raw(fresh.owner, number) == after


@pytest.mark.parametrize("relation", ["unrelated", "child", "grandchild"])
def test_reservation_does_not_grant_origin_release(hub, fresh, relation):
    creator, _, number = reserved(fresh)
    parent = creator.id if relation != "unrelated" else None
    if relation == "grandchild":
        parent = fresh.connect("child", parent=parent).id
    actor = fresh.connect("receiver", parent=parent, trust="trusted")
    if relation != "unrelated":
        fresh.owner.board_set("work", actor.principal.actor)
    edit(fresh.owner, number, status="todo", assignee=actor.principal.actor)
    before = snapshot(fresh.owner, number)
    status, reply = api(
        hub, actor.token, "POST", f"/api/cards/{number}/approve", {"reason": "Try release"}
    )
    assert status == 403, reply
    assert snapshot(fresh.owner, number) == before


@pytest.mark.parametrize("holder", ["self", "other"])
def test_descendant_release_marker_is_not_authority(hub, fresh, holder):
    creator, coordinator, number = reserved(fresh)
    descendant = fresh.connect("descendant", parent=creator.id, trust="trusted")
    fresh.owner.board_set("work", descendant.principal.actor)
    edit(
        fresh.owner,
        number,
        status="todo",
        approved_by=descendant.principal.actor,
        assignee=descendant.principal.actor if holder == "self" else coordinator.principal.actor,
    )
    actor = descendant if holder == "self" else coordinator
    before = snapshot(fresh.owner, number)
    status, reply = api(
        hub,
        actor.token,
        "PATCH",
        f"/api/cards/{number}",
        {"status": "doing", "version": before[0]["version"]},
    )
    assert status == 403, reply
    assert snapshot(fresh.owner, number) == before


@pytest.mark.parametrize(
    "relation", ["unrelated", "descendant", "other-holder", "other-holder-trusted"]
)
def test_cancellation_cannot_drop_another_reservation(hub, fresh, relation):
    creator, coordinator, number = reserved(
        fresh, trust="trusted" if relation == "other-holder-trusted" else "untrusted"
    )
    if relation.startswith("other-holder"):
        actor, holder = creator, coordinator
    else:
        actor = fresh.connect("receiver", parent=creator.id if relation == "descendant" else None)
        holder = actor
    edit(fresh.owner, number, status="todo", assignee=holder.principal.actor)
    before = snapshot(fresh.owner, number)
    status, reply = api(
        hub,
        actor.token,
        "PATCH",
        f"/api/cards/{number}",
        {"status": "backlog", "version": before[0]["version"]},
    )
    assert status == 403, reply
    assert snapshot(fresh.owner, number) == before


@pytest.mark.parametrize("guard", ["extra-fields", "pending", "rejected", "person"])
def test_cancellation_keeps_review_and_update_guards(hub, fresh, guard):
    creator, _, number = reserved(fresh)
    changes = {"status": "todo", "assignee": creator.principal.actor}
    if guard in ("pending", "rejected"):
        changes["proposal_status"] = guard
    if guard == "person":
        changes["ask_to"] = f"user:{fresh.user}"
    edit(fresh.owner, number, **changes)
    before = snapshot(fresh.owner, number)
    update = {"status": "backlog", "version": before[0]["version"]}
    if guard == "extra-fields":
        update["status_line"] = "Do not bundle this write with cancellation"
    status, reply = api(hub, creator.token, "PATCH", f"/api/cards/{number}", update)
    assert status == 403, reply
    assert snapshot(fresh.owner, number) == before
