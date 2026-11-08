"""Reserved report routing and tri-state send authorization.

Persisted deferral is local to one report. Authorization uncertainty is never an
HTTP failure, never consumes that retry budget and never records exception text.
"""

import errno
import json
import os
import sqlite3
import time
from pathlib import Path

from commonplace.projects import orchestration

AUTH_CODE = "report_authorization_retry"
BACKOFF = (1, 2, 4, 8, 16, 30)
PUBLIC_FIELDS = frozenset({"board", "number", "report_id", "reason", "generation", "coordinator"})


def delay(count):
    return BACKOFF[min(count - 1, len(BACKOFF) - 1)]


def control_authority(db, ws, data):
    """Caller holds control lock; None means positively invalidated."""
    if not orchestration.enabled(db, ws):
        return None
    activation = db._conn.execute(
        "SELECT * FROM project_report_activation WHERE workspace_id=?", (ws,)
    ).fetchone()
    if not activation or not activation["active"] or activation["generation"] != data["generation"]:
        return None
    binding = db._conn.execute(
        "SELECT b.* FROM project_report_bindings b JOIN project_orchestrators s "
        "ON s.workspace_id=b.workspace_id AND s.board=b.board AND s.role='main' "
        "AND s.connection_id=b.connection_id WHERE b.workspace_id=? AND b.board=?",
        (ws, data["board"]),
    ).fetchone()
    if (
        binding is None
        or not binding["active"]
        or binding["connection_id"] != data["connection_id"]
        or binding["binding_generation"] != data["binding_generation"]
    ):
        return None
    principal = db.authenticate_connection(binding["connection_id"])
    if (
        principal is None
        or principal.workspace_id != ws
        or principal.actor != data["coordinator"]
        or not principal.policy.has_scope("memory:read")
    ):
        return None
    return principal


def matches(hook, event):
    data = event["data"]
    return (
        hook["workspace_id"] == event["workspace_id"]
        and hook["active"]
        and hook["connection_id"] == data["connection_id"]
        and hook["owner_actor"] == data["coordinator"]
        and (not hook["board"] or hook["board"] == data["board"])
        and ("project.report" in json.loads(hook["kinds"]) or "*" in json.loads(hook["kinds"]))
        and hook["start_seq"] < event["seq"]
    )


def defer_in(db, event):
    """Must share the control transaction that advances the global cursor."""
    db._conn.execute(
        "INSERT OR IGNORE INTO project_report_dispatch_retry(event_seq,workspace_id,envelope) "
        "VALUES(?,?,?)",
        (event["seq"], event["workspace_id"], json.dumps(event, separators=(",", ":"))),
    )


def process(db):
    from commonplace import webhooks

    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM project_report_dispatch_retry WHERE next_at<=? "
            "ORDER BY next_at,event_seq LIMIT 100",
            (time.time(),),
        ).fetchall()
    for work in rows:
        try:
            with db._lock, db._tx():
                # Another dispatcher may have completed or deferred this row.
                current = db._conn.execute(
                    "SELECT * FROM project_report_dispatch_retry WHERE event_seq=? AND next_at<=?",
                    (work["event_seq"], time.time()),
                ).fetchone()
                if current is None:
                    continue
                event = json.loads(current["envelope"])
                data = event["data"]
                receipt = db._conn.execute(
                    "SELECT 1 FROM project_report_receipts "
                    "WHERE workspace_id=? AND report_id=? AND event_seq=?",
                    (event["workspace_id"], data.get("report_id"), event["seq"]),
                ).fetchone()
                if receipt and control_authority(db, event["workspace_id"], data) is not None:
                    hooks = db._conn.execute(
                        "SELECT * FROM webhooks WHERE workspace_id=? AND active=1",
                        (event["workspace_id"],),
                    ).fetchall()
                    for hook in hooks:
                        if matches(hook, event):
                            webhooks._enqueue(db, hook, event)
                db._conn.execute(
                    "DELETE FROM project_report_dispatch_retry WHERE event_seq=?", (event["seq"],)
                )
        except Exception:
            # Routing transaction rolls back ALL enqueues and work deletion.
            # Failure to persist this metadata is an infrastructure error; let
            # it reach the dispatcher's existing exception path.
            with db._lock, db._tx():
                current = db._conn.execute(
                    "SELECT consecutive_failures FROM project_report_dispatch_retry "
                    "WHERE event_seq=?",
                    (work["event_seq"],),
                ).fetchone()
                if current:
                    count = current[0] + 1
                    db._conn.execute(
                        "UPDATE project_report_dispatch_retry SET consecutive_failures=?,next_at=? "
                        "WHERE event_seq=?",
                        (count, time.time() + delay(count), work["event_seq"]),
                    )


def board_authority(gateway, ws, data, principal):
    with orchestration.board_read(gateway.supervisor.workspace_dir(ws)) as conn:
        board = conn.execute(
            "SELECT wing,coordinator,archived FROM boards WHERE slug=?", (data["board"],)
        ).fetchone()
    return bool(
        board
        and not board["archived"]
        and board["coordinator"] == data["coordinator"]
        and principal.policy.can_read_wing(board["wing"])
    )


def filesystem_denied(gateway, ws, error):
    """Only corroborated missing/inaccessible files turn open failures into DENY."""
    if not (
        isinstance(error, OSError)
        and error.errno in (errno.ENOENT, errno.EACCES)
        or isinstance(error, sqlite3.Error)
        and getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_CANTOPEN
    ):
        return False
    path = Path(gateway.supervisor.workspace_dir(ws)) / "boards.sqlite3"
    try:
        path.stat()
    except OSError as exc:
        return exc.errno in (errno.ENOENT, errno.EACCES)
    return not os.access(path, os.R_OK)


def _load(db, did):
    return db._conn.execute(
        "SELECT d.*,w.url,w.secret,w.workspace_id,w.connection_id,w.owner_actor,w.active,"
        "w.auth_header,w.kinds,w.board,w.start_seq FROM webhook_deliveries d JOIN webhooks w "
        "ON w.id=d.webhook_id WHERE d.id=?",
        (did,),
    ).fetchone()


def _stamp(row, principal):
    return (
        tuple(
            row[k]
            for k in (
                "active",
                "connection_id",
                "owner_actor",
                "kinds",
                "board",
                "start_seq",
                "url",
                "auth_header",
                "attempt",
                "report_auth_retry_count",
            )
        ),
        principal.policy.to_json() if principal is not None else None,
    )


def cancel_in(db, did):
    db._conn.execute(
        "UPDATE webhook_deliveries SET status='cancelled',error='',"
        "report_auth_retry_count=0,report_auth_retry_since=NULL WHERE id=?",
        (did,),
    )


def retry_in(db, row):
    from commonplace.webhooks import _now

    count = row["report_auth_retry_count"] + 1
    db._conn.execute(
        "UPDATE webhook_deliveries SET status='retry',error=?,next_at=?,"
        "report_auth_retry_count=?,report_auth_retry_since=coalesce(report_auth_retry_since,?) "
        "WHERE id=? AND status IN ('pending','retry')",
        (AUTH_CODE, time.time() + delay(count), count, _now(), row["id"]),
    )


def prepare(dispatcher, offered):
    """Control snapshot -> closed board read -> control revalidation -> unlocked POST."""
    db, gateway = dispatcher.db, dispatcher.gateway
    with db._lock, db._tx():
        row = _load(db, offered["id"])
        if (
            row is None
            or row["status"] not in ("pending", "retry")
            or row["attempt"] != offered["attempt"]
            or row["report_auth_retry_count"] != offered["report_auth_retry_count"]
        ):
            return None
        try:
            event = json.loads(row["report_envelope"])
            principal = control_authority(db, row["workspace_id"], event["data"])
            if principal is None or not matches(row, event):
                cancel_in(db, row["id"])
                return None
            stamp = _stamp(row, principal)
        except Exception:
            retry_in(db, row)
            return None
    allowed, unknown = False, False
    try:
        allowed = board_authority(gateway, row["workspace_id"], event["data"], principal)
    except Exception as error:
        unknown = not filesystem_denied(gateway, row["workspace_id"], error)
    with db._lock, db._tx():
        current = _load(db, row["id"])
        if (
            current is None
            or current["status"] not in ("pending", "retry")
            or current["attempt"] != row["attempt"]
            or current["report_auth_retry_count"] != row["report_auth_retry_count"]
        ):
            return None
        try:
            principal = control_authority(db, row["workspace_id"], event["data"])
            changed = _stamp(current, principal) != stamp
        except Exception:
            changed = True
        if unknown or changed:
            retry_in(db, current)
            return None
        if not allowed:
            cancel_in(db, current["id"])
            return None
        db._conn.execute(
            "UPDATE webhook_deliveries SET error='',report_auth_retry_count=0,"
            "report_auth_retry_since=NULL WHERE id=?",
            (current["id"],),
        )
        return current
