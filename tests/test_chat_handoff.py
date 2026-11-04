"""A15–A16: deterministic handoff deadlines, claims and crash deduplication."""

from datetime import datetime, timezone

import pytest

from commonplace import events
from tests._chat_helpers import (
    chat_environment,  # noqa: F401
    chat_hub_fixture,  # noqa: F401
    optional,
    successful,
)


@pytest.mark.parametrize(
    "case", ["unclaimed", "member_no_orchestrator", "reply_claims", "card_claims", "not_work"]
)
def test_unclaimed_work_request_is_filed_as_card(chat_hub, case):
    h = chat_hub
    if case not in {"member_no_orchestrator", "not_work"}:
        h.set_coordinator("a1")
        h.subscribe(h.t1)
    poster = h.member if case == "member_no_orchestrator" else h.owner
    user = h.member_user if case == "member_no_orchestrator" else h.admin
    text = "thanks!" if case == "not_work" else "Please fix the login bug\nsteps…"
    message = h.post(poster, text)
    status, _, _ = h.chat(poster)
    assert status == 200  # RED
    claimed_task = None
    if case == "reply_claims":
        successful(h, h.t1, "space_post", board="work", text="I will take this.")
    elif case == "card_claims":
        claimed_task = successful(
            h, h.t1, "task_add", board="work", title="Login fix", message_id=message
        )["number"]
    if case == "not_work":
        for text in (
            "good morning",
            "thank you so much",
            "status",
            "is the login fix done?",
            "any news?",
            "👍",
            "!!!",
        ):
            h.post(poster, text)
    seq = h.seq()
    h.drain(now=h.late())
    cards = h.cards(message)
    notices = [r for r in h.chat(poster)[1]["replies"] if r["kind"] == "handoff"]
    if case in {"unclaimed", "member_no_orchestrator"}:
        assert len(cards) == 1
        card = cards[0]
        assert card["title"] == "Please fix the login bug"
        assert card["body"] == text
        assert card["status"] == "todo" and card["assignee"] is None
        assert card["created_by"] == "user:" + user
        assert card["message_id"] == message
        assert len(notices) == 1 and notices[0]["code"] == "filed"
        assert notices[0]["task"] == card["number"]
        assert f"#{card['number']}" in notices[0]["text"]
        assert h.events_since(seq) == ["card.created"]
        if case == "member_no_orchestrator":
            assert h.chat(h.owner)[1]["replies"] == []
    elif case == "not_work":
        assert cards == [] and h.cards() == []
        assert h.handoffs() == [] and notices == []
    else:
        assert notices == []
        handoff = h.handoffs()[0]
        assert handoff["state"] == "claimed"
        if claimed_task is None:
            assert cards == []
        else:
            assert len(cards) == 1 and cards[0]["number"] == claimed_task
            assert handoff["task"] == claimed_task
    assert h.runner_jobs_count() == 0


def test_one_card_per_message(chat_hub):
    h = chat_hub
    seq_before = h.seq()
    message = h.post(h.owner, "\nPlease fix the login bug\nKeep the whole body.")
    status, _, _ = h.chat(h.owner)
    assert status == 200  # RED
    original = next(
        e
        for e in events.after(h.db, h.workspace, seq_before, h.seq())
        if e["kind"] == "space_message"
    )
    front = optional("commonplace.chat.front")
    front.on_event(h.gateway, original)
    assert len(h.handoffs()) == 1
    assert len([r for r in h.chat(h.owner)[1]["replies"] if r["kind"] == "receipt"]) == 1
    h.drain(now=h.late())
    h.drain(now=h.late())
    cards = h.cards(message)
    assert len(cards) == 1
    task = cards[0]["number"]
    assert cards[0]["title"] == "Please fix the login bug"
    assert len([r for r in h.chat(h.owner)[1]["replies"] if r["kind"] == "handoff"]) == 1
    types = optional("commonplace.chat.types")
    now = h.late()
    stale = datetime.fromtimestamp(now - types.STALE_AFTER_S - 1, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with h.db._lock:
        h.db._conn.execute(
            "UPDATE chat_handoffs SET state='filing', updated_at=? WHERE message_id=?",
            (stale, message),
        )
    optional("commonplace.chat.handoff").recover(h.db, h.gateway, now)
    row = h.handoffs()[0]
    assert (row["state"], row["task"]) == ("filed", task)
    worker = optional("commonplace.chat.worker").FrontWorker(h.gateway, start=False)
    try:
        worker.drain(now=now)
    finally:
        worker.stop()
    assert len(h.cards(message)) == 1
    assert len(h.handoffs()) == 1
    assert len([r for r in h.chat(h.owner)[1]["replies"] if r["kind"] == "handoff"]) == 1
