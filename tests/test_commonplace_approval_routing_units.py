"""Approval routing edge cases and startup bounds on the current implementation."""

import logging

import pytest

from commonplace.boards import tools as board_tools
from commonplace.boards.service import BoardService
from commonplace.boards.store import task_row
from commonplace.extensions import ToolError
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_approval_routing import (
    ANSWER,
    APPROVE,
    NO_HOME,
    count_roles,
    home,
    start,
)
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import mcp


def test_root_cannot_review_grandchild_plan_after_parent_revocation(hub, fresh):
    root = fresh.connect("root")
    parent = fresh.connect("parent", parent=root.id)
    leaf = fresh.connect("leaf", parent=parent.id, trust="untrusted", write_wing="inbox")
    card = leaf.work.task_add(board="work", title="Synthetic descendant work")
    parent.work.task_approve(card["number"], reason="Synthetic proposal approval")
    fresh.owner.task_approve(card["number"], reason="Independent owner recovery")
    leaf.work.task_take_next(task=card["number"])
    leaf.work.task_update(card["number"], labels=["plan"])
    hub.db.revoke(parent.id, actor=fresh.user, workspace_ref=fresh.workspace)

    assert not root.work.needs_me()["approvals"]
    owner_report = fresh.owner.needs_me()
    assert {c["number"] for c in owner_report["plans"]} == {card["number"]}
    assert fresh.owner.task_show(card["number"])["need"] == "you"
    with pytest.raises(ToolError, match="direct parent"):
        root.work.task_approve(card["number"], reason="Synthetic plan approval")


def test_stuck_plan_remains_an_owner_decision_with_live_supervisor(fresh):
    parent = fresh.connect("parent")
    child = fresh.connect("child", parent=parent.id)
    card = fresh.owner.task_add(
        board="work",
        title="Synthetic expired plan",
        labels=["plan"],
        assignee=child.principal.actor,
        claim=True,
    )
    with child.work.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (card["number"],)
        )

    shown = fresh.owner.task_show(card["number"])
    owner_report = fresh.owner.needs_me()
    assert shown["need"] == "you"
    assert {c["number"] for c in owner_report["plans"]} == {card["number"]}
    assert owner_report["count"] == 1
    assert not owner_report["stale"]
    assert {c["number"] for c in parent.work.needs_me()["stale"]} == {card["number"]}
    html = home(fresh, card)
    assert APPROVE in html, NO_HOME
    assert ANSWER not in html


def test_escalation_reason_survives_a_later_holder_handoff(fresh):
    coordinator = fresh.connect("coordinator")
    holder = fresh.connect("holder")
    fresh.owner.board_set("work", coordinator=coordinator.principal.actor)
    card = fresh.owner.task_add(
        board="work",
        title="Synthetic escalated plan",
        labels=["plan"],
        assignee=holder.principal.actor,
        claim=True,
    )
    coordinator.work.task_approve(card["number"], escalate=True, reason="Synthetic uncertainty")
    holder.work.task_handoff(card["number"], "anyone", "Synthetic queue handoff")

    shown = fresh.owner.task_show(card["number"])
    assert shown["events"][-1]["text"] == "Synthetic queue handoff"
    plans = fresh.owner.needs_me()["plans"]
    assert len(plans) == 1
    assert '"Synthetic uncertainty"' in plans[0]["why"]


def test_startup_logs_approval_failure_without_exception_text(hub, fresh, monkeypatch, caplog):
    parent = fresh.connect("parent")

    def unavailable(*args, **kwargs):
        raise RuntimeError("private synthetic exception text")

    monkeypatch.setattr(BoardService, "approvals_for_me", unavailable)
    with caplog.at_level(logging.ERROR, logger="commonplace.session"):
        text = start(hub, parent, monkeypatch)
    assert "Synthetic effective rules" in text
    assert "session approvals unavailable" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "private synthetic exception text" not in caplog.text


@pytest.mark.parametrize("trust", ["standard", "untrusted"])
def test_member_approval_reads_use_needs_me_gate(hub, fresh, monkeypatch, trust):
    member = hub.db.create_user("member@example.test")
    hub.db.add_member(fresh.workspace, member, "member", actor=fresh.user)
    parent = fresh.connect("member-parent", user_ref=member, trust=trust)
    child = fresh.connect("child", parent=parent.id, user_ref=member)
    pending = child.work.task_add(board="work", title="Synthetic gated proposal")
    held = parent.work.task_add(board="work", title="Synthetic held work", claim=True)
    if trust == "untrusted":
        fresh.owner.task_approve(held["number"], reason="Independent owner recovery")
        parent.work.task_take_next(task=held["number"])
    assert parent.principal.role == "member"
    calls = count_roles(monkeypatch)

    assert not parent.work.approvals_for_me()["items"]
    assert pending["title"] not in start(hub, parent, monkeypatch)
    ok, text = mcp(hub, parent.token, "task_heartbeat", task=held["number"])
    assert ok and pending["title"] not in text
    with pytest.raises(ToolError, match="workspace owner"):
        parent.work.needs_me()
    assert not calls
    assert {c["number"] for c in fresh.owner.needs_me()["approvals"]} == {pending["number"]}


def test_related_unapprovable_rows_hit_cap_and_preserve_owner_completeness(hub, fresh, monkeypatch):
    coordinator = fresh.connect("coordinator")
    fresh.owner.board_set("work", coordinator=coordinator.principal.actor)
    cards = [
        fresh.owner.task_add(
            board="work",
            title=f"Synthetic held plan {i}",
            labels=["plan"],
            assignee=coordinator.principal.actor,
            claim=True,
        )
        for i in range(205)
    ]
    calls = count_roles(monkeypatch)
    text = start(hub, coordinator, monkeypatch)
    assert 0 < len(calls) <= 60
    assert "list truncated" in text
    agent_report = coordinator.work.needs_me()
    assert agent_report["approvals_truncated"]
    owner_report = fresh.owner.needs_me()
    assert {c["number"] for c in owner_report["plans"]} == {c["number"] for c in cards}
    assert fresh.owner.needs_me(format="digest")["count"] == owner_report["count"] == len(cards)
    assert coordinator.work.needs_me(format="digest")["count"] == agent_report["count"]


def test_failed_parent_lookup_still_allows_coordinator_role(fresh):
    coordinator = fresh.connect("coordinator")
    fresh.owner.board_set("work", coordinator=coordinator.principal.actor)
    card = fresh.owner.task_add(board="work", title="Synthetic legacy routing", labels=["plan"])
    with coordinator.work.store.connection() as conn:
        row = dict(task_row(conn, card["number"]))
    row["proposed_to"] = coordinator.principal.actor
    assert coordinator.work._release_role(coordinator.principal, row) == "board coordinator"


def test_person_escalation_error_explains_agent_only_operation(fresh):
    card = fresh.owner.task_add(board="work", title="Synthetic owner plan", labels=["plan"])
    before = fresh.owner.task_show(card["number"])
    with pytest.raises(ToolError, match="only agents may escalate to the owner"):
        fresh.owner.task_approve(card["number"], escalate=True)
    assert fresh.owner.task_show(card["number"]) == before


def test_heartbeat_logs_approval_failure_without_exception_text(fresh, monkeypatch, caplog):
    holder = fresh.connect("holder")
    card = holder.work.task_add(board="work", title="Synthetic heartbeat", claim=True)
    before = holder.work.task_show(card["number"])

    def unavailable(*args, **kwargs):
        raise RuntimeError("private synthetic exception text")

    monkeypatch.setattr(BoardService, "approvals_for_me", unavailable)
    with caplog.at_level(logging.ERROR, logger="commonplace.boards.tools"):
        text = board_tools.run(holder.work.ctx, "task_heartbeat", {"task": card["number"]})
    assert text.startswith(f"ok #{card['number']} doing")
    assert "approvals waiting on you" not in text
    assert "heartbeat approvals unavailable" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "private synthetic exception text" not in text + caplog.text
    after = holder.work.task_show(card["number"])
    assert after["version"] == before["version"] + 1
    assert after["events"][-1]["kind"] == "renew"


def test_stuck_plan_without_supervisor_keeps_owner_recovery_actions(fresh):
    holder = fresh.connect("holder")
    card = fresh.owner.task_add(
        board="work",
        title="Synthetic unsupervised plan",
        labels=["plan"],
        assignee=holder.principal.actor,
        claim=True,
    )
    with holder.work.store.transaction() as conn:
        conn.execute(
            "UPDATE tasks SET claim_until='2000-01-01T00:00:00Z' WHERE id=?", (card["number"],)
        )

    assert fresh.owner.task_show(card["number"])["need"] == "you"
    report = fresh.owner.needs_me()
    assert not report["plans"]
    assert report["count"] == 1
    assert {c["number"] for c in report["stale"]} == {card["number"]}
    stale = report["stale"][0]
    assert stale["need"] == "you"
    assert stale["actions"] == ["requeue", "reassign", "park"]
    assert "claim that ran out" in stale["why"]
    html = home(fresh, card)
    assert all(f"need-{action}" in html for action in stale["actions"]), NO_HOME
    assert APPROVE not in html and "need-approve" not in html
    # The owner can still answer the holder: an Answer link to the card.
    assert f'href="#/card/{card["number"]}">Answer<' in html, NO_HOME
