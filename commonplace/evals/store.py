"""The per-workspace eval DB: immutable runs, ordered chunks, payload-free tombstones.

``<workspace>/evals/runs.sqlite3`` is opened through ``sqlite_crypto.connect``,
so an encrypted workspace encrypts it with the workspace key before the file
exists. Triggers reject UPDATE and DELETE on ``runs`` and ``chunks`` unless the
connection-local purge capability is armed; only :func:`purge` arms it, inside
its own transaction, and disarms it on commit, rollback or error.
"""

from __future__ import annotations

import json
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path

from mempalace import sqlite_crypto

from . import (
    EVAL_CHUNK_BYTES,
    EVAL_KEY_TOMBSTONE_RETENTION,
    EVAL_LOCK_WAIT_S,
    EVAL_WORKSPACE_QUOTA,
    EvalError,
)

DIRNAME = "evals"
FILENAME = "runs.sqlite3"
SIDECARS = ("-wal", "-shm", "-journal")
_GUARDED = ("runs", "chunks")
_DDL = (
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        key_digest TEXT NOT NULL UNIQUE,
        content_id TEXT NOT NULL,
        stored_id TEXT NOT NULL,
        source_thread_id TEXT NOT NULL,
        message_count INTEGER NOT NULL,
        source_sha256 TEXT NOT NULL,
        rows_sha256 TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        rows_bytes INTEGER NOT NULL,
        manifest_bytes INTEGER NOT NULL,
        artifact_bytes INTEGER NOT NULL,
        total_bytes INTEGER NOT NULL,
        artifacts TEXT NOT NULL,
        scanner_codes TEXT NOT NULL,
        scanner_version TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS chunks (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        object TEXT NOT NULL,
        idx INTEGER NOT NULL,
        data BLOB NOT NULL,
        PRIMARY KEY (run_id, object, idx))""",
    """CREATE TABLE IF NOT EXISTS tombstones (
        run_id TEXT PRIMARY KEY,
        key_digest TEXT UNIQUE,
        purged_at REAL NOT NULL)""",
    *(
        f"""CREATE TRIGGER IF NOT EXISTS {table}_{op.lower()}_guard BEFORE {op} ON {table}
        WHEN eval_purge_armed() IS NOT 1
        BEGIN SELECT RAISE(ABORT, 'eval_immutable'); END"""
        for table in _GUARDED
        for op in ("UPDATE", "DELETE")
    ),
)


def _now() -> float:
    return time.time()


def paths(directory):
    evals = Path(directory) / DIRNAME
    return evals, evals / FILENAME


def _fix(path, evals, info):
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        return f"{path} (wrong owner or symlink: the admin must fix it)"
    if path == evals:
        return f"{path} (chmod 0700 {path})"
    if path.parent == evals:
        return f"{path} (chmod 0600 {path})"
    return f"{path} (chmod go-w {path})"


def check_permissions(data_dir, directory):
    """Refuse loose modes, foreign owners and symlinks; never chmod or chown."""
    data = Path(data_dir).absolute()
    evals, db = paths(Path(directory).absolute())
    uid = os.getuid()
    offending = []
    ancestors = [evals.parent]
    while ancestors[-1] != data and data in ancestors[-1].parents:
        ancestors.append(ancestors[-1].parent)
    for path in ancestors:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if (path != data and stat.S_ISLNK(info.st_mode)) or info.st_mode & 0o022:
            offending.append(_fix(path, evals, info))
    private = [evals, db, *(db.with_name(db.name + suffix) for suffix in SIDECARS)]
    for path in private:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
            offending.append(_fix(path, evals, info))
    if offending:
        raise EvalError("eval_permissions: " + "; ".join(offending))
    return evals, db


class Capability:
    """Connection-local purge capability; armed only inside :func:`purge`."""

    def __init__(self):
        self.armed = False

    def __call__(self):
        return 1 if self.armed else 0


def _connect(db, **kwargs):
    conn = sqlite_crypto.connect(
        str(db), isolation_level=None, timeout=EVAL_LOCK_WAIT_S, check_same_thread=False, **kwargs
    )
    conn.row_factory = sqlite_crypto.row_factory
    return conn


def open_write(data_dir, directory, *, create):
    """Open for archive or purge: ``(conn, capability)``, or None when absent."""
    evals, db = check_permissions(data_dir, directory)
    if not db.exists():
        if not create:
            return None
        if not evals.exists():
            os.mkdir(evals, 0o700)
    # The DB and its sidecars are created 0600: SQLite gives sidecars the
    # main file's mode. Encryption settings and keys resolve before create.
    old = os.umask(0o077)
    try:
        conn = _connect(db)
    finally:
        os.umask(old)
    try:
        check_permissions(data_dir, directory)
        capability = Capability()
        conn.create_function("eval_purge_armed", 0, capability)
        # Restored or converted copies may be in DELETE mode. The switch must
        # run outside any transaction; a held reader makes it fail as busy.
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except Exception as exc:
            raise EvalError("eval_busy") from exc
        if str(mode).lower() != "wal":
            raise EvalError("eval_busy")
        return conn, capability
    except BaseException:
        conn.close()
        raise


def open_read(data_dir, directory):
    """Read-only open; None when no eval DB exists. Nothing is created."""
    evals, db = check_permissions(data_dir, directory)
    if not db.exists():
        return None
    conn = _connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        check_permissions(data_dir, directory)
        return conn
    except BaseException:
        conn.close()
        raise


def ensure_schema(conn):
    for statement in _DDL:
        conn.execute(statement)


def has_schema(conn) -> bool:
    return (
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('runs', 'chunks', 'tombstones')"
        ).fetchone()[0]
        == 3
    )


def clear_expired(conn, now):
    """Release keys whose retention has passed; a clock behind ``purged_at`` keeps them."""
    conn.execute(
        "UPDATE tombstones SET key_digest = NULL WHERE key_digest IS NOT NULL "
        "AND ? >= purged_at AND ? - purged_at >= ?",
        (now, now, EVAL_KEY_TOMBSTONE_RETENTION),
    )


def _retired(conn, digest, now) -> bool:
    row = conn.execute(
        "SELECT purged_at FROM tombstones WHERE key_digest = ?", (digest,)
    ).fetchone()
    if row is None:
        return False
    purged_at = row[0]
    return now < purged_at or now - purged_at < EVAL_KEY_TOMBSTONE_RETENTION


def _insert_chunk(conn, run_id, name, idx, data):
    conn.execute(
        "INSERT INTO chunks(run_id, object, idx, data) VALUES (?, ?, ?, ?)",
        (run_id, name, idx, data),
    )


def _write_object(conn, run_id, name, data):
    for idx, offset in enumerate(range(0, max(len(data), 1), EVAL_CHUNK_BYTES)):
        _insert_chunk(conn, run_id, name, idx, data[offset : offset + EVAL_CHUNK_BYTES])


def object_bytes(conn, run_id, name) -> bytes:
    return b"".join(
        bytes(row[0])
        for row in conn.execute(
            "SELECT data FROM chunks WHERE run_id = ? AND object = ? ORDER BY idx",
            (run_id, name),
        )
    )


def object_range(conn, run_id, name, offset, length) -> bytes:
    """Decoded bytes ``[offset, offset + length)`` of one object, from its chunks."""
    first, last = offset // EVAL_CHUNK_BYTES, (offset + length - 1) // EVAL_CHUNK_BYTES
    data = b"".join(
        bytes(row[0])
        for row in conn.execute(
            "SELECT data FROM chunks WHERE run_id = ? AND object = ? AND idx BETWEEN ? AND ? "
            "ORDER BY idx",
            (run_id, name, first, last),
        )
    )
    start = offset - first * EVAL_CHUNK_BYTES
    return data[start : start + length]


def physical_bound(conn, db) -> int:
    """The thread store's conservative bound for one SQLite file, in-transaction."""
    # SQLCipher reports page_size as text.
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    pages = int(conn.execute("PRAGMA page_count").fetchone()[0])

    def size(suffix):
        candidate = Path(str(db) + suffix)
        return candidate.stat().st_size if candidate.exists() else 0

    return (
        max(size(""), pages * page_size)
        + size("-wal")
        + max(size("-shm"), 32768)
        + 32
        + pages * (page_size + 24)
    )


def file_bytes(db) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (Path(str(db) + suffix) for suffix in ("", "-wal", "-shm"))
        if candidate.exists()
    )


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        checkpoint("before_commit")
        conn.execute("COMMIT")


def checkpoint(stage):
    """Test seam: called at fixed points; production does nothing."""


def commit_run(conn, run, objects, *, verify, storage=None):
    """Insert one whole run atomically, or return an existing run for the same key.

    ``objects`` maps object names to bytes. ``verify(conn)`` re-reads and
    compares the inserted chunks; ``storage(conn)`` applies the plan bound.
    Returns ``(run_id, created_at, reused)``.
    """
    now = _now()
    with transaction(conn):
        ensure_schema(conn)
        clear_expired(conn, now)
        existing = conn.execute(
            "SELECT run_id, created_at, content_id FROM runs WHERE key_digest = ?",
            (run["key_digest"],),
        ).fetchone()
        if existing is not None:
            if existing["content_id"] != run["content_id"]:
                raise EvalError("eval_conflict")
            return existing["run_id"], existing["created_at"], True
        if _retired(conn, run["key_digest"], now):
            raise EvalError("eval_key_retired")
        used = conn.execute("SELECT coalesce(sum(total_bytes), 0) FROM runs").fetchone()[0]
        if used + run["total_bytes"] > EVAL_WORKSPACE_QUOTA:
            raise EvalError("eval_quota")
        columns = list(run)
        conn.execute(
            f"INSERT INTO runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [run[column] for column in columns],
        )
        for name, data in objects.items():
            _write_object(conn, run["run_id"], name, data)
        verify(conn)
        if storage is not None:
            storage(conn)
    return run["run_id"], run["created_at"], False


def purge(conn, capability, run_id, workspace_id):
    """Delete one run and its chunks, write its tombstone; identical for any ID."""
    now = _now()
    with transaction(conn):
        ensure_schema(conn)
        clear_expired(conn, now)
        row = conn.execute(
            "SELECT key_digest FROM runs WHERE run_id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        ).fetchone()
        if row is None:
            return
        capability.armed = True
        try:
            conn.execute("DELETE FROM chunks WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        finally:
            capability.armed = False
        conn.execute(
            "INSERT INTO tombstones(run_id, key_digest, purged_at) VALUES (?, ?, ?)",
            (run_id, row["key_digest"], now),
        )


def runs(conn, workspace_id):
    if not has_schema(conn):
        return []
    return conn.execute(
        "SELECT * FROM runs WHERE workspace_id = ? ORDER BY created_at, run_id", (workspace_id,)
    ).fetchall()


def run(conn, run_id, workspace_id):
    if not has_schema(conn):
        return None
    return conn.execute(
        "SELECT * FROM runs WHERE run_id = ? AND workspace_id = ?", (run_id, workspace_id)
    ).fetchone()


def artifact_list(row):
    return json.loads(row["artifacts"])
