"""Inactive proposal bookkeeping preserves both release and rejection gates."""

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import mcp


def test_creator_can_annotate_unreleased_proposal(hub, fresh):
    parent = fresh.connect("parent")
    child = fresh.connect("child", parent=parent.id)
    card = child.work.task_add(board="work", title="Work awaiting release")
    hub.db.revoke(parent.id, actor=fresh.user, workspace_ref=fresh.workspace)
    before = fresh.owner.task_show(card["number"])
    ok, reply = mcp(
        hub, child.token, "task_update", task=card["number"], status_line="Waiting on a release"
    )
    assert ok, reply
    after = fresh.owner.task_show(card["number"])
    assert after["status_line"] == "Waiting on a release"
    for field in ("status", "assignee", "claim_until", "approved_by", "proposal_status"):
        assert after[field] == before[field]
    assert after["version"] == before["version"] + 1
    assert after["events"][-1]["kind"] == "edit"
    assert after["events"][-1]["actor"] == child.principal.actor
    ok, reply = mcp(
        hub,
        child.token,
        "task_update",
        task=card["number"],
        status_line="Start now",
        status="doing",
    )
    assert not ok, reply
    assert fresh.owner.task_show(card["number"]) == after


@pytest.mark.parametrize("lane", ["todo", "doing"])
def test_rejected_proposal_handoff_keeps_approval_gate(hub, fresh, lane):
    parent = fresh.connect("parent")
    child = fresh.connect("child", parent=parent.id)
    card = child.work.task_add(board="work", title="Rejected proposal")
    parent.work.task_approve(card["number"], approve=False, reason="The proposal needs revision")
    # A persisted active lane must not bypass a proposal's rejection.
    with parent.work.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET status=?,assignee=? WHERE id=?",
            (lane, parent.principal.actor, card["number"]),
        )
    before = fresh.owner.task_show(card["number"])
    ok, reply = mcp(
        hub,
        parent.token,
        "task_handoff",
        task=card["number"],
        to="anyone",
        note="Make it available",
    )
    assert not ok and "parent approval" in reply, reply
    assert fresh.owner.task_show(card["number"]) == before
