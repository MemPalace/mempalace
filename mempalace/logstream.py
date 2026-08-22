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
import logging
import os
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

logger = logging.getLogger("mempalace_logstream")

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


# ── Watch filters (RFC 003 follow-up: background watchers) ──────────────
#
# ``list_events`` filters are single-valued and positive-only, because they
# map straight onto indexed SQL equality. A watcher needs two things that
# shape cannot express:
#
#   * "wake me for task.request OR patch.ready" — several values per field
#   * "everything addressed to me EXCEPT my own broadcasts" — a negation
#
# The second is not a nicety. ``to_agent=<me>`` deliberately also matches
# broadcast events (``to_agent='*'``), and an agent's own broadcasts are
# broadcasts — so a naive watcher wakes itself every time it posts a status.
#
# Watch therefore pushes down whatever single-valued filters it can (keeping
# the SQL selective) and applies the multi-valued and negative parts here.


def normalize_watch_values(value) -> Optional[set]:
    """Coerce a watch filter argument to a set of strings, or ``None``.

    ``None`` means "any" — an absent filter, not an empty one. An argument
    holding only blanks collapses to ``None`` for the same reason, so a
    stray ``--type ""`` cannot silently match nothing forever.
    """
    if value is None:
        return None
    values = [value] if isinstance(value, str) else list(value)
    out = {str(v) for v in values if v not in (None, "")}
    return out or None


def event_matches_watch(
    event: dict,
    *,
    streams=None,
    rooms=None,
    types=None,
    statuses=None,
    to_agents=None,
    from_agents=None,
    exclude_from_agents=None,
    correlation_ids=None,
) -> bool:
    """Client-side half of a watch filter. Every argument is a set or ``None``.

    Exclusion beats every positive match: an event from an excluded writer is
    rejected even when it satisfies the rest of the filter.
    """
    if exclude_from_agents and event.get("from_agent") in exclude_from_agents:
        return False

    def _ok(allowed, actual, broadcast_matches=False):
        if not allowed:
            return True
        if actual in allowed:
            return True
        # ``to_agent='*'`` reaches everyone, mirroring list_events' SQL.
        return broadcast_matches and actual == "*"

    return (
        _ok(streams, event.get("stream"))
        and _ok(rooms, event.get("room"))
        and _ok(types, event.get("type"))
        and _ok(statuses, event.get("status"))
        and _ok(correlation_ids, event.get("correlation_id"))
        and _ok(from_agents, event.get("from_agent"))
        and _ok(to_agents, event.get("to_agent"), broadcast_matches=True)
    )


def sanitize_watch_spec(spec: dict) -> dict:
    """Validate and normalize every value in a watch spec.

    ``list_events`` sanitizes the filters it is given, so a *single-valued*
    watch filter is checked for free by being pushed down. Multi-valued ones
    are not pushed down and were therefore compared raw, which made
    validation depend on how many values you happened to pass:
    ``--type Task.Request`` alone was rejected, while
    ``--type Task.Request --type patch.ready`` was silently accepted and then
    matched nothing, leaving the watcher waiting forever for an event type
    that cannot exist.

    Sanitizing here — with the same functions ``list_events`` uses — makes
    the two paths agree, and normalizes values (stripping whitespace) so a
    padded routing value still matches. Raises ``ValueError`` naming the
    offending value.
    """
    sanitized = {}
    for key, field in (
        ("streams", "stream"),
        ("rooms", "room"),
        ("to_agents", "to_agent"),
        ("from_agents", "from_agent"),
        ("exclude_from_agents", "exclude_from_agent"),
        ("correlation_ids", "correlation_id"),
    ):
        values = spec.get(key)
        sanitized[key] = {_sanitize_routing(v, field) for v in values} if values else values
    types = spec.get("types")
    sanitized["types"] = {_sanitize_event_type(v) for v in types} if types else types
    statuses = spec.get("statuses")
    sanitized["statuses"] = {_sanitize_status(v) for v in statuses} if statuses else statuses
    return sanitized


def pushdown_watch_filters(spec: dict) -> dict:
    """The subset of a watch spec that ``list_events`` can evaluate in SQL.

    Only single-valued fields push down. Narrowing the query is an
    optimization, never a correctness dependency — whatever stays behind is
    re-checked by :func:`event_matches_watch` on every candidate row.
    """
    pushdown = {}
    for key, column in (
        ("streams", "stream"),
        ("rooms", "room"),
        ("types", "type"),
        ("statuses", "status"),
        ("correlation_ids", "correlation_id"),
        ("to_agents", "to_agent"),
        ("from_agents", "from_agent"),
    ):
        values = spec.get(key)
        if values and len(values) == 1:
            pushdown[column] = next(iter(values))
    return pushdown


# Watch state-file conditions. "No cursor" is not one fact but four, and
# conflating them loses events: see read_watch_state.
WATCH_STATE_ABSENT = "absent"
WATCH_STATE_OK = "ok"
WATCH_STATE_EMPTY = "empty"
WATCH_STATE_CORRUPT = "corrupt"


def read_watch_state(path: str) -> tuple:
    """Return ``(cursor, condition)`` for a watch state file.

    A cursor of ``None`` is ambiguous and the ambiguity is dangerous, so the
    condition disambiguates it:

    ``absent``
        No state file. A genuine first run — the caller may start at the tip.
    ``ok``
        A usable cursor; resume strictly after it.
    ``empty``
        The file exists and records ``cursor: null`` — the watcher started
        against an empty log and has not seen an event yet. **Not** a first
        run: treating it as one and jumping to the tip would skip whatever
        arrived while the watcher was stopped, and the next checkpoint would
        make that permanent.
    ``corrupt``
        Unreadable, truncated, or valid JSON that is not an object. Costs a
        replay, never a skip — refusing to start would cost every event
        after it, and skipping would lose them silently.
    """
    if not path:
        return None, WATCH_STATE_ABSENT
    # Deliberately no os.path.exists() preflight. It answers False both for
    # "no such file" and for "cannot traverse the parent directory", so a
    # checkpoint that exists but is momentarily unreachable would be read as
    # a first run and skip every event since the stored cursor. Opening
    # directly lets FileNotFoundError mean absent and every other OSError
    # (permissions, a directory in the way, I/O error) mean corrupt — which
    # replays. It also closes the exists()/open() race.
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None, WATCH_STATE_ABSENT
    except (OSError, ValueError):
        return None, WATCH_STATE_CORRUPT
    if not isinstance(payload, dict):
        return None, WATCH_STATE_CORRUPT
    cursor = payload.get("cursor")
    if isinstance(cursor, str) and cursor:
        return cursor, WATCH_STATE_OK
    if "cursor" in payload:
        return None, WATCH_STATE_EMPTY
    return None, WATCH_STATE_CORRUPT


def read_watch_cursor(path: str) -> Optional[str]:
    """The cursor from a watch state file, or ``None``.

    Convenience wrapper over :func:`read_watch_state` for callers that do not
    need to tell a first run from a corrupt file. Watchers should prefer
    ``read_watch_state``, because those two cases must not behave alike.
    """
    return read_watch_state(path)[0]


def write_watch_cursor(path: str, cursor: str, agent: str = None, required: bool = False) -> None:
    """Persist a watch cursor atomically.

    Written to a temp file and renamed so a crash mid-write cannot leave a
    half-file that reads back as a *different, earlier* cursor — that would
    silently replay events the watcher had already handled.

    Ordinary checkpoints are best effort, because losing one costs a replay
    while crashing the watcher costs every event after it. That trade-off
    inverts for the *first* checkpoint of a fresh watch: if it never lands,
    the next launch sees no state file, calls itself a first run, and starts
    at the tip — skipping everything that arrived in between, permanently.
    Pass ``required=True`` there so the failure is raised rather than
    swallowed.
    """
    if not path:
        return
    # ``cursor`` may be None: a watcher that started against an empty log
    # must still leave a file behind, or its next launch looks like a first
    # run and skips whatever arrived in between.
    payload = {"cursor": cursor or None, "updated_at": _utc_now_iso()}
    if agent:
        payload["agent"] = agent
    tmp = f"{path}.tmp"
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, path)
    except OSError:
        logger.debug("could not persist watch cursor to %s", path, exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if required:
            raise


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
        replica_id: str = None,
    ):
        from .replica import get_replica_id

        self.db_path = str(db_path)
        self.max_body_bytes = max_body_bytes
        self.max_artifact_bytes = max_artifact_bytes
        db_parent = Path(self.db_path).parent
        db_parent.mkdir(parents=True, exist_ok=True)
        try:
            db_parent.chmod(0o700)
        except (OSError, NotImplementedError):
            pass
        # RFC 004 provenance: every op is stamped with the replica that authored it.
        # replica.json lives in the palace dir (the db's parent).
        self.replica_id = replica_id or get_replica_id(str(db_parent))
        self._connection = None
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        from .hlc import HybridLogicalClock

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
                metadata_json TEXT NOT NULL DEFAULT '{}',
                origin_replica TEXT,
                origin_seq INTEGER,
                hlc TEXT
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
                metadata_json TEXT NOT NULL DEFAULT '{}',
                origin_replica TEXT
            );

            CREATE INDEX IF NOT EXISTS artifacts_sha256_idx ON artifacts(sha256);

            CREATE TABLE IF NOT EXISTS event_artifacts (
                event_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                PRIMARY KEY (event_id, artifact_id)
            );
        """)
        self._migrate_replication_schema(conn)
        conn.commit()
        # Seed the HLC from the newest stamp in the log so monotonicity
        # survives restarts (RFC 004 op envelope).
        row = conn.execute("SELECT max(hlc) AS last FROM events").fetchone()
        self._clock = HybridLogicalClock(self.replica_id, last=row["last"] if row else None)

    def _migrate_replication_schema(self, conn):
        """RFC 004 step 0: add provenance columns to pre-replication logs.

        Fresh databases get the columns from the canonical CREATE TABLE; this
        path upgrades logs created before step 0. Backfill stamps existing
        rows as authored by THIS replica (they were: pre-replication, only
        one copy existed) with origin_seq = rowid and an HLC derived from
        created_at + rowid, preserving their original total order.
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        if "origin_replica" not in existing:
            conn.execute("ALTER TABLE events ADD COLUMN origin_replica TEXT")
        if "origin_seq" not in existing:
            conn.execute("ALTER TABLE events ADD COLUMN origin_seq INTEGER")
        if "hlc" not in existing:
            conn.execute("ALTER TABLE events ADD COLUMN hlc TEXT")
        art_existing = {row["name"] for row in conn.execute("PRAGMA table_info(artifacts)")}
        if "origin_replica" not in art_existing:
            conn.execute("ALTER TABLE artifacts ADD COLUMN origin_replica TEXT")

        from .hlc import render as hlc_render

        rows = conn.execute(
            "SELECT rowid, created_at FROM events WHERE origin_replica IS NULL"
        ).fetchall()
        for row in rows:
            try:
                ms = int(
                    datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                    * 1000
                )
            except ValueError:
                ms = 0
            conn.execute(
                "UPDATE events SET origin_replica = ?, origin_seq = rowid, hlc = ? WHERE rowid = ?",
                (
                    self.replica_id,
                    hlc_render(ms, min(row["rowid"], 0xFFFFFF), self.replica_id),
                    row["rowid"],
                ),
            )
        conn.execute(
            "UPDATE artifacts SET origin_replica = ? WHERE origin_replica IS NULL",
            (self.replica_id,),
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS events_origin_seq_idx "
            "ON events(origin_replica, origin_seq)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS events_hlc_idx ON events(hlc)")

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
            # RFC 004 additive fields — committed compat surface. `seq` stays
            # the LOCAL arrival cursor; `origin_seq` is the author's counter;
            # `hlc` is the cross-replica display/merge order.
            "origin_replica": row["origin_replica"],
            "origin_seq": row["origin_seq"],
            "hlc": row["hlc"],
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
        hlc = self._clock.tick()

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
                cursor = conn.execute(
                    "INSERT INTO events (id, type, stream, room, from_agent, to_agent,"
                    " correlation_id, branch, base_commit, status, body, created_at,"
                    " metadata_json, origin_replica, hlc)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        self.replica_id,
                        hlc,
                    ),
                )
                # Locally-authored ops use the local rowid as their per-origin
                # counter — strictly monotonic, gap-free enough for cursors.
                conn.execute(
                    "UPDATE events SET origin_seq = rowid WHERE rowid = ?",
                    (cursor.lastrowid,),
                )
                for artifact_id in artifact_ids:
                    conn.execute(
                        "INSERT INTO event_artifacts (event_id, artifact_id) VALUES (?, ?)",
                        (event_id, artifact_id),
                    )
            row = conn.execute("SELECT rowid, * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._event_dict(row, artifact_ids=list(artifact_ids))

    @staticmethod
    def _patch_content_warnings(kind: str, content: str) -> list[str]:
        """Advisory checks for ``kind=patch`` content. Never mutates content.

        Verbatim storage means we store exactly what the producer sent —
        but a diff that ``git apply`` will reject as corrupt is a broken
        handoff, so warn at store time where the producer can still fix it.
        Found in the wild during the first RFC 003 dogfood: a patch stored
        without its final trailing newline truncates the last hunk line.
        """
        if kind != "patch":
            return []
        warnings = []
        if not content.endswith("\n"):
            warnings.append(
                "patch content does not end with a trailing newline; "
                "git apply will reject the final hunk as corrupt — "
                "store the diff byte-exactly including its trailing newline"
            )
        if "\r" in content:
            warnings.append(
                "patch content contains carriage returns (CRLF line endings); "
                "git apply often rejects CRLF diffs — generate the diff with "
                "LF endings"
            )
        return warnings

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
        For ``kind=patch``, a ``warnings`` list is included when the diff
        looks unappliable (missing trailing newline, CRLF endings); the
        content itself is still stored verbatim.
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
                    " created_by, created_at, metadata_json, origin_replica)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        artifact_id,
                        kind,
                        digest,
                        len(raw),
                        content,
                        created_by,
                        created_at,
                        metadata_json,
                        self.replica_id,
                    ),
                )
        record = {
            "id": artifact_id,
            "kind": kind,
            "sha256": digest,
            "size_bytes": len(raw),
            "created_by": created_by,
            "created_at": created_at,
            "origin_replica": self.replica_id,
        }
        warnings = self._patch_content_warnings(kind, content)
        if warnings:
            record["warnings"] = warnings
        return record

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
            "origin_replica": row["origin_replica"],
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

    def latest_event_id(self) -> Optional[str]:
        """Id of the newest event, or None on an empty log.

        Live-tail consumers (the SSE stream) capture this at connect time
        as their starting cursor so they receive only post-connect events.
        """
        with self._lock:
            conn = self._conn()
            row = conn.execute("SELECT id FROM events ORDER BY rowid DESC LIMIT 1").fetchone()
        return row["id"] if row is not None else None

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

    def watch_events(
        self,
        *,
        cursor: str = None,
        poll_timeout_ms: int = MAX_WAIT_TIMEOUT_MS,
        limit: int = DEFAULT_LIST_LIMIT,
        poll_interval_s: float = None,
        **spec,
    ):
        """Yield ``(matched_events, cursor)`` forever as new events arrive.

        The long-poll primitive :meth:`wait_events` caps at
        ``MAX_WAIT_TIMEOUT_MS`` and reports a timeout, which makes it a
        building block rather than a watcher: every caller ends up writing
        the same re-arm loop, and each one has to remember to carry the
        cursor forward. This owns both.

        An idle poll yields ``([], cursor)`` rather than blocking silently,
        so a caller can implement an idle timeout, emit a heartbeat, or
        checkpoint its cursor without running a second clock.

        ``poll_timeout_ms`` may be a callable returning the timeout for
        this iteration so a caller can cap it to a remaining idle
        deadline (an idle of 400ms must not wait out a 300s long-poll).

        The cursor advances past every event *examined*, not merely those
        that matched, so a watcher that restarts never rescans what it has
        already judged. ``spec`` takes the set-valued filters described by
        :func:`event_matches_watch`.
        """
        pushdown = pushdown_watch_filters(spec)
        while True:
            timeout = poll_timeout_ms() if callable(poll_timeout_ms) else poll_timeout_ms
            if timeout is not None and int(timeout) <= 0:
                # Only reachable when an idle deadline has already elapsed —
                # a *configured* timeout of zero would make this branch spin
                # without ever polling, so callers must reject that up front
                # (the CLI does). Yield at once so the caller can exit
                # instead of waiting out another long-poll.
                yield [], cursor
                continue
            result = self.wait_events(
                timeout_ms=timeout,
                since_event_id=cursor,
                limit=limit,
                poll_interval_s=poll_interval_s,
                **pushdown,
            )
            events = result.get("events") or []
            if not events:
                yield [], cursor
                continue
            cursor = events[-1]["id"]
            yield [e for e in events if event_matches_watch(e, **spec)], cursor

    # ── Replication (RFC 004 step 0: logstream multi-master) ─────────────

    def version_vector(self) -> dict:
        """{origin_replica: highest origin_seq applied locally}.

        The complete description of this replica's knowledge — peers diff
        their vectors to compute exactly which op ranges are missing.
        """
        with self._lock:
            conn = self._conn()
            rows = conn.execute(
                "SELECT origin_replica, max(origin_seq) AS top FROM events "
                "WHERE origin_replica IS NOT NULL GROUP BY origin_replica"
            ).fetchall()
        return {row["origin_replica"]: row["top"] for row in rows}

    def list_ops(self, origin: str, after_seq: int = 0, limit: int = 500) -> list[dict]:
        """Events authored by ``origin`` with origin_seq > after_seq, in
        author order. The anti-entropy pull unit."""
        origin = _sanitize_routing(origin, "origin")
        if not isinstance(after_seq, int) or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, MAX_LIST_LIMIT)
        with self._lock:
            conn = self._conn()
            rows = conn.execute(
                "SELECT rowid, * FROM events WHERE origin_replica = ? AND origin_seq > ? "
                "ORDER BY origin_seq ASC LIMIT ?",
                (origin, after_seq, limit),
            ).fetchall()
            events = [self._event_dict(row) for row in rows]
            return self._attach_artifact_ids(conn, events)

    def apply_remote_event(self, event: dict) -> bool:
        """Fold one remote op into the local log, idempotently.

        Verbatim rule: the event is stored exactly as authored (id,
        created_at, hlc, origin stamps untouched); only the local rowid —
        the arrival cursor — is ours. Referenced artifacts must already be
        applied (sync pulls artifacts first) so readers never see a dangling
        id, same invariant as append_event. Returns True if inserted, False
        if we already had it. Raises ValueError on malformed input or a
        missing artifact.
        """
        for key in (
            "id",
            "type",
            "stream",
            "room",
            "from_agent",
            "created_at",
            "origin_replica",
            "origin_seq",
            "hlc",
        ):
            if not event.get(key):
                raise ValueError(f"remote event is missing required field {key!r}")
        if event["origin_replica"] == self.replica_id:
            return False  # our own op echoed back — by definition already present
        artifact_ids = list(dict.fromkeys(event.get("artifact_ids") or []))
        metadata_json = _sanitize_metadata(event.get("metadata"))

        with self._lock:
            conn = self._conn()
            with conn:
                dup = conn.execute(
                    "SELECT 1 FROM events WHERE id = ? OR (origin_replica = ? AND origin_seq = ?)",
                    (event["id"], event["origin_replica"], event["origin_seq"]),
                ).fetchone()
                if dup:
                    return False
                for artifact_id in artifact_ids:
                    found = conn.execute(
                        "SELECT 1 FROM artifacts WHERE id = ?", (artifact_id,)
                    ).fetchone()
                    if not found:
                        raise ValueError(
                            f"remote event {event['id']!r} references artifact "
                            f"{artifact_id!r} not yet applied locally"
                        )
                conn.execute(
                    "INSERT INTO events (id, type, stream, room, from_agent, to_agent,"
                    " correlation_id, branch, base_commit, status, body, created_at,"
                    " metadata_json, origin_replica, origin_seq, hlc)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event["id"],
                        event["type"],
                        event["stream"],
                        event["room"],
                        event["from_agent"],
                        event.get("to_agent"),
                        event.get("correlation_id"),
                        event.get("branch"),
                        event.get("base_commit"),
                        event.get("status"),
                        event.get("body") or "",
                        event["created_at"],
                        metadata_json,
                        event["origin_replica"],
                        event["origin_seq"],
                        event["hlc"],
                    ),
                )
                for artifact_id in artifact_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO event_artifacts (event_id, artifact_id)"
                        " VALUES (?, ?)",
                        (event["id"], artifact_id),
                    )
        # Absorb the remote instant so our next local op sorts after it.
        self._clock.observe(event["hlc"])
        return True

    def has_artifact(self, artifact_id: str) -> bool:
        """Cheap existence probe (no content transfer) for the sync engine."""
        artifact_id = _sanitize_routing(artifact_id, "artifact_id")
        with self._lock:
            conn = self._conn()
            row = conn.execute("SELECT 1 FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        return row is not None

    def apply_remote_artifact(self, artifact: dict) -> bool:
        """Fold one remote artifact in, idempotently, verifying its hash.

        Content is verbatim; the sha256 must match or the artifact is
        rejected — a corrupt transfer must never enter the store.
        """
        for key in ("id", "kind", "sha256", "content", "created_by", "created_at"):
            if not artifact.get(key):
                raise ValueError(f"remote artifact is missing required field {key!r}")
        content = artifact["content"]
        digest = sha256(content.encode("utf-8")).hexdigest()
        if digest != artifact["sha256"]:
            raise ValueError(
                f"remote artifact {artifact['id']!r} failed hash verification "
                f"(claimed {artifact['sha256'][:16]}..., computed {digest[:16]}...)"
            )
        metadata_json = _sanitize_metadata(artifact.get("metadata"))
        with self._lock:
            conn = self._conn()
            with conn:
                dup = conn.execute(
                    "SELECT 1 FROM artifacts WHERE id = ?", (artifact["id"],)
                ).fetchone()
                if dup:
                    return False
                conn.execute(
                    "INSERT INTO artifacts (id, kind, sha256, size_bytes, content,"
                    " created_by, created_at, metadata_json, origin_replica)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        artifact["id"],
                        artifact["kind"],
                        artifact["sha256"],
                        len(content.encode("utf-8")),
                        content,
                        artifact["created_by"],
                        artifact["created_at"],
                        metadata_json,
                        artifact.get("origin_replica"),
                    ),
                )
        return True
