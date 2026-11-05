"""Private, metadata-only client cursors, outbox and sync event cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .sync_protocol import SYNC_RETENTION


class StateError(ValueError):
    """Fixed diagnostics safe for client output."""


@dataclass(frozen=True)
class Op:
    op_id: str
    kind: str
    source: str
    path: str
    native_id: str
    offset: int
    length: int
    slice_sha256: str
    body_sha256: str
    state: str = "pending"
    attempts: int = 0


def body_digest(body):
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def feed_key(hub_origin, token_env_name):
    """Identify a feed using its environment variable name, never its credential."""
    return hashlib.sha256((hub_origin + "\n" + token_env_name).encode()).hexdigest()


def check_private(path, *, directory=False):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise StateError("Symlinked client state refused")
    info = path.stat()
    mode = 0o700 if directory else 0o600
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not valid_type
        or stat.S_IMODE(info.st_mode) != mode
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
    ):
        raise StateError("Client state requires private ownership and permissions")


def open_state(path) -> State:
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise StateError("Symlinked client state refused")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    check_private(path.parent, directory=True)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if candidate.exists() or candidate.is_symlink():
            check_private(candidate)
    previous = os.umask(0o077)
    conn = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        check_private(path)
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        state = State(conn)
        for candidate in path.parent.glob(path.name + "*"):
            if candidate.name in (path.name, path.name + "-wal", path.name + "-shm"):
                check_private(candidate)
        return state
    except BaseException:
        if conn is not None:
            conn.close()
        raise
    finally:
        os.umask(previous)


class State:
    def __init__(self, conn):
        self.conn = conn
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cursors (
                source TEXT, path TEXT, native_id TEXT, offset INTEGER,
                prefix_sha256 TEXT, PRIMARY KEY(source,path,native_id));
            CREATE TABLE IF NOT EXISTS outbox (
                op_id TEXT PRIMARY KEY, kind TEXT, source TEXT, path TEXT,
                native_id TEXT, offset INTEGER, length INTEGER,
                slice_sha256 TEXT, body_sha256 TEXT, state TEXT, attempts INTEGER);
            CREATE UNIQUE INDEX IF NOT EXISTS outbox_stream ON outbox(source,native_id);
            CREATE TABLE IF NOT EXISTS file_cache (
                kind TEXT, path TEXT, size INTEGER, mtime_ns INTEGER, value TEXT,
                PRIMARY KEY(kind,path));
            CREATE TABLE IF NOT EXISTS cadence (key TEXT PRIMARY KEY, next_due REAL);
            CREATE TABLE IF NOT EXISTS memory_seen (
                label TEXT, relpath TEXT, sha256 TEXT, PRIMARY KEY(label,relpath,sha256));
            CREATE TABLE IF NOT EXISTS source_status (
                source TEXT, path TEXT, native_id TEXT, status TEXT,
                PRIMARY KEY(source,path,native_id));
            CREATE TABLE IF NOT EXISTS feed_cursor (
                feed_key TEXT PRIMARY KEY, seq INTEGER, state_json TEXT, fetched_at REAL);
            CREATE TABLE IF NOT EXISTS events (
                feed_key TEXT, seq INTEGER, at TEXT, kind TEXT, data_json TEXT,
                PRIMARY KEY(feed_key,seq));
        """)

    def cached_file(self, kind, path, info):
        row = self.conn.execute(
            "SELECT value FROM file_cache WHERE kind=? AND path=? AND size=? AND mtime_ns=?",
            (kind, str(path), info.st_size, info.st_mtime_ns),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def cache_file(self, kind, path, info, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO file_cache VALUES (?,?,?,?,?)",
            (kind, str(path), info.st_size, info.st_mtime_ns, json.dumps(value)),
        )

    def retry_rejected(self):
        with self.transaction():
            self.conn.execute(
                "UPDATE source_status SET status='pending' WHERE EXISTS "
                "(SELECT 1 FROM outbox WHERE state IN ('revoked') "
                "AND outbox.source=source_status.source AND outbox.path=source_status.path "
                "AND outbox.native_id=source_status.native_id)"
            )
            self.conn.execute("UPDATE outbox SET state='pending' WHERE state IN ('revoked')")

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def cursor(self, source, path, native_id):
        """None means unknown: fetch the server cursor before building an op."""
        row = self.conn.execute(
            "SELECT * FROM cursors WHERE source=? AND native_id=? ORDER BY path=? DESC LIMIT 1",
            (source, native_id, str(path)),
        ).fetchone()
        return dict(row) if row else None

    def _cursor(self, source, path, native_id, offset, prefix_sha256):
        if type(offset) is not int or offset < 0 or not _sha(prefix_sha256):
            raise StateError("Invalid capture cursor")
        self.conn.execute("DELETE FROM cursors WHERE source=? AND native_id=?", (source, native_id))
        self.conn.execute(
            "INSERT OR REPLACE INTO cursors VALUES (?,?,?,?,?)",
            (source, str(path), native_id, offset, prefix_sha256),
        )

    def store_cursor(self, source, path, native_id, offset, prefix_sha256):
        with self.transaction():
            if self.conn.execute(
                "SELECT 1 FROM outbox WHERE source=? AND native_id=?", (source, native_id)
            ).fetchone():
                raise StateError("Pending operation cursor is immutable")
            self._cursor(source, path, native_id, offset, prefix_sha256)

    def outbox_pending(self):
        return [
            Op(**dict(row))
            for row in self.conn.execute(
                "SELECT * FROM outbox WHERE state='pending' ORDER BY kind='import',rowid"
            )
        ]

    def outbox_all(self):
        return [Op(**dict(row)) for row in self.conn.execute("SELECT * FROM outbox ORDER BY rowid")]

    def outbox_add(self, op):
        if (
            not isinstance(op, Op)
            or re.fullmatch(r"[0-9a-f]{32}", op.op_id) is None
            or op.kind not in ("capture", "import")
            or op.state != "pending"
            or op.attempts != 0
            or type(op.offset) is not int
            or op.offset < 0
            or type(op.length) is not int
            or op.length <= 0
            or not _sha(op.slice_sha256)
            or not _sha(op.body_sha256)
        ):
            raise StateError("Invalid outbox operation")
        with self.transaction():
            row = self.conn.execute("SELECT * FROM outbox WHERE op_id=?", (op.op_id,)).fetchone()
            if row:
                if dict(row) != asdict(op):
                    raise StateError("Pending operation is immutable")
                return op
            self.conn.execute(
                "INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(asdict(op).values())
            )
        return op

    def outbox_attempt(self, op_id):
        self.conn.execute(
            "UPDATE outbox SET attempts=attempts+1 WHERE op_id=? AND state='pending'", (op_id,)
        )

    def outbox_mark(self, op_id, status):
        if status not in (
            "pending",
            "conflict",
            "rejected",
            "paused",
            "revoked",
            "failed",
            "unreadable",
        ):
            raise StateError("Invalid source status")
        with self.transaction():
            self.conn.execute("UPDATE outbox SET state=? WHERE op_id=?", (status, op_id))
            if status not in ("pending", "failed", "unreadable"):
                self._clear_op_retry(op_id)

    def _clear_op_retry(self, op_id):
        self.conn.execute(
            "DELETE FROM cadence WHERE key LIKE ? OR key LIKE ?",
            ("%:op:" + op_id, "%:op:" + op_id + ":%"),
        )

    def outbox_ack(self, op_id, response):
        """Caller passes a decoded HTTP 200 response; other responses are never acks."""
        with self.transaction():
            row = self.conn.execute("SELECT * FROM outbox WHERE op_id=?", (op_id,)).fetchone()
            if row is None:
                return False
            op = Op(**dict(row))
            if op.kind == "capture":
                if (
                    type(response.get("offset")) is not int
                    or response["offset"] != op.offset + op.length
                    or not _sha(response.get("prefix_sha256"))
                ):
                    raise StateError("Capture acknowledgement mismatch")
                self._cursor(
                    op.source, op.path, op.native_id, response["offset"], response["prefix_sha256"]
                )
            else:
                if response.get("accepted") != 1 or response.get("errors") != 0:
                    raise StateError("Import acknowledgement mismatch")
                label, rest = op.native_id.split(":", 1)
                relpath, digest = rest.rsplit(":", 1)
                self.conn.execute(
                    "INSERT OR IGNORE INTO memory_seen VALUES (?,?,?)", (label, relpath, digest)
                )
            self.conn.execute("DELETE FROM outbox WHERE op_id=?", (op_id,))
            self._clear_op_retry(op_id)
        return True

    def memory_seen(self, label, relpath, digest):
        return (
            self.conn.execute(
                "SELECT 1 FROM memory_seen WHERE label=? AND relpath=? AND sha256=?",
                (label, relpath, digest),
            ).fetchone()
            is not None
        )

    def set_status(self, source, path, native_id, status):
        if status not in (
            "ready",
            "pending",
            "paused",
            "revoked",
            "conflict",
            "rejected",
            "too_large",
            "unreadable",
            "failed",
        ):
            raise StateError("Invalid source status")
        self.conn.execute(
            "INSERT OR REPLACE INTO source_status VALUES (?,?,?,?)",
            (source, str(path), native_id, status),
        )

    def statuses(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM source_status")]

    def next_due(self, key):
        row = self.conn.execute("SELECT next_due FROM cadence WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row else 0.0

    def clear_defer(self, key):
        self.conn.execute("DELETE FROM cadence WHERE key=?", (key,))

    def defer(self, key, until):
        self.conn.execute("INSERT OR REPLACE INTO cadence VALUES (?,?)", (key, float(until)))

    def feed_cursor(self, key):
        row = self.conn.execute("SELECT * FROM feed_cursor WHERE feed_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def store_feed(self, key, envelope, *, fetched_at):
        _check_feed(envelope)
        added = []
        with self.transaction():
            old = self.feed_cursor(key)
            if not envelope["reset"] and old and envelope["seq"] < old["seq"]:
                raise StateError("Sync feed cursor moved backwards")
            if envelope["reset"]:
                self.conn.execute("DELETE FROM events WHERE feed_key=?", (key,))
            for event in envelope["events"]:
                result = self.conn.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?)",
                    (key, event["seq"], event["at"], event["kind"], json.dumps(event["data"])),
                )
                if result.rowcount and (old is None or event["seq"] > old["seq"]):
                    added.append(event)
            self.conn.execute(
                "INSERT OR REPLACE INTO feed_cursor VALUES (?,?,?,?)",
                (key, envelope["seq"], json.dumps(envelope["state"]), float(fetched_at)),
            )
            self.conn.execute(
                "DELETE FROM events WHERE feed_key=? AND seq NOT IN "
                "(SELECT seq FROM events WHERE feed_key=? ORDER BY seq DESC LIMIT ?)",
                (key, key, SYNC_RETENTION),
            )
        return added


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _check_feed(envelope):
    """Allow only the content-free sync schema into persistent state."""
    from .sync_protocol import SYNC_ENVELOPE_KEYS, SYNC_EVENT_KEYS, SYNC_STATE_KEYS

    if (
        not isinstance(envelope, dict)
        or set(envelope) != SYNC_ENVELOPE_KEYS
        or envelope["feed"] != "sync"
        or type(envelope["seq"]) is not int
        or not 0 <= envelope["seq"] < 2**63
        or any(type(envelope[k]) is not bool for k in ("more", "reset", "waited"))
        or not isinstance(envelope["state"], dict)
        or set(envelope["state"]) != SYNC_STATE_KEYS
        or type(envelope["state"]["capture_enabled"]) is not bool
        or type(envelope["state"]["interval_seconds"]) is not int
        or envelope["state"]["interval_seconds"] < 0
        or not _time(envelope["state"]["next_window_at"], nullable=True)
        or not isinstance(envelope["events"], list)
        or (envelope["reset"] and envelope["events"])
    ):
        raise StateError("Invalid sync feed")
    previous = 0
    for event in envelope["events"]:
        if (
            not isinstance(event, dict)
            or set(event) != SYNC_EVENT_KEYS
            or type(event["seq"]) is not int
            or not previous < event["seq"] <= envelope["seq"]
            or not _time(event["at"])
            or not (
                (event["kind"] == "sync.request" and event["data"] == {})
                or (
                    event["kind"] == "sync.capture"
                    and isinstance(event["data"], dict)
                    and set(event["data"]) == {"enabled"}
                    and type(event["data"]["enabled"]) is bool
                )
            )
        ):
            raise StateError("Invalid sync event")
        previous = event["seq"]


def _time(value, *, nullable=False):
    return (value is None and nullable) or (
        isinstance(value, str)
        and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)", value)
        is not None
    )
