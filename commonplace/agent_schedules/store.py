"""Schedule rows in the control database: create, list, pause, resume, cancel, claim.

``agent_schedules`` holds one row per schedule; ``agent_schedule_runs`` holds one row per
due time that was claimed, keyed by (schedule, due time). That key is what makes delivery
at most once: ``claim`` advances ``next_due`` and inserts the run in one transaction, so a
due time is claimed by exactly one caller, across threads and across restarts.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from commonplace import schedule_time as times

MAX_PER_WORKSPACE = 20  # schedules that are active or paused; cancelled ones do not count
MAX_PROMPT = 4000
MAX_NAME = 80
KEEP_RUNS = 30
AUTO_PAUSE_FAILURES = 5
STATES = ("active", "paused", "cancelled")
RUN_STATES = ("claimed", "delivered", "failed", "skipped")


class Limit(Exception):
    """The workspace already holds ``MAX_PER_WORKSPACE`` live schedules."""


class Missing(LookupError):
    """No such schedule in this workspace."""


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_schedules (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, created_by TEXT NOT NULL,
                agent_id TEXT NOT NULL, board TEXT NOT NULL, name TEXT NOT NULL,
                prompt TEXT NOT NULL, schedule TEXT NOT NULL, timezone TEXT NOT NULL,
                anchor TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active','paused','cancelled')),
                paused_reason TEXT NOT NULL DEFAULT '',
                next_due TEXT, last_due TEXT, last_status TEXT NOT NULL DEFAULT '',
                last_code TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS agent_schedules_due
                ON agent_schedules(state, next_due);
            CREATE INDEX IF NOT EXISTS agent_schedules_workspace
                ON agent_schedules(workspace_id, state);
            CREATE TABLE IF NOT EXISTS agent_schedule_runs (
                schedule_id TEXT NOT NULL REFERENCES agent_schedules(id), due_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('claimed','delivered','failed','skipped')),
                code TEXT NOT NULL DEFAULT '', message_id INTEGER,
                claimed_at TEXT NOT NULL, finished_at TEXT,
                PRIMARY KEY (schedule_id, due_at)
            );
            """
        )


def view(row, *, runs=None) -> dict:
    out = {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "board": row["board"],
        "name": row["name"],
        "prompt": row["prompt"],
        "schedule": json.loads(row["schedule"]),
        "timezone": row["timezone"],
        "state": row["state"],
        "paused_reason": row["paused_reason"],
        "next_due": row["next_due"],
        "last_due": row["last_due"],
        "last_status": row["last_status"],
        "last_code": row["last_code"],
        "created_at": row["created_at"],
    }
    if runs is not None:
        out["runs"] = runs
    return out


def create(
    db, *, workspace_id, user_id, agent_id, board, name, prompt, schedule, tz, now: datetime
) -> dict:
    """Store a validated schedule; ``Limit`` when the workspace is full."""
    anchor = now.replace(second=0, microsecond=0)
    first = times.next_due(schedule, tz, now, anchor)
    identity = "sch" + uuid.uuid4().hex[:20]
    stamp = times.stamp(now)
    with db._lock, db._tx() as conn:
        live = conn.execute(
            "SELECT count(*) FROM agent_schedules WHERE workspace_id=? AND state!='cancelled'",
            (workspace_id,),
        ).fetchone()[0]
        if live >= MAX_PER_WORKSPACE:
            raise Limit
        conn.execute(
            "INSERT INTO agent_schedules (id,workspace_id,created_by,agent_id,board,name,prompt,"
            "schedule,timezone,anchor,state,next_due,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?,?)",
            (
                identity,
                workspace_id,
                user_id,
                agent_id,
                board,
                name,
                prompt,
                json.dumps(schedule, sort_keys=True),
                tz,
                times.stamp(anchor),
                times.stamp(first),
                stamp,
                stamp,
            ),
        )
        return view(
            conn.execute("SELECT * FROM agent_schedules WHERE id=?", (identity,)).fetchone()
        )


def _row(conn, workspace_id, identity):
    row = conn.execute(
        "SELECT * FROM agent_schedules WHERE id=? AND workspace_id=?", (identity, workspace_id)
    ).fetchone()
    if row is None:
        raise Missing(identity)
    return row


def runs(conn, identity, limit=10) -> list:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT due_at,state,code,message_id,claimed_at,finished_at FROM agent_schedule_runs "
            "WHERE schedule_id=? ORDER BY due_at DESC LIMIT ?",
            (identity, limit),
        )
    ]


def get(db, workspace_id, identity) -> dict:
    with db._lock:
        row = _row(db._conn, workspace_id, identity)
        return view(row, runs=runs(db._conn, identity))


def listing(db, workspace_id) -> list:
    with db._lock:
        return [
            view(r)
            for r in db._conn.execute(
                "SELECT * FROM agent_schedules WHERE workspace_id=? AND state!='cancelled' "
                "ORDER BY created_at,id",
                (workspace_id,),
            )
        ]


def set_state(db, workspace_id, identity, action, now: datetime) -> dict:
    """``pause``, ``resume`` or ``cancel``. Resuming counts from ``now``: due times that
    passed while paused are not delivered. Cancelling is final."""
    with db._lock, db._tx() as conn:
        row = _row(conn, workspace_id, identity)
        stamp = times.stamp(now)
        if row["state"] == "cancelled" or (action == "pause" and row["state"] == "paused"):
            return view(row)
        if action == "cancel":
            conn.execute(
                "UPDATE agent_schedules SET state='cancelled',next_due=NULL,updated_at=? "
                "WHERE id=?",
                (stamp, identity),
            )
        elif action == "pause":
            conn.execute(
                "UPDATE agent_schedules SET state='paused',paused_reason='owner',next_due=NULL,"
                "updated_at=? WHERE id=?",
                (stamp, identity),
            )
        else:
            following = times.next_due(
                json.loads(row["schedule"]), row["timezone"], now, times.parse_stamp(row["anchor"])
            )
            conn.execute(
                "UPDATE agent_schedules SET state='active',paused_reason='',failures=0,"
                "next_due=?,updated_at=? WHERE id=?",
                (times.stamp(following) if following else None, stamp, identity),
            )
        return view(
            conn.execute("SELECT * FROM agent_schedules WHERE id=?", (identity,)).fetchone()
        )


def due(db, now: datetime, limit=20) -> list:
    with db._lock:
        return [
            dict(r)
            for r in db._conn.execute(
                "SELECT * FROM agent_schedules WHERE state='active' AND next_due<=? "
                "ORDER BY next_due,id LIMIT ?",
                (times.stamp(now), limit),
            )
        ]


def claim(db, row, now: datetime, *, missed: bool) -> bool:
    """Claim the due time ``row['next_due']`` for exactly one caller.

    Advances ``next_due`` past ``now`` and records the run in one transaction. False when
    another caller already claimed it (or the schedule was paused or cancelled meanwhile)."""
    due_at = row["next_due"]
    following = times.next_due(
        json.loads(row["schedule"]), row["timezone"], now, times.parse_stamp(row["anchor"])
    )
    stamp = times.stamp(now)
    with db._lock, db._tx() as conn:
        moved = conn.execute(
            "UPDATE agent_schedules SET next_due=?,last_due=?,updated_at=? "
            "WHERE id=? AND state='active' AND next_due=?",
            (times.stamp(following) if following else None, due_at, stamp, row["id"], due_at),
        ).rowcount
        if moved != 1:
            return False
        conn.execute(
            "INSERT INTO agent_schedule_runs (schedule_id,due_at,state,code,claimed_at,finished_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                row["id"],
                due_at,
                "skipped" if missed else "claimed",
                "missed" if missed else "",
                stamp,
                stamp if missed else None,
            ),
        )
        if missed:
            conn.execute(
                "UPDATE agent_schedules SET last_status='skipped',last_code='missed' WHERE id=?",
                (row["id"],),
            )
        conn.execute(
            "DELETE FROM agent_schedule_runs WHERE schedule_id=? AND due_at NOT IN "
            "(SELECT due_at FROM agent_schedule_runs WHERE schedule_id=? "
            "ORDER BY due_at DESC LIMIT ?)",
            (row["id"], row["id"], KEEP_RUNS),
        )
    return True


def finish(db, row, due_at: str, now: datetime, *, ok: bool, code="", message_id=None) -> None:
    """Record the outcome of a claimed run; repeated failures pause the schedule."""
    stamp = times.stamp(now)
    state = "delivered" if ok else "failed"
    with db._lock, db._tx() as conn:
        conn.execute(
            "UPDATE agent_schedule_runs SET state=?,code=?,message_id=?,finished_at=? "
            "WHERE schedule_id=? AND due_at=? AND state='claimed'",
            (state, code, message_id, stamp, row["id"], due_at),
        )
        failures = (
            0
            if ok
            else conn.execute(
                "SELECT failures FROM agent_schedules WHERE id=?", (row["id"],)
            ).fetchone()[0]
            + 1
        )
        conn.execute(
            "UPDATE agent_schedules SET last_status=?,last_code=?,failures=? WHERE id=?",
            (state, code, failures, row["id"]),
        )
        if failures >= AUTO_PAUSE_FAILURES:
            conn.execute(
                "UPDATE agent_schedules SET state='paused',paused_reason=?,next_due=NULL,"
                "updated_at=? WHERE id=? AND state='active'",
                (code or "failed", stamp, row["id"]),
            )


def recover(db, now: datetime) -> int:
    """Runs left ``claimed`` by a crash are marked failed, never delivered again."""
    stamp = times.stamp(now)
    with db._lock, db._tx() as conn:
        conn.execute(
            "UPDATE agent_schedules SET last_status='failed',last_code='interrupted' WHERE id IN "
            "(SELECT schedule_id FROM agent_schedule_runs WHERE state='claimed')"
        )
        return conn.execute(
            "UPDATE agent_schedule_runs SET state='failed',code='interrupted',finished_at=? "
            "WHERE state='claimed'",
            (stamp,),
        ).rowcount
