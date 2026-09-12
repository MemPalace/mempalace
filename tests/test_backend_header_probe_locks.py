"""Header probes must not drop the writer's POSIX locks.

``SQLiteExactBackend.detect()``, ``ChromaBackend.detect()`` and
``SQLiteExactBackend._database_signature()`` used to ``open()`` the palace
database directly. POSIX fcntl locks belong to the (process, inode) pair, so
closing that plain descriptor dropped every lock the process held on the
file -- including the SHARED lock the long-lived WAL writer keeps for its
whole life. From then on any external connection's clean ``close()`` could
checkpoint and unlink ``-wal`` / ``-shm`` underneath the writer, which kept
writing into the unlinked inodes with no error (silent data loss).

These tests pin the mechanism (the lock survives the probes), the symptom
(sidecars survive an external close, later writes stay visible) and the
#1893 behaviour the probe was introduced for.
"""

import builtins
import io
import json
import os
import sqlite3
import subprocess
import sys

import pytest
from _chroma_palace_helper import make_minimal_chroma_sqlite, make_minimal_sqlite_exact_sqlite

from mempalace.backends import PalaceRef, _magic
from mempalace.backends.chroma import ChromaBackend
from mempalace.backends.sqlite_exact import _DB_FILENAME, SQLiteExactBackend
from mempalace.palace import resolve_backend_name

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX fcntl lock semantics only")

# SQLite's lock bytes (os_unix.c): PENDING_BYTE = 0x40000000, SHARED_FIRST =
# PENDING_BYTE + 2, SHARED_SIZE = 510. A WAL-mode connection holds a read
# lock on the SHARED range for its whole life.
_SHARED_FIRST = 0x40000000 + 2
_SHARED_SIZE = 510

# Try to take a conflicting *write* lock on the SHARED range from another
# process. EAGAIN/EACCES means somebody (the writer) still holds it.
_LOCK_PROBE = f"""
import fcntl, json, sys
fd = open(sys.argv[1], "r+b")
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, {_SHARED_SIZE}, {_SHARED_FIRST})
except OSError:
    print(json.dumps({{"held": True}}))
else:
    fcntl.lockf(fd, fcntl.LOCK_UN, {_SHARED_SIZE}, {_SHARED_FIRST})
    print(json.dumps({{"held": False}}))
"""

# The "attack": an ordinary read-write connection that does one read and
# closes cleanly. sqlite3WalClose() then tries for an EXCLUSIVE lock; if it
# gets it (because the writer's SHARED lock is gone) it checkpoints and
# deletes the -wal/-shm files the writer is still using.
_EXTERNAL_CLOSE = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("select count(*) from sqlite_master").fetchone()
conn.close()
"""

_COUNT_ROWS = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("select count(*) from documents").fetchone()[0])
conn.close()
"""


def _run(script: str, db_path: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", script, db_path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _shared_lock_held(db_path: str) -> bool:
    return json.loads(_run(_LOCK_PROBE, db_path))["held"]


def _writer(tmp_path, name="mempalace_drawers"):
    backend = SQLiteExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend, backend.get_collection(palace=palace, collection_name=name, create=True)


def _add(col, i: int) -> None:
    col.add(
        ids=[f"d{i}"],
        documents=[f"doc {i}"],
        metadatas=[{"i": i}],
        embeddings=[[1.0, float(i)]],
    )


def _sidecar_inodes(db_path: str) -> tuple:
    return tuple(os.stat(db_path + suffix).st_ino for suffix in ("-wal", "-shm"))


@pytest.fixture(autouse=True)
def _cold_probe_cache():
    _magic.forget()
    yield
    _magic.forget()


def test_probes_open_no_plain_descriptor_on_live_databases(tmp_path, monkeypatch):
    """No probe may open the database through anything but SQLite.

    The cache is cold, so this exercises the real probe, not a cache hit.
    """
    backend, col = _writer(tmp_path / "exact")
    _add(col, 0)
    exact_db = os.path.join(str(tmp_path / "exact"), _DB_FILENAME)
    (tmp_path / "chroma").mkdir()
    chroma_db = str(make_minimal_chroma_sqlite(tmp_path / "chroma"))

    opened = []

    def record(open_fn):
        def wrapper(file, *args, **kwargs):
            opened.append(os.fspath(file) if not isinstance(file, int) else file)
            return open_fn(file, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(builtins, "open", record(builtins.open))
    monkeypatch.setattr(io, "open", record(io.open))
    monkeypatch.setattr(os, "open", record(os.open))

    assert SQLiteExactBackend.detect(str(tmp_path / "exact")) is True
    assert SQLiteExactBackend.detect(str(tmp_path / "exact")) is True
    assert ChromaBackend.detect(str(tmp_path / "chroma")) is True
    assert resolve_backend_name(str(tmp_path / "exact")) == "sqlite_exact"
    backend._database_signature(exact_db)
    backend._database_signature(exact_db)

    touched = {p for p in opened if isinstance(p, str) and p.endswith(".sqlite3")}
    assert exact_db not in touched
    assert chroma_db not in touched
    backend.close()


@posix_only
def test_probes_keep_writer_shared_lock(tmp_path):
    """Mechanism: the writer's SHARED lock survives every probe.

    Unpatched, the first ``detect()`` after the writer opened dropped it.
    """
    backend, col = _writer(tmp_path)
    _add(col, 0)
    db_path = os.path.join(str(tmp_path), _DB_FILENAME)
    assert _shared_lock_held(db_path), "precondition: WAL writer holds SHARED"

    for _ in range(3):
        assert SQLiteExactBackend.detect(str(tmp_path)) is True
        assert resolve_backend_name(str(tmp_path)) == "sqlite_exact"
        backend._database_signature(db_path)
        assert _shared_lock_held(db_path)

    backend.close()
    assert not _shared_lock_held(db_path), "closing the writer releases the lock"


@posix_only
def test_external_close_cannot_orphan_wal_after_probes(tmp_path):
    """Symptom: an external clean close must not checkpoint+unlink the live WAL.

    Three tool-level operations (each goes through ``resolve_backend_name``,
    i.e. ``detect()``), then the attack, then one more write that a fresh
    process must be able to see.

    Whether the attacking close actually unlinks the sidecars depends on the
    attacker's SQLite build: observed with 3.50.4 (10/10 writes lost
    unpatched), not with 3.53.3, which leaves the sidecars alone even though
    the writer's lock is gone. ``test_probes_keep_writer_shared_lock`` is the
    build-independent guard; this test pins the user-visible symptom where the
    build reproduces it.
    """
    backend, col = _writer(tmp_path)
    db_path = os.path.join(str(tmp_path), _DB_FILENAME)
    for i in range(3):
        assert resolve_backend_name(str(tmp_path)) == "sqlite_exact"
        _add(col, i)
    before = _sidecar_inodes(db_path)

    _run(_EXTERNAL_CLOSE, db_path)

    assert os.path.exists(db_path + "-wal"), "external close unlinked the live WAL"
    assert _sidecar_inodes(db_path) == before, "external close replaced the sidecars"

    _add(col, 3)
    assert int(_run(_COUNT_ROWS, db_path)) == 4, "write after the attack is invisible"
    backend.close()


def test_detect_rejects_empty_then_accepts_written_header(tmp_path):
    """#1893: a 0-byte file is not a palace until its header lands.

    Negative probes are not cached, positive ones are keyed by inode, so a
    replaced file is re-probed.
    """
    db = tmp_path / _DB_FILENAME
    db.write_bytes(b"")
    assert SQLiteExactBackend.detect(str(tmp_path)) is False

    make_minimal_sqlite_exact_sqlite(tmp_path)
    assert SQLiteExactBackend.detect(str(tmp_path)) is True
    assert SQLiteExactBackend.detect(str(tmp_path)) is True  # cache hit

    garbage = tmp_path / "garbage"
    garbage.write_bytes(b"not a sqlite file" * 64)
    os.replace(garbage, db)  # new inode at the same path
    assert SQLiteExactBackend.detect(str(tmp_path)) is False

    marker = tmp_path / "marker"
    marker.mkdir()
    (marker / _DB_FILENAME).write_bytes(_magic.SQLITE_MAGIC)  # magic only, no page 1
    assert SQLiteExactBackend.detect(str(marker)) is True

    chroma = tmp_path / "chroma"
    chroma.mkdir()
    (chroma / "chroma.sqlite3").write_bytes(b"")
    assert ChromaBackend.detect(str(chroma)) is False
    make_minimal_chroma_sqlite(chroma)
    assert ChromaBackend.detect(str(chroma)) is True


def test_database_signature_tracks_changes(tmp_path):
    backend, col = _writer(tmp_path)
    db_path = os.path.join(str(tmp_path), _DB_FILENAME)
    first = backend._database_signature(db_path)
    assert first[-1] is not None, "header fields must be readable on a live palace"

    _add(col, 0)
    col._handle.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    second = backend._database_signature(db_path)
    assert second != first
    backend.close()

    with pytest.raises(OSError):
        backend._database_signature(os.path.join(str(tmp_path), "missing.sqlite3"))

    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"not a sqlite file" * 64)
    assert backend._database_signature(str(garbage))[-1] is None


def test_read_header_fields_reads_through_sqlite_only(tmp_path):
    db = tmp_path / "x.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 7")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    fields = _magic.read_header_fields(str(db))
    assert fields is not None
    assert fields[3] == 7  # user_version
    assert _magic.read_header_fields(str(tmp_path)) is None  # directory
    (tmp_path / "empty").write_bytes(b"")
    assert _magic.read_header_fields(str(tmp_path / "empty")) is None
