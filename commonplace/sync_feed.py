"""Content-free workspace sync signals and bounded live delivery."""

from __future__ import annotations

import hashlib
import json
import threading
import time

from . import capture, continuity_schema, import_grant, ingest_cadence, plans, sessions
from .http_util import query, send_json
from .sync_protocol import MAX_WAIT_SECONDS, SYNC_FEED, SYNC_PAGE, SYNC_RETENTION

_condition = threading.Condition()
_waiters: dict[tuple[str, str], int] = {}
_MAX_WAITERS = 64
_MAX_PER_CALLER = 2


def authorized(db, principal):
    """Only people and explicitly configured ingest connections see signals."""
    return principal.policy.has_scope("memory:write") and (
        principal.person
        or principal.client in capture.SOURCES
        or import_grant.grant_path(db, principal)
    )


def record(conn, ws, kind, data) -> int:
    """Append inside the writer's locked transaction; notify only after commit."""
    if not (
        kind == "sync.request"
        and data == {}
        or kind == "sync.capture"
        and isinstance(data, dict)
        and set(data) == {"enabled"}
        and type(data["enabled"]) is bool
    ):
        raise ValueError("Invalid sync signal")
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq),0)+1 FROM sync_events WHERE workspace_id=?", (ws,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO sync_events(workspace_id,seq,at,kind,data) VALUES (?,?,?,?,?)",
        (ws, seq, plans._now().strftime("%Y-%m-%dT%H:%M:%SZ"), kind, json.dumps(data)),
    )
    conn.execute(
        "DELETE FROM sync_events WHERE workspace_id=? AND seq<=?", (ws, seq - SYNC_RETENTION)
    )
    return seq


def notify(ws):
    """Wake local waiters after a commit; readers also check other processes."""
    with _condition:
        if any(key[0] == ws for key in _waiters):
            _condition.notify_all()


def page(db, ws, after):
    """Read one committed page and its cursor under the shared control lock."""
    continuity_schema.ensure_schema(db)
    with db._lock:
        oldest, latest = db._conn.execute(
            "SELECT MIN(seq),COALESCE(MAX(seq),0) FROM sync_events WHERE workspace_id=?", (ws,)
        ).fetchone()
        if after > latest or oldest is not None and after < oldest - 1:
            return [], latest, False, True
        rows = db._conn.execute(
            "SELECT seq,at,kind,data FROM sync_events WHERE workspace_id=? AND seq>? "
            "ORDER BY seq LIMIT ?",
            (ws, after, SYNC_PAGE),
        ).fetchall()
        events = [
            {"seq": row[0], "at": row[1], "kind": row[2], "data": json.loads(row[3])}
            for row in rows
        ]
        seq = events[-1]["seq"] if events else latest
        return events, seq, seq < latest, False


def state(db, principal, now) -> dict:
    with db._lock:
        row = db._conn.execute(
            "SELECT enabled FROM capture_settings WHERE workspace_id=?", (principal.workspace_id,)
        ).fetchone()
        interval = ingest_cadence.account_interval(db, principal)
        next_at = ingest_cadence.next_window(
            db, ingest_cadence.account_key(db, principal), interval, now
        )
    return {
        "capture_enabled": bool(row and row[0]),
        "interval_seconds": interval,
        "next_window_at": next_at.isoformat().replace("+00:00", "Z") if next_at else None,
    }


def waiter_count() -> int:
    with _condition:
        return sum(_waiters.values())


def _wait(db, ws, seq, seconds, key):
    with _condition:
        if sum(_waiters.values()) >= _MAX_WAITERS or _waiters.get(key, 0) >= _MAX_PER_CALLER:
            return False
        _waiters[key] = _waiters.get(key, 0) + 1
    try:
        deadline = time.monotonic() + seconds
        while True:
            # Never acquire the control lock while holding the Condition: a
            # writer may notify after committing while still owning its RLock.
            with db._lock:
                latest = db._conn.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM sync_events WHERE workspace_id=?", (ws,)
                ).fetchone()[0]
            remaining = deadline - time.monotonic()
            if latest != seq or remaining <= 0:
                return True
            with _condition:
                _condition.wait(min(1, remaining))
    finally:
        with _condition:
            if _waiters[key] == 1:
                del _waiters[key]
            else:
                _waiters[key] -= 1


def handle(gateway, h, principal):
    db = gateway.db
    if not authorized(db, principal):
        send_json(h, 403, {"error": "Sync feed requires a capture or import connection"})
        return
    params = query(h)
    try:
        after = int(params.get("after", "0"))
        if not 0 <= after <= 9223372036854775807:
            raise ValueError
    except ValueError:
        send_json(h, 400, {"error": "Invalid event cursor"})
        return
    try:
        wait = int(params.get("wait", "0"))
        if not 0 <= wait <= MAX_WAIT_SECONDS:
            raise ValueError
    except ValueError:
        send_json(h, 400, {"error": "Invalid wait"})
        return
    events, seq, more, reset = page(db, principal.workspace_id, after)
    current = state(db, principal, plans._now())
    waited = False
    if not events and not reset and wait and current["interval_seconds"] == 0:
        if principal.person:
            session = sessions.cookie_value(h.headers.get("Cookie")) or principal.user_id
            caller = "session:" + hashlib.sha256(session.encode()).hexdigest()
        else:
            caller = "connection:" + principal.connection_id
        waited = _wait(db, principal.workspace_id, seq, wait, (principal.workspace_id, caller))
        events, seq, more, reset = page(db, principal.workspace_id, after)
        current = state(db, principal, plans._now())
    send_json(
        h,
        200,
        {
            "feed": SYNC_FEED,
            "events": events,
            "seq": seq,
            "more": more,
            "reset": reset,
            "waited": waited,
            "state": current,
        },
    )
