"""Behavioral regressions for the PR 2654 SQLite reader repairs."""

import contextlib
import os
import sqlite3

import pytest

from mempalace.backends import _inproc_sqlite
from test_inproc_sqlite_locks import (
    _EXTERNAL_CLOSE,
    _EXTERNAL_SQLITE_WRITE,
    _SHARED_FIRST,
    _SHARED_SIZE,
    _SHM_DMS,
    _add,
    _chroma,
    _held,
    _open,
    _run,
    _sidecar_inodes,
)


def _force_helper(monkeypatch):
    monkeypatch.setattr(_inproc_sqlite, "_HOLD_LOCKS", True)
    monkeypatch.setattr(_inproc_sqlite, "_ANCHORED", False)
    monkeypatch.setattr(_inproc_sqlite, "_F_OFD_SETLK", None)


@pytest.mark.skipif(os.name != "posix", reason="cross-process POSIX locks")
def test_closed_fast_reader_allows_wal_checkpoint_progress(tmp_path, monkeypatch):
    """F5: lifetime guards must not retain the reader's old WAL snapshot."""
    _force_helper(monkeypatch)
    db = str(tmp_path / "chroma.sqlite3")
    with contextlib.closing(sqlite3.connect(db)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=10")
        writer.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, payload BLOB)")
        writer.execute("INSERT INTO t VALUES(1, zeroblob(4096))")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with _inproc_sqlite.open_reader(db) as reader:
            assert reader.execute("SELECT count(*) FROM t").fetchone() == (1,)
        holder = _inproc_sqlite._holders[_inproc_sqlite._key(db)]
        assert holder.ready and holder.alive()
        assert _held(db, _SHARED_FIRST, _SHARED_SIZE)
        assert _held(db + "-shm", _SHM_DMS, 1)
        for index in range(200):
            writer.execute("UPDATE t SET payload=?", (bytes([index]) * 4096,))
            writer.commit()
        busy, frames, checkpointed = writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        assert busy == 0 and checkpointed == frames, (
            "A closed fast reader must not pin old WAL frames",
            busy,
            frames,
            checkpointed,
        )
        assert os.stat(db + "-wal").st_size < 128 * 1024
        with _inproc_sqlite.open_reader(db) as reader:
            assert reader.execute("SELECT payload FROM t").fetchone() == (bytes([199]) * 4096,)


@pytest.mark.skipif(os.name != "posix", reason="cross-process POSIX locks")
def test_helper_preserves_sidecars_and_chroma_reopen_freshness(tmp_path, monkeypatch):
    """F5 control: ending a snapshot must retain both cross-process guards."""
    _force_helper(monkeypatch)
    backend, collection, db = _chroma(tmp_path, wal=True)
    try:
        _add(collection, 0)
        with _inproc_sqlite.open_reader(db) as reader:
            assert reader.execute("SELECT count(*) FROM embeddings").fetchone() == (1,)
        sidecars = _sidecar_inodes(db)
        _run(_EXTERNAL_CLOSE, db)
        assert _sidecar_inodes(db) == sidecars
        backend.close()
        # Only the helper/anchor now protect the sidecars from the next close.
        assert _held(db, _SHARED_FIRST, _SHARED_SIZE)
        assert _held(db + "-shm", _SHM_DMS, 1)
        _run(_EXTERNAL_CLOSE, db)
        assert _sidecar_inodes(db) == sidecars
        backend, collection = _open(str(tmp_path))
        _add(collection, 1)
        _add(collection, 2)
        assert _sidecar_inodes(db) == sidecars
        with _inproc_sqlite.open_reader(db) as reader:
            assert reader.execute("SELECT count(*) FROM embeddings").fetchone() == (3,)
    finally:
        backend.close()
        _inproc_sqlite.release(db)


@pytest.mark.skipif(os.name != "posix", reason="cross-process POSIX locks")
def test_active_reader_retains_its_snapshot_but_closed_reader_does_not(tmp_path, monkeypatch):
    """F5 control: a real caller's transaction still blocks premature truncation."""
    import json

    _force_helper(monkeypatch)
    db = str(tmp_path / "chroma.sqlite3")
    with contextlib.closing(sqlite3.connect(db)) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE t(x)")
        setup.execute("INSERT INTO t VALUES(0)")
        setup.commit()
    with _inproc_sqlite.open_reader(db) as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT count(*) FROM t").fetchone() == (1,)
        sidecars = _sidecar_inodes(db)
        during = json.loads(_run(_EXTERNAL_SQLITE_WRITE, db, 1, "checkpoint"))
        assert during == {"journal_mode": "wal", "rows": 2, "checkpoint_busy": 1}
        assert reader.execute("SELECT count(*) FROM t").fetchone() == (1,)
    after = json.loads(_run(_EXTERNAL_SQLITE_WRITE, db, 2, "checkpoint"))
    assert after == {"journal_mode": "wal", "rows": 3, "checkpoint_busy": 0}
    assert _sidecar_inodes(db) == sidecars
    with _inproc_sqlite.open_reader(db) as reader:
        assert reader.execute("SELECT count(*) FROM t").fetchone() == (3,)
