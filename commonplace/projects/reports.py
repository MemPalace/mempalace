"""Transactional report capture, bounded observation and receipt-backed publication.

Source and control phases never overlap. Reports carry routing metadata only.
Receipts deliberately outlive source acknowledgements: a concurrent publisher may
still hold an old source row, so retaining its receipt is the safe retry boundary.
"""

import json
import logging
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from commonplace import events
from commonplace.http_util import log_failure
from commonplace.projects import orchestration
from mempalace.sqlite_crypto import connect, row_factory

logger = logging.getLogger(__name__)

BATCH = 100
CONTROL_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS project_report_activation ("
    "workspace_id TEXT PRIMARY KEY,generation INTEGER NOT NULL,active INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS project_report_bindings (workspace_id TEXT NOT NULL,"
    "board TEXT NOT NULL,connection_id TEXT,binding_generation INTEGER NOT NULL,"
    "active INTEGER NOT NULL,PRIMARY KEY(workspace_id,board))",
    "CREATE TABLE IF NOT EXISTS project_report_receipts (workspace_id TEXT NOT NULL,"
    "report_id TEXT NOT NULL,event_seq INTEGER NOT NULL,PRIMARY KEY(workspace_id,report_id))",
    "CREATE TABLE IF NOT EXISTS project_report_dispatch_retry (event_seq INTEGER PRIMARY KEY,"
    "workspace_id TEXT NOT NULL,envelope TEXT NOT NULL,consecutive_failures INTEGER NOT NULL "
    "DEFAULT 0,next_at REAL NOT NULL DEFAULT 0)",
)
SOURCE_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS project_report_outbox (report_id TEXT PRIMARY KEY,"
    "board TEXT NOT NULL,number INTEGER NOT NULL,reason TEXT NOT NULL,generation INTEGER NOT NULL,"
    "coordinator TEXT NOT NULL,connection_id TEXT NOT NULL,binding_generation INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS project_report_episodes (number INTEGER PRIMARY KEY,"
    "generation INTEGER NOT NULL,observed_key TEXT NOT NULL,reported_key TEXT)",
    "CREATE TABLE IF NOT EXISTS project_report_scan (id INTEGER PRIMARY KEY CHECK(id=1),"
    "cursor INTEGER NOT NULL DEFAULT 0)",
    "INSERT OR IGNORE INTO project_report_scan(id,cursor) VALUES(1,0)",
)
WATCHED = ("status", "assignee", "ask", "ask_to", "return_to", "claim_until", "heartbeat_at")


def ensure_control(db):
    with db._lock:
        for statement in CONTROL_SCHEMA:
            db._conn.execute(statement)


def ensure_source(conn):
    """Initialization only; scanner reads never construct or migrate BoardStore."""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "report_reset_rev" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN report_reset_rev INTEGER NOT NULL DEFAULT 0")
    for statement in SOURCE_SCHEMA:
        conn.execute(statement)
    # BoardStore adds legacy task columns before calling us again. SQLite
    # accepts missing trigger references but then breaks later task updates.
    if not set(WATCHED).issubset(columns):
        return
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS project_report_reset AFTER UPDATE OF "
        + ",".join(WATCHED)
        + " ON tasks WHEN "
        + " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in WATCHED)
        + " BEGIN UPDATE tasks SET report_reset_rev=OLD.report_reset_rev+1 WHERE id=NEW.id; END"
    )


def activation_in(db, ws, on):
    db._conn.execute(
        "INSERT INTO project_report_activation VALUES(?,1,?) ON CONFLICT(workspace_id) "
        "DO UPDATE SET generation=generation+1,active=excluded.active",
        (ws, int(on)),
    )


def bind_in(db, ws, board, cid):
    """Owner setup establishes a fresh internal binding even while switched off."""
    db._conn.execute(
        "INSERT INTO project_report_bindings VALUES(?,?,?,1,1) "
        "ON CONFLICT(workspace_id,board) DO UPDATE SET connection_id=excluded.connection_id,"
        "binding_generation=binding_generation+1,active=1",
        (ws, board, cid),
    )


def invalidate_in(db, ws, cid):
    db._conn.execute(
        "UPDATE project_report_bindings SET active=0,binding_generation=binding_generation+1 "
        "WHERE workspace_id=? AND connection_id=? AND active=1",
        (ws, cid),
    )


@dataclass(frozen=True)
class Snapshot:
    enabled: bool
    generation: int
    boards: dict


def snapshot(gateway, ws):
    """Close source map before merging control facts under one control lock."""
    try:
        with orchestration.board_read(gateway.supervisor.workspace_dir(ws)) as conn:
            boards = {
                r["slug"]: r["coordinator"]
                for r in conn.execute("SELECT slug,coordinator FROM boards")
            }
        db = gateway.db
        with db._lock, db._tx():
            on = orchestration.enabled(db, ws)
            activation = db._conn.execute(
                "SELECT * FROM project_report_activation WHERE workspace_id=?", (ws,)
            ).fetchone()
            bindings = {
                r["board"]: dict(r)
                for r in db._conn.execute(
                    "SELECT b.* FROM project_report_bindings b JOIN project_orchestrators s "
                    "ON s.workspace_id=b.workspace_id AND s.board=b.board AND s.role='main' "
                    "AND s.connection_id=b.connection_id WHERE b.workspace_id=?",
                    (ws,),
                )
            }
            result = {}
            for board, actor in boards.items():
                binding = bindings.get(board)
                if binding and binding["active"]:
                    principal = db.authenticate_connection(binding["connection_id"])
                    if principal is None or principal.actor != actor:
                        binding = None
                else:
                    binding = None
                result[board] = (actor, binding)
            return Snapshot(on, activation["generation"] if activation else 0, result)
    except Exception:
        raise orchestration.SettingsUnavailable() from None


def guard(snapshot_value, conn, row, new_status, existing_boards):
    """Reject uncertainty before any relevant status update or lesson write."""
    if row["status"] == "done" or new_status != "done":
        return
    if isinstance(snapshot_value, orchestration.SettingsUnavailable):
        raise orchestration.SettingsUnavailable() from None
    if not snapshot_value.enabled:
        return
    board = conn.execute("SELECT * FROM boards WHERE id=?", (row["board_id"],)).fetchone()
    entry = snapshot_value.boards.get(board["slug"])
    if entry is None:
        if board["id"] in existing_boards:
            raise orchestration.SettingsUnavailable()
        return  # Board inserted by this transaction: history only.
    if entry[0] != board["coordinator"]:
        raise orchestration.SettingsUnavailable()


def capture(conn, row, snapshot_value, reason):
    if isinstance(snapshot_value, orchestration.SettingsUnavailable) or not snapshot_value.enabled:
        return
    entry = snapshot_value.boards.get(row["board"])
    if not entry or not entry[0] or not entry[1]:
        return
    actor, binding = entry
    conn.execute(
        "INSERT INTO project_report_outbox VALUES(?,?,?,?,?,?,?,?)",
        (
            "rpt_" + secrets.token_hex(16),
            row["board"],
            row["id"],
            reason,
            snapshot_value.generation,
            actor,
            binding["connection_id"],
            binding["binding_generation"],
        ),
    )


@contextmanager
def source_write(gateway, ws):
    """Write known initialized source state without running migrations."""
    path = Path(gateway.supervisor.workspace_dir(ws)) / "boards.sqlite3"
    conn = connect(
        path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0.2, isolation_level=None
    )
    conn.row_factory = row_factory
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def episode_key(row):
    values = [row["report_reset_rev"], *(row[c] for c in WATCHED)]
    if row["claim_until"] is None and row["heartbeat_at"] is None:
        values.append(row["updated_at"])
    return json.dumps(values, separators=(",", ":"))


def current_generation(db, ws, snap):
    with db._lock, db._tx():
        row = db._conn.execute(
            "SELECT * FROM project_report_activation WHERE workspace_id=?", (ws,)
        ).fetchone()
        return bool(
            orchestration.enabled(db, ws)
            and row
            and row["active"]
            and row["generation"] == snap.generation
        )


def observe(gateway, ws):
    from commonplace.boards.display import stale_before
    from commonplace.boards.service import _stuck
    from commonplace.webhooks import time

    snap = snapshot(gateway, ws)
    if not snap.enabled:
        return
    # Persisted all-ID cursor gives fresh, waiting and stale cards equal access.
    with orchestration.board_read(gateway.supervisor.workspace_dir(ws)) as conn:
        cursor = conn.execute("SELECT cursor FROM project_report_scan WHERE id=1").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM tasks WHERE id>? ORDER BY id LIMIT ?", (cursor, BATCH)
        ).fetchall()
    if not current_generation(gateway.db, ws, snap):
        return
    cutoff = stale_before(datetime.fromtimestamp(time.time(), timezone.utc))
    with source_write(gateway, ws) as conn:
        for candidate in rows:
            row = conn.execute(
                "SELECT t.*,b.slug AS board,b.coordinator FROM tasks t JOIN boards b ON b.id=t.board_id "
                "WHERE t.id=?",
                (candidate["id"],),
            ).fetchone()
            if row is None:
                continue
            entry = snap.boards.get(row["board"])
            if entry is None or entry[0] != row["coordinator"]:
                # No invented destination and no observation advancement.
                continue
            key = episode_key(row)
            stale = _stuck(dict(row), cutoff)
            old = conn.execute(
                "SELECT * FROM project_report_episodes WHERE number=?", (row["id"],)
            ).fetchone()
            baseline = old is None or old["generation"] != snap.generation
            reported = (key if stale else None) if baseline else old["reported_key"]
            if not baseline and stale and reported != key:
                capture(conn, row, snap, "stuck")
                reported = key
            conn.execute(
                "INSERT OR REPLACE INTO project_report_episodes VALUES(?,?,?,?)",
                (row["id"], snap.generation, key, reported),
            )
        conn.execute(
            "UPDATE project_report_scan SET cursor=? WHERE id=1",
            (rows[-1]["id"] if len(rows) == BATCH else 0,),
        )
        conn.execute(
            "DELETE FROM project_report_episodes WHERE number IN (SELECT e.number "
            "FROM project_report_episodes e LEFT JOIN tasks t ON t.id=e.number "
            "WHERE t.id IS NULL ORDER BY e.number LIMIT ?)",
            (BATCH,),
        )


def ack(gateway, ws, report_id):
    with source_write(gateway, ws) as conn:
        conn.execute("DELETE FROM project_report_outbox WHERE report_id=?", (report_id,))


def publish(gateway, ws):
    from commonplace.projects import report_dispatch

    with orchestration.board_read(gateway.supervisor.workspace_dir(ws)) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM project_report_outbox ORDER BY rowid LIMIT ?", (BATCH,)
            )
        ]
    for row in rows:
        db = gateway.db
        event = None
        with db._lock, db._tx():
            receipt = db._conn.execute(
                "SELECT event_seq FROM project_report_receipts "
                "WHERE workspace_id=? AND report_id=?",
                (ws, row["report_id"]),
            ).fetchone()
            if receipt is None and report_dispatch.control_authority(db, ws, row) is not None:
                event = events.record(
                    db, ws, "project.report", subject=f"#{row['number']}", actor="system", data=row
                )
                db._conn.execute(
                    "INSERT INTO project_report_receipts VALUES(?,?,?)",
                    (ws, row["report_id"], event["seq"]),
                )
        if event is not None:
            events.notify(event)
        ack(gateway, ws, row["report_id"])


class ReportPump:
    """Recover existing sources on the dispatcher's independent monotonic deadlines."""

    def __init__(self, gateway):
        self.gateway = gateway
        self._initialized = set()

    def pass_once(self):
        # A broken workspace must never prevent another workspace's recovery.
        for ws in self.gateway.db.workspaces():
            wid = ws["id"]
            try:
                path = (
                    Path(self.gateway.supervisor.workspace_dir(wid)) / "boards.sqlite3"
                ).resolve()
                if not path.is_file():
                    continue
                stat = path.stat()
                # The creator's connect can expose the file before its WAL and
                # schema setup. Leave that initialization entirely to it.
                if stat.st_size == 0:
                    continue
                with orchestration.board_read(path.parent) as conn:
                    tables = {
                        row[0]
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name IN ('boards','tasks')"
                        )
                    }
                    if tables != {"boards", "tasks"}:
                        continue
                    schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
                key = (path, stat.st_ino, schema_version)
                if key not in self._initialized:
                    with source_write(self.gateway, wid) as conn:
                        ensure_source(conn)
                        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
                    self._initialized = {k for k in self._initialized if k[0] != path}
                    self._initialized.add((path, stat.st_ino, schema_version))
                observe(self.gateway, wid)
                publish(self.gateway, wid)
            except Exception as exc:
                log_failure(logger, "project report pass failed", exc)
                continue


def install(gateway):
    ensure_control(gateway.db)
    # Installation registers the pump only. The first dispatcher scan recovers
    # existing sources without creating directories for unopened workspaces.
    gateway.project_reports = ReportPump(gateway)
