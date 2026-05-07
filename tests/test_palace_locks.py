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
import time

import pytest

from mempalace.palace import (
    MineAlreadyRunning,
    mine_global_lock,
    mine_palace_lock,
)


def _get_mp_context():
    """Pick a start method that works on every CI runner.

    `fork` is cheaper (no re-import) but is unavailable on Windows, so we fall
    back to `spawn` there. `spawn` inherits ``os.environ`` (including the
    monkeypatched ``HOME``) and re-imports the ``mempalace`` package in the
    child, which is sufficient for the lock-file semantics exercised here.
    """
    start_method = "spawn" if os.name == "nt" else "fork"
    return multiprocessing.get_context(start_method)


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


def test_single_acquire_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with mine_palace_lock(str(tmp_path / "palace")):
        pass  # should not raise


def test_lock_reusable_after_release(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    palace = str(tmp_path / "palace")
    with mine_palace_lock(palace):
        pass
    # Re-acquire must succeed now that the previous holder released
    with mine_palace_lock(palace):
        pass


def test_same_palace_serializes_across_processes(tmp_path, monkeypatch):
    """Two processes contending for the same palace: second must be rejected."""
    monkeypatch.setenv("HOME", str(tmp_path))
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
    monkeypatch.setenv("HOME", str(tmp_path))
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
    monkeypatch.setenv("HOME", str(tmp_path))
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

    This is the invariant that makes ``ChromaCollection`` write methods (which
    take ``mine_palace_lock`` for MCP/direct-writer protection) compose with
    ``miner.mine()`` (which already holds the lock for the entire mine
    pipeline). Without the per-thread re-entrant guard the inner acquire
    would self-deadlock on the outer flock.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    palace = str(tmp_path / "palace")
    with mine_palace_lock(palace):
        # Re-enter from the same thread — must yield without raising or hanging.
        with mine_palace_lock(palace):
            pass
        # After the inner exits, the outer is still held: confirm via a
        # subprocess that tries to acquire and reports back.
        ctx = _get_mp_context()
        result_q = ctx.Queue()
        child = ctx.Process(target=_try_acquire_expect_busy, args=(palace, result_q))
        child.start()
        child.join(timeout=5)
        assert (
            result_q.get(timeout=1) == "busy"
        ), "outer lock should still be held by parent after inner re-entrant exit"


def _try_acquire_expect_busy(palace_path, result_q):
    """Helper: try to acquire, push 'busy' (raised) or 'free' (acquired) into queue."""
    try:
        with mine_palace_lock(palace_path):
            result_q.put("free")
    except MineAlreadyRunning:
        result_q.put("busy")


def test_mine_global_lock_is_alias_for_back_compat(tmp_path, monkeypatch):
    """Old callers of `mine_global_lock` should still work."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert mine_global_lock is mine_palace_lock
    with mine_global_lock(str(tmp_path / "palace")):
        pass  # the alias accepts the same palace_path argument
