"""Head-only correctness units; synthetic ControlDB setup is not route proof."""

import pytest

from commonplace.boards.display import merge_ready, need, need_for
from commonplace.boards.service import _stuck
from commonplace.boards.store import task_row
from commonplace.extensions import ALL_PROFILES, ToolError
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_needs_me_human_only import (
    PR,
    SHA1,
    SHA2,
    absent,
    clock,
    holding,
    listed,
    mine,
    owner_items,
    report,
    shown,
)

CUTOFF = "2026-11-01T12:00:00Z"
FRESH = "2026-11-01T12:01:00Z"


@pytest.mark.parametrize(
    "fields,message",
    [
        ({"pr_head": "abc"}, "no: pr_head must be a full lowercase commit SHA"),
        ({"pr_head": "A" * 40}, "no: pr_head must be a full lowercase commit SHA"),
        ({"pr_head": SHA1 + "\n"}, "no: pr_head must be a full lowercase commit SHA"),
        ({"pr_head": None}, "no: pr_head must be a full lowercase commit SHA"),
        ({"pr_state": "unknown"}, "no: pr_state must be open, merged or closed"),
        ({"pr_state": None}, "no: pr_state must be open, merged or closed"),
    ],
)
def test_invalid_pr_report_fields_exact_messages(hub, fresh, fields, message):
    agent = fresh.connect("worker")
    number = holding(fresh, agent)["number"]
    before = shown(hub, fresh, number)
    with pytest.raises(ToolError) as refused:
        agent.work.task_update(number, **fields)
    assert str(refused.value) == message
    assert shown(hub, fresh, number) == before


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({}, True),
        ({"pr_head": "a" * 64}, True),
        ({"status": "review"}, True),
        ({"status": "todo"}, False),
        ({"status": "done"}, False),
        ({"pr_state": None}, False),
        ({"pr_state": "closed"}, False),
        ({"pr_state": "merged"}, False),
        ({"pr_head": None}, False),
        ({"pr_head": "a" * 41}, False),
        ({"pr_head": "A" * 40}, False),
        ({"pr_head": SHA1 + "\n"}, False),
        ({"pr_url": None}, False),
        ({"checks": "pending"}, False),
        ({"pr_reported_at": None}, False),
        ({"pr_reported_at": CUTOFF}, False),
    ],
)
def test_merge_ready_new_report_states(fields, expected):
    card = dict(
        status="doing",
        pr_url=PR,
        pr_state="open",
        pr_head=SHA1,
        checks="passing",
        pr_reported_at=FRESH,
    )
    assert merge_ready({**card, **fields}, CUTOFF) is expected


@pytest.mark.parametrize("release,expected", [(None, "you"), (True, "you"), (False, "plan")])
def test_need_person_release_boolean(fresh, release, expected):
    card = dict(status="backlog", proposal_status="pending", proposed_to="conn:parent")
    assert need(card, owner=True, person_release=release) == expected
    assert need_for(card, fresh.owner.principal, person_release=release) == expected
    assert need(card, me="conn:parent", person_release=False) == "you"


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({}, True),
        ({"assignee": None}, False),
        ({"assignee": "user:person"}, False),
        ({"claim_until": FRESH}, False),
        ({"claim_until": None, "heartbeat_at": CUTOFF}, True),
        ({"claim_until": None, "heartbeat_at": None}, True),
        ({"ask": "Waiting for a person"}, False),
        ({"ask_to": "user:person"}, False),
        ({"status": "todo"}, False),
    ],
)
def test_stuck_predicate_boundaries(fields, expected):
    row = dict(
        status="doing",
        assignee="conn:holder",
        claim_until=CUTOFF,
        heartbeat_at=FRESH,
        updated_at=CUTOFF,
        ask=None,
        ask_to=None,
        return_to=None,
    )
    assert _stuck({**row, **fields}, CUTOFF) is expected


def test_supervisor_order_and_dedup(hub, fresh):
    root = fresh.connect("root")
    parent = fresh.connect("parent", parent=root.id)
    holder = fresh.connect("holder", parent=parent.id)
    card = holding(fresh, holder)
    fresh.owner.board_set("work", coordinator=root.principal.actor)
    with fresh.owner.store.connection() as conn:
        row = task_row(conn, card["number"])
    assert fresh.owner._supervisors(row) == (
        (parent.principal.actor, "parent"),
        (root.principal.actor, "top-level agent"),
    )
    keeper = fresh.connect("coordinator")
    fresh.owner.board_set("work", coordinator=keeper.principal.actor)
    with fresh.owner.store.connection() as conn:
        row = task_row(conn, card["number"])
    assert fresh.owner._supervisors(row) == (
        (parent.principal.actor, "parent"),
        (root.principal.actor, "top-level agent"),
        (keeper.principal.actor, "board coordinator"),
    )


def test_untrusted_parent_with_gates_supervises(hub, fresh, monkeypatch):
    """Synthetic non-admin connection setup; default profile stays ALL_PROFILES."""
    assert "standard" in ALL_PROFILES
    parent = fresh.connect("parent", trust="untrusted", scopes=["memory:read", "memory:write"])
    holder = fresh.connect("holder", parent=parent.id)
    card = holding(fresh, holder)
    clock(monkeypatch, card["claim_until"])
    assert card["number"] in mine(hub, parent)
    absent(owner_items(hub, fresh), card["number"])


@pytest.mark.parametrize("condition", ["disabled", "missing_membership"])
def test_inactive_supervisor_is_dropped_synthetic(hub, fresh, monkeypatch, condition):
    """Head-only synthetic ControlDB writes; no public-route proof is claimed."""
    user = hub.db.create_user("parent-member@example.test")
    hub.db.add_member(fresh.workspace, user, "member", actor=fresh.user)
    parent = fresh.connect("parent", user_ref=user)
    holder = fresh.connect("holder", parent=parent.id)
    card = holding(fresh, holder)
    clock(monkeypatch, card["claim_until"])
    with fresh.owner.store.connection() as conn:
        row = task_row(conn, card["number"])
    assert fresh.owner._supervisors(row) == ((parent.principal.actor, "parent"),)
    with hub.db._lock:
        if condition == "disabled":
            hub.db._conn.execute("UPDATE users SET disabled_at=? WHERE id=?", (CUTOFF, user))
        else:
            # Synthetic orphan membership; preserve the connection and parent link.
            hub.db._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                hub.db._conn.execute(
                    "DELETE FROM memberships WHERE workspace_id=? AND user_id=?",
                    (fresh.workspace, user),
                )
            finally:
                hub.db._conn.execute("PRAGMA foreign_keys=ON")
            assert hub.db.connection(parent.id, fresh.workspace) is not None
            assert hub.db.authenticate_connection(parent.id) is None
    assert fresh.owner._supervisors(row) == ()
    listed(owner_items(hub, fresh), "stale", card["number"])


@pytest.mark.parametrize("missing_tool", ["ask", "task_list"])
def test_supervisor_needs_both_notification_and_escalation_tools(
    hub, fresh, monkeypatch, missing_tool
):
    parent = fresh.connect("parent")
    tools = sorted(parent.principal.policy.tool_names() - {missing_tool})
    limited = fresh.connect("limited", tools=tools)
    holder = fresh.connect("holder", parent=limited.id)
    card = holding(fresh, holder)
    clock(monkeypatch, card["claim_until"])
    listed(owner_items(hub, fresh), "stale", card["number"])


def test_checks_only_preserves_report_time_and_expiry(hub, fresh, monkeypatch):
    agent = fresh.connect("worker")
    number = holding(fresh, agent)["number"]
    original = report(agent, number)
    changed = agent.work.task_update(number, checks="failing")
    assert changed["pr_reported_at"] == original["pr_reported_at"]
    changed = agent.work.task_update(number, checks="passing")
    assert changed["pr_reported_at"] == original["pr_reported_at"]
    clock(monkeypatch, original["pr_reported_at"])
    assert shown(hub, fresh, number)["need"] != "merge"
    # An explicit state-only holder report may renew already-reported checks.
    renewed = agent.work.task_update(number, pr_state="open")
    assert renewed["checks"] == "passing" and renewed["pr_reported_at"]


def test_same_call_new_pr_report_wins_over_resets(hub, fresh):
    agent = fresh.connect("worker")
    number = holding(fresh, agent)["number"]
    report(agent, number)
    changed = report(agent, number, pr="https://example.test/o/r/pull/8", pr_head=SHA2)
    assert changed["pr_state"] == "open" and changed["pr_head"] == SHA2
    assert changed["checks"] == "passing" and changed["pr_reported_at"]


@pytest.mark.parametrize("writer", ["owner", "holder"])
def test_noop_doing_preserves_holder_report(hub, fresh, writer):
    holder = fresh.connect("holder")
    number = holding(fresh, holder)["number"]
    original = report(holder, number)
    service = fresh.owner if writer == "owner" else holder.work
    changed = service.task_update(number, status="doing")
    for key in ("assignee", "pr_state", "pr_head", "pr_reported_at", "checks"):
        assert changed[key] == original[key]


@pytest.mark.parametrize("person", [True, False])
def test_user_addressed_proposal_wording_unit(monkeypatch, person):
    """Pure synthetic row: public creation cannot address a proposal to a user."""
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import Mock

    from commonplace.boards import service

    row = dict(
        status="backlog",
        proposal_status="pending",
        proposed_to="user:owner",
        ask_to=None,
        created_by="conn:proposer",
        board_wing="work",
    )
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = [row]
    store = SimpleNamespace(connection=lambda: nullcontext(conn))
    principal = SimpleNamespace(person=person, user_id="owner", actor="conn:viewer", policy=Mock())
    monkeypatch.setattr(service, "_owner", lambda *a, **kw: None)
    monkeypatch.setattr(service, "_stuck", lambda *a: False)
    monkeypatch.setattr(service, "card", lambda *a, **kw: {"number": 1})
    item = service.needs_me(store, principal)["backlog_from_agents"][0]
    if person:
        assert item["why"] == "proposer proposed this to you. Approve or reject it."
    else:
        assert (
            item["why"]
            == "proposer proposed this to the owner. The owner must approve or reject it."
        )
        assert "to you" not in item["why"]
        assert "Approve or reject it." not in item["why"]
        assert item["actions"] == []
