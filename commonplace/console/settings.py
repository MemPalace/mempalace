"""Per-connection opt-in settings with bounded hot-path reads."""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic

from commonplace.console import types

_SCHEMA = """
CREATE TABLE IF NOT EXISTS console_settings (
    connection_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    record INTEGER NOT NULL CHECK (record IN (0,1)),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
_TTL = 5

skipped: int = 0


@dataclass
class _State:
    cache: dict[str, tuple[bool, float]] = field(default_factory=dict)
    reader: sqlite3.Connection | None = None


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(_SCHEMA)
        if hasattr(db, "_console_settings"):
            return
        state = db._console_settings = _State()

    def close():
        with db._lock:
            if state.reader is not None:
                state.reader.close()
                state.reader = None
            state.cache.clear()

    db.add_pre_close_callback(close)


def recording(db, cid) -> bool:
    global skipped
    state = db._console_settings
    cached = state.cache.get(cid)
    if cached is not None and monotonic() - cached[1] < _TTL:
        return cached[0]
    if not db._lock.acquire(timeout=0.05):
        skipped += 1
        return cached[0] if cached is not None else False
    try:
        if state.reader is None:
            conn = sqlite3.connect(
                db.path, timeout=0.05, check_same_thread=False, isolation_level=None
            )
            try:
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                conn.close()
                raise
            state.reader = conn
        row = state.reader.execute(
            "SELECT record FROM console_settings WHERE connection_id=?", (cid,)
        ).fetchone()
        value = bool(row[0]) if row is not None else False
        state.cache[cid] = (value, monotonic())
        return value
    except sqlite3.OperationalError:
        skipped += 1
        return cached[0] if cached is not None else False
    finally:
        db._lock.release()


def set_recording(db, principal, cid, record: bool) -> None:
    with db._lock:
        row = db._conn.execute(
            "SELECT user_id, workspace_id FROM connections WHERE id=?", (cid,)
        ).fetchone()
        if (
            not principal.person
            or row is None
            or row["user_id"] != principal.user_id
            or row["workspace_id"] != principal.workspace_id
        ):
            raise types.ConsoleDenied("Only a signed-in person can change their own agent")
        with db._tx() as conn:
            conn.execute(
                "INSERT INTO console_settings VALUES (?,?,?,?,?) "
                "ON CONFLICT(connection_id) DO UPDATE SET "
                "workspace_id=excluded.workspace_id, record=excluded.record, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (
                    cid,
                    principal.workspace_id,
                    int(record),
                    principal.user_id,
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
            db.audit(
                action="console.setting",
                decision="ok",
                workspace_id=principal.workspace_id,
                user_id=principal.user_id,
                connection_id=cid,
                reason="on" if record else "off",
            )
        db._console_settings.cache[cid] = (bool(record), monotonic())


def recording_ids(db, ws) -> set[str]:
    with db._lock:
        return {
            row[0]
            for row in db._conn.execute(
                "SELECT connection_id FROM console_settings WHERE workspace_id=? AND record=1",
                (ws,),
            )
        }


def forget(db, connection_id) -> None:
    with db._lock:
        db._console_settings.cache.pop(connection_id, None)
