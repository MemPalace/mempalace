"""Tests for the writer-lease handoff — passing the palace lock between processes.

``mine_palace_lock`` is exclusive and, for a long-lived MCP/daemon writer,
lifetime-scoped: whoever takes the palace first keeps it until the process
exits, so every other server on that palace stays read-only however idle the
holder is. The handoff turns that lease into a baton:

* a contender queues on the palace's *demand* file (``.want``) and polls for
  ownership (``mine_palace_lock(..., wait=N)``),
* the holder sees the demand through a non-blocking probe
  (``palace_lock_wanted``) and — only when no write frame is active —
  quiesces (``begin_palace_lock_handoff``) and releases.

The invariant these tests defend: the lock is never handed away while a write
is running, including a write on the *re-entrant pass-through* path, where the
inner ``mine_palace_lock`` takes no OS lock of its own and trusts the outer
lease to still be there.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time

import pytest

import mempalace.palace as palace_mod
from mempalace.palace import (
    MineAlreadyRunning,
    begin_palace_lock_handoff,
    end_palace_lock_handoff,
    mine_palace_lock,
    palace_lock_holder,
    palace_lock_is_held,
    palace_lock_wanted,
)


def _get_mp_context():
    """``spawn`` only — see the rationale in test_palace_locks._get_mp_context."""
    return multiprocessing.get_context("spawn")


# ---------------------------------------------------------------------------
# Child-process helpers (must be importable top-level for spawn)
# ---------------------------------------------------------------------------


def _hold_lease_and_hand_off(palace_path: str, ready_flag: str, timeout: float) -> int:
    """Hold a writer lease, then release it as soon as a peer queues.

    Models the MCP server's handoff watchdog at the palace layer. Returns 0 if
    the lease was handed off, 2 if nobody ever asked for it.
    """
    lease = mine_palace_lock(palace_path, lease=True)
    lease.__enter__()
    handed = False
    try:
        open(ready_flag, "w").close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if palace_lock_wanted(palace_path) and begin_palace_lock_handoff(palace_path):
                try:
                    lease.__exit__(None, None, None)
                    handed = True
                finally:
                    end_palace_lock_handoff(palace_path)
                break
            time.sleep(0.02)
    finally:
        if not handed:
            lease.__exit__(None, None, None)
    return 0 if handed else 2


def _hold_lease_while_writing(palace_path: str, ready_flag: str, hold_seconds: float) -> int:
    """Hold a lease with a write frame active the whole time.

    Returns 0 if every handoff attempt was correctly refused, 1 if the lock
    was handed away while the write was running.
    """
    with mine_palace_lock(palace_path, lease=True):
        with mine_palace_lock(palace_path):  # re-entrant write frame
            open(ready_flag, "w").close()
            deadline = time.monotonic() + hold_seconds
            while time.monotonic() < deadline:
                if begin_palace_lock_handoff(palace_path):
                    end_palace_lock_handoff(palace_path)
                    return 1
                time.sleep(0.02)
    return 0


def _wait_for(path: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def palace(tmp_path, monkeypatch):
    """Isolated palace + lock dir (lock files live under $HOME/.mempalace)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(palace_mod._LOCK_WAIT_ENV, raising=False)
    path = tmp_path / "palace"
    path.mkdir()
    return str(path)


# ---------------------------------------------------------------------------
# Contract preserved: no waiting unless asked
# ---------------------------------------------------------------------------


def test_default_acquire_still_fails_fast(palace):
    """Hook-spawned mines must keep exiting immediately, not queue as waiters.

    The whole point of the non-blocking lock is that N concurrent
    `mempalace mine` copies collapse to one runner. Waiting has to stay opt-in.
    """
    with mine_palace_lock(palace, lease=True):
        started = time.monotonic()
        with pytest.raises(MineAlreadyRunning):
            _acquire_in_other_process_style(palace)
        assert time.monotonic() - started < 1.0


def _acquire_in_other_process_style(palace_path: str):
    """Acquire as a *foreign* process would: no re-entrant credit.

    Same-process acquisition would pass through, so temporarily hide the
    holder set to exercise the contended path from a single test process.
    """
    with _foreign_process_view():
        with mine_palace_lock(palace_path):
            pass


class _foreign_process_view:
    """Make this process look like a fresh one to the re-entrancy layer."""

    def __enter__(self):
        with palace_mod._palace_lock_guard:
            self._keys = set(palace_mod._holder_keys_locked())
            palace_mod._palace_lock_keys = set()
        return self

    def __exit__(self, *exc):
        with palace_mod._palace_lock_guard:
            palace_mod._palace_lock_keys = self._keys
        return False


def test_env_default_opts_the_cli_into_waiting(palace, monkeypatch):
    monkeypatch.setenv(palace_mod._LOCK_WAIT_ENV, "0.3")
    assert palace_mod._lock_wait_default() == pytest.approx(0.3)
    monkeypatch.setenv(palace_mod._LOCK_WAIT_ENV, "not-a-number")
    assert palace_mod._lock_wait_default() == 0.0


# ---------------------------------------------------------------------------
# Demand signalling
# ---------------------------------------------------------------------------


def test_ownership_probe_outlives_the_recorded_identity(palace):
    """Ownership is what the kernel says, not what the lock file remembers.

    The body keeps naming the last process to take the lock long after it is
    gone, so the probe is the only honest answer to "is this palace locked?".
    """
    assert palace_lock_is_held(palace) is False

    with mine_palace_lock(palace, lease=True):
        assert palace_lock_is_held(palace) is True

    assert palace_lock_is_held(palace) is False
    assert "PID" in palace_lock_holder(palace), "the stale identity is still on disk"


def test_no_demand_reported_when_nobody_is_queued(palace):
    with mine_palace_lock(palace, lease=True):
        assert palace_lock_wanted(palace) is False


def test_demand_visible_to_the_holder_while_a_peer_waits(palace):
    """The queued contender is what makes demand visible — no heartbeat file.

    The contender holds the demand lock for the whole wait, so the holder's
    probe is a plain non-blocking lock attempt and a killed contender stops
    signalling demand the instant the kernel drops its lock.
    """
    with mine_palace_lock(palace, lease=True):
        seen: dict[str, bool] = {}

        def contender():
            with _foreign_process_view():
                try:
                    with mine_palace_lock(palace, wait=1.5, poll=0.02):
                        seen["acquired"] = True
                except MineAlreadyRunning:
                    seen["acquired"] = False

        t = threading.Thread(target=contender)
        t.start()
        try:
            deadline = time.monotonic() + 2.0
            wanted = False
            while time.monotonic() < deadline and not wanted:
                wanted = palace_lock_wanted(palace)
                time.sleep(0.02)
            assert wanted, "holder never saw the queued contender's demand"
        finally:
            t.join(timeout=5)
        assert seen.get("acquired") is False, "contender must not take a held lock"
    # Demand is released with the contender's descriptor, not left behind.
    assert palace_lock_wanted(palace) is False


# ---------------------------------------------------------------------------
# The safety invariant: never hand off mid-write
# ---------------------------------------------------------------------------


def test_idle_lease_can_be_handed_off(palace):
    lease = mine_palace_lock(palace, lease=True)
    lease.__enter__()
    try:
        assert begin_palace_lock_handoff(palace) is True
        end_palace_lock_handoff(palace)
    finally:
        lease.__exit__(None, None, None)


def test_handoff_refused_while_a_write_frame_is_active(palace):
    """A lease is idle; a write under it is not. The depth counter is the line."""
    with mine_palace_lock(palace, lease=True):
        with mine_palace_lock(palace):  # pass-through write frame
            assert begin_palace_lock_handoff(palace) is False
        # Frame closed — handoff allowed again.
        assert begin_palace_lock_handoff(palace) is True
        end_palace_lock_handoff(palace)


def test_handoff_refused_when_this_process_does_not_own_the_lock(palace):
    assert begin_palace_lock_handoff(palace) is False


def test_writer_arriving_mid_handoff_does_not_pass_through(palace):
    """The window closes the TOCTOU hole between "am I holder?" and "release".

    Without the park, a thread that checked the holder set microseconds before
    the release would take pass-through credit and write with no OS lock at
    all, while another process already owned the palace.
    """
    lease = mine_palace_lock(palace, lease=True)
    lease.__enter__()
    lease_open = True
    order: list[str] = []
    entered = threading.Event()

    def writer():
        entered.set()
        with mine_palace_lock(palace, wait=5, poll=0.02):
            order.append("write")

    try:
        assert begin_palace_lock_handoff(palace) is True
        t = threading.Thread(target=writer)
        t.start()
        entered.wait(timeout=5)
        # Give the writer a real chance to (wrongly) proceed.
        time.sleep(0.3)
        assert order == [], "writer entered while the lease was being handed off"
        order.append("released")
        lease.__exit__(None, None, None)
        lease_open = False
        end_palace_lock_handoff(palace)
        t.join(timeout=10)
        assert not t.is_alive(), "writer never woke up after the handoff window"
        assert order == ["released", "write"], f"unexpected ordering: {order}"
    finally:
        if lease_open:
            end_palace_lock_handoff(palace)
            lease.__exit__(None, None, None)


def test_lease_frame_does_not_block_its_own_handoff(palace):
    """``lease=True`` must not count as a write, or nothing could ever hand off."""
    with mine_palace_lock(palace, lease=True):
        key = palace_mod.palace_lock_key(palace)
        assert palace_mod._palace_lock_depth.get(key, 0) == 0
        with mine_palace_lock(palace):
            assert palace_mod._palace_lock_depth.get(key, 0) == 1
        assert palace_mod._palace_lock_depth.get(key, 0) == 0


# ---------------------------------------------------------------------------
# Cross-process: the baton actually changes hands
# ---------------------------------------------------------------------------


def test_waiter_receives_the_lease_from_a_live_idle_holder(palace, tmp_path):
    """The headline behavior: a peer becomes writer without the holder exiting."""
    ctx = _get_mp_context()
    ready = str(tmp_path / "holder_ready")
    child = ctx.Process(target=_hold_lease_and_hand_off, args=(palace, ready, 30.0))
    child.start()
    try:
        assert _wait_for(ready, timeout=60), "child never took the lease"
        assert palace_lock_holder(palace) != "another writer (identity not recorded)"

        started = time.monotonic()
        with mine_palace_lock(palace, wait=30, poll=0.02):
            elapsed = time.monotonic() - started
            # We hold it while the child is still alive — that is the point.
            assert child.is_alive() or child.exitcode == 0
        assert elapsed < 30, "handoff did not complete inside the wait window"
    finally:
        child.join(timeout=30)
    assert child.exitcode == 0, "child reported that nobody ever asked for the lease"


def test_waiter_times_out_against_a_holder_that_is_writing(palace, tmp_path):
    """A busy holder must NOT hand off: the waiter fails exactly as before."""
    ctx = _get_mp_context()
    ready = str(tmp_path / "writer_ready")
    child = ctx.Process(target=_hold_lease_while_writing, args=(palace, ready, 6.0))
    child.start()
    try:
        assert _wait_for(ready, timeout=60), "child never started its write"
        with pytest.raises(MineAlreadyRunning):
            with mine_palace_lock(palace, wait=1.5, poll=0.02):
                pass
    finally:
        child.join(timeout=30)
    assert child.exitcode == 0, "the lock was handed away while a write was in flight"
