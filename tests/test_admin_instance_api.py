"""Instance operational reads: server authority, packaged identity and hostile receipts."""

import json
import os
from contextlib import contextmanager

import pytest

from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_admin_api import agent, clock, cookie, get, member
from tests.test_commonplace_web import request

DENIED = {"error": "Instance administrator access required"}
SHA = "0123456789abcdef" * 2 + "01234567"
STAMP = "2026-11-07T12:00:00Z"


def receipt(mode="online", result="success"):
    return dict(
        schema=1,
        run_id="12345678-1234-4234-8234-123456789abc",
        mode=mode,
        result=result,
        started_at=STAMP,
        finished_at=STAMP,
        stage="complete" if result == "success" else "backup_failed",
        scope="instance_snapshot",
    )


def instance(hub, section, headers, query=""):
    code, headers, body = request(hub, "/api/admin/instance/" + section + query, headers=headers)
    return code, json.loads(body), headers


def test_instance_authority(hub, monkeypatch):
    clock(hub)
    owner = cookie(hub)
    for section in ("deploy", "backups"):
        assert instance(hub, section, owner)[0] == 200
    from commonplace.web import admin

    reads = []
    real_read = admin.read_control

    @contextmanager
    def spy(db, context):
        reads.append(True)
        with real_read(db, context) as conn:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
            yield conn

    monkeypatch.setattr(admin, "read_control", spy)

    opened = []

    def forbidden(*args, **kwargs):
        opened.append(True)
        return {}

    monkeypatch.setattr(admin, "deploy", forbidden)
    from commonplace import backup_status

    monkeypatch.setattr(backup_status, "read_receipts", forbidden)
    member_id = member(hub)
    hub.db._conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (member_id,))
    _, pat = agent(hub, "owner-pat")
    from tests.test_commonplace_oauth import Client

    client = Client(hub)
    grant = client.grant(client.register())
    hub.db._conn.execute("UPDATE connections SET expires_at=NULL WHERE auth='oauth'")
    for headers in (
        cookie(hub, "member@example.com"),
        pat,
        {"Authorization": "Bearer " + grant["access_token"]},
    ):
        for section in ("deploy", "backups"):
            assert instance(hub, section, headers)[:2] == (403, DENIED)
        assert (
            json.loads(request(hub, "/api/me", headers=headers)[2])["can_admin_instance"] is False
        )
    assert not reads, "Cheap person/role refusals must not open control projections"
    assert json.loads(request(hub, "/api/me", headers=owner)[2])["can_admin_instance"] is True
    hub.db._conn.execute("UPDATE users SET is_admin=0 WHERE id=?", (hub.admin,))
    for section in ("deploy", "backups"):
        assert instance(hub, section, {**owner, "X-Can-Admin-Instance": "true"})[:2] == (
            403,
            DENIED,
        )
    assert json.loads(request(hub, "/api/me", headers=owner)[2])["can_admin_instance"] is False
    hub.db._conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (hub.admin,))
    other = hub.db.create_workspace("also-owned", hub.admin)
    assert get(hub, "users", owner, other)[:2] == (404, {"error": "not found"})

    # Missing server row denies; read errors fail closed without operational reads.
    class Missing:
        def fetchone(self):
            return None

    real_select = admin.select
    monkeypatch.setattr(
        admin,
        "select",
        lambda conn, context, sql, parameters=(): (
            Missing()
            if sql.startswith("SELECT is_admin")
            else real_select(conn, context, sql, parameters)
        ),
    )
    assert instance(hub, "deploy", owner)[:2] == (403, DENIED)

    def broken(*args):
        raise OSError("private-source")

    monkeypatch.setattr(admin, "read_control", broken)
    for section in ("deploy", "backups"):
        code, data, _ = instance(hub, section, owner)
        assert code == 503 and "private-source" not in json.dumps(data)
    code, _, raw = request(hub, "/api/me", headers=owner)
    assert code == 200 and json.loads(raw)["can_admin_instance"] is False
    assert not opened, "Denied/read-error paths must not open operational sources"


def test_running_version_and_commit(hub, monkeypatch):
    clock(hub)
    headers = cookie(hub)
    code, data, response = instance(hub, "deploy", headers)
    assert code == 200
    from commonplace import __version__ as cp_version
    from commonplace.web import admin
    from mempalace.version import __version__ as mp_version

    assert data["commonplace_version"] == cp_version
    assert data["mempalace_version"] == mp_version
    assert response["Cache-Control"] == "no-store"

    class Resource:
        def joinpath(self, name):
            assert name == "_build_commit.txt"
            return self

        def read_text(self, **kwargs):
            if marker is None:
                raise FileNotFoundError()
            return marker

    monkeypatch.setattr(admin.resources, "files", lambda package: Resource())
    for marker in (SHA, "a" * 64, "bad", "../unsafe", "a" * 41, None):
        _, data, _ = instance(hub, "deploy", headers)
        valid = marker in (SHA, "a" * 64)
        assert data["commit"] == (marker if valid else None)
        assert data["commit_source"] == ("build" if valid else "unknown")
    assert instance(hub, "deploy", headers, "?path=/tmp/forbidden")[0] == 400


def test_backup_receipts_projection(hub, tmp_path, monkeypatch):
    clock(hub)
    monkeypatch.setenv("COMMONPLACE_BACKUP_STATUS_DIR", str(tmp_path))
    for mode, result in (("online", "success"), ("offline", "failure")):
        (tmp_path / (mode + ".json")).write_text(json.dumps(receipt(mode, result)))
    code, data, _ = instance(hub, "backups", cookie(hub))
    assert code == 200
    for mode, result in (("online", "success"), ("offline", "failure")):
        assert data[mode] == {"state": "ready", **receipt(mode, result)}
    assert set(data) == {"online", "offline"}
    assert str(tmp_path) not in json.dumps(data)
    assert instance(hub, "backups", cookie(hub), "?directory=/tmp")[0] == 400


def test_backup_missing_and_malformed(hub, tmp_path, monkeypatch):
    clock(hub)
    headers = cookie(hub)
    monkeypatch.delenv("COMMONPLACE_BACKUP_STATUS_DIR", raising=False)
    code, data, _ = instance(hub, "backups", headers)
    assert code == 200
    assert data == {
        m: {"state": "unknown", "reason": "not_configured"} for m in ("online", "offline")
    }
    monkeypatch.setenv("COMMONPLACE_BACKUP_STATUS_DIR", str(tmp_path))
    assert instance(hub, "backups", headers)[1]["online"] == {
        "state": "unknown",
        "reason": "no_record",
    }
    good = receipt("offline")
    (tmp_path / "offline.json").write_text(json.dumps(good))
    path = tmp_path / "online.json"
    invalid = [
        "{",
        "x" * 16385,
        *[
            json.dumps({**receipt(), **update})
            for update in (
                {"schema": True},
                {"schema": 2},
                {"mode": "offline"},
                {"result": "running"},
                {"stage": "secret"},
                {"stage": "backup_failed"},
                {"scope": "remote_upload"},
                {"run_id": "../path"},
                {"started_at": "2026-11-07T12:00:00"},
                {"finished_at": "2026-11-06T12:00:00Z"},
                {"token": "hidden"},
            )
        ],
    ]
    for raw in invalid:
        path.write_text(raw)
        _, data, _ = instance(hub, "backups", headers)
        assert data["online"] == {"state": "error", "error": "Backup status unavailable"}
        assert data["offline"] == {"state": "ready", **good}
    path.unlink()
    target = tmp_path / "private"
    target.write_text(json.dumps(receipt()))
    path.symlink_to(target)
    assert instance(hub, "backups", headers)[1]["online"]["state"] == "error"
    path.unlink()
    os.mkfifo(path)
    # No timing threshold: the no-follow/nonblocking flags are asserted before open.
    from commonplace import backup_status

    real_open = os.open
    opened_flags = []

    def checked_open(name, flags, *args, **kwargs):
        if str(name) == "online.json":
            opened_flags.append((flags, kwargs.get("dir_fd")))
            # Keep the hostile FIFO harmless even when testing a broken flag guard.
            return real_open(name, flags | os.O_NONBLOCK, *args, **kwargs)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(backup_status.os, "open", checked_open)
    assert instance(hub, "backups", headers)[1]["online"]["state"] == "error"
    assert opened_flags and all(
        flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK and directory is not None
        for flags, directory in opened_flags
    )
    path.unlink()
    path.mkdir()
    assert instance(hub, "backups", headers)[1]["online"]["state"] == "error"
    missing = tmp_path / "not-created"
    monkeypatch.setenv("COMMONPLACE_BACKUP_STATUS_DIR", str(missing))
    assert instance(hub, "backups", headers)[1]["online"]["state"] == "error"
    assert not missing.exists()


def test_instance_transport_controls(hub):
    for section in ("deploy", "backups"):
        code, data, headers = instance(hub, section, {})
        assert code == 401 and data == {"error": "unauthorized"}
        assert "Bearer" in headers["WWW-Authenticate"]
        assert (
            request(
                hub,
                "/api/admin/instance/" + section,
                method="POST",
                headers={**cookie(hub), "Content-Type": "application/json"},
            )[0]
            == 404
        )
    assert json.loads(request(hub, "/healthz")[2]) == {"ok": True}


def test_me_capability_read_failure_keeps_bootstrap(hub, monkeypatch):
    from commonplace.web import admin

    clock(hub)
    headers = cookie(hub)
    code, _, raw = request(hub, "/api/me", headers=headers)
    assert code == 200
    expected = json.loads(raw)
    real_select = admin.select

    def select(conn, context, sql, parameters=()):
        if sql.startswith("SELECT is_admin"):
            raise OSError("private-capability-read")
        return real_select(conn, context, sql, parameters)

    monkeypatch.setattr(admin, "select", select)
    code, _, raw = request(hub, "/api/me", headers=headers)
    assert code == 200
    assert json.loads(raw) == {**expected, "can_admin_instance": False}
    for section in ("deploy", "backups"):
        assert instance(hub, section, headers)[0] == 503


@pytest.mark.parametrize("field", ["started_at", "finished_at"])
@pytest.mark.parametrize("stamp", ["2026-11-07T12Z", "2026-11-07 12:00:00Z"])
def test_receipt_rejects_noncanonical_timestamps(hub, tmp_path, monkeypatch, field, stamp):
    clock(hub)
    monkeypatch.setenv("COMMONPLACE_BACKUP_STATUS_DIR", str(tmp_path))
    (tmp_path / "online.json").write_text(json.dumps({**receipt(), field: stamp}))
    (tmp_path / "offline.json").write_text(json.dumps(receipt("offline")))
    code, data, _ = instance(hub, "backups", cookie(hub))
    assert code == 200
    assert data["online"] == {"state": "error", "error": "Backup status unavailable"}
    assert data["offline"] == {"state": "ready", **receipt("offline")}


def test_receipt_open_flags_are_observable(hub, tmp_path, monkeypatch):
    from commonplace import backup_status

    clock(hub)
    monkeypatch.setenv("COMMONPLACE_BACKUP_STATUS_DIR", str(tmp_path))
    (tmp_path / "online.json").write_text(json.dumps(receipt()))
    seen = []
    real_open = os.open

    def observe(name, flags, *args, **kwargs):
        seen.append((str(name), flags, kwargs.get("dir_fd")))
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(backup_status.os, "open", observe)
    code, data, _ = instance(hub, "backups", cookie(hub))
    assert code == 200 and data["online"]["state"] == "ready"
    receipt_opens = [item for item in seen if item[0] == "online.json"]
    assert receipt_opens
    assert all(
        flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK and directory is not None
        for _, flags, directory in receipt_opens
    )


def test_instance_read_error_never_opens_operational_sources(hub, monkeypatch):
    from commonplace import backup_status
    from commonplace.web import admin

    clock(hub)
    headers = cookie(hub)
    opened = []
    real_select = admin.select

    def select(conn, context, sql, parameters=()):
        if sql.startswith("SELECT is_admin"):
            raise OSError("private-capability-read")
        return real_select(conn, context, sql, parameters)

    def deploy():
        opened.append("deploy")
        return {}

    def receipts(context):
        opened.append("backups")
        return {mode: {"state": "unknown", "reason": "no_record"} for mode in ("online", "offline")}

    monkeypatch.setattr(admin, "select", select)
    monkeypatch.setattr(admin, "deploy", deploy)
    monkeypatch.setattr(backup_status, "read_receipts", receipts)
    for section in ("deploy", "backups"):
        assert instance(hub, section, headers)[0] == 503
        assert not opened, "The instance handler must deny before any operational source"
