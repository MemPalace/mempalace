"""Automatic project seats and their current orchestrator views."""

import functools
import logging

from commonplace import control_db, events, redact
from commonplace.boards.service import BoardService
from commonplace.chat import front
from commonplace.extensions import ToolContext
from commonplace.http_util import log_failure
from commonplace.projects.types import SEAT_PREFIX, MainView, Seat

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_orchestrators (
    workspace_id TEXT NOT NULL,
    board TEXT NOT NULL,
    role TEXT NOT NULL,
    name TEXT NOT NULL,
    connection_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, board, role)
);
"""


def ensure_schema(db) -> None:
    with db._lock:
        db._conn.executescript(_SCHEMA)


def ensure_main(db, workspace_id: str, board: str) -> Seat:
    """Create an unheld seat without changing an existing seat or coordinator."""
    now = control_db._iso(control_db._now())
    with db._lock:
        db._conn.execute(
            "INSERT OR IGNORE INTO project_orchestrators "
            "(workspace_id, board, role, name, connection_id, created_at, updated_at) "
            "VALUES (?, ?, 'main', ?, NULL, ?, ?)",
            (workspace_id, board, SEAT_PREFIX + board, now, now),
        )
        row = db._conn.execute(
            "SELECT * FROM project_orchestrators WHERE workspace_id=? AND board=? AND role='main'",
            (workspace_id, board),
        ).fetchone()
    return Seat(**dict(row))


def main(gateway, principal, board: str) -> MainView:
    service = BoardService(ToolContext(principal=principal, gateway=gateway))
    with service.store.connection() as conn:
        slug = service._board(conn, board)["slug"]
    seat = ensure_main(gateway.db, principal.workspace_id, slug)
    view = front.orchestrator(gateway, principal, slug)
    seen = False
    if view is not None and view.actor.startswith("conn:"):
        with gateway.db._lock:
            connection = gateway.db._conn.execute(
                "SELECT last_used_at FROM connections WHERE workspace_id=? AND name=?",
                (principal.workspace_id, view.actor.removeprefix("conn:")),
            ).fetchone()
        seen = connection is not None and connection["last_used_at"] is not None
    return MainView(
        role="main",
        name=view.name if view is not None else redact.redact(seat.name)[0],
        source=(
            "coordinator"
            if view is not None and not (seat.connection_id and view.actor == "conn:" + seat.name)
            else "seat"
        ),
        connected=view is not None and view.connection_id is not None,
        status=view.status if view is not None else None,
        wakeable=view.wakeable if view is not None else False,
        connectable=principal.person and principal.role == "owner",
        seen=seen,
    )


def on_event(gateway, event: dict) -> None:
    try:
        if event.get("kind") != "space_created":
            return
        db = gateway.db
        with db._lock:
            stored = db._conn.execute(
                "SELECT * FROM events WHERE seq=?", (event.get("seq"),)
            ).fetchone()
            if stored is None or events._row(stored) != event:
                return
        ensure_main(db, event["workspace_id"], event["data"]["board"])
    except Exception as exc:
        log_failure(logger, "project seat event failed", exc)


def install_listener(gateway) -> None:
    if getattr(gateway, "projects_listener", None) is not None:
        return
    gateway.projects_listener = functools.partial(on_event, gateway)
    events.add_listener(gateway.projects_listener)
    gateway.db.add_pre_close_callback(lambda: events.remove_listener(gateway.projects_listener))
