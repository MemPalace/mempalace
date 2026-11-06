"""Delayed person approvals. Only the background worker applies an answer."""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from commonplace import http_util, sessions
from commonplace.boards.service import BoardService, VersionClash, validate_text
from commonplace.extensions import ToolContext, ToolError
from commonplace.rules.enforce import RulesRefresh, rest_write

HOLD_SECONDS = 60
logger = logging.getLogger(__name__)


def stamp(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="microseconds")


def ensure_schema(db):
    with db._lock:
        db._conn.executescript("""
        CREATE TABLE IF NOT EXISTS answer_holds (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
            card INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('approve','decide')),
            body TEXT NOT NULL, release_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('waiting','settling','released','failed','stopped')),
            code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS answer_holds_waiting
            ON answer_holds(workspace_id,card) WHERE state='waiting';
        """)


def service_for(gateway, principal):
    if principal is None or not principal.person or principal.role != "owner":
        raise ToolError("no: only a signed-in workspace owner can answer this")
    return BoardService(ToolContext(principal=principal, gateway=gateway))


def create(service, body, now):
    if not isinstance(body, dict) or set(body) - {
        "card",
        "kind",
        "version",
        "choice",
        "reply",
        "then",
        "reason",
    }:
        raise ValueError("invalid hold")
    kind = body.get("kind")
    if kind not in ("approve", "decide") or type(body.get("card")) is not int:
        raise ValueError("invalid hold")
    if type(body.get("version")) is not int:
        raise ValueError("version is required")
    allowed = (
        {"card", "kind", "version", "reason"}
        if kind == "approve"
        else {"card", "kind", "version", "choice", "reply", "then"}
    )
    if set(body) - allowed:
        raise ValueError("invalid answer fields")
    with service.transaction() as conn:
        row, board = service._get(conn, body["card"])
        service._write(board, submission=kind == "approve")
        service._version(conn, row, body["version"])
        if kind == "approve":
            if row["proposal_status"] != "pending" or not service._approval_role(
                service.principal, row
            ):
                raise ToolError("no: card is not a pending proposal")
            reason = body.get("reason")
            if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
                raise ValueError("invalid reason")
        elif row["status"] != "review" or body.get("choice") not in json.loads(
            row["ask_options"] or "[]"
        ):
            raise ToolError("no: choose one of the offered options")
        # Board events have second precision. A cursor also distinguishes an older
        # identical answer when an ask is reopened in that same second.
        event_after = conn.execute(
            "SELECT coalesce(max(id),0) FROM task_events WHERE task_id=?", (row["id"],)
        ).fetchone()[0]
    if kind == "decide":
        if body.get("then", "return") not in ("return", "todo", "done", "backlog"):
            raise ValueError("invalid decision destination")
        validate_text(body.get("choice"), "choice", required=True, limit=120)
        if "reply" in body and body["reply"] is not None:
            validate_text(body["reply"], "reply", required=True)
    principal = service.principal
    identity = uuid.uuid4().hex
    release_at = stamp(now + HOLD_SECONDS)
    with service.ctx.db._lock:
        service.ctx.db._conn.execute(
            "INSERT INTO answer_holds "
            "(id,workspace_id,user_id,card,kind,body,release_at,state,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'waiting',?,?)",
            (
                identity,
                principal.workspace_id,
                principal.user_id,
                body["card"],
                kind,
                json.dumps({**body, "_event_after": event_after}),
                release_at,
                stamp(now),
                stamp(now),
            ),
        )
    return {"id": identity, "release_at": release_at}


def listing(db, principal, now):
    with db._lock:
        return [
            dict(r)
            for r in db._conn.execute(
                "SELECT id,card,kind,release_at,state,code,created_at,updated_at FROM answer_holds "
                "WHERE workspace_id=? AND user_id=? AND "
                "(state IN ('waiting','settling') OR updated_at>=?) ORDER BY created_at,id",
                (principal.workspace_id, principal.user_id, stamp(now - 86400)),
            )
        ]


def stop(db, principal, identity, now):
    with db._lock:
        row = db._conn.execute(
            "SELECT * FROM answer_holds WHERE id=? AND workspace_id=? AND user_id=?",
            (identity, principal.workspace_id, principal.user_id),
        ).fetchone()
        if row is None:
            return 404, {"error": "Not found"}
        changed = db._conn.execute(
            "UPDATE answer_holds SET state='stopped',updated_at=? WHERE id=? "
            "AND state='waiting' AND release_at>?",
            (stamp(now), identity, stamp(now)),
        ).rowcount
    return (
        (200, {"state": "stopped"})
        if changed
        else (409, {"error": "too_late", "released_at": row["release_at"]})
    )


class HoldWorker:
    def __init__(self, gateway, *, start=True, now=time.time):
        self.gateway = gateway
        self.now = now
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread = None
        # Only startup recovery may reclaim settling rows. Live workers claim waiting rows.
        with gateway.db._lock:
            self.recovery = [
                r[0]
                for r in gateway.db._conn.execute(
                    "SELECT id FROM answer_holds WHERE state='settling'"
                )
            ]
        if start:
            self.thread = threading.Thread(target=self._run, name="answer-holds", daemon=True)
            self.thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if self.run_once():
                    continue
            except Exception as exc:
                http_util.log_failure(logger, "Approval hold failed", exc)
            self._wake.wait(timeout=1)

    def run_once(self, now=None):
        now = self.now() if now is None else now
        db = self.gateway.db
        with db._lock:
            recovering = bool(self.recovery)
            if recovering:
                identity = self.recovery.pop(0)
            else:
                row = db._conn.execute(
                    "SELECT id FROM answer_holds WHERE state='waiting' AND release_at<=? "
                    "ORDER BY release_at,id LIMIT 1",
                    (stamp(now),),
                ).fetchone()
                if row is None:
                    return False
                identity = row[0]
                if (
                    db._conn.execute(
                        "UPDATE answer_holds SET state='settling',updated_at=? "
                        "WHERE id=? AND state='waiting' AND release_at<=?",
                        (stamp(now), identity, stamp(now)),
                    ).rowcount
                    != 1
                ):
                    return False
            hold = dict(
                db._conn.execute("SELECT * FROM answer_holds WHERE id=?", (identity,)).fetchone()
            )
        state, code = "released", ""
        try:
            principal = sessions.person_principal(db, hold["user_id"], hold["workspace_id"])
            service = service_for(self.gateway, principal)
            body = json.loads(hold["body"])
            card = service.task_show(hold["card"])
            choice = "approve" if hold["kind"] == "approve" else body.get("choice")
            visible = False
            if recovering:
                with service.store.connection() as conn:
                    decided = conn.execute(
                        "SELECT 1 FROM task_events WHERE task_id=? AND id>? "
                        "AND kind='decide' AND choice=? AND stamp_connection IS NULL "
                        "AND stamp_user IN (?,?) AND created_at>=? LIMIT 1",
                        (
                            hold["card"],
                            body.get("_event_after", 0),
                            choice,
                            principal.user_id,
                            principal.actor,
                            datetime.fromisoformat(hold["created_at"])
                            .replace(microsecond=0)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        ),
                    ).fetchone()
                visible = bool(decided) or (
                    hold["kind"] == "approve" and card.get("approved_by") == principal.actor
                )
            if not visible:
                args = {
                    k: v
                    for k, v in body.items()
                    if k not in {"card", "kind", "_events_seen", "_event_after"}
                }
                if hold["kind"] == "approve":
                    service.task_approve(hold["card"], approve=True, **args)
                else:
                    service.decide(hold["card"], **args)
        except VersionClash:
            state, code = "failed", "stale_version"
        except (ToolError, LookupError):
            state, code = "failed", "not_allowed"
        except Exception as exc:
            http_util.log_failure(logger, "Approval settlement failed", exc)
            state, code = "failed", "interrupted"
        with db._lock:
            db._conn.execute(
                "UPDATE answer_holds SET state=?,code=?,updated_at=? WHERE id=? AND state='settling'",
                (state, code, stamp(now), identity),
            )
        return True


def install(gateway):
    if getattr(gateway, "hold_worker", None) is not None:
        return
    ensure_schema(gateway.db)
    gateway.hold_worker = HoldWorker(
        gateway, start=os.environ.get("COMMONPLACE_HOLD_WORKER", "on") != "off"
    )
    gateway.db.add_pre_close_callback(gateway.hold_worker.stop)

    def route(h):
        principal = http_util.require_principal(gateway, h)
        if principal is None:
            return
        try:
            service = service_for(gateway, principal)
            now = gateway.hold_worker.now()
            parts = http_util.path_parts(h, "/api/holds/")
            if h.command == "GET":
                result = 200, {"holds": listing(gateway.db, principal, now)}
            elif h.path == "/api/holds":
                if not h.headers.get("Content-Type", "").startswith("application/json"):
                    http_util.send_json(h, 415, {"error": "Content-Type must be application/json"})
                    return
                try:
                    body = http_util.read_json(h)
                except ValueError:
                    return
                if not isinstance(body, dict):
                    raise ValueError("invalid hold")
                tool = "task_approve" if body.get("kind") == "approve" else "decide"
                with rest_write(service.ctx, h, tool, body):
                    result = 201, create(service, body, now)
            elif len(parts) == 2 and parts[1] == "stop":
                result = stop(gateway.db, principal, parts[0], now)
            else:
                result = 404, {"error": "Not found"}
        except RulesRefresh as exc:
            result = 428, exc.payload
        except (VersionClash, sqlite3.IntegrityError):
            result = 409, {"error": "card changed or already waiting"}
        except ToolError:
            result = 403, {"error": "Forbidden"}
        except ValueError:
            result = 400, {"error": "Invalid hold"}
        http_util.send_json(h, *result)

    gateway.add_route("GET", "/api/holds", route)
    gateway.add_route("POST", "/api/holds", route)
    gateway.add_prefix_route("POST", "/api/holds/", route)
