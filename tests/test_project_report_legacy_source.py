"""Report recovery must not install triggers referring to unmigrated columns."""

import sqlite3

import pytest

from commonplace import webhooks
from commonplace.boards.store import BoardStore
from commonplace.projects import reports
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_admin_runtime_api import OLD, runtime as runtime, task


def legacy_source(hub, runtime):
    webhooks.stop(hub.gateway)
    assert not hub.gateway.webhooks.thread.is_alive(), "report dispatcher did not stop within 7s"
    task(runtime, 1, claim=OLD)
    with runtime[0].connection() as conn:
        conn.execute("DROP TRIGGER IF EXISTS project_report_reset")
        conn.execute("ALTER TABLE boards DROP COLUMN coordinator")
        conn.execute("ALTER TABLE tasks DROP COLUMN heartbeat_at")


def test_pump_leaves_legacy_status_writes_usable(hub, runtime):
    legacy_source(hub, runtime)
    hub.gateway.project_reports.pass_once()
    with runtime[0].connection() as conn:
        assert (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='project_report_reset'").fetchone()
            is None
        ), "report recovery installed a trigger referencing missing heartbeat_at"
        conn.execute("UPDATE tasks SET status = status WHERE id = 1")


def test_boardstore_migration_installs_working_report_trigger(hub, runtime):
    legacy_source(hub, runtime)
    hub.gateway.project_reports.pass_once()
    BoardStore(runtime[1])
    with runtime[0].connection() as conn:
        assert (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='project_report_reset'").fetchone()
            is not None
        )
        before = conn.execute("SELECT report_reset_rev FROM tasks WHERE id=1").fetchone()[0]
        conn.execute("UPDATE tasks SET status='todo', claim_until=NULL WHERE id=1")
        assert conn.execute("SELECT report_reset_rev FROM tasks WHERE id=1").fetchone()[0] == (
            before + 1
        )


@pytest.mark.parametrize("missing", reports.WATCHED)
def test_source_requires_every_watched_column_before_installing_trigger(missing):
    with sqlite3.connect(":memory:") as conn:
        columns = ", ".join(f'"{name}" TEXT' for name in reports.WATCHED if name != missing)
        conn.execute(f"CREATE TABLE tasks(id INTEGER PRIMARY KEY, {columns})")
        reports.ensure_source(conn)
        assert (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='project_report_reset'").fetchone()
            is None
        ), f"report trigger references missing {missing}"
