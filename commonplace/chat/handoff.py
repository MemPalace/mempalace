"""Deadline-based filing of unclaimed chat requests."""

import logging
import re
from datetime import datetime, timezone

from commonplace import http_util, redact, sessions
from commonplace.boards.service import BoardService
from commonplace.chat import store
from commonplace.chat.types import (
    ACK_WAIT_S,
    HANDOFF_COPY,
    HANDOFF_TITLE_CHARS,
    NOT_WORK_WORDS,
    STALE_AFTER_S,
    STATUS_PREFIXES,
    Handoff,
)
from commonplace.control_db import ControlError
from commonplace.extensions import ToolContext, ToolError

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_handoffs (              -- M1: one row per work-request message
    workspace_id TEXT NOT NULL,
    board TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,                    -- the poster; the card is filed as them
    coordinator TEXT,                         -- boards.coordinator actor at post time, or NULL
    state TEXT NOT NULL CHECK (state IN ('waiting','filing','claimed','filed','failed')),
    code TEXT NOT NULL DEFAULT '',            -- HANDOFF_CODES
    task INTEGER,                             -- card number when filed or claimed by a linked card
    due_at TEXT NOT NULL,                     -- created_at + ACK_WAIT_S, UTC
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, board, message_id)   -- dedup per message
);
CREATE INDEX IF NOT EXISTS chat_handoffs_due ON chat_handoffs(state, due_at);
"""


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(_SCHEMA)


def _stamp(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_work_request(text: str) -> bool:
    tokens = re.findall(r"[\w']+", text.lower())
    if not tokens or (len(tokens) <= 4 and all(t in NOT_WORK_WORDS for t in tokens)):
        return False
    if tokens in (["status"], ["progress"], ["update"], ["news"]):
        return False
    if text.rstrip().endswith("?"):
        for prefix in STATUS_PREFIXES:
            words = re.findall(r"[\w']+", prefix)
            if tokens[: len(words)] == words:
                return False
    return True


def open(db, workspace_id, board, message_id, user_id, coordinator: str | None, now: float) -> bool:
    stamp = _stamp(now)
    with db._lock:
        return (
            db._conn.execute(
                "INSERT OR IGNORE INTO chat_handoffs "
                "(workspace_id,board,message_id,user_id,coordinator,state,due_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'waiting',?,?,?)",
                (
                    workspace_id,
                    board,
                    message_id,
                    user_id,
                    coordinator,
                    _stamp(now + ACK_WAIT_S),
                    stamp,
                    stamp,
                ),
            ).rowcount
            == 1
        )


def due(db, now: float, limit: int = 20) -> list[Handoff]:
    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM chat_handoffs WHERE state='waiting' AND due_at<=? "
            "ORDER BY due_at,workspace_id,board,message_id LIMIT ?",
            (_stamp(now), max(0, limit)),
        ).fetchall()
    return [Handoff(**dict(row)) for row in rows]


def _key(h):
    return (h.workspace_id, h.board, h.message_id)


def _finish(db, h, now, state, code="", task=None):
    with db._lock:
        db._conn.execute(
            "UPDATE chat_handoffs SET state=?,code=?,task=?,updated_at=? "
            "WHERE workspace_id=? AND board=? AND message_id=? AND state='filing'",
            (state, code, task, _stamp(now), *_key(h)),
        )
    return state


def _service(gateway, h):
    try:
        poster = sessions.person_principal(gateway.db, h.user_id, h.workspace_id)
    except (ControlError, LookupError):
        return None
    if poster is None:
        return None
    return BoardService(ToolContext(principal=poster, gateway=gateway))


def _linked(service, h):
    return next(
        (
            c["number"]
            for c in service.task_list(board=h.board, all_statuses=True)
            if c.get("message_id") == h.message_id
        ),
        None,
    )


def _notice(gateway, h, code, task=None, coordinator=None):
    if code == "file_failed":
        text = HANDOFF_COPY[code]
    else:
        template = HANDOFF_COPY["filed" if coordinator else "filed_no_orchestrator"]
        text = template.format(
            wait=ACK_WAIT_S // 60, number=task, name=(coordinator or "").removeprefix("conn:")
        )
    text, kinds = redact.redact(text)
    store.open_replies(gateway, h.workspace_id).add(
        board=h.board,
        message_id=h.message_id,
        for_user=h.user_id,
        kind="handoff",
        code=code,
        text=text,
        redacted=tuple(sorted(set(kinds))),
        model=None,
        task=task,
    )


def settle(gateway, handoff: Handoff, now: float) -> str:
    h = handoff
    db = gateway.db
    try:
        with db._lock:
            claimed = db._conn.execute(
                "UPDATE chat_handoffs SET state='filing',updated_at=? "
                "WHERE workspace_id=? AND board=? AND message_id=? AND state='waiting'",
                (_stamp(now), *_key(h)),
            ).rowcount
            if not claimed:
                row = db._conn.execute(
                    "SELECT state FROM chat_handoffs WHERE workspace_id=? AND board=? AND message_id=?",
                    _key(h),
                ).fetchone()
                return row["state"] if row else h.state
        service = _service(gateway, h)
        if service is None:
            raise ToolError("Poster unavailable")
        task = _linked(service, h)
        if task is not None:
            return _finish(db, h, now, "claimed", "claimed", task)
        current = next(
            (b["coordinator"] for b in service.board_list() if b["slug"] == h.board), None
        )
        coordinators = {a for a in (h.coordinator, current) if a}
        later = service.space_read(h.board, after=h.message_id)["messages"]
        if any(m["id"] > h.message_id and m["actor"] in coordinators for m in later):
            return _finish(db, h, now, "claimed", "claimed")
        page = service.space_read(h.board, after=h.message_id - 1)
        message = next((m for m in page["messages"] if m["id"] == h.message_id), None)
        if message is None:
            raise ToolError("Message unavailable")
        text = message["text"]
        title = next((line.strip() for line in text.splitlines() if line.strip()), "Chat request")
        card = service.task_add(
            board=h.board,
            title=title[:HANDOFF_TITLE_CHARS],
            body=text,
            status="todo",
            labels=["from-chat"],
            message_id=h.message_id,
        )
    except ToolError:
        _finish(db, h, now, "failed", "no_access")
        _notice(gateway, h, "file_failed")
        return "failed"
    except Exception as exc:
        http_util.log_failure(logger, "Chat filing failed", exc)
        with db._lock:
            db._conn.execute(
                "UPDATE chat_handoffs SET state='waiting',due_at=?,updated_at=? "
                "WHERE workspace_id=? AND board=? AND message_id=? AND state IN ('waiting','filing')",
                (_stamp(now + 60), _stamp(now), *_key(h)),
            )
        return "waiting"
    task = card["number"]
    _finish(db, h, now, "filed", "filed", task)
    _notice(gateway, h, "filed", task, current or h.coordinator)
    return "filed"


def recover(db, gateway, now: float) -> None:
    cutoff = _stamp(now - STALE_AFTER_S)
    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM chat_handoffs WHERE state='filing' AND updated_at<?", (cutoff,)
        ).fetchall()
    for row in rows:
        try:
            _recover_row(db, gateway, now, cutoff, row)
        except Exception as exc:
            http_util.log_failure(logger, "Chat recovery failed", exc)


def _recover_row(db, gateway, now, cutoff, row):
    h = Handoff(**dict(row))
    # Serialize recovery with claims and reject obsolete snapshots.
    with db._lock:
        fresh = db._conn.execute(
            "SELECT updated_at,state FROM chat_handoffs "
            "WHERE workspace_id=? AND board=? AND message_id=?",
            _key(h),
        ).fetchone()
        if fresh["state"] != "filing" or fresh["updated_at"] >= cutoff:
            return
        service = _service(gateway, h)
        if service is None:
            _finish(db, h, now, "failed", "no_access")
            _notice(gateway, h, "file_failed")
            return
        try:
            task = _linked(service, h)
        except ToolError:
            _finish(db, h, now, "failed", "no_access")
            _notice(gateway, h, "file_failed")
            return
        if task is None:
            _finish(db, h, now, "waiting")
        else:
            _finish(db, h, now, "filed", "filed", task)
            _notice(gateway, h, "filed", task, h.coordinator)
