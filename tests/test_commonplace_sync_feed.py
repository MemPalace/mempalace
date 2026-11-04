"""Content-free sync delivery, cadence display and ordinary-feed controls."""

import threading
from datetime import timedelta

from tests.commonplace_sync_fixtures import (
    T0,
    appended,
    hub as hub,
    raw,
    reached,
    records,
    reset_reached as reset_reached,
)


def test_events_sync_branch_live_long_poll(hub, monkeypatch):
    h = hub(plans="off")
    p = h.signup()
    h.enable_capture(p)
    token, _ = h.pat(p, client="claude-code")
    first = h.feed(token)
    assert first.status == 200  # COUNTED:F1
    reached("F1")
    from commonplace import sync_feed
    from commonplace.sync_protocol import SYNC_ENVELOPE_KEYS, SYNC_EVENT_KEYS

    assert set(first.json) == SYNC_ENVELOPE_KEYS
    assert first.json["state"] == {
        "capture_enabled": True,
        "interval_seconds": 0,
        "next_window_at": None,
    }
    s = first.json["seq"]
    registered = threading.Event()
    original_wait = threading.Condition.wait

    def wait(condition, timeout=None):
        if sync_feed.waiter_count():
            registered.set()
        return original_wait(condition, timeout)

    monkeypatch.setattr(threading.Condition, "wait", wait)
    job = h.client.background(lambda: h.feed(token, s, wait=5))
    assert registered.wait(3)
    assert sync_feed.waiter_count() == 1
    assert h.set_capture(p, False).status == 200
    answer = job.result()
    assert answer.status == 200
    assert answer.json["waited"] is True
    events = answer.json["events"]
    assert len(events) == 1 and set(events[0]) == SYNC_EVENT_KEYS
    assert events[0] == {
        "seq": s + 1,
        "at": T0.isoformat().replace("+00:00", "Z"),
        "kind": "sync.capture",
        "data": {"enabled": False},
    }
    repeated = h.feed(token, s)
    assert repeated.status == 200
    assert repeated.json["events"] == events
    assert repeated.json["seq"] == s + 1
    assert h.sync_now(p).status == 200
    requested = h.feed(token, s + 1)
    assert requested.status == 200
    assert [e["kind"] for e in requested.json["events"]] == ["sync.request"]
    assert h.feed(token, -1).status == 400
    assert h.feed(token, 0, wait=26).status == 400
    grant, _ = h.pat(p, import_=True)
    assert h.feed(grant).status == 200
    future = h.feed(token, s + 50)
    assert future.status == 200
    assert future.json["reset"] is True


def test_events_sync_free_no_wait_next_window(hub):
    h = hub()
    p = h.signup()
    h.enable_capture(p)
    token, _ = h.pat(p, client="claude-code")
    first = h.feed(token, wait=5)
    assert first.status == 200  # COUNTED:F2
    reached("F2")
    assert first.json["waited"] is False
    assert first.json["state"]["interval_seconds"] == 900
    assert first.json["state"]["next_window_at"] == T0.isoformat().replace("+00:00", "Z")
    appended(h.push(token, "claude-code", "S", raw(records("S"))))
    current = h.feed(token)
    assert current.status == 200
    assert current.json["state"]["next_window_at"] == T0.isoformat().replace("+00:00", "Z")
    h.at(61)
    later = h.feed(token)
    assert later.status == 200
    assert later.json["state"]["next_window_at"] == (
        T0 + timedelta(seconds=900)
    ).isoformat().replace("+00:00", "Z")
    owner = h.feed(p)
    assert owner.status == 200
    assert owner.json["state"] == later.json["state"]


def test_events_ordinary_feed_and_scoping_unchanged(hub):
    h = hub()
    p = h.signup()
    default, _ = h.pat(p)
    assert h.feed(default).status == 403
    oauth = h.oauth(p, tick=False)
    assert h.feed(oauth).status == 403
    capture, _ = h.pat(p, client="claude-code")
    ordinary = h.feed(capture, sync=False)
    assert ordinary.status == 403
    assert ordinary.json == {"error": "Workspace event access required"}
    assert h.set_capture(p, False).status == 200
    assert h.set_capture(p, True).status == 200
    owner = h.feed(p, sync=False)
    assert owner.status == 200
    assert set(owner.json) == {"events", "seq"}
    assert all(not e["kind"].startswith("sync.") for e in owner.json["events"])
