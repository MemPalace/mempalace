"""Tests for mine_palace_lock — the per-palace non-blocking mine guard.

Covers the fix for the runaway mine fan-out described alongside issues
#974 and #965: if N copies of `mempalace mine` are spawned concurrently
against the same palace, they must collapse to a single runner rather
than queue as waiters that will drive parallel HNSW inserts. Mines
against *different* palaces must still be free to run in parallel.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
import sys

import pytest

import mempalace.palace as palace_mod
from mempalace.palace import (
    _write_lock_holder,
    MineAlreadyRunning,
    mine_global_lock,
    mine_lock,
    mine_palace_lock,
    reap_stale_mine_locks,
)


def _get_mp_context():
    """Always use ``spawn`` — ``fork`` deadlocks under modern Python.

    The parent (pytest + chromadb + onnxruntime) is multi-threaded by the time
    these tests run. ``fork`` snapshots that state into the child without the
    threads that hold the locks, which Python 3.13 explicitly warns about and
    which deadlocks the CI runners. macOS additionally forbids
    fork-without-exec via CoreFoundation. ``spawn`` re-imports the package in
    the child (slower, but safe) and inherits ``os.environ`` — including the
    monkeypatched ``HOME`` — which is all these lock-file tests need.
    """
    return multiprocessing.get_context("spawn")


def _isolate_home(monkeypatch, tmp_path):
    """Point ``~`` at ``tmp_path`` on both POSIX (HOME) and Windows (USERPROFILE)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hold_lock(palace_path: str, ready_flag: str, release_flag: str) -> int:
    """Acquire mine_palace_lock, signal readiness, wait for release flag.

    Returns 0 if we acquired the lock, 1 if MineAlreadyRunning was raised.
    Runs in a child process for true cross-process locking semantics.
    """
    try:
        with mine_palace_lock(palace_path):
            # Tell the parent we hold the lock
            open(ready_flag, "w").close()
            # Wait until parent tells us to release
            for _ in range(500):
                if os.path.exists(release_flag):
                    return 0
                time.sleep(0.01)
            return 0
    except MineAlreadyRunning:
        return 1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mine_palace_lock_reentrant_across_threads_same_process(tmp_path):
    """Process-wide re-entrancy: a second acquisition from a *different thread*
    of the same process passes through instead of self-conflicting.

    Regression for the MCP HTTP transport (ThreadingHTTPServer): the writer
    lease is acquired on one thread (mcp_server._acquire_mcp_writer_lock) but
    write requests are dispatched on other worker threads. With the old
    thread-local re-entrancy those handlers re-acquired the process-held flock
    and raised MineAlreadyRunning ("palace ... is held by PID <self>").
    Re-entrancy is now process-wide, so same-process cross-thread acquisition is
    a pass-through.
    """
    palace = str(tmp_path / "palace")
    os.makedirs(palace, exist_ok=True)

    outer = mine_palace_lock(palace)
    outer.__enter__()  # main thread holds the lease, like the MCP writer-lease
    try:
        result: dict = {}

        def worker():
            try:
                with mine_palace_lock(palace):
                    result["acquired"] = True
            except MineAlreadyRunning as exc:  # pragma: no cover - failure path
                result["error"] = str(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)

        assert not t.is_alive(), "worker thread hung acquiring the palace lock"
        assert result.get("acquired") is True, (
            f"cross-thread same-process acquisition should pass through, got: {result}"
        )
    finally:
        outer.__exit__(None, None, None)


def test_single_acquire_succeeds(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    with mine_palace_lock(str(tmp_path / "palace")):
        pass  # should not raise


def test_lock_reusable_after_release(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    palace = str(tmp_path / "palace")
    with mine_palace_lock(palace):
        pass
    # Re-acquire must succeed now that the previous holder released
    with mine_palace_lock(palace):
        pass


def test_same_palace_serializes_across_processes(tmp_path, monkeypatch):
    """Two processes contending for the same palace: second must be rejected."""
    _isolate_home(monkeypatch, tmp_path)
    palace = str(tmp_path / "palace")
    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")

    ctx = _get_mp_context()
    holder = ctx.Process(target=_hold_lock, args=(palace, ready, release))
    holder.start()
    try:
        # Wait for the holder to acquire
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "holder failed to acquire lock in time"

        # From the parent, we must not be able to acquire the same palace lock
        with pytest.raises(MineAlreadyRunning):
            with mine_palace_lock(palace):
                pytest.fail("second acquire of same palace should have raised")
    finally:
        open(release, "w").close()
        holder.join(timeout=5)
        assert holder.exitcode == 0


def test_different_palaces_dont_conflict(tmp_path, monkeypatch):
    """Mines against different palaces must NOT block each other."""
    _isolate_home(monkeypatch, tmp_path)
    palace_a = str(tmp_path / "palace_a")
    palace_b = str(tmp_path / "palace_b")
    ready = str(tmp_path / "ready_a")
    release = str(tmp_path / "release_a")

    ctx = _get_mp_context()
    holder = ctx.Process(target=_hold_lock, args=(palace_a, ready, release))
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "holder failed to acquire lock in time"

        # Different palace — must succeed even while palace_a is held
        with mine_palace_lock(palace_b):
            pass  # no exception expected
    finally:
        open(release, "w").close()
        holder.join(timeout=5)


def test_palace_path_is_normalized(tmp_path, monkeypatch):
    """Relative and absolute forms of the same path must use the same lock.

    Cross-process variant: a child holds the absolute form, a relative form
    in the parent must hash to the same lock key and raise
    ``MineAlreadyRunning``. (The same-thread case is now a re-entrant
    pass-through by design — see ``test_reentrant_same_thread_passes_through``
    — so we exercise the normalization invariant across a process boundary
    where re-entrance does not apply.)
    """
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    os.makedirs(tmp_path / "palace", exist_ok=True)
    absolute = str(tmp_path / "palace")
    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")

    ctx = _get_mp_context()
    holder = ctx.Process(target=_hold_lock, args=(absolute, ready, release))
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "holder failed to acquire lock in time"

        # Parent holds CWD = tmp_path so "palace" is the same on-disk dir as
        # the absolute form. The lock key is sha256(realpath+normcase) so the
        # two forms must collide.
        with pytest.raises(MineAlreadyRunning):
            with mine_palace_lock("palace"):
                pytest.fail("normalized path collision should have raised")
    finally:
        open(release, "w").close()
        holder.join(timeout=5)


def test_reentrant_same_thread_passes_through(tmp_path, monkeypatch):
    """Same thread re-acquiring the same palace lock must not deadlock or raise.

    This is the invariant that makes ``ChromaCollection`` write methods
    (which take ``mine_palace_lock`` for MCP/direct-writer protection)
    compose with ``miner.mine()`` (which already holds the lock for the
    entire mine pipeline). Without the per-thread re-entrant guard the inner
    acquire would self-deadlock on the outer flock.
    """
    _isolate_home(monkeypatch, tmp_path)
    palace = str(tmp_path / "palace")
    with mine_palace_lock(palace):
        # Re-enter from the same thread — must yield without raising or hanging.
        with mine_palace_lock(palace):
            pass
        # After the inner exits, the outer is still held. Use spawn so the
        # child does not inherit the parent's open lock fd or SQLite/Chroma
        # process state from the full test suite.
        ctx = _get_mp_context()
        result_q = ctx.Queue()
        child = ctx.Process(target=_try_acquire_expect_busy, args=(palace, result_q))
        try:
            child.start()
            assert result_q.get(timeout=10) == "busy", (
                "outer lock should still be held by parent after inner re-entrant exit"
            )
            child.join(timeout=5)
            assert child.exitcode == 0
        finally:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)


def _try_acquire_expect_busy(palace_path, result_q):
    """Helper: try to acquire, push 'busy' (raised) or 'free' (acquired) into queue."""
    try:
        with mine_palace_lock(palace_path):
            result_q.put("free")
    except MineAlreadyRunning:
        result_q.put("busy")


def _hold_lock_send_pid(palace_path: str, ready_flag: str, release_flag: str, pid_q) -> None:
    """Acquire the lock, push our PID + cmdline through the queue, then wait."""
    import sys as _sys

    try:
        with mine_palace_lock(palace_path):
            pid_q.put((os.getpid(), list(_sys.argv[:3])))
            open(ready_flag, "w").close()
            for _ in range(500):
                if os.path.exists(release_flag):
                    return
                time.sleep(0.01)
    except MineAlreadyRunning:
        pid_q.put(("error", "raised"))


def test_lock_failure_message_names_holder(tmp_path, monkeypatch):
    """Regression #1264: failed acquire must identify the holder by PID.

    Before this fix, a `mempalace mine` colliding with another writer
    (mine, MCP server, anything taking mine_palace_lock) saw a generic
    "another `mempalace mine` is already running" message and exited
    silently. The operator had no signal of which process to wait for
    or stop. The new message includes ``PID N`` so the holder can be
    identified directly.
    """
    _isolate_home(monkeypatch, tmp_path)
    palace = str(tmp_path / "palace")
    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")

    ctx = _get_mp_context()
    pid_q = ctx.Queue()
    holder = ctx.Process(target=_hold_lock_send_pid, args=(palace, ready, release, pid_q))
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "holder failed to acquire lock in time"
        holder_pid, _holder_argv = pid_q.get(timeout=2)

        with pytest.raises(MineAlreadyRunning) as excinfo:
            with mine_palace_lock(palace):
                pytest.fail("second acquire of same palace should have raised")

        msg = str(excinfo.value)
        assert f"PID {holder_pid}" in msg, (
            f"lock-failure message must name the holder PID; got: {msg!r}"
        )
    finally:
        open(release, "w").close()
        holder.join(timeout=5)


def test_write_lock_holder_writes_utf8_bytes_for_non_ascii_argv(tmp_path, monkeypatch):
    """Regression #1435: lock-holder identity must be written as UTF-8 bytes.

    The holder byte count and the on-disk bytes must agree even when argv
    contains characters that are not representable in a Windows ANSI codepage.
    """
    monkeypatch.setattr(
        sys,
        "argv",
        ["mempalace", "mine", "café/北"],
    )

    lock_path = tmp_path / "holder.lock"
    lock_path.write_bytes(b"\0stale-holder-identity-that-must-be-truncated")

    with lock_path.open("r+b") as lock_file:
        _write_lock_holder(lock_file)

    ident = f"{os.getpid()} {' '.join(sys.argv[:3])}".strip()
    assert lock_path.read_bytes() == b"\0" + ident.encode("utf-8")


def test_write_lock_holder_is_best_effort_on_unicode_error(monkeypatch):
    """Regression #1435: holder-write failures must not block lock acquisition."""

    class UnicodeFailingLock:
        def seek(self, _offset):
            pass

        def truncate(self, _size):
            pass

        def write(self, _data):
            raise UnicodeEncodeError("cp1252", "北", 0, 1, "not representable")

        def flush(self):
            pass

    monkeypatch.setattr(sys, "argv", ["mempalace", "mine", "北"])
    _write_lock_holder(UnicodeFailingLock())


def test_lock_holder_identity_persists_across_release(tmp_path, monkeypatch):
    """The holder line is overwritten by each new acquirer, not appended.

    Without explicit truncate the lock file would accumulate lines across
    runs and grow without bound. Verify that re-acquire keeps the body
    bounded.
    """
    # ``os.path.expanduser("~")`` reads HOME on POSIX but USERPROFILE on
    # Windows; setting both makes the ``~/.mempalace/locks`` lookup land
    # under ``tmp_path`` regardless of platform.
    _isolate_home(monkeypatch, tmp_path)
    palace = str(tmp_path / "palace")
    for _ in range(5):
        with mine_palace_lock(palace):
            pass

    # Locate the lock file. The key derivation is internal but we can find
    # it by scanning the mempalace locks dir for mine_palace_*.lock entries.
    lock_dir = tmp_path / ".mempalace" / "locks"
    lock_files = list(lock_dir.glob("mine_palace_*.lock"))
    assert lock_files, "expected the palace lock file to exist after acquire/release"
    # Read as bytes so the byte-0 sentinel (\x00) is preserved without
    # decode quirks; the bound is on the file size, not its line count.
    body = lock_files[0].read_bytes()
    # Body is byte-0 sentinel + identity (no trailing accumulation).
    # Identity is ``f"{pid} {sys.argv[:3]}"``; cap at a generous bound that
    # still rules out unbounded growth across the 5 re-acquires.
    assert len(body) < 1024, f"lock body must not grow across re-acquires; got {len(body)} bytes"


def test_mine_global_lock_is_alias_for_back_compat(tmp_path, monkeypatch):
    """Old callers of `mine_global_lock` should still work."""
    _isolate_home(monkeypatch, tmp_path)
    assert mine_global_lock is mine_palace_lock
    with mine_global_lock(str(tmp_path / "palace")):
        pass  # the alias accepts the same palace_path argument


def test_holder_set_not_orphaned_by_interrupt_after_mark_held(tmp_path, monkeypatch):
    """An async interrupt right after the hold is recorded must not leave the
    palace key stranded in the process-wide holder set.

    ``_mark_held`` sits inside the ``try`` whose ``finally`` runs
    ``_mark_released``, so the two are paired on every exit. If ``_mark_held``
    ran before the ``try``, a ``KeyboardInterrupt``/signal landing in the gap
    would strand the key: the outer ``finally`` still frees the flock, so
    ``_held_by_this_process`` would report a hold the OS lock no longer backs,
    and the next re-entrant acquire in this process would pass through and
    write without the flock (two concurrent writers into one palace).
    """
    _isolate_home(monkeypatch, tmp_path)
    palace = str(tmp_path / "palace")

    before = set(palace_mod._palace_lock_keys)

    # Model the interrupt: run the real _mark_held (records the hold), then
    # raise, as a signal arriving at that instant would.
    real_mark_held = palace_mod._mark_held

    def _mark_then_interrupt(lock_key):
        real_mark_held(lock_key)
        raise KeyboardInterrupt

    palace_mod._mark_held = _mark_then_interrupt
    try:
        with pytest.raises(KeyboardInterrupt):
            with mine_palace_lock(palace):
                pass
    finally:
        # Restore by hand (not monkeypatch.setattr, which unpatches only at
        # teardown) so the reuse check below calls the real _mark_held.
        palace_mod._mark_held = real_mark_held

    assert set(palace_mod._palace_lock_keys) == before, (
        "palace key was stranded in the holder set after an interrupt: "
        "the in-memory hold outlived the flock"
    )
    # The flock was freed and no stale hold remains, so the lock is reusable.
    with mine_palace_lock(palace):
        pass


# ---------------------------------------------------------------------------
# reap_stale_mine_locks — orphaned per-source-file lock garbage collection
# ---------------------------------------------------------------------------
#
# mine_lock's own finally-block cleanup (_cleanup_mine_lock_file) only runs
# for the specific lock a process just released, and only if that process
# reaches its own finally block at all. A process killed abruptly (SIGKILL,
# force-quit, host crash) never runs it, and nothing else in the codebase
# later revisits that lock file — it orphans permanently. One long-lived
# installation was found with 5,636 such orphaned lock files, the oldest
# several months old, none held by any live process. These tests cover the
# reaper added to reclaim them safely.


def _hold_source_lock(source_file: str, ready_flag: str, release_flag: str) -> int:
    """Acquire mine_lock(source_file), signal readiness, wait for release.

    Runs in a child process for true cross-process locking semantics,
    mirroring _hold_lock above but for the per-source-file mine_lock
    rather than the per-palace mine_palace_lock.
    """
    with mine_lock(source_file):
        open(ready_flag, "w").close()
        for _ in range(500):
            if os.path.exists(release_flag):
                return 0
            time.sleep(0.01)
        return 0


def test_reap_removes_stale_unlocked_lock(tmp_path, monkeypatch):
    """A lock file that's old and held by nobody is safe to remove."""
    _isolate_home(monkeypatch, tmp_path)
    lock_dir = tmp_path / ".mempalace" / "locks"
    lock_dir.mkdir(parents=True)
    stale = lock_dir / "0000000000000000.lock"
    stale.write_bytes(b"")
    old_time = time.time() - 7200  # 2h old, past the default 1h threshold
    os.utime(stale, (old_time, old_time))

    reaped, skipped = reap_stale_mine_locks(min_age_seconds=3600)

    assert reaped == 1
    assert skipped == 0
    assert not stale.exists()


def test_reap_leaves_recently_touched_lock_alone(tmp_path, monkeypatch):
    """A lock younger than the age threshold is left alone even if unheld.

    The flock check is what makes removal *safe*; the age threshold exists
    only to avoid racing a lock that was just released and may still be
    mid-rendezvous with a waiter on the same pathname.
    """
    _isolate_home(monkeypatch, tmp_path)
    lock_dir = tmp_path / ".mempalace" / "locks"
    lock_dir.mkdir(parents=True)
    fresh = lock_dir / "1111111111111111.lock"
    fresh.write_bytes(b"")  # mtime is "now" — well under the threshold

    reaped, skipped = reap_stale_mine_locks(min_age_seconds=3600)

    assert reaped == 0
    assert fresh.exists()


def test_reap_never_removes_a_lock_held_by_another_process(tmp_path, monkeypatch):
    """The core safety property: a lock genuinely held by a live process,
    however old it looks by mtime, is never removed — the age threshold is
    a courtesy throttle, the flock check is the actual safety mechanism."""
    _isolate_home(monkeypatch, tmp_path)
    source_file = str(tmp_path / "some_source.py")
    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")

    ctx = _get_mp_context()
    holder = ctx.Process(target=_hold_source_lock, args=(source_file, ready, release))
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "holder failed to acquire lock in time"

        lock_dir = tmp_path / ".mempalace" / "locks"
        lock_files = list(lock_dir.glob("*.lock"))
        assert lock_files, "expected the held lock file to exist"
        # Backdate mtime so it would be a reap candidate by age alone —
        # the flock held by the child process must still protect it.
        old_time = time.time() - 7200
        os.utime(lock_files[0], (old_time, old_time))

        reaped, skipped = reap_stale_mine_locks(min_age_seconds=3600)

        assert reaped == 0
        assert skipped == 1
        assert lock_files[0].exists(), "a held lock must never be removed by the reaper"
    finally:
        open(release, "w").close()
        holder.join(timeout=5)
        assert holder.exitcode == 0


def test_reap_skips_mine_palace_prefixed_locks(tmp_path, monkeypatch):
    """mine_palace_*.lock belongs to the newer per-palace lock (mine_palace_lock)
    with its own lifecycle and holder tracking — this reaper targets only the
    per-source-file locks mine_lock creates, and must not touch those."""
    _isolate_home(monkeypatch, tmp_path)
    lock_dir = tmp_path / ".mempalace" / "locks"
    lock_dir.mkdir(parents=True)
    palace_lock = lock_dir / "mine_palace_deadbeefdeadbeef.lock"
    palace_lock.write_bytes(b"")
    old_time = time.time() - 7200
    os.utime(palace_lock, (old_time, old_time))

    reaped, skipped = reap_stale_mine_locks(min_age_seconds=3600)

    assert reaped == 0
    assert palace_lock.exists()


def test_reap_missing_lock_dir_is_a_noop(tmp_path, monkeypatch):
    """No ~/.mempalace/locks directory yet (fresh install) must not raise."""
    _isolate_home(monkeypatch, tmp_path)
    reaped, skipped = reap_stale_mine_locks()
    assert (reaped, skipped) == (0, 0)


def test_maybe_reap_is_throttled(tmp_path, monkeypatch):
    """The opportunistic call site runs at most once per interval — a stale
    lock created between two rapid-fire mine_lock calls must survive the
    second call because the reap itself was skipped, not because reaping
    failed."""
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(palace_mod, "_LOCK_REAP_INTERVAL_SECONDS", 3600)

    with mine_lock(str(tmp_path / "a.py")):
        pass  # first call: no marker exists yet, this call creates it

    lock_dir = tmp_path / ".mempalace" / "locks"
    stale = lock_dir / "2222222222222222.lock"
    stale.write_bytes(b"")
    old_time = time.time() - 7200
    os.utime(stale, (old_time, old_time))

    with mine_lock(str(tmp_path / "b.py")):
        pass  # second call: marker is fresh, reap should be skipped this time

    assert stale.exists(), "reap should have been throttled on the second mine_lock call"
