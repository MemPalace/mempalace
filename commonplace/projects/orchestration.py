"""Workspace opt-in and shared, transactional project admission accounting."""

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from commonplace import control_db
from commonplace.extensions import ToolError
from commonplace.projects import limits

DEFAULT_MAX_AGENTS = 3
SCHEMA = """CREATE TABLE IF NOT EXISTS orchestration_settings (
 workspace_id TEXT PRIMARY KEY,
 enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
 updated_by TEXT NOT NULL,
 updated_at TEXT NOT NULL
)"""


class AdmissionRefused(ToolError):
    status = 409

    def __init__(self, code, detail=""):
        self.code = code
        super().__init__(f"no: {code}" + (f" {detail}" if detail else ""))


class SettingsUnavailable(AdmissionRefused):
    status = 503

    def __init__(self):
        super().__init__("settings_unavailable")


def ensure_schema(db):
    with db._lock:
        db._conn.execute(SCHEMA)
    from commonplace.projects import reports

    reports.ensure_control(db)


def enabled(db, ws):
    if not db._lock.acquire(timeout=1):
        raise SettingsUnavailable()
    try:
        if not db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='orchestration_settings'"
        ).fetchone():
            return False
        row = db._conn.execute(
            "SELECT enabled FROM orchestration_settings WHERE workspace_id=?", (ws,)
        ).fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        raise SettingsUnavailable() from None
    finally:
        db._lock.release()


def set_enabled(db, principal, value):
    if not principal.person or principal.role != "owner":
        raise PermissionError("Only workspace owners can configure orchestration")
    if type(value) is not bool:
        raise ValueError("enabled must be a boolean")
    with db._lock, db._tx():
        db._conn.execute(SCHEMA)
        if not value:
            from commonplace.projects.connect import demote_workspace_in

            demote_workspace_in(db, principal)
        db._conn.execute(
            "INSERT OR REPLACE INTO orchestration_settings VALUES (?,?,?,?)",
            (
                principal.workspace_id,
                int(value),
                principal.user_id,
                control_db._iso(control_db._now()),
            ),
        )
        from commonplace.projects import reports

        reports.activation_in(db, principal.workspace_id, value)
        db.audit(
            action="workspace.orchestration",
            decision="allow",
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
            auth=principal.auth,
        )
    return {"enabled": value}


def effective_cap(board_conn, board_id, config_cap=None, *, on):
    row = limits.get(board_conn, board_id)["max_agents"]
    if config_cap is not None:
        return config_cap if row is None else min(config_cap, row)
    return row if row is not None else DEFAULT_MAX_AGENTS if on else None


@dataclass(frozen=True)
class Usage:
    slots: frozenset[tuple[str, str | int]]
    cap: int | None

    @property
    def used(self):
        return len(self.slots)


def usage(board_conn, runner_conn, *, board_id, board_slug, workers, now, lease=False):
    from commonplace.runner.jobs import _after

    cards = {
        row["id"]: row
        for row in board_conn.execute(
            "SELECT id,status,assignee FROM tasks WHERE board_id=?", (board_id,)
        )
    }
    slots = set()
    for row in cards.values():
        actor = row["assignee"]
        if row["status"] == "doing" and actor and actor.startswith("conn:"):
            slots.add(("card", row["id"]) if actor in workers else ("agent", actor))
    states = (
        "'leased','starting','uncertain'" if lease else "'queued','leased','starting','uncertain'"
    )
    for job in runner_conn.execute(
        "SELECT task_number,state FROM runner_jobs WHERE board_slug=? AND "
        f"(state IN ({states}) OR (state='started' AND started_at>?))",
        (board_slug, _after(now, -3600)),
    ):
        card = cards.get(job["task_number"])
        if job["state"] == "started" and (card is None or card["status"] not in ("todo", "doing")):
            continue
        if (
            card
            and card["status"] == "doing"
            and card["assignee"]
            and card["assignee"].startswith("conn:")
            and card["assignee"] not in workers
        ):
            continue
        slots.add(("card", job["task_number"]))
    config_cap = None
    if runner_conn.execute("SELECT 1 FROM sqlite_master WHERE name='orch_configs'").fetchone():
        from commonplace.orch import store

        version = store.accepted_in(runner_conn, board_slug)
        if version is not None:
            config = version.config()
            if config.runner is not None:
                config_cap = config.max_agents
    return Usage(frozenset(slots), effective_cap(board_conn, board_id, config_cap, on=True))


def require_lock_timeout(exc):
    """Let callers map lock timeouts, while preserving other database errors."""
    if not any(word in str(exc).lower() for word in ("locked", "busy")):
        raise exc


@contextmanager
def admission(gateway, ws, *, store=None):
    from commonplace.runner.jobs import open_store

    try:
        with (store or open_store(gateway, ws)).transaction() as conn:
            yield conn
    except sqlite3.OperationalError as exc:
        require_lock_timeout(exc)
        raise AdmissionRefused("busy") from None


def admit(used, actor, card_id):
    if ("agent", actor) in used.slots or ("card", card_id) in used.slots:
        return
    if used.cap is not None and used.used >= used.cap:
        raise AdmissionRefused("project_limit", f"{used.used}/{used.cap}")


def requester_refusal(gateway, principal, board_row):
    if principal.person:
        return None
    from commonplace.projects.seats import active_connection

    row = active_connection(
        gateway.db, principal.workspace_id, (board_row["coordinator"] or "").removeprefix("conn:")
    )
    if principal.auth != "pat" or row is None or row["id"] != principal.connection_id:
        return "not_requested_by_orchestrator"
    return None


def busy_agent(board_conn, board_id, assignee):
    return bool(
        assignee
        and assignee.startswith("conn:")
        and board_conn.execute(
            "SELECT 1 FROM tasks WHERE board_id=? AND status='doing' AND assignee=? LIMIT 1",
            (board_id, assignee),
        ).fetchone()
    )


def workers(db, ws):
    with db._lock:
        if not db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='runner_machines'"
        ).fetchone():
            return frozenset()
        rows = db._conn.execute(
            "SELECT c.name FROM runner_machines m JOIN connections c ON c.id=m.worker_connection_id WHERE m.workspace_id=?",
            (ws,),
        ).fetchall()
    return frozenset("conn:" + row[0] for row in rows)


def seat_connection(db, ws, cid):
    with db._lock:
        if not db._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='project_orchestrators'"
        ).fetchone():
            return False
        return bool(
            db._conn.execute(
                "SELECT 1 FROM project_orchestrators WHERE workspace_id=? AND connection_id=?",
                (ws, cid),
            ).fetchone()
        )


def check_start(gateway, ws, runner, slug, card_id, workers, now, *, lease=False):
    """Read committed cards under the caller's runner admission transaction."""
    with board_read(gateway.workspace_dir(ws)) as boards:
        board = boards.execute("SELECT id FROM boards WHERE slug=?", (slug,)).fetchone()
        if board is None:
            raise AdmissionRefused("board_not_found")
        used = usage(
            boards,
            runner,
            board_id=board["id"],
            board_slug=slug,
            workers=workers,
            now=now,
            lease=lease,
        )
        admit(used, None, card_id)


@contextmanager
def board_read(workspace_dir):
    """A read connection without BoardStore's schema migration/write lock."""
    from pathlib import Path

    from mempalace.sqlite_crypto import connect, row_factory

    conn = connect(
        (Path(workspace_dir) / "boards.sqlite3").resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=10,
        isolation_level=None,
    )
    conn.row_factory = row_factory
    try:
        yield conn
    finally:
        conn.close()
