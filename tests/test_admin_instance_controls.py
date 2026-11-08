"""Operational read side effects, source deadlines and build metadata propagation."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from commonplace import sessions
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_admin_api import agent, clock, cookie, snapshot
from tests.test_admin_instance_api import instance, receipt
from tests.test_commonplace_web import request


def test_operational_reads_never_write_or_take_application_locks(hub, tmp_path, monkeypatch):
    from commonplace.web import admin

    clock(hub)
    headers = cookie(hub)
    principal = sessions.session_principal(hub.db, sessions.cookie_value(headers["Cookie"]))
    monkeypatch.setattr(hub.gateway, "principal_for", lambda handler: principal)
    monkeypatch.setenv("COMMONPLACE_BACKUP_STATUS_DIR", str(tmp_path))
    path = tmp_path / "online.json"
    path.write_text(json.dumps(receipt()))
    original_bytes = path.read_bytes()
    original_entries = sorted(p.name for p in tmp_path.iterdir())
    before = snapshot(hub)
    real_read = admin.read_control
    sql = []

    @contextmanager
    def readonly(db, context):
        with real_read(db, context) as conn:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE users SET is_admin=0")
            conn.set_trace_callback(sql.append)
            yield conn

    class NoLock:
        def __enter__(self):
            raise AssertionError("Operational GET acquired application lock")

        def __exit__(self, *args):
            pass

    with monkeypatch.context() as patch:
        patch.setattr(hub.db, "_lock", NoLock())
        patch.setattr(admin, "read_control", readonly)
        for section in ("deploy", "backups"):
            assert instance(hub, section, headers)[0] == 200
    assert snapshot(hub) == before
    assert path.read_bytes() == original_bytes
    assert sorted(p.name for p in tmp_path.iterdir()) == original_entries
    assert any("WHERE id='" + hub.admin + "'" in query for query in sql)
    assert not any("INSERT " in query or "UPDATE " in query for query in sql)


def test_instance_deadlines_and_mode_fault_isolation(hub, tmp_path, monkeypatch):
    from commonplace import backup_status
    from commonplace.web import admin

    clock(hub)
    headers = cookie(hub)
    tick = [0.0]
    clock(hub, monotonic=lambda: tick[0])
    monkeypatch.setenv("COMMONPLACE_BACKUP_STATUS_DIR", str(tmp_path))
    (tmp_path / "online.json").write_text(json.dumps(receipt()))
    (tmp_path / "offline.json").write_text(json.dumps(receipt("offline")))
    real_validate = backup_status._validate

    def expire(value, mode):
        if mode == "offline":
            tick[0] = 3.0
        return real_validate(value, mode)

    monkeypatch.setattr(backup_status, "_validate", expire)
    code, data, _ = instance(hub, "backups", headers)
    assert code == 200 and data["online"]["state"] == "ready"
    assert data["offline"] == {"state": "error", "error": "Backup status unavailable"}
    tick[0] = 0
    real_select = admin.select

    def slow(conn, context, sql, parameters=()):
        value = real_select(conn, context, sql, parameters)
        if sql.startswith("SELECT is_admin"):
            tick[0] = 3
        return value

    monkeypatch.setattr(admin, "select", slow)
    assert instance(hub, "deploy", headers)[0] == 503


def test_instance_machine_and_worker_transport(hub):
    for kind, code, message in (
        ("machine", 404, "not found"),
        ("worker", 403, "tool not allowed for this connection"),
    ):
        cid, bearer = agent(hub, "instance-" + kind)
        hub.db._conn.execute(
            "INSERT INTO runner_connections VALUES (?,?,?)", (cid, "fixture", kind)
        )
        for section in ("deploy", "backups"):
            assert instance(hub, section, bearer)[:2] == (code, {"error": message})
    assert request(hub, "/healthz")[0] == 200


def test_build_marker_recipe_and_compose():
    root = Path(__file__).resolve().parents[1]
    recipe = (root / "deploy/commonplace/Dockerfile").read_text()
    assert 'ARG COMMONPLACE_BUILD_COMMIT=""' in recipe
    assert recipe.index("_build_commit.txt") < recipe.index('RUN pip install ".[commonplace]"')
    compose = yaml.safe_load((root / "deploy/compose.host.yml.example").read_text())
    assert compose["services"]["commonplace"]["build"]["args"] == {
        "COMMONPLACE_BUILD_COMMIT": "${COMMONPLACE_BUILD_COMMIT:-}"
    }
