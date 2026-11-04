"""Control-side continuity schema and pure archive layout readers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

MARKER = "capture.base"
FORK_PREFIX = "capture-fork:"

_DDL = (
    """CREATE TABLE IF NOT EXISTS sync_groups (
        group_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
        source TEXT NOT NULL, native_id TEXT NOT NULL, holder_stored_id TEXT NOT NULL,
        UNIQUE(workspace_id,user_id,source,native_id))""",
    """CREATE TABLE IF NOT EXISTS sync_streams (
        workspace_id TEXT NOT NULL, stream_id TEXT NOT NULL, user_id TEXT NOT NULL,
        connection_id TEXT NOT NULL, kind TEXT NOT NULL, group_id TEXT NOT NULL,
        own_stored_id TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0, last_sync TEXT,
        fork_stored_id TEXT, fork_offset INTEGER, next_index INTEGER NOT NULL DEFAULT 0,
        session_bytes INTEGER, session_sha256 TEXT, part_format TEXT,
        final INTEGER NOT NULL DEFAULT 0, legacy INTEGER NOT NULL DEFAULT 0,
        listed_messages INTEGER, PRIMARY KEY(workspace_id,stream_id))""",
    "CREATE INDEX IF NOT EXISTS sync_streams_own ON sync_streams(own_stored_id)",
    "CREATE INDEX IF NOT EXISTS sync_streams_fork ON sync_streams(fork_stored_id)",
    """CREATE TABLE IF NOT EXISTS sync_parts (
        workspace_id TEXT NOT NULL, stream_id TEXT NOT NULL, idx INTEGER NOT NULL,
        descriptor TEXT NOT NULL, PRIMARY KEY(workspace_id,stream_id,idx))""",
    """CREATE TABLE IF NOT EXISTS sync_backfill (
        workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
        prefix_idx INTEGER NOT NULL DEFAULT 0, after_source TEXT, after_id TEXT,
        done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(workspace_id,user_id))""",
    """CREATE TABLE IF NOT EXISTS capture_ops (
        workspace_id TEXT NOT NULL, actor_id TEXT NOT NULL, client_op_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, response TEXT NOT NULL, at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,actor_id,client_op_id))""",
    """CREATE TABLE IF NOT EXISTS sync_events (
        workspace_id TEXT NOT NULL, seq INTEGER NOT NULL, at TEXT NOT NULL,
        kind TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(workspace_id,seq))""",
)


@dataclass(frozen=True)
class StreamRow:
    workspace_id: str
    stream_id: str
    user_id: str
    connection_id: str
    kind: str
    group_id: str
    own_stored_id: str
    offset: int = 0
    last_sync: str | None = None
    fork_stored_id: str | None = None
    fork_offset: int | None = None
    next_index: int = 0
    session_bytes: int | None = None
    session_sha256: str | None = None
    part_format: str | None = None
    final: int = 0
    legacy: int = 0
    listed_messages: int | None = None


@dataclass(frozen=True)
class GroupRow:
    group_id: str
    workspace_id: str
    user_id: str
    source: str
    native_id: str
    holder_stored_id: str


@dataclass(frozen=True)
class Segment:
    """Inclusive stored row bounds, including the archive's parsed rows."""

    start_row: int
    end_row: int
    raw: bytes
    at: str


def ensure_schema(db):
    """Create tables under the caller's shared lock without starting a transaction."""
    with db._lock:
        for statement in _DDL:
            db._conn.execute(statement)


def fork_id(user_id, stream_id) -> str:
    digest = hashlib.sha256(stream_id.encode("utf-8")).hexdigest()[:32]
    return f"{FORK_PREFIX}{user_id}:{digest}"


def archive_segments(messages) -> list[Segment]:
    """Read snapshots, multipart imports and capture archives without read views."""
    rows = list(messages)
    starts = [
        i
        for i, m in enumerate(rows)
        if m.role == "system"
        and m.kind == "other"
        and (m.name in ("import.raw", "capture.raw") or m.seq == 0 and m.name == "")
    ]
    return [
        Segment(
            rows[start].seq,
            rows[starts[i + 1] - 1].seq if i + 1 < len(starts) else rows[-1].seq,
            rows[start].raw.encode("utf-8"),
            rows[start].at,
        )
        for i, start in enumerate(starts)
    ]
