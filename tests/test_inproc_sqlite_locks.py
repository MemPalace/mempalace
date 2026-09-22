"""Python ``sqlite3`` readers must not break Chroma's SQLite in the same process (#2302).

ChromaDB opens ``chroma.sqlite3`` through its own statically linked SQLite, so
the fast paths that read the file through Python's ``sqlite3`` are a second
SQLite library on one file in one process. Closing their descriptor dropped
Chroma's POSIX locks for good, and their reads raced Chroma's commits because
fcntl locks never conflict within a process. See
:mod:`mempalace.backends._inproc_sqlite`.
"""

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from mempalace.backends import PalaceRef, _inproc_sqlite
from mempalace.backends.chroma import (
    ChromaBackend,
    _sqlite_embedding_count,
    _sqlite_wing_room_counts,
    sqlite_room_wing_hall_counts,
)
from mempalace.repair import sqlite_integrity_status

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX fcntl lock semantics only")
anchored_only = pytest.mark.skipif(
    not _inproc_sqlite._ANCHORED, reason="anchor guards need OFD locks (Linux)"
)

_NAME = "mempalace_drawers"

# os_unix.c lock bytes: SHARED_FIRST = PENDING_BYTE + 2, SHARED_SIZE = 510, and
# the -shm DMS byte at UNIX_SHM_BASE (120) + SQLITE_SHM_NLOCK (8).
_SHARED_FIRST = 0x40000000 + 2
_SHARED_SIZE = 510
_SHM_DMS = 120 + 8

# From another process, try a conflicting write lock on a byte range.
# EAGAIN/EACCES means this test process still holds a lock there.
_LOCK_PROBE = """
import fcntl, json, sys
path, start, size = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
with open(path, "r+b") as fd:
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, size, start)
    except OSError:
        print(json.dumps(True))
    else:
        fcntl.lockf(fd, fcntl.LOCK_UN, size, start)
        print(json.dumps(False))
"""

# An ordinary connection that reads and closes cleanly. If it can take
# EXCLUSIVE on close it checkpoints and unlinks the sidecars Chroma is using.
_EXTERNAL_CLOSE = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("select count(*) from sqlite_master").fetchone()
conn.close()
"""

_COUNT_EMBEDDINGS = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("select count(*) from embeddings").fetchone()[0])
conn.close()
"""


def _run(script: str, *args) -> str:
    return subprocess.run(
        [sys.executable, "-c", script, *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _held(path: str, start: int, size: int) -> bool:
    return json.loads(_run(_LOCK_PROBE, path, start, size))


def _held_elsewhere(lock) -> bool:
    """True when another thread cannot take ``lock`` right now."""
    got = []

    def attempt():
        acquired = lock.acquire(blocking=False)
        got.append(acquired)
        if acquired:
            lock.release()

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join()
    return not got[0]


def _open(palace: str):
    backend = ChromaBackend()
    col = backend.get_collection(
        palace=PalaceRef(id=palace, local_path=palace), collection_name=_NAME, create=True
    )
    return backend, col


def _chroma(tmp_path, *, wal: bool):
    palace = str(tmp_path)
    db = os.path.join(palace, "chroma.sqlite3")
    if wal:
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
    backend, col = _open(palace)
    return backend, col, db


def _add(col, i: int, batch: int = 1) -> None:
    col.add(
        ids=[f"d{i}-{k}" for k in range(batch)],
        documents=[f"palace drawer {i} {k} " + "memory " * 40 for k in range(batch)],
        metadatas=[{"wing": "w", "room": f"r{(i + k) % 3}"} for k in range(batch)],
        embeddings=[[float(i), float(k), 1.0] for k in range(batch)],
    )


def _sidecar_inodes(db: str) -> tuple:
    return tuple(os.stat(db + suffix).st_ino for suffix in ("-wal", "-shm"))


def _make_db(path, rows: int) -> None:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE t(x)")
        conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
        conn.commit()


@pytest.fixture(autouse=True)
def _no_anchors():
    _inproc_sqlite.release_all()
    yield
    _inproc_sqlite.release_all()


class _SpyCollection:
    def __init__(self, inner, on_write):
        self._inner = inner
        self._on_write = on_write

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def add(self, **kwargs):
        self._on_write()
        return self._inner.add(**kwargs)


def test_chroma_writes_hold_the_palace_lock(tmp_path):
    backend, col, db = _chroma(tmp_path, wal=False)
    lock = _inproc_sqlite.palace_db_lock(db)
    seen = []
    col._collection = _SpyCollection(col._collection, lambda: seen.append(_held_elsewhere(lock)))

    _add(col, 0)

    assert seen == [True], "Chroma's write ran without the palace lock"
    assert not _held_elsewhere(lock), "the write left the lock held"
    backend.close()


def test_reader_holds_the_lock_until_close(tmp_path):
    db = tmp_path / "chroma.sqlite3"
    _make_db(db, rows=2)
    lock = _inproc_sqlite.palace_db_lock(db)

    reader = _inproc_sqlite.open_reader(db)
    assert _held_elsewhere(lock)
    assert reader.execute("SELECT count(*) FROM t").fetchone() == (2,)
    reader.close()
    reader.close()  # idempotent

    assert not _held_elsewhere(lock)


def test_failed_open_releases_the_lock(tmp_path):
    missing = tmp_path / "absent" / "chroma.sqlite3"
    with pytest.raises(sqlite3.Error):
        _inproc_sqlite.open_reader(missing)
    assert not _held_elsewhere(_inproc_sqlite.palace_db_lock(missing))


def test_nested_and_repeated_reads_do_not_leak_connection_state(tmp_path):
    db = tmp_path / "chroma.sqlite3"
    _make_db(db, rows=1)

    outer = _inproc_sqlite.open_reader(db)
    outer.row_factory = sqlite3.Row
    inner = _inproc_sqlite.open_reader(db)  # same thread, while outer is open
    assert inner.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    inner.close()
    assert outer.execute("SELECT 1 AS one").fetchone()["one"] == 1
    outer.close()

    again = _inproc_sqlite.open_reader(db)
    try:
        assert again.row_factory is None
        assert not again.in_transaction
    finally:
        again.close()


def test_writer_commits_and_releases_on_exit(tmp_path):
    db = tmp_path / "chroma.sqlite3"
    _make_db(db, rows=0)
    lock = _inproc_sqlite.palace_db_lock(db)

    with _inproc_sqlite.open_writer(db) as conn:
        assert _held_elsewhere(lock)
        conn.execute("INSERT INTO t VALUES (1)")

    assert not _held_elsewhere(lock)
    with contextlib.closing(sqlite3.connect(db)) as check:
        assert check.execute("SELECT count(*) FROM t").fetchone() == (1,)


def test_replaced_database_is_read_afresh(tmp_path):
    db = tmp_path / "chroma.sqlite3"
    _make_db(db, rows=1)
    reader = _inproc_sqlite.open_reader(db)
    assert reader.execute("SELECT count(*) FROM t").fetchone() == (1,)
    reader.close()

    replacement = tmp_path / "replacement.sqlite3"
    _make_db(replacement, rows=2)
    os.replace(replacement, db)

    reader = _inproc_sqlite.open_reader(db)
    try:
        assert reader.execute("SELECT count(*) FROM t").fetchone() == (2,)
    finally:
        reader.close()


@posix_only
@pytest.mark.parametrize("wal", [False, True], ids=["rollback-journal", "wal"])
def test_quick_check_never_reads_a_chroma_commit_in_progress(tmp_path, wal):
    """Unpatched, about half of these probes failed with 'database disk image is malformed'."""
    backend, col, _db = _chroma(tmp_path, wal=wal)
    _add(col, 0)
    stop = threading.Event()
    writer_errors = []

    def writer():
        i = 1
        try:
            while not stop.is_set():
                _add(col, i, batch=25)
                i += 1
        except Exception as exc:  # surfaced below
            writer_errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    verdicts = []
    deadline = time.monotonic() + 2.0
    try:
        while time.monotonic() < deadline:
            verdicts.append(sqlite_integrity_status(str(tmp_path)).errors)
    finally:
        stop.set()
        thread.join()
        backend.close()

    assert not writer_errors
    assert len(verdicts) > 1
    assert [errors for errors in verdicts if errors] == []


@posix_only
@anchored_only
def test_fast_path_reads_keep_the_palace_locked(tmp_path):
    """Unpatched, the first read dropped SHARED and DMS and Chroma never took them back.

    A long-lived reader instead of the anchor made quick_check report a false
    ``malformed inverted index for FTS5 table`` here."""
    backend, col, db = _chroma(tmp_path, wal=True)
    palace = str(tmp_path)
    _add(col, 0)
    assert _held(db, _SHARED_FIRST, _SHARED_SIZE), "precondition: Chroma holds SHARED"
    assert _held(db + "-shm", _SHM_DMS, 1), "precondition: Chroma holds DMS"

    for i in range(1, 4):
        assert _sqlite_embedding_count(palace, _NAME) == i
        assert _sqlite_wing_room_counts(palace, _NAME) is not None
        assert sqlite_room_wing_hall_counts(palace, _NAME)
        assert sqlite_integrity_status(palace).errors == ()
        assert _held(db, _SHARED_FIRST, _SHARED_SIZE), "a fast-path read dropped SHARED"
        assert _held(db + "-shm", _SHM_DMS, 1), "a fast-path read dropped DMS"
        _add(col, i)

    before = _sidecar_inodes(db)
    _run(_EXTERNAL_CLOSE, db)
    assert _sidecar_inodes(db) == before, "an external close replaced the live sidecars"

    _add(col, 4)
    assert int(_run(_COUNT_EMBEDDINGS, db)) == 5, "a write after the external close was lost"
    backend.close()


@posix_only
@anchored_only
def test_chroma_reopen_keeps_python_reads_current(tmp_path):
    """Without the OFD guards, Chroma's reopen recreated -wal/-shm under the anchor
    and every Python connection kept reading the stale wal-index (1 of 3 rows)."""
    backend, col, db = _chroma(tmp_path, wal=True)
    palace = str(tmp_path)
    _add(col, 0)
    assert _sqlite_embedding_count(palace, _NAME) == 1
    before = _sidecar_inodes(db)

    backend.close()
    backend, col = _open(palace)
    _add(col, 1)
    _add(col, 2)

    assert _sidecar_inodes(db) == before
    assert _sqlite_embedding_count(palace, _NAME) == 3
    with contextlib.closing(sqlite3.connect(db)) as fresh:
        assert fresh.execute("select count(*) from embeddings").fetchone() == (3,)
    backend.close()
