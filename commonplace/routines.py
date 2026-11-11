"""Routines: a schedule that creates one card per due time, with a history of runs.

A routine is a name, a card template, a schedule and an owner (a workspace owner).
Each due time the background worker creates exactly one card through the normal
card path (``BoardService.task_add`` as the routine's owner, so priority, origin and
authorization rules apply). Every due time first claims a ``routine_runs`` row that is
unique per routine and due time; the card carries a marker label, so a restart between
the claim and the card never creates a second one. Only signed-in workspace owners
can read or change routines.
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from commonplace import http_util, schedule_time, sessions
from commonplace.boards import priority as priorities
from commonplace.boards.service import BoardService, validate_text
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext, ToolError

logger = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 60
MAX_ROUTINES = 25  # per workspace, not counting deleted ones
MAX_CATCH_UP = 3  # due times made up after downtime; older ones become one "skipped" row
SCAN_LIMIT = 2000  # due times looked at per tick
RUN_HISTORY = 200  # run rows kept per routine
PAUSE_AFTER_FAILURES = 5
MARKER = "routine-run:"
TEMPLATE_KEYS = {"board", "title", "body", "labels", "priority", "status"}


def stamp(now):
    return schedule_time.stamp(datetime.fromtimestamp(now, timezone.utc))


def ensure_schema(db):
    with db._lock:
        db._conn.executescript("""
        CREATE TABLE IF NOT EXISTS routines (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, owner_user_id TEXT NOT NULL,
            name TEXT NOT NULL, template TEXT NOT NULL, schedule TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            state TEXT NOT NULL CHECK(state IN ('active','paused')),
            paused_reason TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
            cursor TEXT NOT NULL, anchor_at TEXT NOT NULL, created_by TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS routines_by_workspace ON routines(workspace_id, deleted_at);
        CREATE TABLE IF NOT EXISTS routine_runs (
            id TEXT PRIMARY KEY, routine_id TEXT NOT NULL REFERENCES routines(id),
            due_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('claimed','created','failed','skipped')),
            board TEXT, card INTEGER, code TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(routine_id, due_at)
        );
        """)


def require_owner(principal):
    if principal is None or not principal.person or principal.role != "owner":
        raise ToolError("no: only a signed-in workspace owner can manage routines")
    return principal


def service_for(gateway, principal):
    return BoardService(ToolContext(principal=principal, gateway=gateway))


def _template(service, value):
    if not isinstance(value, dict) or set(value) - TEMPLATE_KEYS:
        raise ToolError("no: template takes board, title, body, labels, priority and status")
    result = {
        "board": validate_text(value.get("board"), "board", required=True, limit=100),
        "title": validate_text(value.get("title"), "title", required=True, limit=200),
        "body": validate_text(value.get("body", ""), "body", limit=20_000),
        "status": value.get("status", "todo"),
        "priority": priorities.validate(value.get("priority", priorities.DEFAULT)),
    }
    if result["status"] not in ("todo", "backlog"):
        raise ToolError("no: a routine card starts in todo or backlog")
    labels = value.get("labels", [])
    if not isinstance(labels, list) or len(labels) > 20:
        raise ToolError("no: labels must be a list of at most 20")
    result["labels"] = [validate_text(x, "label", required=True, limit=100) for x in labels]
    with service.store.connection() as conn:
        service._board(conn, result["board"])
    return result


def _schedule(value, tz, now):
    try:
        return schedule_time.validate(
            value,
            tz,
            datetime.fromtimestamp(now, timezone.utc),
            min_interval=MIN_INTERVAL_MINUTES,
        )
    except ValueError as exc:
        raise ToolError(f"no: {exc}") from None


def _next(row, after):
    return schedule_time.next_due(
        json.loads(row["schedule"]),
        row["timezone"],
        after,
        schedule_time.parse_stamp(row["anchor_at"]),
    )


def due_between(row, after, upto, limit):
    """Due times in (after, upto], oldest first, at most ``limit``."""
    result, cursor = [], after
    while len(result) < limit:
        cursor = _next(row, cursor)
        if cursor is None or cursor > upto:
            break
        result.append(cursor)
    return result


def _owner(gateway, principal, value):
    """The user whose authority the cards are created with."""
    user = principal.user_id if value is None else value
    if not isinstance(user, str) or gateway.db.role(principal.workspace_id, user) != "owner":
        raise ToolError("no: the routine's owner must be an owner of this workspace")
    return user


def _name(value):
    return validate_text(value, "name", required=True, limit=120)


def _view(db, row, now, *, runs=0):
    result = {
        "id": row["id"],
        "name": row["name"],
        "owner": row["owner_user_id"],
        "template": json.loads(row["template"]),
        "schedule": json.loads(row["schedule"]),
        "timezone": row["timezone"],
        "state": row["state"],
        "paused_reason": row["paused_reason"],
        "created_at": row["created_at"],
        "next_due": None,
    }
    if row["state"] == "active":
        after = max(
            schedule_time.parse_stamp(row["cursor"]), datetime.fromtimestamp(now, timezone.utc)
        )
        due = _next(row, after)
        result["next_due"] = schedule_time.stamp(due) if due else None
    with db._lock:
        history = db._conn.execute(
            "SELECT due_at,state,board,card,code,detail,created_at FROM routine_runs "
            "WHERE routine_id=? ORDER BY due_at DESC LIMIT ?",
            (row["id"], runs or 1),
        ).fetchall()
    history = [dict(r) for r in history]
    if runs:
        result["runs"] = history
    else:
        result["last_run"] = history[0] if history else None
    return result


def _find(db, principal, identity):
    with db._lock:
        row = db._conn.execute(
            "SELECT * FROM routines WHERE id=? AND workspace_id=? AND deleted_at IS NULL",
            (identity, principal.workspace_id),
        ).fetchone()
    if row is None:
        raise LookupError(identity)
    return row


def listing(db, principal, now):
    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM routines WHERE workspace_id=? AND deleted_at IS NULL ORDER BY created_at,id",
            (principal.workspace_id,),
        ).fetchall()
    return [_view(db, r, now) for r in rows]


def show(db, principal, identity, now):
    return _view(db, _find(db, principal, identity), now, runs=50)


def create(service, body, now):
    principal = require_owner(service.principal)
    gateway = service.ctx.gateway
    if not isinstance(body, dict) or set(body) - {
        "name",
        "template",
        "schedule",
        "timezone",
        "owner",
    }:
        raise ValueError("invalid routine")
    name = _name(body.get("name"))
    template = _template(service, body.get("template"))
    tz = body.get("timezone", "UTC")
    schedule = _schedule(body.get("schedule"), tz, now)
    owner = _owner(gateway, principal, body.get("owner"))
    identity = uuid.uuid4().hex
    db, at = gateway.db, stamp(now)
    with db._lock:
        count = db._conn.execute(
            "SELECT count(*) FROM routines WHERE workspace_id=? AND deleted_at IS NULL",
            (principal.workspace_id,),
        ).fetchone()[0]
        if count >= MAX_ROUTINES:
            raise ToolError(f"no: a workspace can have at most {MAX_ROUTINES} routines")
        db._conn.execute(
            "INSERT INTO routines (id,workspace_id,owner_user_id,name,template,schedule,timezone,"
            "state,cursor,anchor_at,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'active',?,?,?,?,?)",
            (
                identity,
                principal.workspace_id,
                owner,
                name,
                json.dumps(template),
                json.dumps(schedule),
                tz,
                at,
                at,
                principal.user_id,
                at,
                at,
            ),
        )
    return show(db, principal, identity, now)


def update(service, identity, body, now):
    principal = require_owner(service.principal)
    gateway = service.ctx.gateway
    if (
        not isinstance(body, dict)
        or not body
        or set(body)
        - {
            "name",
            "template",
            "schedule",
            "timezone",
            "owner",
            "paused",
        }
    ):
        raise ValueError("invalid routine")
    row = _find(gateway.db, principal, identity)
    changes = {}
    if "name" in body:
        changes["name"] = _name(body["name"])
    if "template" in body:
        changes["template"] = json.dumps(_template(service, body["template"]))
    if "owner" in body:
        changes["owner_user_id"] = _owner(gateway, principal, body["owner"])
    if "schedule" in body or "timezone" in body:
        tz = body.get("timezone", row["timezone"])
        value = body["schedule"] if "schedule" in body else json.loads(row["schedule"])
        changes["schedule"] = json.dumps(_schedule(value, tz, now))
        changes["timezone"] = tz
        changes["anchor_at"] = changes["cursor"] = stamp(now)
    if "paused" in body:
        if not isinstance(body["paused"], bool):
            raise ValueError("paused must be a boolean")
        changes["state"] = "paused" if body["paused"] else "active"
        changes["paused_reason"] = "paused by an owner" if body["paused"] else ""
        if not body["paused"]:
            # Time spent paused is not made up on resume.
            changes["cursor"] = stamp(now)
            changes["failures"] = 0
    changes["updated_at"] = stamp(now)
    names = sorted(changes)
    with gateway.db._lock:
        gateway.db._conn.execute(
            "UPDATE routines SET " + ",".join(f"{n}=?" for n in names) + " WHERE id=?",
            [changes[n] for n in names] + [row["id"]],
        )
    return show(gateway.db, principal, identity, now)


def delete(service, identity, now):
    principal = require_owner(service.principal)
    db = service.ctx.gateway.db
    row = _find(db, principal, identity)
    with db._lock:
        db._conn.execute(
            "UPDATE routines SET deleted_at=?,updated_at=? WHERE id=?",
            (stamp(now), stamp(now), row["id"]),
        )
    return {"deleted": row["id"]}


def _fill(text, due, tz):
    return text.replace("{date}", due.astimezone(schedule_time.zone(tz)).date().isoformat())


class RoutineWorker:
    def __init__(self, gateway, *, start=True, now=time.time):
        self.gateway = gateway
        self.now = now
        self._stop = threading.Event()
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._run, name="routines", daemon=True)
            self.thread.start()

    def stop(self):
        self._stop.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                http_util.log_failure(logger, "Routine pass failed", exc)
            self._stop.wait(timeout=15)

    def run_once(self, now=None):
        """Claim and settle everything due at ``now``; returns the number of rows touched."""
        now = self.now() if now is None else now
        at = datetime.fromtimestamp(now, timezone.utc)
        db = self.gateway.db
        touched = 0
        # A claimed row seen at the start of a pass was left by an interrupted one.
        with db._lock:
            orphans = db._conn.execute(
                "SELECT id FROM routine_runs WHERE state='claimed' ORDER BY due_at"
            ).fetchall()
        for orphan in orphans:
            touched += self._settle(orphan["id"], now)
        with db._lock:
            rows = db._conn.execute(
                "SELECT * FROM routines WHERE state='active' AND deleted_at IS NULL ORDER BY id"
            ).fetchall()
        for row in rows:
            try:
                touched += self._advance(row, at, now)
            except Exception as exc:
                http_util.log_failure(logger, "Routine could not be scheduled", exc)
        return touched

    def _insert(self, row, due, state, now, *, code="", detail=""):
        identity = uuid.uuid4().hex
        with self.gateway.db._lock:
            made = self.gateway.db._conn.execute(
                "INSERT OR IGNORE INTO routine_runs (id,routine_id,due_at,state,code,detail,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    identity,
                    row["id"],
                    schedule_time.stamp(due),
                    state,
                    code,
                    detail,
                    stamp(now),
                    stamp(now),
                ),
            ).rowcount
            self.gateway.db._conn.execute(
                "UPDATE routines SET cursor=? WHERE id=? AND cursor<?",
                (schedule_time.stamp(due), row["id"], schedule_time.stamp(due)),
            )
        return identity if made else None

    def _advance(self, row, at, now):
        dues = due_between(row, schedule_time.parse_stamp(row["cursor"]), at, SCAN_LIMIT)
        if not dues:
            return 0
        keep = [] if len(dues) == SCAN_LIMIT else dues[-MAX_CATCH_UP:]
        missed = dues[: len(dues) - len(keep)]
        touched = 0
        if missed:
            self._insert(
                row,
                missed[-1],
                "skipped",
                now,
                code="missed",
                detail=f"{len(missed)} due time(s) passed while the hub was not running",
            )
            touched += 1
        for due in keep:
            identity = self._insert(row, due, "claimed", now)
            if identity is not None:
                touched += self._settle(identity, now)
        self._prune(row["id"])
        return touched

    def _prune(self, routine):
        with self.gateway.db._lock:
            self.gateway.db._conn.execute(
                "DELETE FROM routine_runs WHERE routine_id=? AND state!='claimed' AND id NOT IN "
                "(SELECT id FROM routine_runs WHERE routine_id=? ORDER BY due_at DESC LIMIT ?)",
                (routine, routine, RUN_HISTORY),
            )

    def _finish(self, run, state, now, *, board=None, card=None, code="", detail=""):
        db = self.gateway.db
        with db._lock:
            db._conn.execute(
                "UPDATE routine_runs SET state=?,board=?,card=?,code=?,detail=?,updated_at=? "
                "WHERE id=? AND state='claimed'",
                (state, board, card, code, detail[:300], stamp(now), run["id"]),
            )
            if state == "created":
                db._conn.execute("UPDATE routines SET failures=0 WHERE id=?", (run["routine_id"],))
            else:
                db._conn.execute(
                    "UPDATE routines SET failures=failures+1 WHERE id=?", (run["routine_id"],)
                )
                db._conn.execute(
                    "UPDATE routines SET state='paused',paused_reason=?,updated_at=? "
                    "WHERE id=? AND failures>=? AND state='active'",
                    (
                        f"{PAUSE_AFTER_FAILURES} runs in a row failed",
                        stamp(now),
                        run["routine_id"],
                        PAUSE_AFTER_FAILURES,
                    ),
                )

    def _settle(self, identity, now):
        db = self.gateway.db
        with db._lock:
            run = db._conn.execute(
                "SELECT * FROM routine_runs WHERE id=? AND state='claimed'", (identity,)
            ).fetchone()
            row = (
                db._conn.execute(
                    "SELECT * FROM routines WHERE id=? AND deleted_at IS NULL", (run["routine_id"],)
                ).fetchone()
                if run
                else None
            )
        if run is None:
            return 0
        if row is None:
            self._finish(run, "failed", now, code="deleted", detail="the routine was deleted")
            return 1
        template = json.loads(row["template"])
        try:
            principal = sessions.person_principal(db, row["owner_user_id"], row["workspace_id"])
            if principal is None or principal.role != "owner":
                raise ToolError("no: the routine's owner is no longer an owner of this workspace")
            service = service_for(self.gateway, principal)
            marker = MARKER + run["id"]
            number = self._existing(service, marker)
            if number is None:
                due = schedule_time.parse_stamp(run["due_at"])
                with service.store.connection() as conn:
                    service._board(conn, template["board"])  # never create a board here
                card = service.task_add(
                    _fill(template["title"], due, row["timezone"]),
                    body=_fill(template["body"], due, row["timezone"]),
                    board=template["board"],
                    status=template["status"],
                    labels=[*template["labels"], "routine", marker],
                    priority=template["priority"],
                )
                number = card["number"]
            self._finish(run, "created", now, board=template["board"], card=number)
        except (ToolError, LookupError, ControlError) as exc:
            self._finish(run, "failed", now, code="not_allowed", detail=str(exc))
        except Exception as exc:
            http_util.log_failure(logger, "Routine card could not be created", exc)
            self._finish(run, "failed", now, code="error")
        return 1

    @staticmethod
    def _existing(service, marker):
        with service.store.connection() as conn:
            found = conn.execute(
                "SELECT id FROM tasks WHERE labels LIKE ? LIMIT 1", (f'%"{marker}"%',)
            ).fetchone()
        return found["id"] if found else None


def install(gateway):
    if getattr(gateway, "routine_worker", None) is not None:
        return
    ensure_schema(gateway.db)
    gateway.routine_worker = RoutineWorker(
        gateway, start=os.environ.get("COMMONPLACE_ROUTINE_WORKER", "on") != "off"
    )
    gateway.db.add_pre_close_callback(gateway.routine_worker.stop)

    def route(h):
        principal = http_util.require_principal(gateway, h)
        if principal is None:
            return
        try:
            service = service_for(gateway, require_owner(principal))
            now = gateway.routine_worker.now()
            parts = http_util.path_parts(h, "/api/routines")
            body = None
            if h.command in ("POST", "PATCH"):
                try:
                    body = http_util.read_json(h)
                except ValueError:
                    return
            if not parts and h.command == "GET":
                result = (
                    200,
                    {
                        "routines": listing(gateway.db, principal, now),
                        "limits": {
                            "routines": MAX_ROUTINES,
                            "min_interval_minutes": MIN_INTERVAL_MINUTES,
                        },
                    },
                )
            elif not parts and h.command == "POST":
                result = 201, create(service, body, now)
            elif len(parts) == 1 and h.command == "GET":
                result = 200, show(gateway.db, principal, parts[0], now)
            elif len(parts) == 1 and h.command in ("POST", "PATCH"):
                result = 200, update(service, parts[0], body, now)
            elif len(parts) == 1 and h.command == "DELETE":
                result = 200, delete(service, parts[0], now)
            else:
                result = 404, {"error": "Not found"}
        except LookupError:
            result = 404, {"error": "Routine not found"}
        except ToolError as exc:
            message = str(exc).removeprefix("no: ")
            result = (403 if "owner can" in message else 400), {"error": message}
        except (ValueError, sqlite3.IntegrityError):
            result = 400, {"error": "Invalid routine"}
        http_util.send_json(h, *result)

    gateway.add_route("GET", "/api/routines", route)
    gateway.add_route("POST", "/api/routines", route)
    for method in ("GET", "POST", "PATCH", "DELETE"):
        gateway.add_prefix_route(method, "/api/routines/", route)
