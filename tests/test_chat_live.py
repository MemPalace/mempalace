"""Live chat: delivery to the board's orchestrator and the states the poster sees."""

import time

import pytest

from commonplace import sessions
from commonplace.chat import live
from commonplace.chat.types import ACK_WAIT_S
from tests._chat_helpers import (
    chat_environment,  # noqa: F401
    chat_hub_fixture,  # noqa: F401
    no_outbound,
)
from tests._console_helpers import successful
from tests.test_commonplace_boards import service


def state(h, auth):
    status, body, _ = h.chat(auth)
    assert status == 200, body
    return body["live"]


def deliveries(h, message):
    with h.db._lock:
        return [
            dict(row)
            for row in h.db._conn.execute(
                "SELECT d.id,d.status,w.owner_actor FROM webhook_deliveries d "
                "JOIN webhooks w ON w.id=d.webhook_id JOIN chat_live_posts p "
                "ON p.event_seq=d.event_seq WHERE p.workspace_id=? AND p.message_id=?",
                (h.workspace, message),
            )
        ]


def settle_wake(h, message, status):
    """Fix the wake's outcome; the dispatcher only rewrites pending and retry rows."""
    rows = deliveries(h, message)
    assert len(rows) == 1
    with h.db._lock:
        h.db._conn.execute(
            "UPDATE webhook_deliveries SET status=? WHERE id=?", (status, rows[0]["id"])
        )


def later(monkeypatch, seconds):
    real = time.time
    monkeypatch.setattr(live.time, "time", lambda: real() + seconds)


def test_no_message_is_idle(chat_hub):
    h = chat_hub
    h.set_coordinator("a1")
    assert state(h, h.owner) == {
        "message_id": None,
        "state": "idle",
        "reason": None,
        "agent": "a1",
        "at": None,
        "seen_at": None,
        "answer_id": None,
    }


def test_message_is_delivered_to_the_orchestrator_at_once(chat_hub, monkeypatch):
    h = chat_hub
    outbound = no_outbound(monkeypatch)
    h.set_coordinator("a1")
    h.subscribe(h.t1)
    message = h.post(h.owner, "Please fix the login bug")
    # The wake is queued for the orchestrator before the poster's next read.
    queued = deliveries(h, message)
    assert [row["owner_actor"] for row in queued] == ["conn:a1"]
    first = state(h, h.owner)
    assert (first["message_id"], first["state"], first["agent"]) == (message, "delivered", "a1")
    assert first["reason"] in ("wake_pending", "wake_retrying", "wake_sent")
    settle_wake(h, message, "delivered")
    assert state(h, h.owner)["reason"] == "wake_sent"
    # The orchestrator gets the exact words through its own read.
    page = service(h, h.principal(h.t1)).space_read("work", after=message - 1)
    assert page["messages"][0]["text"] == "Please fix the login bug"
    assert outbound == []


@pytest.mark.parametrize("case", ["none", "gone"])
def test_no_agent_connected(chat_hub, case):
    h = chat_hub
    if case == "gone":
        h.set_coordinator("a1")
        h.db.revoke(h.a1, actor=h.admin)
    message = h.post(h.owner, "hello")
    current = state(h, h.owner)
    assert (current["message_id"], current["state"], current["reason"]) == (
        message,
        "no_agent",
        case,
    )
    assert deliveries(h, message) == []


def test_without_a_wake_hook_the_message_waits_for_the_next_check(chat_hub, monkeypatch):
    h = chat_hub
    h.set_coordinator("a1")
    h.post(h.owner, "hello")
    assert (state(h, h.owner)["state"], state(h, h.owner)["reason"]) == ("delivered", "wake_none")
    # No hook means the agent polls, so one minute of silence is not yet "no answer".
    later(monkeypatch, live.PICKUP_WAIT_S + 5)
    assert state(h, h.owner)["state"] == "delivered"
    later(monkeypatch, ACK_WAIT_S + 5)
    current = state(h, h.owner)
    assert (current["state"], current["reason"]) == ("no_answer", "not_picked_up")


def test_working_once_the_orchestrator_reads_and_answer_arrives_without_reload(chat_hub):
    h = chat_hub
    h.set_coordinator("a1")
    h.subscribe(h.t1)
    message = h.post(h.owner, "Please fix the login bug")
    opened = h.chat(h.owner)[1]
    assert opened["live"]["state"] == "delivered"
    # Another agent or a person reading does not count as the orchestrator picking it up.
    successful(h, h.t2, "space_read", board="work", last=5)
    service(h, h.person(h.member_user)).space_read("work", last=5)
    assert state(h, h.owner)["state"] == "delivered"
    successful(h, h.t1, "space_read", board="work", after=message - 1)
    working = state(h, h.owner)
    assert (working["state"], working["message_id"]) == ("working", message)
    assert working["seen_at"]
    successful(h, h.t1, "space_post", board="work", text="On it: the login bug is card 7.")
    # The app's poll (same cursors as the open pane) carries the reply and the new state.
    poll = h.chat(h.owner, after=opened["cursor"], replies_after=opened["reply_cursor"])[1]
    assert [m["text"] for m in poll["messages"]] == ["On it: the login bug is card 7."]
    assert poll["messages"][0]["actor"] == "conn:a1"
    assert (poll["live"]["state"], poll["live"]["answer_id"]) == (
        "answered",
        poll["messages"][0]["id"],
    )


def test_read_cursor_never_moves_back(chat_hub):
    h = chat_hub
    h.set_coordinator("a1")
    first = h.post(h.owner, "one")
    second = h.post(h.owner, "two")
    agent = service(h, h.principal(h.t1))
    agent.space_read("work", after=first)
    agent.space_read("work", before=second, last=1)
    assert live._read(h.db, h.workspace, "work", "conn:a1")["message_id"] == second
    assert state(h, h.owner)["state"] == "working"


@pytest.mark.parametrize(
    "case,reason",
    [("woken", "not_picked_up"), ("wake_dead", "wake_failed"), ("read", "no_reply")],
)
def test_agent_did_not_answer(chat_hub, monkeypatch, case, reason):
    h = chat_hub
    h.set_coordinator("a1")
    h.subscribe(h.t1)
    message = h.post(h.owner, "Please fix the login bug")
    if case == "woken":
        settle_wake(h, message, "delivered")
        assert state(h, h.owner)["state"] == "delivered"
        later(monkeypatch, live.PICKUP_WAIT_S + 5)
    elif case == "wake_dead":
        settle_wake(h, message, "dead")
    else:
        successful(h, h.t1, "space_read", board="work", after=message - 1)
        later(monkeypatch, live.PICKUP_WAIT_S + 5)
        assert state(h, h.owner)["state"] == "working"
        later(monkeypatch, ACK_WAIT_S + 5)
    current = state(h, h.owner)
    assert (current["state"], current["reason"], current["message_id"]) == (
        "no_answer",
        reason,
        message,
    )
    # A late answer still ends the state.
    successful(h, h.t1, "space_post", board="work", text="Sorry, here now.")
    assert state(h, h.owner)["state"] == "answered"


def test_wake_follows_the_orchestrator_of_the_post(chat_hub):
    h = chat_hub
    h.set_coordinator("a1")
    h.subscribe(h.t1)
    message = h.post(h.owner, "hello")
    settle_wake(h, message, "delivered")
    assert live._wake(h.db, h.workspace, "work", message, "conn:a1") == "sent"
    # A newly chosen orchestrator was never woken for the earlier message.
    h.set_coordinator("a2")
    current = state(h, h.owner)
    assert (current["agent"], current["state"], current["reason"]) == (
        "a2",
        "delivered",
        "wake_none",
    )


def test_member_and_owner_each_see_only_their_own_message(chat_hub):
    h = chat_hub
    h.set_coordinator("a1")
    h.subscribe(h.t1)
    assert state(h, h.member)["state"] == "idle"
    mine = h.post(h.member, "hello from a member")
    member = state(h, h.member)
    assert (member["message_id"], member["state"]) == (mine, "delivered")
    # The owner has not written: the member's delivery state is not theirs to see.
    assert state(h, h.owner)["message_id"] is None
    theirs = h.post(h.owner, "hello from the owner")
    successful(h, h.t1, "space_read", board="work", after=0)
    owner, member = state(h, h.owner), state(h, h.member)
    assert (owner["message_id"], owner["state"]) == (theirs, "working")
    assert (member["message_id"], member["state"]) == (mine, "working")
    # The owner may see this agent's activity; the member sees the state, not the time.
    assert owner["seen_at"]
    assert member["seen_at"] is None
    assert h.chat(h.member)[1]["orchestrator"]["visible"] is False
    # A member cannot connect an agent, so "no agent" must not depend on the role either.
    h.db.revoke(h.a1, actor=h.admin)
    assert state(h, h.member)["state"] == state(h, h.owner)["state"] == "no_agent"


def test_nothing_crosses_into_another_workspace(chat_hub):
    h = chat_hub
    other = sessions.person_principal(h.db, h.other_user, h.other_workspace)
    service(h, other).board_add(slug="work", wing="shared")
    status, body, _ = h.request(h.other, "POST", "/api/boards/work/chat", {"text": "other words"})
    assert status == 201, body
    theirs = body["id"]

    h.set_coordinator("a1")
    h.subscribe(h.t1)
    mine = h.post(h.owner, "first workspace words")
    assert mine == theirs  # same board slug and message id in both workspaces
    successful(h, h.t1, "space_read", board="work", after=0)
    successful(h, h.t1, "space_post", board="work", text="answer for the first workspace")
    assert state(h, h.owner)["state"] == "answered"

    page = h.chat(h.other)[1]
    assert [m["text"] for m in page["messages"]] == ["other words"]
    assert page["replies"] and all(r["message_id"] == theirs for r in page["replies"])
    assert "first workspace" not in str(page)
    assert (page["live"]["message_id"], page["live"]["state"], page["live"]["agent"]) == (
        theirs,
        "no_agent",
        None,
    )
    # The read cursor and the wake belong to the first workspace only.
    assert live._read(h.db, h.other_workspace, "work", "conn:a1") is None
    assert live._wake(h.db, h.other_workspace, "work", theirs, "conn:a1") == "none"
    # And the first workspace's person cannot read the other chat at all.
    assert "other words" not in str(h.chat(h.owner)[1])
    assert "other words" not in str(h.chat(h.member)[1])


def test_live_state_never_calls_out(chat_hub, monkeypatch):
    h = chat_hub
    outbound = no_outbound(monkeypatch)
    calls = h.fake_model(monkeypatch)
    h.set_coordinator("a1")
    message = h.post(h.owner, "hello")
    successful(h, h.t1, "space_read", board="work", after=message - 1)
    assert state(h, h.owner)["state"] == "working"
    h.drain()
    assert calls == [] and outbound == []
    # Only ids and times are recorded, never the words.
    with h.db._lock:
        rows = [
            tuple(row)
            for table in ("chat_live_posts", "chat_live_reads")
            for row in h.db._conn.execute(f"SELECT * FROM {table}")
        ]
    assert rows and "hello" not in str(rows)
