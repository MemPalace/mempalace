"""#428: a warm BoardStore read takes no write lock; SQLite failures log their name.

Live failure (prod, 11 Nov 2026 15:50-15:54 CET): 12 ``OperationalError`` in the
trigger sweep, the gateway MCP and the stall check, all raised by ``BoardStore.__init__``
re-running its schema script and ``BEGIN IMMEDIATE`` migration on every construction.
``create=False`` waited only 0.2 s, and ``log_failure`` logged just the type name.
"""

import logging
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from commonplace.boards import store as board_store
from commonplace.boards.service import BoardService
from commonplace.boards.store import BoardStore
from commonplace.http_util import log_failure
from commonplace.orch import durable, stall, store as orch_store, triggers
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests._split_helpers import world
from tests.test_orch_runner import BOARD

T0 = "2030-01-01T00:00:00Z"
HAS_ERRORNAME = sys.version_info >= (3, 11)


def short_busy_timeout(monkeypatch):
    """Clamp every boards connection's busy wait: a read that needs the write lock
    fails fast instead of passing after the holder lets go."""
    real = board_store.connect
    monkeypatch.setattr(
        board_store, "connect", lambda target, **kw: real(target, **{**kw, "timeout": 0.2})
    )


def hold_write_lock(path):
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("BEGIN IMMEDIATE")
    return conn


def test_warm_reads_succeed_while_another_connection_holds_the_write_lock(hub, monkeypatch):
    w = world(hub, on=False, runner=True)
    card = w.card()
    # A PR write sets pr_repo itself, so the next open has no backfill to write.
    BoardService(w.ctx(w.owner)).task_update(card, pr="https://example.test/org/repo/pull/7")
    triggers.arm_cursor(w.gw, w.ws, BOARD)
    directory = w.gw.workspace_dir(w.ws)
    BoardStore(directory)  # warm: this process already migrated the file
    st = orch_store.open_existing(w.gw, w.ws)
    with BoardStore(directory).connection() as conn:
        board_row = dict(conn.execute("SELECT * FROM boards WHERE slug=?", (BOARD,)).fetchone())
    short_busy_timeout(monkeypatch)
    holder = hold_write_lock(str(Path(directory) / "boards.sqlite3"))
    try:
        assert isinstance(triggers._sweep_board(w.js, st, BOARD, T0), list)
        picture = stall.assess(directory, board_row, None, T0, 20, None)
        assert picture["todo"] == [{"number": card, "title": picture["todo"][0]["title"]}]
        assert durable._board(w.ctx(w.owner), BOARD)["slug"] == BOARD
        # The gateway's open_store (with legacy_origin) and a plain read; task_show is a
        # write path (claim expiry) and waits out the busy timeout instead.
        service = BoardService(w.ctx(w.owner))
        assert service.store.board_for_task(card)["slug"] == BOARD
    finally:
        holder.rollback()
        holder.close()


def test_create_false_waits_for_a_briefly_held_write_lock(tmp_path):
    BoardStore(tmp_path)
    holder = hold_write_lock(str(tmp_path / "boards.sqlite3"))
    release = threading.Timer(1.0, holder.rollback)
    release.start()
    try:
        store = BoardStore(tmp_path, create=False)
        with store.transaction() as conn:  # needs the write lock: waits ~1 s, not 0.2 s
            conn.execute("UPDATE meta SET value=value WHERE key='changes'")
        assert store.timeout >= 5
    finally:
        release.cancel()
        release.join()
        holder.close()


def test_explicit_short_timeout_still_fails_fast(tmp_path):
    BoardStore(tmp_path)
    holder = hold_write_lock(str(tmp_path / "boards.sqlite3"))
    try:
        store = BoardStore(tmp_path, create=False, timeout=0.05)
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            with store.transaction():
                pass
    finally:
        holder.rollback()
        holder.close()


def test_migration_reruns_when_the_schema_changed_under_this_process(tmp_path):
    BoardStore(tmp_path)
    path = str(tmp_path / "boards.sqlite3")
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE tasks DROP COLUMN status_line")
    BoardStore(tmp_path)
    with sqlite3.connect(path) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "status_line" in columns


def test_migration_reruns_for_a_replaced_file(tmp_path):
    BoardStore(tmp_path)
    path = tmp_path / "boards.sqlite3"
    for sidecar in ("-wal", "-shm"):
        Path(str(path) + sidecar).unlink(missing_ok=True)
    path.unlink()
    with sqlite3.connect(path) as conn:  # an older file at the same path
        conn.execute("CREATE TABLE other(x)")
    BoardStore(tmp_path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='changes'").fetchone() == (0,)


def test_warm_open_still_applies_a_pending_legacy_origin(tmp_path):
    store = BoardStore(tmp_path)
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO boards(slug,name,wing,stamp_user,stamp_trust,created_at) "
            "VALUES ('b','b','w','u','trusted','t')"
        )
        conn.execute(
            "INSERT INTO tasks(board_id,title,body,status,position,stamp_user,"
            "stamp_connection,stamp_trust,created_at,updated_at,updated_by) "
            "VALUES (1,'t','','backlog',1,'u','c','trusted','t','t','u')"
        )
        conn.execute("UPDATE tasks SET restricted_origin=NULL")
    BoardStore(tmp_path, legacy_origin=lambda row: True)
    with store.connection() as conn:
        assert conn.execute("SELECT restricted_origin FROM tasks").fetchone()[0] == 1


def logged(caplog, exc):
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        log_failure(logging.getLogger("t428"), "board read failed", exc)
    return caplog.records[-1].getMessage()


def raised(fn):
    try:
        fn()
    except Exception as exc:
        return exc
    raise AssertionError("no error")


def test_log_failure_names_a_locked_database(tmp_path, caplog):
    path = str(tmp_path / "x.sqlite3")
    holder = hold_write_lock(path)
    try:
        other = sqlite3.connect(path, timeout=0, isolation_level=None)
        exc = raised(lambda: other.execute("BEGIN IMMEDIATE"))
        other.close()
    finally:
        holder.close()
    message = logged(caplog, exc)
    assert "'database is locked'" in message
    if HAS_ERRORNAME:
        assert "SQLITE_BUSY" in message


def test_log_failure_drops_a_message_that_may_hold_user_data(caplog):
    conn = sqlite3.connect(":memory:")
    exc = raised(lambda: conn.execute("SELECT * FROM secret_card_title_xyz"))
    message = logged(caplog, exc)
    assert "secret_card_title_xyz" not in message and "no such table" not in message
    if HAS_ERRORNAME:
        assert "SQLITE_ERROR" in message


def test_log_failure_keeps_allowlisted_text_only_for_sqlite_errors(caplog):
    assert "database is locked" not in logged(caplog, ValueError("database is locked"))
    assert "database is locked" in logged(caplog, sqlite3.OperationalError("database is locked"))
    assert "x database is locked" not in logged(
        caplog, sqlite3.OperationalError("x database is locked")
    )
