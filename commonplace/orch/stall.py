"""No-progress wake: a board with open work and no card done for N minutes is nudged.

Liveness is not progress. A coordinator can hold a live lease and tick every few minutes
while no card moves. This module measures progress instead: when a runner-configured
board has actionable open work, free agent slots or expired claims, and no card reached
``done`` for ``minutes`` (default 20, per board, 0 turns it off), the hub emits one
``board.stalled`` event. That goes to the app's event feed and through every matching
webhook, which is the same wake path as card events. The payload names the doing cards
with their age and claim state, the unstarted todo cards and the expired claims.

Backoff: after a nudge, the next one waits another ``minutes`` with no done card. A done
card resets the streak. After ``ESCALATE_AFTER`` nudges in a row without a done card, the
board is listed in the owner's Needs me (``stalled``).

Never nudged: a board whose open cards are all blocked by another open card or waiting on a
person (that is a clear board for the orchestrator), a board with no open card, a board
at its agent cap with no expired claim and nothing unstarted.

State lives in ``runner.sqlite3`` (``orch_stall``). Reads of ``boards.sqlite3`` are plain
autocommit selects; the evaluation runs after the board's trigger sweep, outside it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from commonplace.boards.store import BoardStore
from commonplace.http_util import log_failure

logger = logging.getLogger(__name__)

DEFAULT_MINUTES = 20
MAX_MINUTES = 24 * 60
ESCALATE_AFTER = 3
LIST = 20
DDL = (
    "CREATE TABLE IF NOT EXISTS orch_stall (board TEXT PRIMARY KEY, "
    "minutes INTEGER NOT NULL DEFAULT 20 CHECK (minutes BETWEEN 0 AND 1440), "
    "nudged_at TEXT, streak INTEGER NOT NULL DEFAULT 0)"
)
LINE = (
    "No card finished in the last {n} minutes. Check what is stuck, unblock or reassign "
    "it, or start the next card."
)


def _ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _table(conn) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_stall'"
        ).fetchone()
    )


def settings_in(conn, board: str) -> dict:
    """``{minutes, nudged_at, streak}`` of a board (defaults when never stored)."""
    row = (
        conn.execute("SELECT * FROM orch_stall WHERE board=?", (board,)).fetchone()
        if _table(conn)
        else None
    )
    if row is None:
        return {"minutes": DEFAULT_MINUTES, "nudged_at": None, "streak": 0}
    return {"minutes": row["minutes"], "nudged_at": row["nudged_at"], "streak": row["streak"]}


def set_minutes(st, board: str, minutes) -> dict:
    """Owner setting: minutes without a done card before a nudge; 0 turns nudges off."""
    if type(minutes) is not int or not 0 <= minutes <= MAX_MINUTES:
        raise ValueError(f"minutes must be an integer from 0 to {MAX_MINUTES}")
    with st.transaction() as conn:
        conn.execute(DDL)
        conn.execute(
            "INSERT INTO orch_stall(board, minutes) VALUES (?, ?) "
            "ON CONFLICT(board) DO UPDATE SET minutes=excluded.minutes",
            (board, minutes),
        )
        return settings_in(conn, board)


def _blocked(conn, card_id) -> bool:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_blockers'").fetchone():
        return False
    return bool(
        conn.execute(
            "SELECT 1 FROM task_blockers b JOIN tasks t ON t.id=b.blocker_id "
            "WHERE b.task_id=? AND t.status!='done' LIMIT 1",
            (card_id,),
        ).fetchone()
    )


def _on_person(row) -> bool:
    return bool(row["ask"]) or any(
        (row[key] or "").startswith("user:") for key in ("assignee", "ask_to")
    )


def assess(directory, board_row: dict, cap: int | None, now: str, minutes: int, since) -> dict:
    """The board's progress picture at ``now``. ``stalled`` is True when a nudge is due:
    actionable open work, a free slot or an expired claim or an unstarted card, and no
    card done after ``max(last done, since)`` for ``minutes``."""
    with BoardStore(directory, create=False).connection() as conn:
        rows = conn.execute(
            "SELECT id, title, status, assignee, ask, ask_to, claim_until, claim_expired_at, "
            "updated_at FROM tasks WHERE board_id=? AND status IN ('todo','doing') ORDER BY id",
            (board_row["id"],),
        ).fetchall()
        actionable = [r for r in rows if not _on_person(r) and not _blocked(conn, r["id"])]
        last_done = conn.execute(
            "SELECT MAX(e.created_at) FROM task_events e JOIN tasks t ON t.id=e.task_id "
            "WHERE t.board_id=? AND e.to_status='done'",
            (board_row["id"],),
        ).fetchone()[0]
    doing, todo, expired = [], [], []
    for r in actionable:
        if r["status"] == "todo":
            if not r["assignee"]:
                todo.append({"number": r["id"], "title": r["title"]})
            continue
        lapsed = r["claim_expired_at"] is not None or (
            r["claim_until"] is not None and r["claim_until"] <= now
        )
        card = {
            "number": r["id"],
            "title": r["title"],
            "assignee": r["assignee"],
            "since": r["updated_at"],
            "claim": "expired" if lapsed else "live",
        }
        doing.append(card)
        if lapsed:
            expired.append(card)
    agents = sum(
        1 for c in doing if c["claim"] == "live" and (c["assignee"] or "").startswith("conn:")
    )
    free = cap is None or agents < cap
    marks = [x for x in (last_done, since, board_row.get("created_at")) if x]
    quiet_from = max(marks) if marks else now
    quiet = (_ts(now) - _ts(quiet_from)).total_seconds() / 60
    stalled = bool(
        minutes and actionable and (expired or (free and (todo or doing))) and quiet >= minutes
    )
    return {
        "stalled": stalled,
        "minutes": minutes,
        "quiet_minutes": int(quiet),
        "last_done": last_done,
        "doing": doing[:LIST],
        "todo": todo[:LIST],
        "expired": expired[:LIST],
        "free_slots": None if cap is None else max(cap - agents, 0),
    }


def message(board: str, picture: dict) -> str:
    def cards(items, extra):
        return "; ".join(f"#{c['number']} {c['title']}{extra(c)}" for c in items) or "none"

    return "\n".join(
        (
            f"{board}: " + LINE.format(n=picture["minutes"]),
            "Doing: "
            + cards(picture["doing"], lambda c: f" (since {c['since']}, claim {c['claim']})"),
            "Unstarted todo: " + cards(picture["todo"], lambda c: ""),
            "Expired claims: " + cards(picture["expired"], lambda c: f" ({c['assignee']})"),
        )
    )


def evaluate(gateway, workspace_id: str, st, board_row: dict, cap, now: str) -> dict | None:
    """Run after a board's trigger sweep: nudge once when due. Returns the emitted event
    data, or None. Errors are logged and never break the sweep."""
    board = board_row["slug"]
    try:
        with st.connection() as conn:
            current = settings_in(conn, board)
        if not current["minutes"]:
            return None
        picture = assess(
            gateway.workspace_dir(workspace_id),
            board_row,
            cap,
            now,
            current["minutes"],
            current["nudged_at"],
        )
        reset = current["streak"] and (
            picture["last_done"] is not None
            and current["nudged_at"] is not None
            and picture["last_done"] > current["nudged_at"]
        )
        if not picture["stalled"]:
            if reset or (current["streak"] and not picture["doing"] and not picture["todo"]):
                with st.transaction() as conn:
                    conn.execute(DDL)
                    conn.execute("UPDATE orch_stall SET streak=0 WHERE board=?", (board,))
            return None
        streak = 1 if reset else current["streak"] + 1
        data = {
            "board": board,
            "title": LINE.format(n=current["minutes"]),
            "minutes": current["minutes"],
            "streak": streak,
            "summary": message(board, picture),
            "doing": [c["number"] for c in picture["doing"]],
            "todo": [c["number"] for c in picture["todo"]],
            "expired": [c["number"] for c in picture["expired"]],
        }
        from commonplace import events

        # Emit first: a wake that was never delivered must not count toward escalation.
        events.emit(
            gateway.db,
            workspace_id,
            "board.stalled",
            subject=f"board:{board}",
            actor="system",
            data=data,
        )
        with st.transaction() as conn:
            conn.execute(DDL)
            conn.execute(
                "INSERT INTO orch_stall(board, minutes, nudged_at, streak) VALUES (?,?,?,?) "
                "ON CONFLICT(board) DO UPDATE SET nudged_at=excluded.nudged_at, "
                "streak=excluded.streak",
                (board, current["minutes"], now, streak),
            )
        return data
    except Exception as exc:
        log_failure(logger, "orchestrator no-progress check failed", exc)
        return None


def stalled(gateway, workspace_id: str) -> list[dict]:
    """Boards nudged ``ESCALATE_AFTER`` times in a row without a done card, for the
    owner's Needs me."""
    from pathlib import Path

    from . import store as orch_store

    if not (Path(gateway.workspace_dir(workspace_id)) / "runner.sqlite3").exists():
        return []
    try:
        st = orch_store.open_existing(gateway, workspace_id)
        with st.connection() as conn:
            if not _table(conn):
                return []
            rows = conn.execute(
                "SELECT * FROM orch_stall WHERE streak>=? ORDER BY board", (ESCALATE_AFTER,)
            ).fetchall()
    except Exception as exc:
        log_failure(logger, "orchestrator no-progress state unavailable", exc)
        return []
    return [
        {
            "board": r["board"],
            "nudges": r["streak"],
            "since": r["nudged_at"],
            "message": f"The orchestrator on {r['board']} did not finish a card after "
            f"{r['streak']} nudges; check it is running.",
        }
        for r in rows
    ]
