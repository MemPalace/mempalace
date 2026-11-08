"""Owner-only Admin projections over synthetic workspace data."""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from commonplace import sessions
from commonplace.policy import Policy
from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_commonplace_web import request

FIXED_NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
DENIED = {"error": "a signed-in workspace owner is required"}


def cookie(hub, email="owner@example.com", workspace=None):
    link = sessions.create_login_link(hub.db, email, workspace or hub.workspace)
    session = sessions.redeem_login_link(hub.db, link)
    if not session:
        raise RuntimeError("Synthetic session setup failed")
    return {"Cookie": f"{sessions.COOKIE}={session}"}


def get(hub, section, headers, workspace=None, query=""):
    path = f"/api/admin/workspaces/{quote(workspace or hub.workspace, safe='')}/{section}{query}"
    status, response_headers, body = request(hub, path, headers=headers)
    return status, json.loads(body), response_headers


def clock(hub, now=FIXED_NOW, monotonic=lambda: 0):
    hub.gateway.admin_clock = SimpleNamespace(utc_now=lambda: now, monotonic=monotonic)


def agent(hub, name, user=None, **updates):
    cid, token = hub.db.create_token(
        workspace_ref=hub.workspace,
        user_ref=user or hub.admin,
        name=name,
        client="synthetic",
        policy=Policy.build(scopes=["memory:read", "admin"], read_wings=["notes"], trust="trusted"),
        actor=hub.admin,
        expires_days=None,
    )
    for key, value in updates.items():
        if key not in {"auth", "expires_at", "revoked_at", "last_used_at", "policy", "client"}:
            raise RuntimeError("Invalid fixture column")
        hub.db._conn.execute(f"UPDATE connections SET {key}=? WHERE id=?", (value, cid))
    return cid, {"Authorization": f"Bearer {token}"}


def member(hub, email="member@example.com", role="member"):
    uid = hub.db.create_user(email, "Morgan")
    hub.db.add_member(hub.workspace, uid, role, actor=hub.admin)
    return uid


def test_me_admin_capability(hub):
    headers = cookie(hub)
    status, _, body = request(hub, "/api/me", headers=headers)
    assert status == 200
    result = json.loads(body)
    assert result.get("can_admin") is True
    assert result.get("can_admin_instance") is True
    assert result["user"] == hub.admin and result["workspace"] == hub.workspace
    member(hub)
    _, _, body = request(hub, "/api/me", headers=cookie(hub, "member@example.com"))
    assert json.loads(body)["can_admin"] is False
    _, headers = agent(hub, "owner-agent")
    _, _, body = request(hub, "/api/me", headers=headers)
    assert json.loads(body)["can_admin"] is False
    assert json.loads(body)["can_admin_instance"] is False


def test_users_inventory(hub):
    uid = member(hub)
    hub.db._conn.execute("UPDATE users SET disabled_at=? WHERE id=?", ("2026-01-01T00:00:00Z", uid))
    hub.db._conn.execute(
        "UPDATE memberships SET created_at=? WHERE user_id=?", ("2026-02-01T00:00:00Z", uid)
    )
    hub.db.create_user("outsider@example.com")
    status, data, headers = get(hub, "users", cookie(hub))
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert data["workspace_id"] == hub.workspace
    assert data["workspace"] == {
        k: hub.db.workspace(hub.workspace)[k] for k in ("id", "name", "created_at")
    }
    assert data["count"] == 2 and not data["has_more"] and data["next_cursor"] is None
    rows = {r["id"]: r for r in data["members"]}
    assert rows[uid]["role"] == "member" and rows[hub.admin]["role"] == "owner"
    assert rows[uid]["disabled_at"] == "2026-01-01T00:00:00Z"
    assert rows[uid]["joined_at"] == "2026-02-01T00:00:00Z"
    assert rows[uid]["created_at"] == hub.db.user(uid)["created_at"]
    assert "outsider@example.com" not in json.dumps(data)


def test_member_refused(hub):
    member(hub)
    headers = cookie(hub, "member@example.com")
    for section in ("users", "connections"):
        status, data, _ = get(hub, section, headers)
        assert status == 403
        assert data == DENIED


def test_owner_agent_refused(hub):
    _, pat = agent(hub, "trusted-owner")
    for section in ("users", "connections"):
        status, data, _ = get(hub, section, pat)
        assert status == 403
        assert data == DENIED
    # Exercise a real OAuth bearer issued by the existing HTTP consent flow.
    from tests.test_commonplace_oauth import Client

    client = Client(hub)
    cid = client.register()
    grant = client.grant(cid)
    hub.db._conn.execute("UPDATE connections SET expires_at=NULL WHERE auth='oauth'")
    for section in ("users", "connections"):
        status, data, _ = get(hub, section, {"Authorization": f"Bearer {grant['access_token']}"})
        assert status == 403
        assert data == DENIED


def test_foreign_workspace_concealed(hub, monkeypatch):
    headers = cookie(hub)
    status, _, _ = get(hub, "users", headers)
    assert status == 200
    other = hub.db.create_user("foreign@example.com")
    foreign = hub.db.create_workspace("foreign", other)
    shared = hub.db.create_workspace("also-owned", hub.admin)
    # Auth remains real; no denied request may reach the new projection connection.
    import commonplace.web.admin as admin

    def denied_read(*args):
        raise AssertionError("Denied source read")

    monkeypatch.setattr(admin, "read_control", denied_read)
    for target in (foreign, shared, "nonexistent", hub.workspace + "%2fother"):
        for section in ("users", "connections"):
            status, data, _ = get(hub, section, headers, target)
            assert status == 404 and data == {"error": "not found"}


def test_signed_out_refused(hub):
    status, data, headers = get(hub, "users", {})
    assert status == 401
    assert data == {"error": "unauthorized"}
    assert "Bearer" in headers["WWW-Authenticate"]


def test_connection_inventory(hub):
    clock(hub)
    headers = cookie(hub)
    active, _ = agent(
        hub, "active", expires_at="2026-11-07T13:00:00Z", last_used_at="2026-11-07T11:59:00Z"
    )
    revoked, _ = agent(
        hub, "revoked", revoked_at="2026-11-07T10:00:00Z", expires_at="2026-11-07T11:00:00Z"
    )
    expired, _ = agent(
        hub, "expired", expires_at="2026-11-07T11:00:00Z", last_used_at="2026-11-07T11:50:00Z"
    )
    uid = member(hub, role="owner")
    disabled, _ = agent(hub, "disabled", user=uid, expires_at="2026-11-07T11:00:00Z")
    hub.db._conn.execute("UPDATE users SET disabled_at=? WHERE id=?", ("2026-11-07T10:00:00Z", uid))
    status, data, _ = get(hub, "connections", headers)
    assert status == 200
    rows = {r["id"]: r for r in data["connections"]}
    assert data["count"] == 4
    assert [rows[c]["state"] for c in (active, revoked, expired, disabled)] == [
        "active",
        "revoked",
        "expired",
        "disabled",
    ]
    assert rows[active]["trust"] == "trusted" and rows[active]["scopes"] == ["admin", "memory:read"]
    assert rows[active]["activity"] == "working" and rows[expired]["activity"] == "idle"
    assert rows[revoked]["activity"] == "unused" and rows[disabled]["user_disabled"] is True
    assert rows[active]["read_wings"] == ["notes"]
    assert rows[active]["expires_at"] == "2026-11-07T13:00:00Z"
    assert data["recorded_at"] == "2026-11-07T12:00:00Z"
    clock(hub, datetime(2026, 11, 7, 14, tzinfo=timezone.utc))
    status, data, _ = get(hub, "connections", headers)
    assert status == 200
    assert next(r for r in data["connections"] if r["id"] == active)["state"] == "expired"


def test_projection_redaction(hub):
    headers = cookie(hub)
    canary = "ghp_" + "X" * 36
    link = "https://example.invalid/app/login?t=synthetic-login-value"
    hub.db._conn.execute(
        "UPDATE users SET name=? WHERE id=?", (f"Robin {canary} {link}", hub.admin)
    )
    cid, _ = agent(hub, "benign", client=f"Bearer {canary}")
    hub.db._conn.execute("UPDATE connections SET last_used_at=? WHERE id=?", (link, cid))
    for section in ("users", "connections"):
        status, data, _ = get(hub, section, headers)
        assert status == 200
        text = json.dumps(data)
        assert canary not in text and link not in text and "/app/login" not in text
        for key in ("token_hash", "token_hint", "policy", "cookie", "access_token", "lease_token"):
            assert not re.search(rf'"{key}"\s*:', text)
    assert "Robin" in json.dumps(get(hub, "users", headers)[1])
    assert "benign" in json.dumps(get(hub, "connections", headers)[1])


def snapshot(hub):
    conn = hub.db._conn
    tables = [
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    return (
        list(conn.execute("SELECT sql FROM sqlite_master ORDER BY name")),
        {t: [tuple(r) for r in conn.execute(f'SELECT * FROM "{t}"')] for t in tables},
        {
            str(p.relative_to(Path(hub.db.path).parent))
            for p in Path(hub.db.path).parent.rglob("*")
            if not str(p).endswith(("-wal", "-shm"))
        },
    )


def test_admin_gets_do_not_write(hub, monkeypatch):
    headers = cookie(hub)
    agent(hub, "observed")
    before = snapshot(hub)
    status, _, _ = get(hub, "users", headers)
    assert status == 200
    assert get(hub, "connections", headers)[0] == 200
    assert snapshot(hub) == before
    from commonplace.web import admin

    principal = sessions.session_principal(hub.db, sessions.cookie_value(headers["Cookie"]))

    class NoLock:
        def __enter__(self):
            raise AssertionError("Admin acquired an application lock")

        def __exit__(self, *args):
            pass

    with monkeypatch.context() as patch:
        patch.setattr(hub.db, "_lock", NoLock())
        context = admin.ReadContext(hub.gateway.admin_clock)
        with admin.read_control(hub.gateway.db, context) as conn:
            admin.users(conn, principal.workspace_id, context)
            admin.connections(conn, principal.workspace_id, context)
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("DELETE FROM connections")
    assert snapshot(hub) == before


def test_missing_policy_denies_and_source_error_is_safe(hub, monkeypatch):
    headers = cookie(hub)
    cid, _ = agent(hub, "broken", policy="{broken")
    status, data, _ = get(hub, "connections", headers)
    assert status == 200
    row = next(r for r in data["connections"] if r["id"] == cid)
    assert row["scopes"] == [] and row["read_wings"] == [] and row["trust"] == "untrusted"
    from commonplace.web import admin

    def fail(*args):
        raise sqlite3.OperationalError("private-source-canary")

    monkeypatch.setattr(admin, "connections", fail)
    status, data, _ = get(hub, "connections", headers)
    assert status == 503 and data == {"error": "Connections unavailable; try again"}
    assert "private-source-canary" not in json.dumps(data)
    assert get(hub, "users", headers)[0] == 200


@pytest.mark.parametrize("section", ["users", "connections"])
def test_inventory_counts_match_listed_members(hub, section):
    headers = cookie(hub)
    uid = member(hub, role="owner")
    if section == "connections":
        agent(hub, "current-member")
        agent(hub, "former-member", user=uid)
        hub.db._conn.execute("PRAGMA foreign_keys=OFF")
        hub.db._conn.execute(
            "DELETE FROM memberships WHERE workspace_id=? AND user_id=?", (hub.workspace, uid)
        )
    else:
        hub.db._conn.execute("PRAGMA foreign_keys=OFF")
        hub.db._conn.execute("DELETE FROM users WHERE id=?", (uid,))
    status, data, _ = get(hub, section, headers)
    assert status == 200
    field = "members" if section == "users" else "connections"
    assert data["count"] == len(data[field]) == 1
    assert not data["has_more"] and data["next_cursor"] is None
