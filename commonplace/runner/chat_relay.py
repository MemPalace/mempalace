"""Content-free chat relay queue and frozen runner contracts."""

import hashlib
import json
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from commonplace import control_db, redact, sessions
from commonplace.boards.service import BoardService
from commonplace.chat import front, store
from commonplace.chat.types import MAX_REPLY_CHARS
from commonplace.extensions import ToolContext, ToolError
from commonplace.http_util import log_failure
from commonplace.projects import models

from . import chat_context
from .registry import CHAT_MODEL

logger = logging.getLogger(__name__)

CHAT_ONLINE_S = 90
CHAT_WAIT_S = 120
CHAT_LEASE_S = 120
CHAT_MAIN_FRESH_S = 600
DAILY_CAP = 200
MAX_ANSWER_BYTES = 16_384
MAX_BODY_BYTES = 20_480
CONTEXTS = ("minimal", "full")
RELAY_STATES = ("queued", "leased", "done", "failed", "expired", "skipped")
RELAY_CODES = (
    "ok",
    "no_runner",
    "lease_expired",
    "cli_failed",
    "cli_timeout",
    "cli_not_configured",
    "bad_output",
    "no_access",
    "daily_limit",
)
RUNNER_FAIL_CODES = ("cli_failed", "cli_timeout", "cli_not_configured", "bad_output")


class InvalidReply(ValueError):
    """The submitted reply or failure code is invalid (HTTP 400)."""


class ReplyBusy(Exception):
    """Reply storage is temporarily unavailable; keep the lease (HTTP 503)."""


class Fenced(Exception):
    """The job's lease no longer authorizes this operation (HTTP 409)."""


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_runner_jobs (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                board TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                cli TEXT,
                model TEXT,
                state TEXT NOT NULL CHECK (state IN ('queued','leased','done','failed','expired','skipped')),
                code TEXT NOT NULL DEFAULT '',
                machine_id TEXT,
                lease_hash TEXT,
                lease_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (workspace_id, board, message_id)
            );
            CREATE INDEX IF NOT EXISTS chat_runner_jobs_user ON chat_runner_jobs(workspace_id, user_id, state, created_at);
            CREATE TABLE IF NOT EXISTS chat_runners_seen (
                machine_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                clis TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT 'minimal' CHECK (context IN ('minimal','full')),
                relay_always INTEGER NOT NULL DEFAULT 0 CHECK (relay_always IN (0,1)),
                seen_at TEXT NOT NULL
            );
        """)


def _stamp(now: float) -> str:
    return control_db._iso(datetime.fromtimestamp(now, timezone.utc))


def _day(now: float) -> tuple[str, str]:
    start = datetime.fromtimestamp(now, timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return control_db._iso(start), control_db._iso(start + timedelta(days=1))


def _used_today(conn, workspace_id, user_id, now):
    start, end = _day(now)
    return conn.execute(
        "SELECT COUNT(*) FROM chat_runner_jobs WHERE workspace_id=? AND user_id=? "
        "AND state!='skipped' AND created_at>=? AND created_at<?",
        (workspace_id, user_id, start, end),
    ).fetchone()[0]


def sweep(db, now: float) -> None:
    with db._lock:
        db._conn.execute(
            "UPDATE chat_runner_jobs SET state='expired',code='no_runner',updated_at=? "
            "WHERE state='queued' AND created_at<?",
            (_stamp(now), _stamp(now - CHAT_WAIT_S)),
        )
        db._conn.execute(
            "UPDATE chat_runner_jobs SET state='failed',code='lease_expired',updated_at=? "
            "WHERE state='leased' AND lease_until<=?",
            (_stamp(now), _stamp(now)),
        )


def online(db, workspace_id: str, user_id: str, now: float, cli: str | None = None) -> list[dict]:
    with db._lock:
        rows = db._conn.execute(
            "SELECT s.machine_id,s.relay_always,s.clis FROM chat_runners_seen s "
            "JOIN runner_machines m ON m.id=s.machine_id "
            "AND m.workspace_id=s.workspace_id AND m.enrolled_by=s.user_id "
            "WHERE s.workspace_id=? AND s.user_id=? AND s.seen_at>=? "
            "AND m.enabled=1 AND m.revoked_at IS NULL ORDER BY s.seen_at DESC,s.machine_id",
            (workspace_id, user_id, _stamp(now - CHAT_ONLINE_S)),
        ).fetchall()
    return [
        {"machine_id": row["machine_id"], "relay_always": bool(row["relay_always"])}
        for row in rows
        if cli is None or cli in json.loads(row["clis"])
    ]


def main_active(gateway, principal, board: str, now: float) -> bool:
    view = front.orchestrator(gateway, principal, board)
    if view is None or not view.connection_id or not view.wakeable:
        return False
    with gateway.db._lock:
        row = gateway.db._conn.execute(
            "SELECT last_used_at FROM connections WHERE id=? AND workspace_id=?",
            (view.connection_id, principal.workspace_id),
        ).fetchone()
    return bool(
        row and row["last_used_at"] and row["last_used_at"] >= _stamp(now - CHAT_MAIN_FRESH_S)
    )


def enqueue(gateway, principal, board: str, message_id: int, now: float) -> bool:
    db = gateway.db
    sweep(db, now)
    ws, user = principal.workspace_id, principal.user_id
    choice = models.resolve(db, ws, board, "main")
    cli, model = (choice.provider, choice.model) if choice else (None, None)
    eligible = online(db, ws, user, now, cli)
    if not eligible:
        return False
    if main_active(gateway, principal, board, now) and not any(m["relay_always"] for m in eligible):
        return False
    with db._lock, db._tx() as conn:
        capped = _used_today(conn, ws, user, now) >= DAILY_CAP
        inserted = conn.execute(
            "INSERT OR IGNORE INTO chat_runner_jobs "
            "(id,workspace_id,board,message_id,user_id,cli,model,state,code,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "chat_" + secrets.token_hex(8),
                ws,
                board,
                message_id,
                user,
                cli,
                model,
                "skipped" if capped else "queued",
                "daily_limit" if capped else "",
                _stamp(now),
                _stamp(now),
            ),
        ).rowcount
    return bool(inserted and not capped)


def _poster(gateway, row):
    try:
        poster = sessions.person_principal(gateway.db, row["user_id"], row["workspace_id"])
        if poster is None:
            return None
        service = BoardService(ToolContext(principal=poster, gateway=gateway))
        page = service.space_read(row["board"], after=row["message_id"] - 1)
        return poster if any(m["id"] == row["message_id"] for m in page["messages"]) else None
    except (LookupError, ToolError):
        return None


def _finish(conn, row, state, code, now):
    return (
        conn.execute(
            "UPDATE chat_runner_jobs SET state=?,code=?,updated_at=? "
            "WHERE id=? AND state='leased' AND machine_id=? AND lease_hash=? AND lease_until>?",
            (
                state,
                code,
                _stamp(now),
                row["id"],
                row["machine_id"],
                row["lease_hash"],
                _stamp(now),
            ),
        ).rowcount
        == 1
    )


def lease(
    gateway, machine: dict, clis: list[str], context: str, relay_always: bool, now: float
) -> dict | None:
    machine = dict(machine)
    db = gateway.db
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    claimed = None
    with db._lock, db._tx() as conn:
        conn.execute(
            "INSERT INTO chat_runners_seen "
            "(machine_id,workspace_id,user_id,clis,context,relay_always,seen_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(machine_id) DO UPDATE SET workspace_id=excluded.workspace_id,"
            "user_id=excluded.user_id,clis=excluded.clis,context=excluded.context,"
            "relay_always=excluded.relay_always,seen_at=excluded.seen_at",
            (
                machine["id"],
                machine["workspace_id"],
                machine["enrolled_by"],
                json.dumps(clis),
                context,
                int(relay_always),
                _stamp(now),
            ),
        )
        sweep(db, now)
        if conn.execute(
            "SELECT 1 FROM chat_runner_jobs WHERE machine_id=? AND state='leased' AND lease_until>?",
            (machine["id"], _stamp(now)),
        ).fetchone():
            return None
        rows = conn.execute(
            "SELECT * FROM chat_runner_jobs WHERE workspace_id=? AND user_id=? "
            "AND state='queued' ORDER BY created_at,rowid",
            (machine["workspace_id"], machine["enrolled_by"]),
        ).fetchall()
        for row in rows:
            if row["cli"] is not None and row["cli"] not in clis:
                continue
            if conn.execute(
                "UPDATE chat_runner_jobs SET state='leased',machine_id=?,lease_hash=?,"
                "lease_until=?,updated_at=? WHERE id=? AND state='queued'",
                (machine["id"], digest, _stamp(now + CHAT_LEASE_S), _stamp(now), row["id"]),
            ).rowcount:
                claimed = dict(row)
                claimed.update(machine_id=machine["id"], lease_hash=digest)
                break
    if claimed is None:
        return None
    try:
        poster = _poster(gateway, claimed)
        if poster is None:
            with db._lock:
                _finish(db._conn, claimed, "skipped", "no_access", now)
            return None
        system, prompt = chat_context.build(
            gateway, poster, claimed["board"], claimed["message_id"], context
        )
    except Exception:
        try:
            with db._lock:
                _finish(db._conn, claimed, "failed", "bad_output", now)
        except Exception as exc:
            log_failure(logger, "chat relay finish failed", exc)
        return None
    return {
        "job": claimed["id"],
        "lease_token": token,
        "cli": claimed["cli"],
        "model": claimed["model"],
        "system": system,
        "prompt": prompt,
        "max_chars": MAX_REPLY_CHARS,
        "expires_in": CHAT_LEASE_S,
    }


def _fenced(conn, machine, job, lease_token, now):
    row = conn.execute(
        "SELECT * FROM chat_runner_jobs WHERE id=? AND workspace_id=? AND user_id=? "
        "AND state='leased' AND machine_id=? AND lease_until>?",
        (job, machine["workspace_id"], machine["enrolled_by"], machine["id"], _stamp(now)),
    ).fetchone()
    digest = hashlib.sha256(lease_token.encode()).hexdigest()
    if row is None or not secrets.compare_digest(row["lease_hash"] or "", digest):
        raise Fenced()
    return dict(row)


def answer(
    gateway, machine: dict, job: str, lease_token: str, text: str, cli: str, model: str, now: float
) -> dict:
    machine = dict(machine)
    db = gateway.db
    with db._lock:
        row = _fenced(db._conn, machine, job, lease_token, now)
    if (
        not 1 <= len(text.encode("utf-8")) <= MAX_ANSWER_BYTES
        or CHAT_MODEL.fullmatch(model) is None
    ):
        raise InvalidReply("Invalid answer")
    try:
        poster = _poster(gateway, row)
    except (OSError, sqlite3.Error):
        raise ReplyBusy() from None
    with db._lock, db._tx() as conn:
        row = _fenced(conn, machine, job, lease_token, now)
        seen = conn.execute(
            "SELECT clis FROM chat_runners_seen WHERE machine_id=?", (machine["id"],)
        ).fetchone()
        if (row["cli"] is not None and cli != row["cli"]) or (
            row["cli"] is None and (seen is None or cli not in json.loads(seen["clis"]))
        ):
            raise InvalidReply("Invalid answer CLI")
        if poster is None:
            _finish(conn, row, "failed", "no_access", now)
        else:
            text, kinds = redact.redact(text[:MAX_REPLY_CHARS])
            kinds = set(kinds) | {kind for kind in redact.KINDS if f"[redacted:{kind}]" in text}
            try:
                store.open_replies(gateway, row["workspace_id"]).add(
                    board=row["board"],
                    message_id=row["message_id"],
                    for_user=row["user_id"],
                    kind="answer",
                    code="ok",
                    text=text,
                    redacted=tuple(sorted(kinds)),
                    model=f"runner:{cli}/{model}",
                )
            except (OSError, sqlite3.Error):
                raise ReplyBusy() from None
            _finish(conn, row, "done", "ok", now)
    if poster is None:
        raise Fenced()
    return {"job": job, "state": "done"}


def fail(gateway, machine: dict, job: str, lease_token: str, code: str, now: float) -> dict:
    machine = dict(machine)
    db = gateway.db
    with db._lock, db._tx() as conn:
        row = _fenced(conn, machine, job, lease_token, now)
        if code not in RUNNER_FAIL_CODES:
            raise InvalidReply("Invalid runner failure code")
        _finish(conn, row, "failed", code, now)
    return {"job": job, "state": "failed", "code": code}


def caller_state(db, workspace_id: str, user_id: str, now: float, cli: str | None = None) -> dict:
    sweep(db, now)
    eligible = online(db, workspace_id, user_id, now, cli)
    start, end = _day(now)
    with db._lock:
        used = _used_today(db._conn, workspace_id, user_id, now)
        error = db._conn.execute(
            "SELECT code FROM chat_runner_jobs WHERE workspace_id=? AND user_id=? "
            "AND state IN ('failed','expired','skipped') AND created_at>=? AND created_at<? "
            "ORDER BY updated_at DESC,created_at DESC,rowid DESC LIMIT 1",
            (workspace_id, user_id, start, end),
        ).fetchone()
    return {
        "online": bool(eligible),
        "cli": cli,
        "last_error": error["code"] if error else None,
        "used_today": used,
        "daily_cap": DAILY_CAP,
    }
