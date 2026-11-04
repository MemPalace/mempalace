"""One ordered event log per hub: what changed, for the app and for webhooks.

Every module that changes something people or agents watch calls
:func:`emit` after its own transaction commits. The web app polls
``GET /api/events?after=<seq>``; the webhook dispatcher is a listener.

Events carry ids and short labels, never memory text. ``data`` holds what a
receiver needs to decide whether to fetch (a card's number, title, status,
who asked whom).

Kinds in use (add, never rename):

    card.created  card.updated  card.moved  card.commented  card.claimed
    card.expired  card.handoff  card.review_needed  card.decided  card.ask_withdrawn
    memory.saved  memory.linked  fact.saved
    rule.changed  rule.suggestion
    space_created
    webhook.failed
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from .http_util import log_failure

logger = logging.getLogger("commonplace.events")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_workspace ON events(workspace_id, seq);
"""

Listener = Callable[[dict], None]
_listeners: list = []
_listeners_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(SCHEMA)


def add_listener(listener: Listener) -> None:
    with _listeners_lock:
        if listener not in _listeners:
            _listeners.append(listener)


def remove_listener(listener: Listener) -> None:
    with _listeners_lock:
        if listener in _listeners:
            _listeners.remove(listener)


def _row(row) -> dict:
    return {
        "seq": row["seq"],
        "at": row["at"],
        "workspace_id": row["workspace_id"],
        "kind": row["kind"],
        "subject": row["subject"],
        "actor": row["actor"],
        "data": json.loads(row["data"] or "{}"),
    }


def emit(
    db,
    workspace_id: str,
    kind: str,
    *,
    subject: str = "",
    actor: str = "",
    data: Optional[dict] = None,
) -> dict:
    """Append one event and tell the listeners. Returns the stored event."""
    ensure_schema(db)
    with db._lock:
        event = record(db, workspace_id, kind, subject=subject, actor=actor, data=data)
    notify(event)
    return event


def record(
    db,
    workspace_id: str,
    kind: str,
    *,
    subject: str = "",
    actor: str = "",
    data: Optional[dict] = None,
) -> dict:
    """Append one event without telling the listeners, inside the caller's
    transaction (the caller holds ``db._lock``). Call :func:`notify` after the
    transaction commits, so a listener never waits on an open write."""
    payload = json.dumps(data or {}, ensure_ascii=False, sort_keys=True)
    cur = db._conn.execute(
        "INSERT INTO events(at, workspace_id, kind, subject, actor, data) VALUES (?,?,?,?,?,?)",
        (_now(), workspace_id, kind, subject, actor, payload),
    )
    row = db._conn.execute("SELECT * FROM events WHERE seq = ?", (cur.lastrowid,)).fetchone()
    return _row(row)


def notify(event: dict) -> None:
    """Tell the listeners about a stored event."""
    with _listeners_lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(event)
        except Exception as exc:
            log_failure(logger, "event listener failed", exc)


def after(db, workspace_id: str, seq: int = 0, limit: int = 200) -> list:
    ensure_schema(db)
    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM events WHERE workspace_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (workspace_id, int(seq), int(limit)),
        ).fetchall()
    return [_row(r) for r in rows]


def latest_seq(db, workspace_id: str) -> int:
    ensure_schema(db)
    with db._lock:
        row = db._conn.execute(
            "SELECT MAX(seq) AS s FROM events WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
    return int(row["s"] or 0)
