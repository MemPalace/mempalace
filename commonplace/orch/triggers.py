"""Todo triggers on configured boards: the per-board cursor, arming and the sweep.

Scope: only boards whose accepted live config has ``[runner]`` (``orch_cursor`` rows exist
only for them); other boards are never scanned. The sweep reads committed
``task_events`` of the board's cards after the board's cursor, so an event emission that
failed after its commit is still found.

Firing rule (``durable.FIRING_EVENT``): ``to_status='todo' AND (from_status IS NULL OR
from_status!='todo')``, the plan-approval row ``kind='edit' AND choice='approve plan'
AND to_status='todo'``, or an origin release ``kind='decide' AND choice='release
origin'`` (a released card that already waited in todo has no new todo transition).

A firing event whose start cannot even be planned (``prepare`` refuses, or the config
lacks the role the start needs) is a coded miss that advances the cursor, like any other
refusal; only infrastructure errors leave the cursor.

Cursor protocol (board events live in ``boards.sqlite3``, the cursor in
``runner.sqlite3``, so no transaction spans both):

1. A live accept that configures the board commits ``cursor=NULL, gen=gen+1`` ("arming",
   ``commonplace.orch.store``); so does a restore acknowledgement for every configured
   board. Removing ``[runner]`` deletes the row. The sweep skips a NULL cursor.
2. ``arm_cursor`` reads ``gen``, then ``COALESCE(MAX(task_events.id), 0)``, then sets
   ``cursor=MAX WHERE board=? AND gen=? AND cursor IS NULL``; a stale arm updates 0 rows.
   It runs right after the accept (the surface calls it) and at the start of every sweep
   for a NULL cursor, so a crash between 1 and 2 heals itself. Events committed before
   the MAX read are never fired. ``task_events.id`` is an ``INTEGER PRIMARY KEY`` of an
   append-only log (``boards/schema.sql`` triggers abort every UPDATE and DELETE), so ids
   only grow.
3. The sweep reads ``(cursor, gen)`` first, then the candidates, on an autocommit
   connection (no transaction). Each candidate goes through
   ``runner_bridge.fenced_start``, whose first statement inside its ``BEGIN IMMEDIATE`` is
   the ``(cursor, gen)`` re-check; a mismatch drops the candidate (no job, no miss) and
   ends the board's sweep. A job or a coded miss commits together with the advance
   ``UPDATE ... SET cursor=<event> WHERE board=? AND gen=? AND cursor=<recorded>``; the
   sweep then re-records the pair. Only infrastructure errors leave the cursor.

Actor: rebuilt from ``stamp_user``/``stamp_connection`` against the pre-transaction
control snapshot: unrebuildable or revoked -> miss ``requester_unknown``; fails
``requester_ok`` for the config's machine -> miss ``requester_not_allowed``.

Triggers run from ``jobs._sweep`` after ``jobs.sweep`` returns (each runner lease call,
start, renewal, result and status read), never inside its transaction.

Locks: the sweep opens the orchestrator store with ``Store.existing`` (no schema script,
no ``orch_state`` insert, no marker conversion), so up to ``fenced_start`` its only
``runner.sqlite3`` write is the arming ``UPDATE`` of a NULL cursor. After a board's sweep,
``orch_sweep_health`` records a failure streak (cleared by the next clean sweep, no write
while healthy); the second failure in a row alerts once (``failing``, Needs me, event). The store is created
by the accept that configures a board; a pending restore marker is converted by the next
explicit store open (surface, CLI, lease call or start), and until then the sweep skips
the workspace as it does under the restore flag.

Barrier hooks (``barrier(name, hook)``) exist for concurrency tests; a hook runs on the
sweeping thread and fails loudly if the sweep's connection holds a transaction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import contextmanager

from commonplace.boards.store import BoardStore, task_row
from commonplace.control_db import ControlError
from commonplace.http_util import log_failure
from commonplace.runner import jobs, registry
from commonplace.runner.predicate import requester_ok
from commonplace.sessions import person_principal

from . import durable, runner_bridge, schema, store as orch_store

logger = logging.getLogger(__name__)
BATCH = 50
HOOKS = (
    "arm_after_gen",  # arm_cursor: after the gen read, before the MAX read
    "sweep_after_pair",  # sweep: after the (cursor, gen) read, before the candidate read
    "sweep_after_requester",  # sweep: after a candidate's actor evaluation
    "sweep_before_start",  # sweep: after the candidate reads, before fenced_start
)
_hooks: dict[str, Callable[[], None]] = {}
_EVENTS = (
    "SELECT id, task_id, kind, stamp_user, stamp_connection, created_at FROM task_events "
    "WHERE id>? AND task_id IN (SELECT id FROM tasks WHERE board_id=?) AND "
    + durable.FIRING_EVENT
    + " ORDER BY id LIMIT ?"
)


@contextmanager
def barrier(name: str, hook: Callable[[], None]):
    """Install ``hook`` at the named barrier for the duration of the block (tests)."""
    if name not in HOOKS:
        raise ValueError("unknown barrier")
    _hooks[name] = hook
    try:
        yield
    finally:
        _hooks.pop(name, None)


def _barrier(name: str, conn) -> None:
    hook = _hooks.get(name)
    if hook is None:
        return
    if conn.in_transaction:
        raise AssertionError(f"trigger barrier {name}: the sweep connection holds a transaction")
    hook()


def _max_event(directory) -> int:
    with BoardStore(directory).connection() as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]


def _arm(st, directory, board: str) -> bool:
    with st.connection() as conn:
        pair = orch_store.cursor_in(conn, board)
        if pair is None or pair[0] is not None:
            return False
        gen = pair[1]
        _barrier("arm_after_gen", conn)
        top = _max_event(directory)
        changed = conn.execute(
            "UPDATE orch_cursor SET cursor=? WHERE board=? AND gen=? AND cursor IS NULL",
            (top, board, gen),
        ).rowcount
    return changed == 1


def arm_cursor(gateway, workspace_id: str, board: str) -> bool:
    """Arm the board's NULL cursor at the current ``MAX(task_events.id)`` (0 if none).

    Call right after a configuring live accept or a restore acknowledgement commits.
    Returns False when there is nothing to arm or the arm was stale (``gen`` moved)."""
    st = orch_store.open_store(gateway, workspace_id)
    return _arm(st, gateway.workspace_dir(workspace_id), board)


def _actor(gateway, workspace_id, event, machine):
    """``(principal, requester ref, refusal code)`` of a firing event."""
    user, name = event["stamp_user"], event["stamp_connection"]
    try:
        if name is None:
            principal = person_principal(gateway.db, user, workspace_id)
        else:
            row = gateway.db.connection(name, workspace_id)
            principal = (
                gateway.db.authenticate_connection(row["id"]) if row["user_id"] == user else None
            )
    except (ControlError, LookupError, TypeError, ValueError):
        principal = None
    if principal is None or principal.workspace_id != workspace_id:
        return None, None, "requester_unknown"
    ref = jobs.requester_ref(principal)
    if machine is None or not requester_ok(gateway, principal, machine):
        return None, ref, "requester_not_allowed"
    return principal, ref, None


def _sweep_board(js, st, board: str, now) -> list[dict]:
    gateway, ws = js.gateway, js.workspace_id
    directory = gateway.workspace_dir(ws)
    results: list[dict] = []
    from commonplace.projects import orchestration

    on = orchestration.enabled(gateway.db, ws)
    with st.connection() as conn:  # autocommit reads; the lock is fenced_start's
        pair = orch_store.cursor_in(conn, board)
        if pair is None or pair[0] is None:
            return results
        cursor, gen = pair
        _barrier("sweep_after_pair", conn)
        with BoardStore(directory).connection() as boards:
            board_row = boards.execute("SELECT * FROM boards WHERE slug=?", (board,)).fetchone()
            if board_row is None or board_row["archived"]:
                return results
            board_row = dict(board_row)
            events = boards.execute(_EVENTS, (cursor, board_row["id"], BATCH)).fetchall()
            cards = {row["task_id"]: dict(task_row(boards, row["task_id"])) for row in events}
        if not events and not on:
            return results
        version = orch_store.resolve_in(conn, board)
        machine = registry.get_machine(gateway.db, ws, version.config().runner.machine)
        for event in events:
            principal, ref, refusal = _actor(gateway, ws, event, machine)
            _barrier("sweep_after_requester", conn)
            try:
                plan = runner_bridge.prepare(
                    gateway,
                    ws,
                    board_row=board_row,
                    card_row=cards[event["task_id"]],
                    version=version,
                    principal=principal,
                    requested_by=ref or "unknown",
                    person=False,
                    split_start=on,
                )
            except (runner_bridge.StartRefused, schema.ConfigError) as exc:
                # A start that cannot even be planned is a coded miss for this event,
                # not an infrastructure error: leaving the cursor would block the board.
                code = exc.code if isinstance(exc, runner_bridge.StartRefused) else "config_invalid"
                trigger = runner_bridge.TriggerRef(event["id"], cursor, gen, ref)
                if not runner_bridge.trigger_miss(
                    js, board, event["task_id"], trigger, code, now=now
                ):
                    results.append({"event": event["id"], "dropped": True})
                    break
                cursor = event["id"]
                results.append(
                    {"event": event["id"], "card": event["task_id"], "job": None, "miss": code}
                )
                continue
            _barrier("sweep_before_start", conn)
            outcome = runner_bridge.fenced_start(
                js,
                plan,
                path="trigger",
                trigger=runner_bridge.TriggerRef(event["id"], cursor, gen, ref),
                actor_refusal=refusal,
                now=now,
            )
            if outcome.dropped:
                results.append({"event": event["id"], "dropped": True})
                break
            cursor = event["id"]  # re-recorded from the advance
            results.append(
                {
                    "event": event["id"],
                    "card": event["task_id"],
                    "job": outcome.job["id"] if outcome.job else None,
                    "miss": outcome.refusal.code if outcome.refusal else None,
                }
            )
    if on:
        results.extend(_reconcile(js, st, board, now or jobs._now()))
    return results


def _reconcile(js, st, board, now):
    from commonplace.projects import orchestration, split

    gateway, ws = js.gateway, js.workspace_id
    with orchestration.board_read(gateway.workspace_dir(ws)) as boards:
        b = boards.execute("SELECT * FROM boards WHERE slug=? AND archived=0", (board,)).fetchone()
        if b is None:
            return []
        roots = split.eligible(boards, b, b["coordinator"])
        cards = {r["id"]: dict(task_row(boards, r["id"])) for r in roots}
        b = dict(b)
    results = []
    with st.connection() as conn:
        pair = orch_store.cursor_in(conn, board)
        if pair is None or pair[0] is None:
            return results
        version = orch_store.resolve_in(conn, board)
        machine = registry.get_machine(gateway.db, ws, version.config().runner.machine)
        for row in roots:
            if not split.retry_due(conn, row, now):
                continue
            n = len(split.attempts(conn, row["id"])) + 1
            event = {"stamp_user": row["stamp_user"], "stamp_connection": row["stamp_connection"]}
            principal, ref, refusal = _actor(gateway, ws, event, machine)
            try:
                plan = runner_bridge.prepare(
                    gateway,
                    ws,
                    board_row=b,
                    card_row=cards[row["id"]],
                    version=version,
                    principal=principal,
                    requested_by=ref or "unknown",
                    person=False,
                    split_start=True,
                )
            except (runner_bridge.StartRefused, schema.ConfigError) as exc:
                # Counted like any refused attempt: three of them end reconciliation.
                code = exc.code if isinstance(exc, runner_bridge.StartRefused) else "config_invalid"
                trigger = runner_bridge.TriggerRef(0, pair[0], pair[1], ref, retry=n)
                if runner_bridge.trigger_miss(js, board, row["id"], trigger, code, now=now):
                    results.append({"card": row["id"], "retry": n, "job": None, "miss": code})
                continue
            outcome = runner_bridge.fenced_start(
                js,
                plan,
                path="trigger",
                request_key=f"split-retry:{row['id']}:{n}",
                trigger=runner_bridge.TriggerRef(0, pair[0], pair[1], ref, retry=n),
                actor_refusal=refusal,
                now=now,
            )
            results.append(
                {
                    "card": row["id"],
                    "retry": n,
                    "job": outcome.job["id"] if outcome.job else None,
                    "miss": outcome.refusal.code if outcome.refusal else None,
                    "dropped": outcome.dropped,
                }
            )
    return results


def run(js, *, now=None) -> dict:
    """One trigger sweep of the workspace of ``js`` (a ``jobs.JobStore``).

    Skips everything while a restore marker is pending or the restore flag is set.
    Returns ``{board: [results]}``; an infrastructure error on one board is logged and
    leaves that board's cursor. Takes no ``runner.sqlite3`` write lock before
    ``fenced_start`` except the arming ``UPDATE`` (see the module doc)."""
    with js.connection() as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_configs'"
        ).fetchone():
            return {}
        if not orch_store.configured_boards_in(conn):
            return {}
    gateway, ws = js.gateway, js.workspace_id
    directory = gateway.workspace_dir(ws)
    if orch_store.marker_pending(directory):  # an explicit store open converts it
        return {}
    st = orch_store.open_existing(gateway, ws)  # no statement on open
    with st.connection() as conn:
        if orch_store.restore_state_in(conn).flagged:
            return {}
        boards = orch_store.configured_boards_in(conn)
    report: dict = {}
    for board in boards:
        try:
            _arm(st, directory, board)
            report[board] = _sweep_board(js, st, board, now)
        except AssertionError:
            raise
        except Exception as exc:
            log_failure(logger, "orchestrator trigger sweep of a board failed", exc)
            report[board] = None
            _record_failure(js, st, board, exc, now or jobs._now())
        else:
            _record_success(st, board)
            _no_progress(js, st, directory, board, now or jobs._now())
    return report


def _no_progress(js, st, directory, board: str, now: str) -> None:
    """The board's no-progress wake (``stall.evaluate``), after a clean sweep."""
    from commonplace.projects import limits

    from . import stall

    try:
        with BoardStore(directory).connection() as boards:
            row = boards.execute(
                "SELECT * FROM boards WHERE slug=? AND archived=0", (board,)
            ).fetchone()
            if row is None:
                return
            row = dict(row)
            limit = limits.get(boards, row["id"])["max_agents"]
        with st.connection() as conn:
            config_cap = orch_store.resolve_in(conn, board).config().max_agents
    except Exception as exc:
        log_failure(logger, "orchestrator no-progress check could not read the board", exc)
        return
    caps = [c for c in (config_cap, limit) if c is not None]
    stall.evaluate(js.gateway, js.workspace_id, st, row, min(caps) if caps else None, now)


# --------------------------------------------------------------- sweep health

HEALTH_DDL = (
    "CREATE TABLE IF NOT EXISTS orch_sweep_health (board TEXT PRIMARY KEY, "
    "failures INTEGER NOT NULL, first_at TEXT NOT NULL, last_at TEXT NOT NULL, "
    "error TEXT NOT NULL, alerted INTEGER NOT NULL DEFAULT 0)"
)
ALERT_AFTER = 2  # consecutive failed sweeps of one board before the owner is alerted


def _health_table(conn) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_sweep_health'"
        ).fetchone()
    )


def _record_success(st, board: str) -> None:
    """A clean sweep ends the board's failure streak (no write while healthy)."""
    try:
        with st.connection() as conn:
            if (
                not _health_table(conn)
                or not conn.execute(
                    "SELECT 1 FROM orch_sweep_health WHERE board=?", (board,)
                ).fetchone()
            ):
                return
        with st.transaction() as conn:
            conn.execute("DELETE FROM orch_sweep_health WHERE board=?", (board,))
    except Exception as exc:
        log_failure(logger, "orchestrator sweep health could not be cleared", exc)


def _record_failure(js, st, board: str, exc: Exception, now: str) -> None:
    """Count consecutive failed sweeps of a board. The second one in a row emits one
    ``orch.trigger.failing`` event (app and webhooks) and the owner's Needs me lists the
    board until a sweep succeeds: a sweep that fails on every poll is never just a log line.
    Only the exception's type is stored, never its text."""
    error = type(exc).__name__
    try:
        with st.transaction() as conn:
            conn.execute(HEALTH_DDL)
            conn.execute(
                "INSERT INTO orch_sweep_health(board,failures,first_at,last_at,error) "
                "VALUES (?,1,?,?,?) ON CONFLICT(board) DO UPDATE SET "
                "failures=failures+1,last_at=excluded.last_at,error=excluded.error",
                (board, now, now, error),
            )
            row = conn.execute("SELECT * FROM orch_sweep_health WHERE board=?", (board,)).fetchone()
            alert = row["failures"] >= ALERT_AFTER and not row["alerted"]
            if alert:
                conn.execute("UPDATE orch_sweep_health SET alerted=1 WHERE board=?", (board,))
            failures, since = row["failures"], row["first_at"]
    except Exception as health_exc:
        log_failure(logger, "orchestrator sweep health could not be recorded", health_exc)
        return
    if alert:
        from commonplace import events

        try:
            events.emit(
                js.gateway.db,
                js.workspace_id,
                "orch.trigger.failing",
                subject=f"board:{board}",
                actor="system",
                data={"board": board, "failures": failures, "since": since, "error": error},
            )
        except Exception as emit_exc:
            log_failure(logger, "orchestrator sweep alert could not be emitted", emit_exc)


def failing(gateway, workspace_id: str) -> list[dict]:
    """Boards whose trigger sweep keeps failing: ``[{board, failures, since, error,
    message}]`` for the owner's Needs me. Empty when the store or table does not exist."""
    directory = gateway.workspace_dir(workspace_id)
    from pathlib import Path

    if not (Path(directory) / "runner.sqlite3").exists():
        return []
    try:
        st = orch_store.open_existing(gateway, workspace_id)
        with st.connection() as conn:
            if not _health_table(conn):
                return []
            rows = conn.execute(
                "SELECT * FROM orch_sweep_health WHERE failures>=? ORDER BY board",
                (ALERT_AFTER,),
            ).fetchall()
    except Exception as exc:
        log_failure(logger, "orchestrator sweep health unavailable", exc)
        return []
    return [
        {
            "board": row["board"],
            "failures": row["failures"],
            "since": row["first_at"],
            "error": row["error"],
            "message": f"The orchestrator cannot start work on {row['board']}: its trigger "
            f"sweep failed {row['failures']} times in a row since {row['first_at']} "
            f"({row['error']}). New and released cards will not start until it is fixed.",
        }
        for row in rows
    ]
