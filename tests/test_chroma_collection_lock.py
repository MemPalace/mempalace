"""Tests for ChromaCollection's palace-write-lock integration.

Closes the gap left by ``mine_palace_lock`` only protecting the
``mempalace mine`` pipeline: MCP/direct writers that call
``ChromaCollection.add/upsert/update/delete`` must also serialize against
mine and against each other to avoid the multi-threaded HNSW corruption
documented in #974/#965.

Property tested:

* ``ChromaCollection(c, palace_path=p)`` wraps every write with
  ``mine_palace_lock(p)``.
* Writes raise ``MineAlreadyRunning`` when another holder owns the lock
  (instead of silently racing into the underlying chromadb call).
* Re-entrant composition with ``miner.mine()`` does not self-deadlock:
  ``with mine_palace_lock(p): col.upsert(...)`` runs to completion.
* ``ChromaCollection(c)`` (no palace_path) preserves legacy no-lock
  behaviour for tests/callers that build the adapter directly without
  going through ``ChromaBackend``.

POSIX-only: ``mine_palace_lock`` uses ``fcntl`` on Unix and ``msvcrt`` on
Windows; the contention semantics differ enough that the cross-process
tests are skipped on Windows runners.
"""

from __future__ import annotations

import multiprocessing
import os
import time

import pytest

from mempalace.backends.chroma import ChromaCollection
from mempalace.palace import MineAlreadyRunning, mine_palace_lock


def _get_mp_context():
    """Same start-method picker as test_palace_locks.py."""
    start_method = "spawn" if os.name == "nt" else "fork"
    return multiprocessing.get_context(start_method)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeChromaCollection:
    """Records calls; never blocks. Stand-in for chromadb.Collection."""

    def __init__(self):
        self.adds: list[dict] = []
        self.upserts: list[dict] = []
        self.updates: list[dict] = []
        self.deletes: list[dict] = []

    def add(self, **kwargs):
        self.adds.append(kwargs)

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def delete(self, **kwargs):
        self.deletes.append(kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hold_lock(palace_path: str, ready_flag: str, release_flag: str) -> int:
    """Acquire ``mine_palace_lock``, signal readiness, wait for release.

    Mirrors the helper in ``test_palace_locks.py`` so the contention
    semantics match across both test files.
    """
    try:
        with mine_palace_lock(palace_path):
            open(ready_flag, "w").close()
            for _ in range(500):
                if os.path.exists(release_flag):
                    return 0
                time.sleep(0.01)
            return 0
    except MineAlreadyRunning:
        return 1


# ---------------------------------------------------------------------------
# Tests — opt-in lock wiring
# ---------------------------------------------------------------------------


def test_palace_path_none_skips_lock(tmp_path, monkeypatch):
    """Legacy callers (``ChromaCollection(c)``) keep no-lock behaviour.

    A ``ChromaCollection`` built without ``palace_path`` must not touch the
    lock infrastructure at all. This guards against regressions where a
    test or third-party caller relies on the historical bare-write path.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = _FakeChromaCollection()
    col = ChromaCollection(fake)  # no palace_path -> no lock

    # Hold the lock in a child process. Without palace_path, the parent
    # write must still succeed (the lock does not gate this caller).
    palace = str(tmp_path / "palace")
    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")
    ctx = _get_mp_context()
    holder = ctx.Process(target=_hold_lock, args=(palace, ready, release))
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "holder failed to acquire lock"

        col.upsert(documents=["doc"], ids=["id-1"])
        assert fake.upserts == [{"documents": ["doc"], "ids": ["id-1"]}]
    finally:
        open(release, "w").close()
        holder.join(timeout=5)


def test_writer_blocks_during_mine(tmp_path, monkeypatch):
    """A held ``mine_palace_lock`` causes ``ChromaCollection`` writes to raise.

    This is the property that closes the MCP-bypass gap: when a mine is in
    flight, MCP/direct writes raise ``MineAlreadyRunning`` rather than
    silently entering chromadb's write path concurrent with mine.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    palace = str(tmp_path / "palace")
    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")

    ctx = _get_mp_context()
    holder = ctx.Process(target=_hold_lock, args=(palace, ready, release))
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "holder failed to acquire lock"

        fake = _FakeChromaCollection()
        col = ChromaCollection(fake, palace_path=palace)

        with pytest.raises(MineAlreadyRunning):
            col.upsert(documents=["doc"], ids=["id-1"])
        with pytest.raises(MineAlreadyRunning):
            col.add(documents=["doc"], ids=["id-2"])
        with pytest.raises(MineAlreadyRunning):
            col.update(ids=["id-3"], documents=["doc"])
        with pytest.raises(MineAlreadyRunning):
            col.delete(ids=["id-4"])

        # The fake must have received NO calls — the lock must gate
        # before reaching the underlying chromadb layer.
        assert fake.upserts == []
        assert fake.adds == []
        assert fake.updates == []
        assert fake.deletes == []
    finally:
        open(release, "w").close()
        holder.join(timeout=5)


def test_reentrant_inside_mine_passes_through(tmp_path, monkeypatch):
    """``ChromaCollection.upsert`` inside ``mine_palace_lock`` does not deadlock.

    ``miner.mine()`` already holds ``mine_palace_lock(palace_path)`` for the
    full mine pipeline; ``_mine_body`` then calls
    ``collection.upsert(...)``. With the per-thread re-entrant guard in
    ``mine_palace_lock``, the inner acquire is a pass-through and the
    underlying chromadb call runs immediately.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    palace = str(tmp_path / "palace")
    fake = _FakeChromaCollection()
    col = ChromaCollection(fake, palace_path=palace)

    with mine_palace_lock(palace):
        # If the re-entrant guard were missing, this would self-deadlock on
        # the underlying flock. We rely on pytest-timeout (configured in
        # pyproject.toml) to enforce this in CI; the assertion just confirms
        # the call landed.
        col.upsert(documents=["d"], ids=["i"], metadatas=[{"k": "v"}])
        col.add(documents=["d2"], ids=["i2"])
        col.update(ids=["i"], documents=["d-updated"])
        col.delete(ids=["i2"])

    assert len(fake.upserts) == 1
    assert len(fake.adds) == 1
    assert len(fake.updates) == 1
    assert len(fake.deletes) == 1


class _SlowFakeChromaCollection(_FakeChromaCollection):
    """Fake whose write methods hold the caller for ``hold_seconds``.

    Used to keep ``mine_palace_lock`` acquired long enough for a sibling
    process to contend deterministically.
    """

    def __init__(self, hold_seconds: float = 0.3):
        super().__init__()
        self._hold = hold_seconds

    def upsert(self, **kwargs):
        time.sleep(self._hold)
        super().upsert(**kwargs)


def _slow_writer_target(palace_path, tmp_path_str, pid, result_q):
    """Subprocess target: try a slow upsert, report ok/busy."""
    os.environ["HOME"] = tmp_path_str
    # Fresh import inside child so HOME monkeypatch routes the lock dir.
    from mempalace.backends.chroma import ChromaCollection as _CC
    from mempalace.palace import MineAlreadyRunning as _MAR

    fake = _SlowFakeChromaCollection(hold_seconds=0.3)
    col = _CC(fake, palace_path=palace_path)
    try:
        col.upsert(documents=[f"d{pid}"], ids=[f"i{pid}"])
        result_q.put(("ok", pid))
    except _MAR:
        result_q.put(("busy", pid))


def test_concurrent_writers_serialize(tmp_path, monkeypatch):
    """Two processes calling ``ChromaCollection.upsert`` against the same
    palace must be serialized: at most one enters chromadb at a time, the
    other raises ``MineAlreadyRunning``.

    This is the property that prevents the parallel HNSW insert race that
    drives #974/#965 — under concurrent MCP write fan-out, exactly one
    writer reaches chromadb and the rest fail loudly instead of corrupting
    the index.

    The slow fake holds the lock for 0.3s per writer, large enough for the
    second process to contend even on slow CI runners.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    palace = str(tmp_path / "palace")

    ctx = _get_mp_context()
    result_q = ctx.Queue()

    p1 = ctx.Process(target=_slow_writer_target, args=(palace, str(tmp_path), 1, result_q))
    p2 = ctx.Process(target=_slow_writer_target, args=(palace, str(tmp_path), 2, result_q))
    p1.start()
    # Tiny stagger so p1 wins the race deterministically; without it the
    # OS scheduler can pick either, which is also a valid outcome but
    # makes the assertion brittle on slow CI.
    time.sleep(0.05)
    p2.start()
    p1.join(timeout=5)
    p2.join(timeout=5)

    outcomes = [result_q.get(timeout=1) for _ in range(2)]
    statuses = sorted(o[0] for o in outcomes)
    assert statuses == ["busy", "ok"], f"expected one ok + one busy, got {outcomes}"


def test_read_path_does_not_acquire_lock(tmp_path, monkeypatch):
    """``query`` / ``get`` / ``count`` must not be gated by the write lock.

    Read traffic is the dominant workload (semantic search, MCP get, etc.)
    and serializing it against mine would tank latency for no correctness
    benefit. This test pins that property: with another process holding
    the write lock, reads must still complete instantly.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    palace = str(tmp_path / "palace")
    ready = str(tmp_path / "ready")
    release = str(tmp_path / "release")

    ctx = _get_mp_context()
    holder = ctx.Process(target=_hold_lock, args=(palace, ready, release))
    holder.start()
    try:
        for _ in range(500):
            if os.path.exists(ready):
                break
            time.sleep(0.01)
        assert os.path.exists(ready), "holder failed to acquire lock"

        # _FakeChromaCollection doesn't implement query/get/count; we only
        # need to confirm the wrapper does not call into mine_palace_lock
        # for reads, which we assert by observing the wrapped methods are
        # NOT in ChromaCollection's _write_lock path. A direct check via
        # source inspection is more honest than mocking the entire chroma
        # surface here.
        import inspect

        from mempalace.backends.chroma import ChromaCollection as _CC

        for write_attr in ("add", "upsert", "update", "delete"):
            src = inspect.getsource(getattr(_CC, write_attr))
            assert "_write_lock" in src, f"{write_attr} should acquire write lock"

        for read_attr in ("query", "get", "count"):
            method = getattr(_CC, read_attr, None)
            if method is None:
                continue
            src = inspect.getsource(method)
            assert (
                "_write_lock" not in src
            ), f"{read_attr} must NOT acquire the write lock (read path)"
    finally:
        open(release, "w").close()
        holder.join(timeout=5)
