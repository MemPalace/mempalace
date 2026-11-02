"""Check settings, store identity guards and thread-local internal calls.

All probes use SQLite modes that cannot create a missing store. Store bindings
live outside the workspace generation, while copied stores retain their ids.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from time import monotonic

from commonplace import events
from commonplace.extensions import ToolError
from commonplace.http_util import log_failure
from mempalace.sqlite_crypto import connect, driver, encryption_settings, row_factory

from .store import bump, migrate, now

logger = logging.getLogger(__name__)
STORE_LOST = "rules_seq store_lost: a rules or boards store was replaced; the owner must confirm it"
LOST_ERROR = "the rules store was replaced; the owner must confirm it"
_internal = ContextVar("commonplace_internal_call", default=False)
_deadline = ContextVar("commonplace_rules_deadline", default=None)


def remaining(timeout):
    end = _deadline.get()
    if end is None:
        return timeout
    left = end - monotonic()
    if left <= 0:
        raise TimeoutError("rules deadline expired")
    return min(timeout, left)


@contextmanager
def deadline(timeout):
    """Nested readers may shorten a caller's deadline, never extend it."""
    end = monotonic() + timeout
    prior = _deadline.get()
    token = _deadline.set(min(prior, end) if prior is not None else end)
    try:
        remaining(timeout)
        yield
        remaining(timeout)
    finally:
        _deadline.reset(token)


class _DeadlineMixin:
    """Refresh lock waits before each statement; interrupt long SQLite work."""

    def _refresh(self):
        milliseconds = int(remaining(getattr(self, "wait_limit", 0.1)) * 1000)
        super().execute(f"PRAGMA busy_timeout={milliseconds}")

    def execute(self, *args, **kwargs):
        self._refresh()
        return super().execute(*args, **kwargs)

    def commit(self):
        self._refresh()
        return super().commit()

    def rollback(self):
        # Cleanup must still run after expiry, including an interrupted writer.
        self.set_progress_handler(None, 0)
        return super().rollback()


class _DeadlineConnection(_DeadlineMixin, sqlite3.Connection):
    pass


@contextmanager
def internal_call():
    token = _internal.set(True)
    try:
        yield
    finally:
        _internal.reset(token)


def is_internal_call():
    return _internal.get()


class StoreLost(ToolError):
    def __init__(self):
        super().__init__("no: " + LOST_ERROR)


@contextmanager
def connection(path, *, write=False, timeout=0.1):
    bounded = _deadline.get() is not None
    factory = _DeadlineConnection if bounded else sqlite3.Connection
    if encryption_settings(Path(path)) is not None:

        class CipherDeadlineConnection(_DeadlineMixin, driver().Connection):
            pass

        factory = CipherDeadlineConnection if bounded else driver().Connection
    conn = connect(
        Path(path).resolve().as_uri() + ("?mode=rw" if write else "?mode=ro"),
        uri=True,
        timeout=max(0, remaining(timeout)),
        isolation_level=None,
        factory=factory,
    )
    conn.row_factory = row_factory
    if bounded:
        conn.wait_limit = timeout
        end = _deadline.get()
        conn.set_progress_handler(lambda: monotonic() >= end, 1000)
    try:
        yield conn
    finally:
        conn.close()


def meta(path, key, *, timeout=0.1):
    if not Path(path).exists():
        return None
    with connection(path, timeout=timeout) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone():
            return None
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row is not None else None


def guard(db, workspace_id, directory, *, bind=True, emit=True, timeout=0.1):
    """Compare both files before an opener can recreate either of them."""
    directory = Path(directory)
    ids = [
        meta(directory / f"{name}.sqlite3", "store_id", timeout=timeout)
        for name in ("rules", "boards")
    ]
    if _deadline.get() is None:
        bound = db.rule_stores(workspace_id) or {}
    else:
        # The shared control connection has a long lock wait and a Python lock.
        # Injection uses an independent read-only connection under its deadline.
        with connection(db.path, timeout=timeout) as conn:
            row = conn.execute(
                "SELECT * FROM rule_stores WHERE workspace_id=?", (workspace_id,)
            ).fetchone()
            bound = dict(row) if row is not None else {}
    if bind and any(
        value is not None and bound.get(key) is None
        for key, value in zip(("rules_store_id", "boards_store_id"), ids)
    ):
        bound = db.bind_rule_stores(workspace_id, *ids)
    lost = any(
        bound.get(key) is not None and bound[key] != value
        for key, value in zip(("rules_store_id", "boards_store_id"), ids)
    )
    if lost:
        if emit:
            try:
                events.emit(db, workspace_id, "rule.store_lost", data={})
            except Exception as exc:
                log_failure(logger, "rules store loss event failed", exc)
        raise StoreLost()
    return "ready" if (directory / "rules.sqlite3").exists() else "empty"


def mode(gateway, principal):
    if getattr(gateway, "rules_check_off", False):
        return "off"
    try:
        if _deadline.get() is None:
            values = gateway.db.rule_checks(principal.workspace_id)
        else:
            with connection(gateway.db.path) as conn:
                values = dict(
                    conn.execute(
                        "SELECT agent,mode FROM rule_checks WHERE workspace_id=?",
                        (principal.workspace_id,),
                    )
                )
        return values.get(principal.actor, values.get("*", "off"))
    except Exception:
        return "unknown"


def visibility(directory, *, timeout=0.1):
    return meta(Path(directory) / "boards.sqlite3", "visibility", timeout=timeout) or 0


def reconcile(db, workspace_id, directory, *, timeout=0.2, bind=True, emit=True):
    """One short, independent transaction catches changes and restored boards."""
    state = guard(db, workspace_id, directory, bind=bind, emit=emit, timeout=min(0.1, timeout))
    if state == "empty":
        return
    path = Path(directory) / "rules.sqlite3"
    current = visibility(directory, timeout=min(0.1, timeout))
    with connection(path, timeout=min(0.1, timeout)) as conn:
        seen = conn.execute("SELECT value FROM meta WHERE key='boards_visibility_seen'").fetchone()
        migrated = (
            conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('agent_seq','agent_served')"
            ).fetchone()[0]
            == 2
        )
        if migrated and seen is not None and seen[0] == current:
            return
    with connection(path, write=True, timeout=timeout) as conn:
        # Migration and the bump share one short write transaction.
        guard(db, workspace_id, directory, bind=bind, emit=emit, timeout=min(0.1, timeout))
        conn.execute("BEGIN IMMEDIATE")
        try:
            migrate(conn, current)
            current = visibility(directory, timeout=min(0.1, timeout))
            seen = conn.execute(
                "SELECT value FROM meta WHERE key='boards_visibility_seen'"
            ).fetchone()[0]
            if seen != current:
                if conn.execute(
                    "SELECT 1 FROM rules WHERE scope IN ('board','repo') LIMIT 1"
                ).fetchone():
                    bump(conn, "*")
                    conn.execute("UPDATE meta SET value=value+1 WHERE key='seq'")
                conn.execute(
                    "UPDATE meta SET value=? WHERE key='boards_visibility_seen'", (current,)
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def record_served(ctx, rules_seq, state, *, prior=None):
    directory = ctx.workspace_dir()
    guard(ctx.db, ctx.principal.workspace_id, directory)
    if prior is not None and prior["state"] == state and prior["rules_seq"] == rules_seq:
        return False
    with connection(Path(directory) / "rules.sqlite3", write=True, timeout=0.2) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            migrate(conn, visibility(directory))
            cursor = conn.execute(
                "INSERT INTO agent_served VALUES (?,?,?,?) ON CONFLICT(agent) DO UPDATE SET "
                "state=excluded.state,rules_seq=excluded.rules_seq,at=excluded.at "
                "WHERE agent_served.state!=excluded.state OR agent_served.rules_seq IS NOT excluded.rules_seq",
                (ctx.principal.actor, state, rules_seq, now()),
            )
            conn.commit()
            return cursor.rowcount != 0
        except BaseException:
            conn.rollback()
            raise


def migrate_workspace(db, workspace_id, directory, *, accept=False):
    """Deploy/owner migration opens present files only, and binds after all succeed."""
    directory = Path(directory)
    boards = directory / "boards.sqlite3"
    rules = directory / "rules.sqlite3"
    bound = {} if accept else (db.rule_stores(workspace_id) or {})
    if boards.exists() and bound.get("boards_store_id") is None:
        from commonplace.boards.store import BoardStore

        BoardStore(directory, create=False)
    if rules.exists() and bound.get("rules_store_id") is None:
        with connection(rules, write=True, timeout=0.2) as conn:
            # Bound boards need no probe here; effective reconciliation catches
            # any visibility change on the next rules read.
            current = 0 if bound.get("boards_store_id") is not None else visibility(directory)
            migrate(conn, current)
    ids = [
        bound[key] if bound.get(key) is not None else meta(path, "store_id")
        for path, key in ((rules, "rules_store_id"), (boards, "boards_store_id"))
    ]
    db.bind_rule_stores(workspace_id, *ids, accept=accept)
    return {"accepted": True} if accept else None


def deploy(gateway):
    try:
        workspaces = gateway.db.workspaces()
    except Exception as exc:
        log_failure(logger, "rules store deployment enumeration failed", exc)
        return
    for workspace in workspaces:
        try:
            bound = gateway.db.rule_stores(workspace["id"])
            directory = Path(gateway.workspace_dir(workspace["id"]))
            if bound is None or any(
                bound.get(f"{name}_store_id") is None and (directory / f"{name}.sqlite3").exists()
                for name in ("rules", "boards")
            ):
                migrate_workspace(gateway.db, workspace["id"], directory)
        except Exception as exc:
            log_failure(logger, "rules store deployment migration failed", exc)


def configure(gateway):
    gateway.rules_check_off = os.environ.get("COMMONPLACE_RULES_CHECK", "").lower() == "off"
    gateway.rules_inject_off = os.environ.get("COMMONPLACE_RULES_INJECT", "").lower() == "off"
