"""Lazy, generation-aware workspace activity logs. No user content is stored."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

from commonplace import redact
from commonplace.http_util import log_failure
from mempalace.sqlite_crypto import connect, operational_error_type, row_factory

_SCHEMA = """
CREATE TABLE IF NOT EXISTS console_activity (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    name TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ok','error','denied')),
    ms INTEGER NOT NULL CHECK (ms BETWEEN 0 AND 10000000)
);
CREATE INDEX IF NOT EXISTS console_activity_conn ON console_activity(connection_id, seq);
"""
_PRUNE = """
DELETE FROM console_activity WHERE seq IN (
    SELECT seq FROM console_activity
    WHERE at < :cutoff OR seq <= :seq - 20000 ORDER BY seq LIMIT 500
)
"""
_logger = logging.getLogger(__name__)
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _cutoff():
    return (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")


class ActivityStore:
    dropped: int = 0

    def __init__(self, gateway, ws):
        self._supervisor = gateway.supervisor
        self._ws = ws
        self._lock = threading.Lock()
        self._path = None
        self._conn = None
        self._schema_ready = False
        self._last_failure = None
        self._failure_lock = threading.Lock()
        self.dropped = 0

    def _connection(self, *, create=False):
        # The supervisor reads the current generation pointer without mkdir.
        path = Path(self._supervisor.workspace_dir(self._ws)).resolve() / "console.sqlite3"
        if path != self._path:
            if self._conn is not None:
                self._conn.close()
            self._conn, self._path = None, path
            self._schema_ready = False
        if not create and not path.is_file():
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._schema_ready = False
            return None
        if self._conn is not None:
            if create and not self._schema_ready:
                self._conn.executescript(_SCHEMA)
                self._schema_ready = True
            return self._conn
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(
            str(path) if create else path.as_uri() + "?mode=rw",
            uri=not create,
            timeout=0.05,
            check_same_thread=False,
            isolation_level=None,
        )
        try:
            conn.row_factory = row_factory
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            if create:
                conn.executescript(_SCHEMA)
                self._schema_ready = True
        except Exception:
            conn.close()
            raise
        self._conn = conn
        return conn

    def _drop(self, exc):
        self.dropped += 1
        if self._failure_lock.acquire(blocking=False):
            try:
                now = monotonic()
                if self._last_failure is None or now - self._last_failure >= 60:
                    self._last_failure = now
                    log_failure(_logger, "console activity dropped", exc)
            finally:
                self._failure_lock.release()

    def append(self, cid, name, outcome, ms) -> int | None:
        if not self._lock.acquire(timeout=0.05):
            self._drop(TimeoutError())
            return None
        try:
            conn = self._connection(create=True)
            now = datetime.now(timezone.utc)
            safe_name = redact.redact(name)[0]
            if not isinstance(safe_name, str) or _NAME.fullmatch(safe_name) is None:
                safe_name = "unknown"
            seq = conn.execute(
                "INSERT INTO console_activity(at,connection_id,name,outcome,ms) VALUES (?,?,?,?,?)",
                (now.strftime("%Y-%m-%dT%H:%M:%SZ"), cid, safe_name, outcome, ms),
            ).lastrowid
            if seq % 500 == 0:
                conn.execute("PRAGMA busy_timeout=0")
                try:
                    conn.execute(
                        _PRUNE,
                        {
                            "cutoff": (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "seq": seq,
                        },
                    )
                except operational_error_type(conn):
                    pass
                finally:
                    conn.execute("PRAGMA busy_timeout=50")
            return seq
        except Exception as exc:
            self._drop(exc)
            return None
        finally:
            self._lock.release()

    def _read(self, sql, params):
        with self._lock:
            try:
                conn = self._connection()
                return conn.execute(sql, params).fetchall() if conn is not None else []
            except (sqlite3.OperationalError, operational_error_type(self._conn)) as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                return []

    def after(self, cid, seq, limit) -> list[dict]:
        return [
            dict(row)
            for row in self._read(
                "SELECT * FROM console_activity WHERE connection_id=? AND seq>? AND at>=? "
                "ORDER BY seq LIMIT ?",
                (cid, seq, _cutoff(), limit),
            )
        ]

    def tail(self, cid, limit) -> list[dict]:
        return [
            dict(row)
            for row in self._read(
                "SELECT * FROM console_activity WHERE connection_id=? AND at>=? "
                "ORDER BY seq DESC LIMIT ?",
                (cid, _cutoff(), limit),
            )
        ]

    def latest_at(self, cids) -> dict[str, str]:
        cids = list(cids)
        if not cids:
            return {}
        placeholders = ",".join("?" for _ in cids)
        return dict(
            self._read(
                "SELECT connection_id, MAX(at) FROM console_activity "
                f"WHERE connection_id IN ({placeholders}) AND at>=? GROUP BY connection_id",
                [*cids, _cutoff()],
            )
        )


def exists(gateway, ws) -> bool:
    return (Path(gateway.supervisor.workspace_dir(ws)) / "console.sqlite3").is_file()


def peek(gateway, ws) -> ActivityStore | None:
    activity = open_activity(gateway, ws)
    with activity._lock:
        try:
            return activity if activity._connection() is not None else None
        except (sqlite3.OperationalError, operational_error_type(activity._conn)) as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            return None


def open_activity(gateway, ws) -> ActivityStore:
    stores = gateway.__dict__.setdefault("_console_activity", {})
    if ws not in stores:
        stores.setdefault(ws, ActivityStore(gateway, ws))
    return stores[ws]
