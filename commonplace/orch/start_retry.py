"""Bounded, backed-off retries of todo starts the trigger sweep refused for a transient reason.

A firing todo event whose start is refused advances the cursor (a coded ``orch_misses``
row), so the card would sit in todo until a person looked. A *transient* refusal (capacity,
a busy or unavailable store, a config that moved mid-start, an unreachable worker, a recent
start) can clear by itself, so ``fenced_start`` also writes an ``orch_start_retries`` row in
the same transaction as the miss and the cursor advance. The sweep starts every due row
again under the event's own request key (``trig:<event>``, so a start that landed in between
is returned, never doubled):

- a start succeeds: the row is ``started`` and keeps the job id;
- a transient refusal again: ``attempts`` grows, ``next_at`` moves out by ``BACKOFF[attempts]``
  seconds, and a miss row records the refusal; after ``len(BACKOFF)`` retries the row is
  ``exhausted`` and a ``retry_exhausted`` miss is recorded;
- a permanent refusal (anything not in ``TRANSIENT``: the card moved on, an actor or grant
  problem, an unresolved model, a pending restore) ends the row as ``refused`` and is not
  tried again.

Only boards without workspace orchestration use this: with it on, root cards already
have the split reconciliation (``triggers._reconcile``), which counts its own attempts.
Every statement here runs inside the caller's ``fenced_start`` or ``trigger_miss``
transaction except ``due``, a read."""

from __future__ import annotations

from commonplace.runner import jobs

# Refusals that can clear without anybody touching the card.
TRANSIENT = frozenset(
    {
        "at_cap",  # the board is at its agent cap
        "project_limit",  # the board's project limit (orchestration admission)
        "busy",  # the runner store or admission lock was held
        "settings_unavailable",  # the settings store could not be read
        "config_changed",  # the accepted config moved between the read and the lock
        "worker_unavailable",  # the machine's worker connection is not reachable
        "started_recently",  # a start of this card is inside its cool-down
    }
)
BACKOFF = (60, 300, 900, 1800, 3600)  # seconds before retry 1..5
DDL = (
    """CREATE TABLE IF NOT EXISTS orch_start_retries (
 board TEXT NOT NULL,
 event_id INTEGER NOT NULL,
 card INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','started','refused','exhausted')),
 attempts INTEGER NOT NULL DEFAULT 0,
 next_at TEXT,
 last_code TEXT NOT NULL,
 job_id TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY (board, event_id)
)""",
)


def transient(code: str | None) -> bool:
    return code in TRANSIENT


def ensure_schema(conn) -> None:
    for statement in DDL:
        conn.execute(statement)


def _has_table(conn) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_start_retries'"
        ).fetchone()
    )


def due(conn, board: str, now: str, limit: int = 50) -> list[dict]:
    """Pending retries of ``board`` whose backoff has passed, oldest event first."""
    if not _has_table(conn):
        return []
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM orch_start_retries WHERE board=? AND state='pending' AND next_at<=? "
            "ORDER BY event_id LIMIT ?",
            (board, now, limit),
        )
    ]


def claimable(conn, board: str, event: int, retry: int) -> bool:
    """In-lock: retry number ``retry`` of ``event`` is still the next one (a concurrent
    sweep that already settled it makes this False, so an attempt is never counted twice)."""
    row = conn.execute(
        "SELECT state, attempts FROM orch_start_retries WHERE board=? AND event_id=?",
        (board, event),
    ).fetchone()
    return row is not None and row["state"] == "pending" and row["attempts"] == retry - 1


def settle(
    conn, *, board: str, card: int, event: int, code: str | None, now: str, retry: int = 0, job=None
) -> None:
    """Record the outcome of a trigger start: ``code`` is its refusal (None: it started).

    ``retry`` is 0 for the original start and the retry number otherwise."""
    if not retry:
        if transient(code):
            ensure_schema(conn)
            conn.execute(
                "INSERT OR IGNORE INTO orch_start_retries(board,event_id,card,state,attempts,"
                "next_at,last_code,created_at,updated_at) VALUES (?,?,?,'pending',0,?,?,?,?)",
                (board, event, card, jobs._after(now, BACKOFF[0]), code, now, now),
            )
        return
    if code is None:
        state, next_at = "started", None
    elif not transient(code):
        state, next_at = "refused", None
    elif retry >= len(BACKOFF):
        state, next_at = "exhausted", None
    else:
        state, next_at = "pending", jobs._after(now, BACKOFF[retry])
    conn.execute(
        "UPDATE orch_start_retries SET state=?, attempts=?, next_at=?, last_code=?, job_id=?, "
        "updated_at=? WHERE board=? AND event_id=?",
        (state, retry, next_at, code or "started", job["id"] if job else None, now, board, event),
    )
    if state == "exhausted":
        conn.execute(
            "INSERT INTO orch_misses(board,event_id,card,code,actor,gen,created_at) "
            "SELECT ?,?,?,?,actor,gen,? FROM orch_misses WHERE board=? AND event_id=? "
            "ORDER BY id LIMIT 1",
            (board, event, card, "retry_exhausted", now, board, event),
        )
