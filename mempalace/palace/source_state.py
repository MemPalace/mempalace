"""Per-source mining state: one record per source stream (the RFC 007 ``source_head``).

This is a regular module, not one of the fragments ``mempalace.palace``
execs into its namespace, and it depends only on the standard library.

A *source stream* is one miner's view of one source file: the path, the
producer that files it (``project``, ``convo``, ``format``, ...), the
extraction mode and the destination wing. Its record says how many drawers
the last complete filing produced (``chunk_total``), which snapshot of the
file it read (``source_fingerprint`` / ``source_mtime``), which normalizer and
chunker revisions produced it, and whether that filing finished
(``pending`` / ``complete``).

Before this record, that bookkeeping lived on every drawer row, so a re-mine
that changed one drawer still rewrote all of them to restamp it. With the
record, a re-mine writes only drawers that changed, then flips the one record.

Crash safety (no source may read as mined while drawers are missing): a writer marks the record
``pending`` before its first drawer write and ``complete`` in one transaction
only after its drawers are durable and the stale ones are pruned. A crash in
between leaves it pending, which reads as "not mined", so the next mine redoes
the file.

A record never proves a source mined on its own. The readers in
``mempalace.palace`` also count the stream's drawers and require at least
``chunk_total`` of them, so drawers deleted behind the record's back (delete
by source, a wing move, a recreated palace) still read as "not mined".

Records live in ``source_state.sqlite3`` in the palace directory, beside the
drawers and in the same privacy domain. Each call opens and closes its own
connection: concurrent hook mines file different sources into the same
database, and a lingering handle keeps files locked on Windows.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from ..timeutil import utc_now_iso

logger = logging.getLogger(__name__)

SOURCE_STATE_FILENAME = "source_state.sqlite3"

PENDING = "pending"
COMPLETE = "complete"

_DDL = """
CREATE TABLE IF NOT EXISTS source_head (
    source_file TEXT NOT NULL,
    owner TEXT NOT NULL,
    extract_mode TEXT NOT NULL,
    wing TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'complete')),
    chunk_total INTEGER,
    source_fingerprint TEXT,
    source_mtime REAL,
    normalize_version INTEGER,
    chunker_version INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_file, owner, extract_mode, wing)
);
"""

_COLUMNS = (
    "source_file",
    "owner",
    "extract_mode",
    "wing",
    "state",
    "chunk_total",
    "source_fingerprint",
    "source_mtime",
    "normalize_version",
    "chunker_version",
    "updated_at",
)

# The producer each conversation-style extract mode belongs to, so a reader
# that scopes by extract mode alone (the convo miner passes no owner to its
# mined-set checks) names the same stream its writer does.
_MODE_OWNERS = {"exchange": "convo", "general": "convo", "format": "format"}


@dataclass(frozen=True)
class SourceStream:
    """The key of one source stream. Build it with :func:`source_stream`."""

    source_file: str
    owner: str
    extract_mode: str
    wing: str


def source_stream(
    source_file: str,
    *,
    owner: Optional[str] = None,
    extract_mode: Optional[str] = None,
    wing: Optional[str] = None,
) -> SourceStream:
    """The stream key a writer and every reader of one source must share.

    ``owner`` defaults to the producer of ``extract_mode``; ``None`` values
    are stored as empty strings so the key is always a plain tuple.
    """
    if owner is None:
        owner = _MODE_OWNERS.get(extract_mode or "", "")
    return SourceStream(str(source_file), owner, extract_mode or "", wing or "")


@dataclass(frozen=True)
class SourceHead:
    """One stream's record."""

    stream: SourceStream
    state: str
    chunk_total: Optional[int]
    source_fingerprint: Optional[str]
    source_mtime: Optional[float]
    normalize_version: Optional[int]
    chunker_version: Optional[int]
    updated_at: str

    @property
    def complete(self) -> bool:
        return self.state == COMPLETE


def _head(row) -> SourceHead:
    values = dict(zip(_COLUMNS, row))
    stream = SourceStream(
        values.pop("source_file"),
        values.pop("owner"),
        values.pop("extract_mode"),
        values.pop("wing"),
    )
    return SourceHead(stream=stream, **values)


class SourceStateStore:
    """The palace's ``source_head`` table.

    ``read_only`` stores (dry runs) never create the database and never
    write: their writes are no-ops and a missing database reads as empty.
    """

    def __init__(self, palace_path: str, *, read_only: bool = False):
        self.path = os.path.join(str(palace_path), SOURCE_STATE_FILENAME)
        self.read_only = read_only

    # -- connections -------------------------------------------------------

    def _connect(self, *, write: bool) -> Optional[sqlite3.Connection]:
        if not write and not os.path.exists(self.path):
            return None
        if write:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            if not os.path.exists(self.path):
                # Paths of the user's sources: private to the palace owner.
                os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_DDL)
        else:
            uri = Path(os.path.abspath(self.path)).as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _read(self, sql: str, params: tuple) -> list:
        conn = self._connect(write=False)
        if conn is None:
            return []
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            # A writer creates the file before its table.
            if "no such table" in str(exc):
                return []
            raise
        finally:
            conn.close()

    def _write(self, sql: str, rows: list) -> None:
        if self.read_only or not rows:
            return
        conn = self._connect(write=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(sql, rows)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    # -- reads -------------------------------------------------------------

    def get(self, stream: SourceStream) -> Optional[SourceHead]:
        rows = self._read(
            f"SELECT {', '.join(_COLUMNS)} FROM source_head "
            "WHERE source_file = ? AND owner = ? AND extract_mode = ? AND wing = ?",
            (stream.source_file, stream.owner, stream.extract_mode, stream.wing),
        )
        return _head(rows[0]) if rows else None

    def get_many(
        self,
        *,
        owner: Optional[str] = None,
        extract_mode: Optional[str] = None,
        wing: Optional[str] = None,
        source_files: Optional[Iterable[str]] = None,
    ) -> dict[str, SourceHead]:
        """``source_file -> record`` for every stream in one scope."""
        scope = source_stream("", owner=owner, extract_mode=extract_mode, wing=wing)
        rows = self._read(
            f"SELECT {', '.join(_COLUMNS)} FROM source_head "
            "WHERE owner = ? AND extract_mode = ? AND wing = ?",
            (scope.owner, scope.extract_mode, scope.wing),
        )
        wanted = None if source_files is None else {str(path) for path in source_files}
        heads = {}
        for row in rows:
            head = _head(row)
            if wanted is None or head.stream.source_file in wanted:
                heads[head.stream.source_file] = head
        return heads

    # -- writes ------------------------------------------------------------

    @staticmethod
    def _row(stream: SourceStream, state: str, fields: dict) -> tuple:
        return (
            stream.source_file,
            stream.owner,
            stream.extract_mode,
            stream.wing,
            state,
            fields.get("chunk_total"),
            fields.get("source_fingerprint"),
            fields.get("source_mtime"),
            fields.get("normalize_version"),
            fields.get("chunker_version"),
            utc_now_iso(),
        )

    _UPSERT = f"INSERT OR REPLACE INTO source_head ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})"
    _INSERT_IF_ABSENT = f"INSERT OR IGNORE INTO source_head ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})"

    def mark_pending(self, stream: SourceStream, **fields) -> None:
        """Record that a filing of ``stream`` has started and is not durable yet."""
        self._write(self._UPSERT, [self._row(stream, PENDING, fields)])

    def mark_complete(self, stream: SourceStream, **fields) -> None:
        """Record a finished filing, in one transaction.

        Call only after the stream's drawers are written and its stale ones
        pruned.
        """
        self._write(self._UPSERT, [self._row(stream, COMPLETE, fields)])

    def backfill(self, records: Iterable[tuple]) -> None:
        """Insert complete records proven from legacy drawer rows.

        ``records`` is ``(stream, fields)`` pairs. A stream that already has
        a record keeps it: a writer's record always wins over a backfill, so
        this is idempotent and safe to race with a mine. Failures are logged
        and dropped; a missing record only means the legacy rows are read
        again next time.
        """
        rows = [self._row(stream, COMPLETE, fields) for stream, fields in records]
        try:
            self._write(self._INSERT_IF_ABSENT, rows)
        except (OSError, sqlite3.Error):
            logger.warning("could not backfill %d source records", len(rows), exc_info=True)
