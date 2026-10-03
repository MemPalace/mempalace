"""access_stats.py — How often, and how recently, each drawer was read.

Retrieval counts live in their own small SQLite file beside the palace
(``<palace>/access.sqlite3``), not in drawer metadata. A read therefore never
rewrites a drawer, never goes through the vector store's write path, and never
needs the palace writer lease; a search records its hits with one batched
upsert, and the increment happens inside SQLite so concurrent readers cannot
lose each other's counts.

This is local usage state, the same class as the index: it is not verbatim
content and nothing replicates it. Recording is best-effort. A failure here is
logged and never fails the read that triggered it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

ACCESS_DB_NAME = "access.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drawer_access (
    drawer_id TEXT PRIMARY KEY,
    access_count INTEGER NOT NULL DEFAULT 0,
    first_accessed TEXT NOT NULL,
    last_accessed TEXT NOT NULL
)
"""

_UPSERT = """
INSERT INTO drawer_access (drawer_id, access_count, first_accessed, last_accessed)
VALUES (?, 1, ?, ?)
ON CONFLICT(drawer_id) DO UPDATE SET
    access_count = access_count + 1,
    last_accessed = excluded.last_accessed
"""

_lock = threading.Lock()


def _db_path(palace_path: str) -> str:
    return os.path.join(palace_path, ACCESS_DB_NAME)


@contextlib.contextmanager
def _connection(palace_path: str, create: bool):
    """A short-lived connection, or ``None`` when there is nothing to open.

    Not cached: an open handle would pin the file, and on Windows that stops
    a palace directory from being moved or deleted while the server runs.
    """
    path = _db_path(palace_path)
    if not os.path.isdir(palace_path) or (not create and not os.path.exists(path)):
        yield None
        return
    conn = sqlite3.connect(path, timeout=5)
    try:
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
        yield conn
    finally:
        conn.close()


def record_access(palace_path: str, drawer_ids) -> None:
    """Count one read of each distinct id in ``drawer_ids``."""
    ids = list(dict.fromkeys(d for d in drawer_ids or () if isinstance(d, str) and d))
    if not palace_path or not ids:
        return
    now = datetime.now().isoformat()
    try:
        with _lock, _connection(palace_path, create=True) as conn:
            if conn is None:
                return
            with conn:
                conn.executemany(_UPSERT, [(d, now, now) for d in ids])
    except sqlite3.Error:
        logger.debug("access stats: record failed for %d ids", len(ids), exc_info=True)


def access_for(palace_path: str, drawer_ids) -> dict[str, dict]:
    """Return ``{drawer_id: {retrieval_count, last_retrieved}}`` for known ids."""
    ids = [d for d in drawer_ids or () if isinstance(d, str) and d]
    if not palace_path or not ids:
        return {}
    out: dict[str, dict] = {}
    try:
        with _lock, _connection(palace_path, create=False) as conn:
            if conn is None:
                return {}
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT drawer_id, access_count, last_accessed FROM drawer_access "
                    f"WHERE drawer_id IN ({marks})",
                    chunk,
                ).fetchall()
                for drawer_id, count, last in rows:
                    out[drawer_id] = {"retrieval_count": count, "last_retrieved": last}
    except sqlite3.Error:
        logger.debug("access stats: read failed", exc_info=True)
    return out
