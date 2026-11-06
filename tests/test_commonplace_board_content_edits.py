"""Content edits must not release restricted work or grant claim authority."""

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api, mcp


def unclaimed_restricted_card(fresh):
    creator = fresh.connect("creator", trust="untrusted", write_wing="inbox")
    card = creator.work.task_add(
        board="work", title="Original title", body="Original body", claim=True
    )
    creator.work.task_handoff(card["number"], "anyone", "Available for coordination")
    current = fresh.owner.task_show(card["number"])
    assert current["status"] == "todo" and current["assignee"] is None
    assert current["restricted"] and current["approved_by"] is None
    return current


def editor(fresh, authority):
    who = fresh.connect("editor", trust="standard" if authority == "coordinator" else "trusted")
    if authority == "coordinator":
        fresh.owner.board_set("work", who.principal.actor)
    return who


@pytest.mark.parametrize("authority", ["trusted", "coordinator"])
@pytest.mark.parametrize("route", ["mcp", "api"])
@pytest.mark.parametrize("fields", [("body",), ("title",), ("title", "body")])
def test_authorized_content_edit_is_audited_without_releasing_origin(
    fresh, hub, authority, route, fields
):
    before = unclaimed_restricted_card(fresh)
    who = editor(fresh, authority)
    updates = {field: f"  Exact {field}\nowner-requested addition\n" for field in fields}
    if route == "mcp":
        ok, reply = mcp(hub, who.token, "task_update", task=before["number"], **updates)
        assert ok, reply
    else:
        status, reply = api(
            hub,
            who.token,
            "PATCH",
            f"/api/cards/{before['number']}",
            {"version": before["version"], **updates},
        )
        assert status == 200, reply
    after = fresh.owner.task_show(before["number"])
    for field in ("title", "body"):
        assert after[field] == updates.get(field, before[field])
    for field in ("status", "assignee", "claim_until", "restricted", "approved_by", "created_by"):
        assert after[field] == before[field]
    assert after["version"] == before["version"] + 1
    assert len(after["events"]) == len(before["events"]) + 1
    assert after["events"][-1]["kind"] == "edit"
    assert after["events"][-1]["actor"] == who.principal.actor
    with fresh.owner.store.connection() as conn:
        event = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (before["number"],),
        ).fetchone()
        assert event["stamp_user"] == fresh.user
        assert event["stamp_connection"] == who.principal.connection
        assert event["stamp_trust"] == who.principal.policy.trust
        assert event["from_status"] == event["to_status"] == "todo"


@pytest.mark.parametrize(
    "trust,wing", [("standard", "shared"), ("untrusted", "shared"), ("trusted", "inbox")]
)
def test_other_connections_cannot_edit_restricted_content(fresh, hub, trust, wing):
    before = unclaimed_restricted_card(fresh)
    who = fresh.connect("other", trust=trust, write_wing=wing)
    for field in ("body", "title"):
        ok, reply = mcp(hub, who.token, "task_update", task=before["number"], **{field: "Refused"})
        assert not ok, reply
        assert fresh.owner.task_show(before["number"]) == before


@pytest.mark.parametrize("authority", ["trusted", "coordinator"])
def test_content_edit_exception_does_not_allow_other_updates(fresh, hub, authority):
    before = unclaimed_restricted_card(fresh)
    who = editor(fresh, authority)
    for extra in (
        {"status": "doing"},
        {"status": "todo"},
        {"status": "backlog"},
        {"status": "done"},
        {"labels": ["changed"]},
        {"status_line": "Changed"},
        {"checks": "passing"},
        {"repo": "example/project"},
        {"branch": "changed"},
        {"before": before["number"]},
    ):
        ok, reply = mcp(
            hub, who.token, "task_update", task=before["number"], body="Refused", **extra
        )
        assert not ok, reply
        assert fresh.owner.task_show(before["number"]) == before


@pytest.mark.parametrize("authority", ["trusted", "coordinator"])
@pytest.mark.parametrize("state", ["reserved", "doing", "backlog", "proposal"])
def test_content_edit_exception_keeps_other_card_guards(fresh, hub, authority, state):
    before = unclaimed_restricted_card(fresh)
    who = editor(fresh, authority)
    # Existing persisted states, without granting an origin release as an owner move would.
    with fresh.owner.store.transaction() as conn:
        if state in ("reserved", "doing"):
            conn.execute(
                "UPDATE tasks SET assignee=? WHERE id=?", (before["created_by"], before["number"])
            )
        if state in ("doing", "backlog"):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (state, before["number"]))
        if state == "proposal":
            conn.execute(
                "UPDATE tasks SET proposal_status='pending' WHERE id=?", (before["number"],)
            )
    before = fresh.owner.task_show(before["number"])
    ok, reply = mcp(hub, who.token, "task_update", task=before["number"], body="Refused")
    assert not ok, reply
    assert fresh.owner.task_show(before["number"]) == before
