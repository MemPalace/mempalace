"""
logstream.py — Agent coordination event log for MemPalace (RFC 003)
===================================================================

A small append-only event layer stored in the active palace directory as
``logstream.sqlite3``. Agents use it to delegate work, wait for replies,
and exchange exact patch/file artifacts through the shared MemPalace hub
without a human relaying messages between machines.

Design constraints (RFC 003):

- No Chroma dependency, no vector index open — plain SQLite only.
- Append-only: events are immutable; corrections are new events that
  reference prior events (see :meth:`Logstream.ack_event`).
- Exact payloads: event bodies and artifact content are stored verbatim.
- Safe under concurrent HTTP requests (WAL + per-instance lock, same
  pattern as ``knowledge_graph.py``).
- Explicit size limits with clear errors, never silent truncation.

Usage:
    from mempalace.logstream import Logstream

    ls = Logstream(db_path="/path/to/palace/logstream.sqlite3")
    evt = ls.append_event(
        type="task.request", stream="project/mempalace", room="delegation",
        from_agent="mac-codex", to_agent="windows-codex",
        correlation_id="task_123", body="Please fix search echo ranking.",
    )
    reply = ls.wait_events(correlation_id="task_123", type="patch.ready",
                           timeout_ms=300_000)
"""

import json
import random
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Optional

from .config import sanitize_iso_temporal, strip_lone_surrogates

LOGSTREAM_DB_FILENAME = "logstream.sqlite3"

# Size limits (RFC 003 suggested defaults). Byte lengths of the UTF-8
# encoding, checked with explicit errors — payloads are never truncated.
DEFAULT_MAX_BODY_BYTES = 256 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024

# Wait limits: polling loop inside one MCP request, 250-1000 ms with jitter.
MAX_WAIT_TIMEOUT_MS = 300_000
_POLL_BASE_S = 0.25
_POLL_MAX_S = 1.0

MAX_LIST_LIMIT = 500
DEFAULT_LIST_LIMIT = 50

EVENT_STATUSES = frozenset(
    {"open", "claimed", "ready", "applied", "blocked", "failed", "superseded"}
)
ARTIFACT_KINDS = frozenset({"patch", "file", "log", "json", "note"})
ACK_EVENT_TYPE = "event.ack"

# Event types are dotted lowercase identifiers (task.request, patch.ready).
# Routing fields must stay structured — agents should never have to infer
# them from prose — so the character set is enforced, not just length.
_EVENT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

_MAX_ROUTING_LENGTH = 256


def _utc_now_iso() -> str:
    """Canonical UTC timestamp, second precision: YYYY-MM-DDTHH:MM:SSZ.

    Matches the canonical temporal shape accepted by
    ``config.sanitize_iso_temporal`` so ``since_created_at`` filters
    compare like against like. Ordering ties within one second are
    resolved by the SQLite rowid (``seq``), not the timestamp.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id(prefix: str) -> str:
    """Server-generated id: readable UTC stamp + random suffix.

    Uniqueness comes from the random suffix; ordering guarantees come
    from the rowid, so clock skew between writers cannot reorder or
    collide events.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}_{stamp}_{secrets.token_hex(6)}"


def _sanitize_routing(value, field_name: str, required: bool = True) -> Optional[str]:
    """Validate a short routing field (stream, room, agent, correlation_id).

    Streams may contain ``/`` (``project/mempalace``), so this is looser
    than ``config.sanitize_name`` — but null bytes, control characters,
    and over-length values are still rejected.
    """
    if value is None or value == "":
        if required:
            raise ValueError(f"{field_name} must be a non-empty string")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    value = strip_lone_surrogates(value.strip())
    if len(value) > _MAX_ROUTING_LENGTH:
        raise ValueError(f"{field_name} exceeds maximum length of {_MAX_ROUTING_LENGTH} characters")
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in value):
        raise ValueError(f"{field_name} contains control characters")
    return value


def _sanitize_event_type(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("type must be a non-empty string")
    value = value.strip()
    if not _EVENT_TYPE_RE.match(value):
        raise ValueError(
            f"type={value!r} is not a valid event type "
            "(lowercase letters, digits, '.', '_', '-'; max 64 chars)"
        )
    return value


def _sanitize_status(value) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in EVENT_STATUSES:
        allowed = ", ".join(sorted(EVENT_STATUSES))
        raise ValueError(f"status={value!r} is not one of: {allowed}")
    return value


def _sanitize_body(value, max_bytes: int, field_name: str = "body") -> str:
    """Validate verbatim payload text. Empty is allowed; ``None`` becomes ''."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if "\x00" in value:
        raise ValueError(f"{field_name} contains null bytes")
    value = strip_lone_surrogates(value)
    size = len(value.encode("utf-8"))
    if size > max_bytes:
        raise ValueError(f"{field_name} is {size} bytes; maximum is {max_bytes} bytes")
    return value


def _sanitize_metadata(value) -> str:
    """Validate optional metadata dict and return its canonical JSON text."""
    if value is None:
        return "{}"
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"metadata is not JSON-serializable: {exc}") from None
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata exceeds maximum size of {MAX_METADATA_BYTES} bytes")
    return encoded


class Logstream:
    """Durable append-only coordination log (events + artifacts).

    Storage and threading mirror ``KnowledgeGraph``: one SQLite file in
    WAL mode, a per-instance lock around writes, ``check_same_thread=False``
    so the MCP HTTP server can call from worker threads.
    """

    def __init__(
        self,
        db_path: str,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ):
        self.db_path = str(db_path)
        self.max_body_bytes = max_body_bytes
        self.max_artifact_bytes = max_artifact_bytes
        db_parent = Path(self.db_path).parent
        db_parent.mkdir(parents=True, exist_ok=True)
        try:
            db_parent.chmod(0o700)
        except (OSError, NotImplementedError):
            pass
        self._connection = None
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        conn = self._conn()
        conn.executescript("""
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                stream TEXT NOT NULL,
                room TEXT NOT NULL,
                from_agent TEXT NOT NULL,
                to_agent TEXT,
                correlation_id TEXT,
                branch TEXT,
                base_commit TEXT,
                status TEXT,
                body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS events_stream_created_idx
                ON events(stream, created_at);
            CREATE INDEX IF NOT EXISTS events_correlation_idx
                ON events(correlation_id, created_at);
            CREATE INDEX IF NOT EXISTS events_to_agent_idx
                ON events(to_agent, created_at);
            CREATE INDEX IF NOT EXISTS events_type_idx
                ON events(type, created_at);

            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS artifacts_sha256_idx ON artifacts(sha256);

            CREATE TABLE IF NOT EXISTS event_artifacts (
                event_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                PRIMARY KEY (event_id, artifact_id)
            );
        """)
        conn.commit()

    def _conn(self):
        if self._connection is None:
            self._connection = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.row_factory = sqlite3.Row
        return self._connection

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # ── Row shaping ───────────────────────────────────────────────────────

    @staticmethod
    def _event_dict(row, artifact_ids: Optional[list] = None) -> dict:
        try:
            metadata = json.loads(row["metadata_json"])
        except (ValueError, TypeError):
            metadata = {}
        return {
            "id": row["id"],
            "seq": row["rowid"],
            "type": row["type"],
            "stream": row["stream"],
            "room": row["room"],
            "from_agent": row["from_agent"],
            "to_agent": row["to_agent"],
            "correlation_id": row["correlation_id"],
            "branch": row["branch"],
            "base_commit": row["base_commit"],
            "status": row["status"],
            "artifact_ids": artifact_ids or [],
            "body": row["body"],
            "created_at": row["created_at"],
            "metadata": metadata,
        }

    def _attach_artifact_ids(self, conn, events: list[dict]) -> list[dict]:
        if not events:
            return events
        by_id = {e["id"]: e for e in events}
        placeholders = ",".join("?" for _ in by_id)
        rows = conn.execute(
            f"SELECT event_id, artifact_id FROM event_artifacts "
            f"WHERE event_id IN ({placeholders}) ORDER BY artifact_id",
            list(by_id),
        ).fetchall()
        for row in rows:
            by_id[row["event_id"]]["artifact_ids"].append(row["artifact_id"])
        return events

    # ── Write operations (append-only) ────────────────────────────────────

    def append_event(
        self,
        type: str,
        stream: str,
        room: str,
        from_agent: str,
        to_agent: str = None,
        correlation_id: str = None,
        branch: str = None,
        base_commit: str = None,
        status: str = None,
        body: str = "",
        metadata: dict = None,
        artifact_ids: list = None,
    ) -> dict:
        """Append one immutable event. Returns the stored event dict.

        ``artifact_ids`` must reference already-stored artifacts; unknown
        ids are rejected so readers never see a dangling reference.
        """
        type = _sanitize_event_type(type)
        stream = _sanitize_routing(stream, "stream")
        room = _sanitize_routing(room, "room")
        from_agent = _sanitize_routing(from_agent, "from_agent")
        to_agent = _sanitize_routing(to_agent, "to_agent", required=False)
        correlation_id = _sanitize_routing(correlation_id, "correlation_id", required=False)
        branch = _sanitize_routing(branch, "branch", required=False)
        base_commit = _sanitize_routing(base_commit, "base_commit", required=False)
        status = _sanitize_status(status)
        body = _sanitize_body(body, self.max_body_bytes)
        metadata_json = _sanitize_metadata(metadata)

        if artifact_ids is None:
            artifact_ids = []
        if not isinstance(artifact_ids, list) or not all(
            isinstance(a, str) and a for a in artifact_ids
        ):
            raise ValueError("artifact_ids must be a list of artifact id strings")
        artifact_ids = list(dict.fromkeys(artifact_ids))  # dedup, keep order

        event_id = _new_id("evt")
        created_at = _utc_now_iso()

        with self._lock:
            conn = self._conn()
            with conn:
                for artifact_id in artifact_ids:
                    found = conn.execute(
                        "SELECT 1 FROM artifacts WHERE id = ?", (artifact_id,)
                    ).fetchone()
                    if not found:
                        raise ValueError(
                            f"artifact_ids references unknown artifact {artifact_id!r}"
                        )
                conn.execute(
                    "INSERT INTO events (id, type, stream, room, from_agent, to_agent,"
                    " correlation_id, branch, base_commit, status, body, created_at,"
                    " metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        type,
                        stream,
                        room,
                        from_agent,
                        to_agent,
                        correlation_id,
                        branch,
                        base_commit,
                        status,
                        body,
                        created_at,
                        metadata_json,
                    ),
                )
                for artifact_id in artifact_ids:
                    conn.execute(
                        "INSERT INTO event_artifacts (event_id, artifact_id) VALUES (?, ?)",
                        (event_id, artifact_id),
                    )
            row = conn.execute("SELECT rowid, * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event_dict(row, artifact_ids=list(artifact_ids))

    def put_artifact(
        self,
        kind: str,
        content: str,
        created_by: str,
        metadata: dict = None,
    ) -> dict:
        """Store exact artifact content (v1: UTF-8 text only).

        Returns the artifact record without echoing ``content`` back —
        callers already hold the content; readers use :meth:`get_artifact`.
        """
        if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
            allowed = ", ".join(sorted(ARTIFACT_KINDS))
            raise ValueError(f"kind={kind!r} is not one of: {allowed}")
        created_by = _sanitize_routing(created_by, "created_by")
        if not isinstance(content, str) or not content:
            raise ValueError("content must be a non-empty string")
        if "\x00" in content:
            raise ValueError("content contains null bytes")
        content = strip_lone_surrogates(content)
        raw = content.encode("utf-8")
        if len(raw) > self.max_artifact_bytes:
            raise ValueError(
                f"content is {len(raw)} bytes; maximum is {self.max_artifact_bytes} bytes"
            )
        metadata_json = _sanitize_metadata(metadata)

        artifact_id = _new_id("art")
        created_at = _utc_now_iso()
        digest = sha256(raw).hexdigest()

        with self._lock:
            conn = self._conn()
            with conn:
                conn.execute(
                    "INSERT INTO artifacts (id, kind, sha256, size_bytes, content,"
                    " created_by, created_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        artifact_id,
                        kind,
                        digest,
                        len(raw),
                        content,
                        created_by,
                        created_at,
                        metadata_json,
                    ),
                )
        return {
            "id": artifact_id,
            "kind": kind,
            "sha256": digest,
            "size_bytes": len(raw),
            "created_by": created_by,
            "created_at": created_at,
        }

    def ack_event(
        self,
        event_id: str,
        from_agent: str,
        status: str = None,
        body: str = "",
    ) -> dict:
        """Append an ``event.ack`` referencing a prior event.

        The target event is never mutated. The ack copies the target's
        stream/room, copies its ``correlation_id`` (falling back to the
        target's id so request/ack stay tied together), and routes back
        to the target's ``from_agent``.
        """
        event_id = _sanitize_routing(event_id, "event_id")
        with self._lock:
            conn = self._conn()
            target = conn.execute(
                "SELECT rowid, * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        if target is None:
            raise ValueError(f"event {event_id!r} not found")

        return self.append_event(
            type=ACK_EVENT_TYPE,
            stream=target["stream"],
            room=target["room"],
            from_agent=from_agent,
            to_agent=target["from_agent"],
            correlation_id=target["correlation_id"] or target["id"],
            status=status,
            body=body,
            metadata={"ack_of": event_id},
        )

    def submit_patch(
        self,
        content: str,
        from_agent: str,
        stream: str,
        room: str = "patches",
        to_agent: str = None,
        correlation_id: str = None,
        branch: str = None,
        base_commit: str = None,
        body: str = "",
        metadata: dict = None,
    ) -> dict:
        """Convenience wrapper: store a patch artifact + ``patch.ready`` event.

        Both writes go through the validated single-writer paths; the event
        insert re-checks the artifact exists, so readers never observe a
        ``patch.ready`` event with a dangling artifact id.
        """
        artifact = self.put_artifact(
            kind="patch",
            content=content,
            created_by=from_agent,
            metadata=metadata,
        )
        event = self.append_event(
            type="patch.ready",
            stream=stream,
            room=room,
            from_agent=from_agent,
            to_agent=to_agent,
            correlation_id=correlation_id,
            branch=branch,
            base_commit=base_commit,
            status="ready",
            body=body,
            metadata=metadata,
            artifact_ids=[artifact["id"]],
        )
        return {"artifact": artifact, "event": event}

    # ── Read operations ───────────────────────────────────────────────────

    def get_artifact(self, artifact_id: str) -> Optional[dict]:
        """Fetch one artifact by id, exact content included. None if missing."""
        artifact_id = _sanitize_routing(artifact_id, "artifact_id")
        with self._lock:
            conn = self._conn()
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            return None
        try:
            metadata = json.loads(row["metadata_json"])
        except (ValueError, TypeError):
            metadata = {}
        return {
            "id": row["id"],
            "kind": row["kind"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "content": row["content"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "metadata": metadata,
        }

    def list_events(
        self,
        stream: str = None,
        room: str = None,
        type: str = None,
        to_agent: str = None,
        from_agent: str = None,
        correlation_id: str = None,
        status: str = None,
        since_event_id: str = None,
        since_created_at: str = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[dict]:
        """List events matching structured filters, oldest first.

        Cursor semantics:

        - ``since_event_id`` is the precise cursor: strictly after that
          event in append order (rowid), regardless of timestamp ties.
        - ``since_created_at`` is inclusive (``>=``) so second-granularity
          timestamps never skip events; callers dedup by ``id``.
        - ``to_agent`` also matches broadcast events (``to_agent='*'``).
        """
        stream = _sanitize_routing(stream, "stream", required=False)
        room = _sanitize_routing(room, "room", required=False)
        if type not in (None, ""):
            type = _sanitize_event_type(type)
        else:
            type = None
        to_agent = _sanitize_routing(to_agent, "to_agent", required=False)
        from_agent = _sanitize_routing(from_agent, "from_agent", required=False)
        correlation_id = _sanitize_routing(correlation_id, "correlation_id", required=False)
        status = _sanitize_status(status)
        since_event_id = _sanitize_routing(since_event_id, "since_event_id", required=False)
        since_created_at = sanitize_iso_temporal(since_created_at, "since_created_at") or None
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, MAX_LIST_LIMIT)

        where = []
        params = []
        for column, value in (
            ("stream", stream),
            ("room", room),
            ("type", type),
            ("from_agent", from_agent),
            ("correlation_id", correlation_id),
            ("status", status),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        if to_agent is not None:
            where.append("(to_agent = ? OR to_agent = '*')")
            params.append(to_agent)
        if since_created_at is not None:
            where.append("created_at >= ?")
            params.append(since_created_at)

        with self._lock:
            conn = self._conn()
            if since_event_id is not None:
                anchor = conn.execute(
                    "SELECT rowid FROM events WHERE id = ?", (since_event_id,)
                ).fetchone()
                if anchor is None:
                    raise ValueError(f"since_event_id {since_event_id!r} not found")
                where.append("rowid > ?")
                params.append(anchor["rowid"])

            sql = "SELECT rowid, * FROM events"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY rowid ASC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            events = [self._event_dict(row) for row in rows]
            return self._attach_artifact_ids(conn, events)

    def wait_events(
        self,
        timeout_ms: int = 60_000,
        poll_interval_s: float = None,
        **filters,
    ) -> dict:
        """Block until at least one matching event exists or timeout expires.

        v1 implementation per RFC 003: a polling loop inside the request,
        sleeping 250-1000 ms with jitter. Timeouts are clamped to
        ``MAX_WAIT_TIMEOUT_MS`` and return ``{"timed_out": True,
        "events": []}`` rather than raising.

        ``filters`` accepts the same keyword filters as :meth:`list_events`.
        ``poll_interval_s`` pins the sleep (tests); default is jittered.
        """
        if not isinstance(timeout_ms, (int, float)) or timeout_ms < 0:
            raise ValueError("timeout_ms must be a non-negative number")
        timeout_ms = min(timeout_ms, MAX_WAIT_TIMEOUT_MS)
        deadline = time.monotonic() + timeout_ms / 1000.0

        attempt = 0
        while True:
            events = self.list_events(**filters)
            if events:
                return {"timed_out": False, "events": events}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"timed_out": True, "events": []}
            if poll_interval_s is not None:
                delay = poll_interval_s
            else:
                base = min(_POLL_MAX_S, _POLL_BASE_S * (1.5**attempt))
                delay = base * (0.75 + random.random() * 0.25)
            time.sleep(min(delay, remaining))
            attempt += 1
