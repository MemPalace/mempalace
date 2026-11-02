"""Verified bidirectional row copies for explicit workspace transitions.

The source is opened read-only in a snapshot. The destination must be empty.
Any mismatch rolls back every copied row, keeping the original file intact.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

from mempalace.sqlite_crypto import connect, row_factory

from .thread_frames import sql_string, string
from .thread_store import ThreadStore, canonical_sha256
from .thread_store_v2 import ThreadStoreV2
from .thread_store_v2_schema import FILENAME


class MigrationMismatch(ValueError):
    """The source's rows or the copied content fail lossless verification."""


def _auxiliary_rows(store, table):
    # Read-only sources from before B1c have no progress table to upgrade.
    if (
        table == "mirror_progress"
        and not store._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='mirror_progress'"
        ).fetchone()
    ):
        return []
    return store._conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


@contextmanager
def read_store(path, *, compact=False):
    """Read a snapshot without initializing or repairing the source file."""
    cls = ThreadStoreV2 if compact else ThreadStore
    store = cls.__new__(cls)
    store._lock = threading.RLock()
    store._conn = connect(
        Path(path).resolve().as_uri() + "?mode=ro",
        uri=True,
        isolation_level=None,
        check_same_thread=False,
    )
    store._conn.row_factory = row_factory
    try:
        store._conn.execute("BEGIN")
        store._fts_enabled = bool(
            store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?",
                ("chunk_fts" if compact else "chunks_fts",),
            ).fetchone()
        )
        yield store
    finally:
        store.close()


def verify_stores(source, target):
    """Compare identities, auxiliary rows, all fields, boundaries and digests."""
    for table in ("threads", "handoff_docs", "fact_extraction", "mirror_progress"):
        original = list(map(tuple, _auxiliary_rows(source, table)))
        copied = list(map(tuple, _auxiliary_rows(target, table)))
        if original != copied:
            raise MigrationMismatch("Copied thread or auxiliary rows mismatch")
    for row in source._conn.execute("SELECT * FROM threads ORDER BY id"):
        items = source.messages(row["id"])
        chunks = source.chunks(row["id"])
        if (
            not items
            or len(items) != row["message_count"]
            or len(chunks) != row["chunk_count"]
            or any(item.seq != seq for seq, item in enumerate(items))
            or any(chunk.index != idx for idx, chunk in enumerate(chunks))
            or items != target.messages(row["id"])
            or chunks != target.chunks(row["id"])
            or canonical_sha256(items) != row["sha256"]
            or canonical_sha256(target.messages(row["id"])) != row["sha256"]
        ):
            raise MigrationMismatch("Copied messages, chunks or digest mismatch")


def require_v1_representable(old):
    unsupported = 0
    for row in old._conn.execute("SELECT id FROM threads"):
        try:
            for item in old.messages(row["id"]):
                for value in (
                    item.role,
                    item.kind,
                    item.name,
                    item.call_id,
                    item.at,
                    item.text,
                    item.raw,
                ):
                    value.encode("utf-8")
        except UnicodeEncodeError:
            unsupported += 1
    unsupported_docs = 0
    for row in old._conn.execute("SELECT markdown FROM handoff_docs"):
        try:
            string(row[0]).encode("utf-8")
        except UnicodeEncodeError:
            unsupported_docs += 1
    if unsupported or unsupported_docs:
        raise MigrationMismatch(
            f"Rollback refused: {unsupported} threads and {unsupported_docs} documents "
            "cannot be represented by v1"
        )


def migrate_v2(source: str, destination: str) -> int:
    """Reverse row copy, including writes and append-time boundaries from v2."""
    if Path(source).resolve() == Path(destination).resolve():
        raise ValueError("Migration needs a separate v1 file")
    with read_store(source, compact=True) as old:
        require_v1_representable(old)
        new = ThreadStore(destination)
        try:
            with new._lock, new._conn:
                new._conn.execute("BEGIN IMMEDIATE")
                for table in (
                    "threads",
                    "messages",
                    "chunks",
                    "handoff_docs",
                    "fact_extraction",
                    "mirror_progress",
                ):
                    if new._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                        raise ValueError("Migration destination must be empty")
                rows = old._conn.execute("SELECT * FROM threads ORDER BY rowid").fetchall()
                for table in ("threads", "handoff_docs", "fact_extraction", "mirror_progress"):
                    for row in _auxiliary_rows(old, table):
                        placeholders = ",".join("?" for _ in row)
                        new._conn.execute(
                            f"INSERT INTO {table} ({','.join(row.keys())}) VALUES ({placeholders})",
                            tuple(row),
                        )
                for row in rows:
                    for item in old.messages(row["id"]):
                        new._conn.execute(
                            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
                            (
                                row["id"],
                                item.seq,
                                *map(
                                    sql_string,
                                    (
                                        item.role,
                                        item.kind,
                                        item.name,
                                        item.call_id,
                                        item.at,
                                        item.text,
                                        item.raw,
                                    ),
                                ),
                            ),
                        )
                    for chunk in old.chunks(row["id"]):
                        cursor = new._conn.execute(
                            "INSERT INTO chunks VALUES (?,?,?,?,?)",
                            (
                                row["id"],
                                chunk.index,
                                chunk.seq_start,
                                chunk.seq_end,
                                sql_string(chunk.text),
                            ),
                        )
                        if new._fts_enabled:
                            from .thread_store_v2 import _index_text

                            new._conn.execute(
                                "INSERT INTO chunks_fts(rowid,thread_id,idx,text) VALUES (?,?,?,?)",
                                (
                                    cursor.lastrowid,
                                    row["id"],
                                    chunk.index,
                                    _index_text(chunk.text, 0),
                                ),
                            )
                verify_stores(old, new)
                return len(rows)
        finally:
            new.close()


def migrate_v1(source: str, destination: str | None = None) -> int:
    source_path = Path(source).resolve()
    target = Path(destination) if destination is not None else source_path.with_name(FILENAME)
    if target.resolve() == source_path:
        raise ValueError("Migration needs a separate v2 file")
    old = ThreadStore.__new__(ThreadStore)
    old._lock = threading.RLock()
    old._conn = connect(source_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    old._conn.row_factory = row_factory
    try:
        old._conn.execute("BEGIN")
        new = ThreadStoreV2(str(target))
        try:
            return _copy(old, new)
        finally:
            new.close()
    finally:
        old.close()


def _copy(old, new):
    with new._lock, new._conn:
        new._conn.execute("BEGIN IMMEDIATE")
        for table in (
            "threads",
            "line",
            "message",
            "block",
            "blob",
            "dict",
            "chunk",
            "handoff_docs",
            "fact_extraction",
            "mirror_progress",
        ):
            if new._conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise ValueError("Migration destination must be empty")
        threads = old._conn.execute("SELECT * FROM threads ORDER BY rowid").fetchall()
        for row in threads:
            names = list(row.keys())
            new._conn.execute(
                f"INSERT INTO threads ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                tuple(row),
            )
        for row in threads:
            items = old.messages(row["id"])
            chunks = old.chunks(row["id"])
            if (
                not items
                or len(items) != row["message_count"]
                or len(chunks) != row["chunk_count"]
                or any(item.seq != seq for seq, item in enumerate(items))
                or any(chunk.index != idx for idx, chunk in enumerate(chunks))
                or canonical_sha256(items) != row["sha256"]
            ):
                raise MigrationMismatch("Source thread metadata or digest mismatch")
            cache = new._write_messages(row["id"], items)
            try:
                new._write_chunks(row["id"], chunks, items, cache=cache)
            except (ValueError, IndexError) as error:
                raise MigrationMismatch("Copied chunk text mismatch") from error
            rebuilt = new.messages(row["id"])
            if rebuilt != items:
                raise MigrationMismatch("Copied message fields mismatch")
            if new.chunks(row["id"]) != chunks:
                raise MigrationMismatch("Copied chunk boundaries or text mismatch")
            if canonical_sha256(rebuilt) != row["sha256"]:
                raise MigrationMismatch("Copied thread digest mismatch")
        copied_threads = new._conn.execute("SELECT * FROM threads ORDER BY id").fetchall()
        original_threads = old._conn.execute("SELECT * FROM threads ORDER BY id").fetchall()
        if list(map(tuple, copied_threads)) != list(map(tuple, original_threads)):
            raise MigrationMismatch("Copied thread rows mismatch")
        for table in ("handoff_docs", "fact_extraction", "mirror_progress"):
            rows = _auxiliary_rows(old, table)
            if rows:
                placeholders = ",".join("?" for _ in rows[0])
                new._conn.executemany(
                    f"INSERT INTO {table} VALUES ({placeholders})", map(tuple, rows)
                )
            copied = _auxiliary_rows(new, table)
            original = _auxiliary_rows(old, table)
            if list(map(tuple, copied)) != list(map(tuple, original)):
                raise MigrationMismatch("Copied auxiliary rows mismatch")
        return len(threads)
