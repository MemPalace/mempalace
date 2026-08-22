"""MCP-side wiring of the writer-lease handoff.

The palace layer (tests/test_palace_lock_handoff.py) proves the baton protocol
itself. These tests cover the server's use of it: when the watchdog is allowed
to give the lease away, when it must refuse, how a contender waits for it, and
what ``mempalace_status`` reports about ownership.
"""

from __future__ import annotations

import threading
import time

import pytest

import mempalace.palace as palace_mod
from mempalace import mcp_server


class _DummyLease:
    """Stands in for the entered ``mine_palace_lock`` context manager."""

    def __init__(self):
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False


@pytest.fixture
def leased(tmp_path, monkeypatch):
    """A server that holds the lease on an isolated palace, handoff enabled."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(mcp_server._MCP_WRITER_HANDOFF_DISABLED_ENV, raising=False)
    monkeypatch.delenv(mcp_server._MCP_WRITER_HANDOFF_WAIT_ENV, raising=False)
    monkeypatch.delenv(mcp_server._MCP_WRITER_MIN_HOLD_ENV, raising=False)
    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    monkeypatch.setattr(
        type(mcp_server._config), "palace_path", property(lambda self: str(palace_path))
    )
    monkeypatch.setattr(mcp_server, "_discard_mcp_storage_handles", lambda: None)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_HANDOFF_BACKOFF_UNTIL", 0.0)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_HANDOFFS_GRANTED", 0)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_HANDOFFS_TAKEN", 0)

    lease = _DummyLease()
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", lease)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_READ_ONLY", False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LEASE_SINCE", time.monotonic() - 3600)
    # The dummy lease is not a real palace lock, so tell the palace layer this
    # process owns the key — that is what begin_palace_lock_handoff checks.
    key = palace_mod.palace_lock_key(str(palace_path))
    with palace_mod._palace_lock_guard:
        palace_mod._holder_keys_locked().add(key)
    yield lease
    with palace_mod._palace_lock_guard:
        palace_mod._holder_keys_locked().discard(key)
        palace_mod._palace_lock_depth.pop(key, None)
        palace_mod._palace_lock_handoff.discard(key)


def test_default_wait_outlasts_the_default_hold():
    """The defaults are a system, not three independent numbers.

    A contender must be willing to wait longer than the holder's floor plus one
    probe interval. If a future bump to the minimum hold crosses the wait, every
    handoff silently times out and the feature quietly stops working — with all
    other tests still green, because they set the knobs explicitly.
    """
    assert (
        mcp_server._MCP_WRITER_HANDOFF_WAIT_DEFAULT
        > mcp_server._MCP_WRITER_MIN_HOLD_DEFAULT + mcp_server._MCP_WRITER_HANDOFF_POLL_DEFAULT
    )


def test_handoff_releases_the_lease_when_a_peer_is_queued(leased, monkeypatch):
    monkeypatch.setattr(palace_mod, "palace_lock_wanted", lambda path: True)
    discarded = []
    monkeypatch.setattr(mcp_server, "_discard_mcp_storage_handles", lambda: discarded.append(1))

    assert mcp_server._maybe_hand_off_writer_lease() is True
    assert leased.exited is True
    assert mcp_server._MCP_WRITER_LOCK_CM is None
    assert mcp_server._MCP_WRITER_READ_ONLY is True
    assert discarded, "storage handles must be closed before releasing the lock"
    assert mcp_server._MCP_WRITER_HANDOFFS_GRANTED == 1


def test_no_handoff_when_nobody_is_waiting(leased, monkeypatch):
    monkeypatch.setattr(palace_mod, "palace_lock_wanted", lambda path: False)

    assert mcp_server._maybe_hand_off_writer_lease() is False
    assert leased.exited is False
    assert mcp_server._MCP_WRITER_LOCK_CM is leased


def test_minimum_hold_prevents_lease_ping_pong(leased, monkeypatch):
    """Reopening storage costs real time; two chatty sessions must not thrash."""
    monkeypatch.setattr(palace_mod, "palace_lock_wanted", lambda path: True)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LEASE_SINCE", time.monotonic())
    monkeypatch.setenv(mcp_server._MCP_WRITER_MIN_HOLD_ENV, "60")

    assert mcp_server._maybe_hand_off_writer_lease() is False
    assert leased.exited is False

    # Same demand, but the floor has passed.
    monkeypatch.setenv(mcp_server._MCP_WRITER_MIN_HOLD_ENV, "0")
    assert mcp_server._maybe_hand_off_writer_lease() is True


def test_no_handoff_while_a_request_is_being_dispatched(leased, monkeypatch):
    """Reads serve from cached handles without the palace lock — closing those
    mid-request would be a use-after-close, so the dispatch lock gates it."""
    monkeypatch.setattr(palace_mod, "palace_lock_wanted", lambda path: True)

    holding = threading.Event()
    release = threading.Event()

    def dispatcher():
        with mcp_server._REQUEST_DISPATCH_LOCK:
            holding.set()
            release.wait(timeout=10)

    t = threading.Thread(target=dispatcher)
    t.start()
    try:
        assert holding.wait(timeout=5)
        assert mcp_server._maybe_hand_off_writer_lease() is False
        assert leased.exited is False
    finally:
        release.set()
        t.join(timeout=10)

    # Request finished — the next watchdog tick may hand off.
    assert mcp_server._maybe_hand_off_writer_lease() is True


def _handoff_verdict_while_dispatching(monkeypatch, tool_name: str) -> bool:
    """Run one request through _dispatch_locally and probe the handoff.

    The probe runs on the *main* thread while the request is in flight on
    another, because that is the real topology: the watchdog is a separate
    thread, and the dispatch lock is reentrant — probing from the dispatching
    thread itself would take its own credit and prove nothing.
    """
    entered = threading.Event()
    release = threading.Event()

    def fake_handle_request(request):
        entered.set()
        release.wait(timeout=10)
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "handle_request", fake_handle_request)

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": {}},
    }
    t = threading.Thread(target=lambda: mcp_server._dispatch_locally(request))
    t.start()
    try:
        assert entered.wait(timeout=5)
        return mcp_server._maybe_hand_off_writer_lease()
    finally:
        release.set()
        t.join(timeout=10)


def test_logstream_tools_dispatch_without_blocking_the_handoff(leased, monkeypatch):
    """A five-minute event_wait must not sit in front of every handoff.

    Logstream tools reach only logstream.sqlite3, so they dispatch outside the
    lock on both transports. Without that exemption one agent's long-poll would
    defer the baton for as long as it polls.
    """
    monkeypatch.setattr(palace_mod, "palace_lock_wanted", lambda path: True)

    assert _handoff_verdict_while_dispatching(monkeypatch, "mempalace_event_wait") is True
    assert leased.exited is True


def test_chroma_touching_tools_still_hold_the_dispatch_lock(leased, monkeypatch):
    """The exemption is one set of tools, not the policy: anything that can
    reach Chroma keeps the barrier that stops handles closing under it."""
    monkeypatch.setattr(palace_mod, "palace_lock_wanted", lambda path: True)

    assert _handoff_verdict_while_dispatching(monkeypatch, "mempalace_search") is False
    assert leased.exited is False


def test_no_handoff_while_a_write_frame_is_active(leased, monkeypatch):
    monkeypatch.setattr(palace_mod, "palace_lock_wanted", lambda path: True)
    key = palace_mod.palace_lock_key(mcp_server._config.palace_path)
    with palace_mod._palace_lock_guard:
        palace_mod._palace_lock_depth[key] = 1

    assert mcp_server._maybe_hand_off_writer_lease() is False
    assert leased.exited is False

    with palace_mod._palace_lock_guard:
        palace_mod._palace_lock_depth.pop(key, None)
    assert mcp_server._maybe_hand_off_writer_lease() is True


def test_handoff_can_be_switched_off(leased, monkeypatch):
    monkeypatch.setenv(mcp_server._MCP_WRITER_HANDOFF_DISABLED_ENV, "1")
    monkeypatch.setattr(palace_mod, "palace_lock_wanted", lambda path: True)

    assert mcp_server._maybe_hand_off_writer_lease() is False
    assert leased.exited is False
    assert mcp_server._writer_handoff_wait_seconds() == 0.0


def test_contender_waits_for_the_baton_then_backs_off(monkeypatch):
    """A peer that never answers must not make every write pay the full wait."""
    monkeypatch.delenv(mcp_server._MCP_WRITER_HANDOFF_DISABLED_ENV, raising=False)
    monkeypatch.setenv(mcp_server._MCP_WRITER_HANDOFF_WAIT_ENV, "8")
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_HANDOFF_BACKOFF_UNTIL", 0.0)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_READ_ONLY", False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_ERROR", "")

    assert mcp_server._writer_handoff_wait_seconds() == pytest.approx(8.0)

    seen = {}

    def refuse(palace_path, **kwargs):
        seen.update(kwargs)
        raise palace_mod.MineAlreadyRunning(f"palace {palace_path} is held by PID 999")

    monkeypatch.setattr(palace_mod, "mine_palace_lock", refuse)
    ok, reason = mcp_server._acquire_mcp_writer_lock()

    assert ok is False
    assert "already holds the palace lock" in reason
    assert seen == {"lease": True, "wait": pytest.approx(8.0)}, (
        "the lease must be taken as a lease and be willing to wait for a handoff"
    )
    # Timed out once: later calls use the short wait until the backoff expires.
    assert mcp_server._writer_handoff_wait_seconds() == pytest.approx(
        mcp_server._MCP_WRITER_HANDOFF_BACKOFF_WAIT
    )


def test_acquire_records_lease_and_clears_backoff(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(mcp_server._MCP_WRITER_HANDOFF_DISABLED_ENV, raising=False)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LEASE_SINCE", 0.0)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_HANDOFF_BACKOFF_UNTIL", time.monotonic() + 300)
    monkeypatch.setattr(mcp_server, "_discard_mcp_storage_handles", lambda: None)
    monkeypatch.setattr(palace_mod, "mine_palace_lock", lambda path, **kwargs: _DummyLease())

    ok, reason = mcp_server._acquire_mcp_writer_lock()

    assert (ok, reason) == (True, "")
    assert mcp_server._MCP_WRITER_LEASE_SINCE > 0
    assert mcp_server._MCP_WRITER_HANDOFF_BACKOFF_UNTIL == 0.0
    mcp_server._MCP_WRITER_LOCK_CM = None


def test_status_reports_who_owns_the_palace(leased, monkeypatch):
    monkeypatch.setattr(palace_mod, "palace_lock_wanted", lambda path: True)

    decorated = mcp_server._decorate_mcp_tool_result("mempalace_status", {"total_drawers": 0})
    lease_status = decorated["writer_lease"]

    assert lease_status["held_by_this_server"] is True
    assert lease_status["peer_waiting"] is True
    assert lease_status["handoff_enabled"] is True
    assert "held_for_seconds" in lease_status


def test_status_names_the_holder_when_this_server_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    monkeypatch.setattr(
        type(mcp_server._config), "palace_path", property(lambda self: str(palace_path))
    )
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_ERROR", "peer holds it")

    with palace_mod.mine_palace_lock(str(palace_path), lease=True):
        decorated = mcp_server._decorate_mcp_tool_result("mempalace_status", {})

    lease_status = decorated["writer_lease"]
    assert lease_status["held_by_this_server"] is False
    assert lease_status["palace_locked"] is True
    assert "PID" in lease_status["holder"], "status should name the recorded lock holder"
    assert lease_status["last_reason"] == "peer holds it"


def test_status_does_not_name_a_dead_holder_once_the_lock_is_free(tmp_path, monkeypatch):
    """The lock-file body outlives its writer — status must not quote it blindly.

    After the previous owner exits, the body still reads "PID <dead>". Reporting
    that as the current holder sends whoever is debugging a stuck write chasing
    a process that no longer exists.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    monkeypatch.setattr(
        type(mcp_server._config), "palace_path", property(lambda self: str(palace_path))
    )
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_ERROR", "")

    # Take and release the lock: the identity stays recorded in the file body.
    with palace_mod.mine_palace_lock(str(palace_path), lease=True):
        pass
    assert "PID" in palace_mod.palace_lock_holder(str(palace_path))

    lease_status = mcp_server._decorate_mcp_tool_result("mempalace_status", {})["writer_lease"]

    assert lease_status["palace_locked"] is False
    assert lease_status["holder"] is None
