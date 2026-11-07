"""Browser-only GitHub grants with synthetic credentials and no remote traffic."""

import importlib
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from commonplace import sessions, urls
from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext
from commonplace.social import accounts, providers
from tests.test_commonplace_public_surface import make_surface as make_surface, token
from tests.test_commonplace_social import request, rows


class Remote:
    def __init__(self):
        self.calls = []
        self.scope = ""
        self.expiry = 3600
        self.subject = 123
        self.status = 204
        self.entered = Event()
        self.release = Event()
        self.block = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("access_token"):
            self.entered.set()
            if self.block:
                assert self.release.wait(5)
            return (
                200,
                {},
                {
                    "access_token": "synthetic-issued-token",
                    "scope": self.scope,
                    "expires_in": self.expiry,
                    "refresh_token": "synthetic-refresh",
                },
            )
        if url.endswith("/user"):
            return 200, {"X-OAuth-Scopes": self.scope}, {"id": self.subject, "login": "test-person"}
        assert method == "DELETE" and "/applications/" in url
        return self.status, {}, {}


@pytest.fixture
def setup(make_surface, monkeypatch, tmp_path):
    monkeypatch.setenv("COMMONPLACE_GITHUB_CONNECT", "on")
    monkeypatch.setenv("COMMONPLACE_GITHUB_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("COMMONPLACE_GITHUB_CLIENT_SECRET", "synthetic-app-secret")
    key = tmp_path / "wrapping.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    monkeypatch.setenv("COMMONPLACE_GITHUB_KEY_FILE", str(key))
    monkeypatch.setattr(urls, "APP_URL", "https://app.example")
    surface = make_surface()
    surface.gateway.rate_limits = False
    # Public users, not administrators.
    surface.db._conn.execute("UPDATE users SET is_admin = 0")
    surface.session = sessions.start_session(surface.db, surface.uid, surface.workspace)
    surface.cookie = sessions.COOKIE + "=" + surface.session
    principal = sessions.session_principal(surface.db, surface.session)
    service = BoardService(ToolContext(principal=principal, gateway=surface.gateway))
    service.board_add(slug="work", name="Work")
    surface.card = service.task_add(board="work", title="Review")["number"]
    remote = Remote()
    surface.remote = remote
    if hasattr(surface.gateway, "github"):
        module = importlib.import_module("commonplace.github.client")
        monkeypatch.setattr(module, "request", remote.request)
    original = socket.socket.connect

    def local_only(sock, address):
        assert address[0] == "127.0.0.1", "unexpected network"
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    return surface


def status(s):
    code, _, raw = request(s, "/api/github", cookie=s.cookie)
    assert code == 200, (code, raw)
    return json.loads(raw)


def write(s, path="/api/github/connect", method="POST", payload=None, **headers):
    data = {"csrf": status(s)["csrf"], "card": s.card, **(payload or {})}
    return request(
        s,
        path,
        method,
        cookie=s.cookie,
        raw=json.dumps(data).encode(),
        headers={"Content-Type": "application/json", "Origin": urls.APP_URL, **headers},
    )


def begin(s):
    code, headers, raw = write(s)
    assert code == 200, (code, raw)
    data = json.loads(raw)
    params = parse_qs(urlsplit(data["authorize_url"]).query, keep_blank_values=True)
    assert params["scope"] == [""] and "offline_access" not in data["authorize_url"]
    assert params["code_challenge_method"] == ["S256"]
    return params["state"][0], headers["Set-Cookie"].split(";", 1)[0]


def finish(s, flow):
    state, cookie = flow
    return request(
        s,
        "/app/auth/github/callback?" + urlencode({"state": state, "code": "synthetic-code"}),
        cookie=s.cookie + "; " + cookie,
    )


def connect(s):
    response = finish(s, begin(s))
    assert response[0] == 303
    assert "synthetic-code" not in response[1]["Location"]
    pending = status(s)
    if pending["state"] == "confirm":
        assert write(s, "/api/github/confirm", payload={"accept": True})[0] == 200
    assert status(s)["state"] == "connected"


def test_owner_identity_first(setup):
    s = setup
    accounts.resolve(
        s.db, "github", providers.Profile("123", "Person", ("owner@example.com",)), signup=False
    )
    tables = {r[0] for r in s.db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "github_account_owners" in tables
    assert rows(s, "github_account_owners")[0]["user_id"] == s.uid
    # Legacy identity without an owner row must still defeat a foreign grant.
    s.db._conn.execute("DELETE FROM github_account_owners")
    other = s.db.create_user("other@example.com")
    s.db.add_member(s.workspace, other, "member", actor=s.uid)
    s.cookie = sessions.COOKIE + "=" + sessions.start_session(s.db, other, s.workspace)
    finish(s, begin(s))
    if status(s)["state"] == "confirm":
        write(s, "/api/github/confirm", payload={"accept": True})
    assert status(s)["state"] != "connected"
    assert len([c for c in s.remote.calls if c[0] == "DELETE"]) == 1


def test_owner_migration(setup):
    s = setup
    accounts.resolve(
        s.db, "github", providers.Profile("123", "Person", ("owner@example.com",)), signup=False
    )
    tables = {r[0] for r in s.db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "github_account_owners" in tables
    s.db._conn.execute("DELETE FROM github_account_owners")
    accounts.ensure_schema(s.db)
    accounts.ensure_schema(s.db)
    assert len(rows(s, "github_account_owners")) == 1
    other = s.db.create_user("other@example.com")
    s.db._conn.execute("UPDATE github_account_owners SET user_id=?", (other,))
    with pytest.raises(providers.SignInError, match="github_owner"):
        accounts.ensure_schema(s.db)
    assert rows(s, "github_account_owners")[0]["user_id"] == other


def test_owner_races(setup):
    s = setup
    status(s)
    accounts.reserve_github_owner(s.db._conn, "123", s.uid)
    before = len(rows(s, "users"))
    with pytest.raises(providers.SignInError, match="github_owner"):
        accounts.resolve(
            s.db, "github", providers.Profile("123", "Person", ("new@example.com",)), signup=True
        )
    assert len(rows(s, "users")) == before
    # Numeric subjects of other providers never participate in GitHub ownership.
    assert (
        accounts.resolve(
            s.db, "email", providers.Profile("123", "Person", ("new@example.com",)), signup=True
        )
        != s.uid
    )
    with ThreadPoolExecutor(2) as pool:
        result = list(
            pool.map(
                lambda _: accounts.resolve(
                    s.db,
                    "github",
                    providers.Profile("123", "Person", ("owner@example.com",)),
                    signup=False,
                ),
                range(2),
            )
        )
    assert result == [s.uid, s.uid]


def test_disconnect_callback(setup):
    s = setup
    flow = begin(s)
    s.remote.block = True
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(finish, s, flow)
        assert s.remote.entered.wait(5)
        assert write(s, "/api/github", "DELETE")[0] == 200
        assert write(s)[0] == 409
        s.remote.release.set()
        assert pending.result()[0] == 303
    assert status(s)["state"] != "connected"
    deletes = [c for c in s.remote.calls if c[0] == "DELETE"]
    assert len(deletes) == 1 and deletes[0][1].endswith("/token")


def test_deletion_callback(setup):
    s = setup
    flow = begin(s)
    accounts.reserve_github_owner(s.db._conn, "123", s.uid)
    s.remote.block = True
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(finish, s, flow)
        assert s.remote.entered.wait(5)
        s.gateway.github.delete_user(s.uid)
        assert rows(s, "github_account_owners")[0]["user_id"] == s.uid
        s.remote.release.set()
        assert pending.result()[0] == 303
    assert not rows(s, "github_grants")
    assert not rows(s, "github_account_owners")
    assert len([c for c in s.remote.calls if c[0] == "DELETE"]) == 1


def test_lifecycle_recovery(setup):
    s = setup
    connect(s)
    s.remote.status = 422
    code, _, raw = write(s, "/api/github", "DELETE")
    assert code == 200 and "unconfirmed" in raw.decode().lower()
    assert not rows(s, "github_grants")
    assert write(s)[0] == 409
    assert write(s, payload={"acknowledge": True})[0] == 200
    s.gateway.github.recover()
    assert not rows(s, "github_connect_flows")
    assert status(s)["state"] != "connected"


def test_consent_dispatch(setup):
    s = setup
    flow = begin(s)
    before = (rows(s, "users"), rows(s, "sessions"), rows(s, "user_identities"))
    response = finish(s, flow)
    assert response[0] == 303 and sessions.COOKIE not in repr(response[1].get_all("Set-Cookie", []))
    assert status(s)["state"] == "confirm"
    assert write(s, "/api/github/confirm", payload={"accept": False})[0] == 200
    assert len([c for c in s.remote.calls if c[0] == "DELETE"]) == 1
    finish(s, flow)
    assert len([c for c in s.remote.calls if c[1].endswith("access_token")]) == 1
    assert before == (rows(s, "users"), rows(s, "sessions"), rows(s, "user_identities"))


def test_scopes_secrets(setup, caplog):
    s = setup
    connect(s)
    for call in s.remote.calls:
        assert not call[1].endswith("/emails")
    raw = (
        repr(rows(s, "github_grants"))
        + repr(rows(s, "github_connect_flows"))
        + repr(s.db.audit_entries())
        + caplog.text
        + json.dumps(status(s))
    )
    for secret in (
        "synthetic-issued-token",
        "synthetic-refresh",
        "synthetic-code",
        "synthetic-app-secret",
    ):
        assert secret not in raw
    for path in (s.db.path, s.db.path + "-wal"):
        from pathlib import Path

        if Path(path).exists():
            assert b"synthetic-issued-token" not in Path(path).read_bytes()


@pytest.mark.parametrize("guard", ["bearer", "origin", "csrf", "workspace", "logout"])
def test_security_guards(setup, guard):
    s = setup
    initial = status(s)
    if guard == "bearer":
        auth = token(s)[2]
        assert request(s, "/api/github", headers=auth)[0] == 403
    elif guard == "origin":
        assert write(s, Origin="https://evil.example")[0] == 403
        raw = json.dumps({"csrf": initial["csrf"], "card": s.card}).encode()
        assert (
            request(
                s,
                "/api/github/connect",
                "POST",
                cookie=s.cookie,
                raw=raw,
                headers={"Content-Type": "application/json"},
            )[0]
            == 403
        )
    elif guard == "csrf":
        assert write(s, payload={"csrf": "wrong"})[0] == 403
    else:
        flow = begin(s)
        if guard == "logout":
            sessions.end_session(s.db, s.session)
        else:
            wid = s.db.create_workspace("other", s.uid)
            s.db._conn.execute(
                "UPDATE sessions SET workspace_id=? WHERE id_hash=?",
                (wid, sessions._hash(s.session)),
            )
        finish(s, flow)
        assert not rows(s, "github_grants")
        assert not [c for c in s.remote.calls if c[1].endswith("access_token")]


@pytest.mark.parametrize(
    "scope,expiry",
    [
        (None, 3600),
        ("repo", 3600),
        ("public_repo", 3600),
        ("", None),
        ("", 0),
        ("", -1),
        ("", True),
    ],
)
def test_refused_scope_or_expiry_revokes_one_token(setup, scope, expiry):
    s = setup
    s.remote.scope, s.remote.expiry = scope, expiry
    finish(s, begin(s))
    assert status(s)["state"] == "absent"
    deletes = [c for c in s.remote.calls if c[0] == "DELETE"]
    assert len(deletes) == 1 and deletes[0][1].endswith("/token")
    assert not rows(s, "github_grants")


@pytest.mark.parametrize("scope", ["read:user", "user:email", "read:user user:email"])
def test_inherited_scopes_require_confirmation_even_for_login_subject(setup, scope):
    s = setup
    status(s)
    accounts.resolve(
        s.db, "github", providers.Profile("123", "Person", ("owner@example.com",)), signup=False
    )
    s.remote.scope = scope
    finish(s, begin(s))
    assert status(s)["state"] == "confirm"
    assert not rows(s, "github_grants")
    assert write(s, "/api/github/confirm", payload={"accept": True})[0] == 200
    assert status(s)["state"] == "connected"


def test_one_flow_and_one_grant(setup):
    s = setup
    flow = begin(s)
    assert write(s)[0] == 409
    finish(s, flow)
    write(s, "/api/github/confirm", payload={"accept": True})
    assert status(s)["state"] == "connected"
    assert write(s)[0] == 409
    assert len(rows(s, "github_grants")) == 1
    assert not [c for c in s.remote.calls if c[0] == "DELETE"]


def test_key_permissions_tamper_and_workspace_fail_closed(setup, monkeypatch):
    import os
    from pathlib import Path

    s = setup
    connect(s)
    key = Path(os.environ["COMMONPLACE_GITHUB_KEY_FILE"])
    key.chmod(0o644)
    assert request(s, "/api/github", cookie=s.cookie)[0] == 409
    key.chmod(0o600)
    assert status(s)["state"] == "connected"
    s.db._conn.execute("UPDATE github_grants SET secret=?", (b"tampered",))
    assert request(s, "/api/github", cookie=s.cookie)[0] == 409


def test_workspace_separation_of_connection_state(setup):
    s = setup
    connect(s)
    wid = s.db.create_workspace("other", s.uid)
    s.cookie = sessions.COOKIE + "=" + sessions.start_session(s.db, s.uid, wid)
    assert status(s)["state"] == "other_workspace"
    assert write(s, "/api/github", "DELETE")[0] == 200
    assert len(rows(s, "github_grants")) == 0


def test_disabled_callback_and_hidden_card_do_not_exchange(setup, monkeypatch):
    s = setup
    assert write(s, payload={"card": 99999})[0] == 409
    assert not rows(s, "github_connect_flows")
    flow = begin(s)
    monkeypatch.delenv("COMMONPLACE_GITHUB_CONNECT")
    finish(s, flow)
    assert not s.remote.calls
    monkeypatch.setenv("COMMONPLACE_GITHUB_CONNECT", "on")
    assert write(s, payload={"card": 99999})[0] == 409
    assert not s.remote.calls


def test_confirm_disconnect_race_does_not_resurrect(setup):
    s = setup
    finish(s, begin(s))
    assert status(s)["state"] == "confirm"
    assert write(s, "/api/github", "DELETE")[0] == 200
    assert write(s, "/api/github/confirm", payload={"accept": True})[0] == 409
    assert not rows(s, "github_grants")
    assert len([c for c in s.remote.calls if c[0] == "DELETE"]) == 1


def test_login_provisioning_commit_failure_rolls_back_every_control_row(setup):
    import sqlite3

    s = setup
    status(s)
    before = {
        table: rows(s, table)
        for table in (
            "users",
            "workspaces",
            "memberships",
            "user_identities",
            "github_account_owners",
            "account_plans",
        )
    }
    s.db._conn.execute(
        "CREATE TRIGGER fail_identity BEFORE INSERT ON user_identities BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        accounts.resolve(
            s.db, "github", providers.Profile("456", "New", ("new@example.com",)), signup=True
        )
    assert before == {table: rows(s, table) for table in before}


def test_callback_checks_card_visibility_again(setup):
    s = setup
    flow = begin(s)
    s.db._conn.execute(
        "DELETE FROM memberships WHERE workspace_id=? AND user_id=?", (s.workspace, s.uid)
    )
    finish(s, flow)
    assert not s.remote.calls
    assert not rows(s, "github_grants")


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/github"),
        ("POST", "/api/github/connect"),
        ("POST", "/api/github/confirm"),
        ("DELETE", "/api/github"),
    ],
)
def test_agent_and_bearer_cannot_use_browser_routes(setup, method, path):
    s = setup
    initial = status(s)
    auth = token(s)[2]
    response = request(
        s,
        path,
        method,
        cookie=s.cookie,
        raw=json.dumps({"csrf": initial["csrf"], "card": s.card}).encode(),
        headers={**auth, "Origin": urls.APP_URL, "Content-Type": "application/json"},
    )
    assert response[0] == 403
    assert not s.remote.calls


def test_cross_worker_login_and_connect_arbitrate_one_owner(setup):
    from threading import Barrier
    from types import SimpleNamespace

    from commonplace.control_db import ControlDB

    s = setup
    status(s)
    module = importlib.import_module("commonplace.github")
    other = s.db.create_user("other@example.com")
    db2 = ControlDB(s.db.path)
    worker = module.GitHub(SimpleNamespace(db=db2, sign_in=s.gateway.sign_in))
    barrier = Barrier(2)

    def login():
        barrier.wait(5)
        try:
            return accounts.resolve(
                s.db,
                "github",
                providers.Profile("123", "Person", ("owner@example.com",)),
                signup=False,
            )
        except providers.SignInError:
            return None

    def grant():
        barrier.wait(5)
        try:
            with db2._lock, db2._tx() as conn:
                worker.commit(
                    conn,
                    other,
                    {"workspace_id": s.workspace},
                    {
                        "subject": "123",
                        "login": "other",
                        "token": "synthetic-other",
                        "expires_at": 9999999999,
                    },
                )
            return other
        except providers.SignInError:
            return None

    try:
        with ThreadPoolExecutor(2) as pool:
            a, b = pool.submit(login), pool.submit(grant)
            outcomes = [a.result(), b.result()]
        assert sum(value is not None for value in outcomes) == 1
        owner = rows(s, "github_account_owners")
        assert len(owner) == 1 and owner[0]["user_id"] in outcomes
        for table in ("user_identities", "github_grants"):
            assert all(row["user_id"] == owner[0]["user_id"] for row in rows(s, table))
        assert len(rows(s, "users")) == 2
    finally:
        db2.close()


@pytest.mark.parametrize("outcome", [204, 401, 422, 500, "network", "expired"])
def test_disconnect_outcomes_purge_and_never_retry(setup, monkeypatch, outcome):
    s = setup
    connect(s)
    original = s.remote.request

    def remote(method, url, **kwargs):
        if method == "DELETE" and outcome == "network":
            s.remote.calls.append((method, url, kwargs))
            raise providers.SignInError("github_transport")
        return original(method, url, **kwargs)

    monkeypatch.setattr(importlib.import_module("commonplace.github.client"), "request", remote)
    if outcome == "expired":
        s.db._conn.execute("UPDATE github_grants SET expires_at=0")
    else:
        s.remote.status = outcome
    code, _, raw = write(s, "/api/github", "DELETE")
    assert code == 200
    assert not rows(s, "github_grants")
    assert len([c for c in s.remote.calls if c[0] == "DELETE"]) == (
        0 if outcome == "expired" else 1
    )
    assert ("unconfirmed" in raw.decode().lower()) == (outcome != 204)
    assert status(s)["acknowledge_required"] == (outcome != 204)
    assert write(s)[0] == (200 if outcome == 204 else 409)
    if outcome != 204:
        assert write(s, payload={"acknowledge": True})[0] == 200
    assert len([c for c in s.remote.calls if c[0] == "DELETE"]) == (
        0 if outcome == "expired" else 1
    )


@pytest.mark.parametrize("header", ["repo", "read:user", 42])
def test_conflicting_header_evidence_revokes_only_issued_token(setup, monkeypatch, header):
    s = setup
    status(s)
    original = s.remote.request

    def remote(method, url, **kwargs):
        code, headers, data = original(method, url, **kwargs)
        if url.endswith("/user"):
            headers = {"X-OAuth-Scopes": header}
        return code, headers, data

    monkeypatch.setattr(importlib.import_module("commonplace.github.client"), "request", remote)
    finish(s, begin(s))
    assert status(s)["state"] == "absent"
    deletes = [c for c in s.remote.calls if c[0] == "DELETE"]
    assert len(deletes) == 1 and deletes[0][1].endswith("/token")


@pytest.mark.parametrize(
    "problem",
    ["missing-cookie", "wrong-cookie", "expired", "denied", "purpose", "session", "state", "post"],
)
def test_callback_cookie_ttl_denial_and_binding(setup, problem):
    s = setup
    flow = begin(s)
    before = rows(s, "users"), rows(s, "user_identities"), rows(s, "sessions")
    state, cookie = flow
    if problem == "missing-cookie":
        cookie = ""
    elif problem == "wrong-cookie":
        cookie = "cmmn_github_connect=wrong"
    elif problem == "expired":
        s.db._conn.execute("UPDATE github_connect_flows SET expires_at=0")
    elif problem == "purpose":
        s.db._conn.execute("UPDATE github_connect_flows SET purpose='signin'")
    elif problem == "session":
        s.cookie = sessions.COOKIE + "=" + sessions.start_session(s.db, s.uid, s.workspace)
        before = rows(s, "users"), rows(s, "user_identities"), rows(s, "sessions")
    elif problem == "state":
        state = "gc1_unknown"
    parameters = {"state": state, "code": "synthetic-code"}
    if problem == "denied":
        parameters = {"state": state, "error": "access_denied"}
    response = request(
        s,
        "/app/auth/github/callback?" + urlencode(parameters),
        "POST" if problem == "post" else "GET",
        cookie=s.cookie + "; " + cookie,
    )
    assert response[0] == (405 if problem == "post" else 303)
    assert not s.remote.calls
    assert before == (rows(s, "users"), rows(s, "user_identities"), rows(s, "sessions"))
    assert not rows(s, "github_grants") and not rows(s, "github_confirmations")


def test_expired_confirmation_has_one_token_delete(setup):
    s = setup
    finish(s, begin(s))
    assert status(s)["state"] == "confirm"
    s.db._conn.execute("UPDATE github_confirmations SET expires_at=0")
    assert write(s)[0] == 200
    assert not rows(s, "github_confirmations")
    deletes = [c for c in s.remote.calls if c[0] == "DELETE"]
    assert len(deletes) == 1 and deletes[0][1].endswith("/token")


def test_normal_login_alongside_connect_does_not_dispatch_to_connect(setup, monkeypatch):
    s = setup
    connect_flow = begin(s)
    original = s.gateway.sign_in.providers["github"]

    # The sign-in profile is synthetic; the actual signin state/cookie/dispatch stays real.
    class LoginProvider:
        id = "github"
        daily_cap = None

        def authorize_url(self, *args):
            return original.authorize_url(*args)

        def profile(self, *args):
            return providers.Profile("123", "Person", ("owner@example.com",))

    monkeypatch.setitem(s.gateway.sign_in.providers, "github", LoginProvider())
    code, headers, _ = request(s, "/app/auth/github", cookie=s.cookie + "; " + connect_flow[1])
    assert code == 302
    login_state = parse_qs(urlsplit(headers["Location"]).query)["state"][0]
    login_cookie = headers["Set-Cookie"].split(";", 1)[0]
    result = request(
        s,
        "/app/auth/github/callback?"
        + urlencode({"state": login_state, "code": "synthetic-login-code"}),
        cookie=s.cookie + "; " + connect_flow[1] + "; " + login_cookie,
    )
    assert result[0] == 303 and sessions.COOKIE in result[1]["Set-Cookie"]
    assert len(rows(s, "github_connect_flows")) == 1
    assert len(rows(s, "user_identities")) == 1
    assert not s.remote.calls


def test_held_grant_delete_and_late_token_cleanup_both_block_reconnect(setup, monkeypatch):
    s = setup
    connect(s)
    # Model an exchange already accepted by an older worker before the current grant committed.
    with s.db._lock:
        s.db._conn.execute(
            "UPDATE github_lifecycle SET active_exchanges=1 WHERE user_id=?", (s.uid,)
        )
        s.gateway.github._live = 1
    entered, release = Event(), Event()
    original = s.remote.request

    def remote(method, url, **kwargs):
        if method == "DELETE" and url.endswith("/grant"):
            entered.set()
            assert release.wait(5)
        return original(method, url, **kwargs)

    monkeypatch.setattr(importlib.import_module("commonplace.github.client"), "request", remote)
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(write, s, "/api/github", "DELETE")
        try:
            assert entered.wait(5)
            assert write(s)[0] == 409
            assert s.gateway.github.revoke_token(s.uid, "synthetic-late-token")
            s.gateway.github.finish_exchange(s.uid)
            assert write(s)[0] == 409
        finally:
            release.set()
        assert pending.result()[0] == 200
    assert not rows(s, "github_grants")
    deletes = [c for c in s.remote.calls if c[0] == "DELETE"]
    assert sorted(c[1].rsplit("/", 1)[1] for c in deletes) == ["grant", "token"]
    assert write(s)[0] == 200


def test_process_crash_restart_purges_without_replaying_delete(setup):
    import subprocess
    import sys

    s = setup
    connect(s)
    # Release this synthetic worker, then crash its replacement after disable-before-purge.
    s.gateway.github.close()
    program = """
import os, sys, json
from types import SimpleNamespace
from commonplace.control_db import ControlDB
from commonplace.social import accounts, providers
from commonplace.github import GitHub, client
client.request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected remote replay"))
db = ControlDB(sys.argv[1])
accounts.ensure_schema(db)
if sys.argv[2] == "restart":
    import time
    import commonplace.github as github
    from types import SimpleNamespace as NS
    now = time.time() + github.WORKER_LEASE_TTL + 1
    github.time = NS(time=lambda: now)
g = GitHub(SimpleNamespace(db=db, sign_in=SimpleNamespace(providers={p.id:p for p in providers.configured()})))
if sys.argv[2] == "crash":
    with db._lock, db._tx() as conn:
        conn.execute("UPDATE github_grants SET disabled=1")
        conn.execute("UPDATE github_lifecycle SET disconnect_pending=1,remote_work=1,generation=generation+1")
    os._exit(17)
assert not db._conn.execute("SELECT 1 FROM github_grants").fetchall()
assert not db._conn.execute("SELECT 1 FROM github_connect_flows").fetchall()
assert not db._conn.execute("SELECT 1 FROM github_confirmations").fetchall()
row=db._conn.execute("SELECT * FROM github_lifecycle").fetchone()
assert row["unconfirmed"] == 1 and row["disconnect_pending"] == row["remote_work"] == row["active_exchanges"] == 0
print(json.dumps({"recovered":True,"generation":row["generation"]}))
db.close()
"""
    crash = subprocess.run(
        [sys.executable, "-c", program, s.db.path, "crash"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert crash.returncode == 17, crash.stderr
    from types import SimpleNamespace

    from commonplace.control_db import ControlDB

    db2 = ControlDB(s.db.path)
    try:
        blocked = importlib.import_module("commonplace.github").GitHub(
            SimpleNamespace(db=db2, sign_in=s.gateway.sign_in)
        )
        assert not blocked.ready
        assert rows(s, "github_grants")[0]["disabled"] == 1
    finally:
        db2.close()
    restart = subprocess.run(
        [sys.executable, "-c", program, s.db.path, "restart"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert restart.returncode == 0, restart.stdout + restart.stderr
    assert json.loads(restart.stdout)["recovered"]
    importlib.import_module("commonplace.github").install(s.gateway)
    assert write(s)[0] == 409
    assert write(s, payload={"acknowledge": True})[0] == 200


def test_connection_never_provisions_or_enables_login(setup):
    s = setup
    before = (
        rows(s, "users"),
        rows(s, "sessions"),
        rows(s, "user_identities"),
        rows(s, "memberships"),
    )
    connect(s)
    assert before == (
        rows(s, "users"),
        rows(s, "sessions"),
        rows(s, "user_identities"),
        rows(s, "memberships"),
    )
    assert len(rows(s, "github_account_owners")) == 1


def test_failed_grant_commit_rolls_back_owner_and_deletes_issued_token(setup):
    s = setup
    finish(s, begin(s))
    assert status(s)["state"] == "confirm"
    s.db._conn.execute(
        "CREATE TRIGGER fail_grant BEFORE INSERT ON github_grants BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
    )
    assert write(s, "/api/github/confirm", payload={"accept": True})[0] == 500
    assert not rows(s, "github_grants") and not rows(s, "github_account_owners")
    assert not rows(s, "github_confirmations")
    deletes = [c for c in s.remote.calls if c[0] == "DELETE"]
    assert len(deletes) == 1 and deletes[0][1].endswith("/token")


@pytest.mark.parametrize("secure", [False, True])
def test_connect_cookie_is_callback_scoped_and_secure_when_required(setup, secure):
    s = setup
    s.gateway.secure_cookies = secure
    code, headers, _ = write(s)
    assert code == 200
    cookie = headers["Set-Cookie"]
    for part in ("Path=/app/auth/github/callback", "HttpOnly", "SameSite=Lax", "Max-Age=600"):
        assert part in cookie
    assert ("; Secure" in cookie) == secure


def test_default_off_refuses_new_connect(setup, monkeypatch):
    s = setup
    assert status(s)["state"] == "absent"
    monkeypatch.delenv("COMMONPLACE_GITHUB_CONNECT")
    assert status(s)["state"] == "unconfigured"
    assert write(s)[0] == 409
    assert not rows(s, "github_connect_flows") and not s.remote.calls


@pytest.mark.parametrize(
    "origin,allowed", [("http://remote.example", False), ("http://127.0.0.1:8080", True)]
)
def test_connect_requires_https_except_loopback_development(setup, monkeypatch, origin, allowed):
    s = setup
    status(s)
    monkeypatch.setattr(urls, "APP_URL", origin)
    assert write(s)[0] == (200 if allowed else 409)


@pytest.mark.parametrize("case", ["direct", "timeout", "size", "redirect"])
def test_client_method_json_headers_bounds_no_proxy_or_redirect(setup, monkeypatch, case):
    import io
    from urllib.error import HTTPError

    s = setup
    status(s)
    module = importlib.import_module("commonplace.github.client")
    # The installed service uses the synthetic remote; exercise the actual transport separately.
    spec = importlib.util.spec_from_file_location(
        "commonplace.github._transport_test", module.__file__
    )
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    actual = vars(transport)
    seen = {}

    class Response(io.BytesIO):
        status = 204
        headers = {"X-OAuth-Scopes": ""}

    class Opener:
        def open(self, request, timeout):
            seen.update(request=request, timeout=timeout)
            if case == "timeout":
                raise TimeoutError("synthetic-private-body")
            if case == "redirect":
                raise HTTPError(request.full_url, 302, "synthetic-private-body", {}, None)
            return Response(b"x" * (module.MAX_RESPONSE + 1) if case == "size" else b"")

    def opener(*handlers):
        seen["handlers"] = handlers
        return Opener()

    actual["request"].__globals__["build_opener"] = opener
    if case in ("timeout", "size"):
        with pytest.raises(providers.SignInError):
            actual["request"](
                "DELETE",
                "https://api.github.com/applications/synthetic-client/token",
                json_body={"access_token": "synthetic-issued-token"},
                credentials=("synthetic-client", "synthetic-app-secret"),
            )
    else:
        code, headers, data = actual["request"](
            "DELETE",
            "https://api.github.com/applications/synthetic-client/token",
            json_body={"access_token": "synthetic-issued-token"},
            credentials=("synthetic-client", "synthetic-app-secret"),
        )
        assert code == (302 if case == "redirect" else 204)
        assert data == {}
    assert seen["timeout"] == 10
    assert seen["handlers"][0].proxies == {}
    assert isinstance(seen["handlers"][1], providers._NoRedirect)
    req = seen["request"]
    assert req.method == "DELETE"
    assert json.loads(req.data) == {"access_token": "synthetic-issued-token"}
    assert req.get_header("Authorization").startswith("Basic ")
    assert req.get_header("Content-type") == "application/json"


def test_other_person_cannot_read_or_revoke_a_grant(setup):
    s = setup
    connect(s)
    uid = s.db.create_user("other@example.com")
    workspace = s.db.create_workspace("other", uid)
    s.cookie = sessions.COOKIE + "=" + sessions.start_session(s.db, uid, workspace)
    assert status(s)["state"] == "absent"
    assert write(s, "/api/github", "DELETE")[0] == 200
    assert not [call for call in s.remote.calls if call[0] == "DELETE"]
    assert len(rows(s, "github_grants")) == 1
    s.cookie = sessions.COOKIE + "=" + s.session
    assert status(s)["state"] == "connected"


def test_refresh_material_is_discarded_before_encryption(setup):
    s = setup
    finish(s, begin(s))
    assert status(s)["state"] == "confirm"
    pending = rows(s, "github_confirmations")[0]
    candidate = s.gateway.github.store.decrypt(s.db._conn, s.uid, "confirmation", pending["secret"])
    assert set(candidate) == {"token", "subject", "login", "scopes", "expires_at"}
    assert "synthetic-refresh" not in repr(candidate)
    assert write(s, "/api/github/confirm", payload={"accept": True})[0] == 200
    grant = rows(s, "github_grants")[0]
    assert s.gateway.github.store.decrypt(s.db._conn, s.uid, "grant", grant["secret"]) == {
        "token": "synthetic-issued-token"
    }


def test_expired_grant_reconnects_without_disconnect_or_acknowledgement(setup):
    s = setup
    connect(s)
    before_calls = list(s.remote.calls)
    generation = rows(s, "github_lifecycle")[0]["generation"]
    s.db._conn.execute("UPDATE github_grants SET expires_at=0")
    assert status(s)["state"] == "expired"
    assert status(s)["acknowledge_required"] is False
    code, _, raw = write(s)
    assert code == 200, raw
    assert not rows(s, "github_grants")
    assert len(rows(s, "github_connect_flows")) == 1
    assert rows(s, "github_lifecycle")[0]["generation"] == generation + 1
    assert status(s)["acknowledge_required"] is False
    assert s.remote.calls == before_calls


def test_repeated_scope_headers_all_cross_checked(setup, monkeypatch):
    from email.parser import Parser

    s = setup
    status(s)
    original = s.remote.request

    def remote(method, url, **kwargs):
        code, headers, value = original(method, url, **kwargs)
        if url.endswith("/user"):
            headers = Parser().parsestr("X-OAuth-Scopes: \nX-OAuth-Scopes: repo\n\n")
        return code, headers, value

    monkeypatch.setattr(importlib.import_module("commonplace.github.client"), "request", remote)
    finish(s, begin(s))
    assert status(s)["state"] == "absent"
    assert len([c for c in s.remote.calls if c[0] == "DELETE"]) == 1


def test_callback_card_recheck_with_live_membership(setup):
    s = setup
    flow = begin(s)
    principal = sessions.session_principal(s.db, s.session)
    assert principal is not None
    # Model a stale reference after a board restore without changing session membership.
    s.db._conn.execute("UPDATE github_connect_flows SET card=99999")
    assert sessions.session_principal(s.db, s.session) is not None
    finish(s, flow)
    assert not s.remote.calls and not rows(s, "github_grants")


def test_callback_returns_to_selected_needs_consent(setup):
    s = setup
    response = finish(s, begin(s))
    location = urlsplit(response[1]["Location"])
    assert location.fragment == "/needs"
    assert parse_qs(location.query)["github_card"] == [str(s.card)]
    assert status(s)["state"] == "confirm"


def test_disabled_feature_keeps_existing_grant_revocable(setup, monkeypatch):
    s = setup
    connect(s)
    monkeypatch.delenv("COMMONPLACE_GITHUB_CONNECT")
    assert status(s)["state"] == "connected"
    assert status(s)["connect_enabled"] is False
    assert write(s, "/api/github", "DELETE")[0] == 200
    assert not rows(s, "github_grants")
    assert len([c for c in s.remote.calls if c[0] == "DELETE"]) == 1


@pytest.mark.parametrize("kind", ["grant", "confirmation"])
@pytest.mark.parametrize("damage", ["tamper", "missing-key"])
def test_disconnect_purges_unreadable_credentials(setup, monkeypatch, kind, damage):
    s = setup
    if kind == "grant":
        connect(s)
    else:
        finish(s, begin(s))
        assert status(s)["state"] == "confirm"
    initial = status(s)
    generation = rows(s, "github_lifecycle")[0]["generation"]
    if damage == "tamper":
        table = "github_grants" if kind == "grant" else "github_confirmations"
        s.db._conn.execute(f"UPDATE {table} SET secret=?", (b"tampered",))
    else:
        monkeypatch.delenv("COMMONPLACE_GITHUB_KEY_FILE")

    def disconnect():
        return request(
            s,
            "/api/github",
            "DELETE",
            cookie=s.cookie,
            raw=json.dumps({"csrf": initial["csrf"]}).encode(),
            headers={"Content-Type": "application/json", "Origin": urls.APP_URL},
        )

    code, _, raw = disconnect()
    assert code == 200, raw
    assert "unconfirmed" in json.loads(raw)["message"].lower()
    assert not rows(s, "github_grants") and not rows(s, "github_confirmations")
    assert not rows(s, "github_connect_flows")
    life = rows(s, "github_lifecycle")[0]
    assert life["generation"] == generation + 1 and life["unconfirmed"] == 1
    assert life["disconnect_pending"] == life["active_exchanges"] == life["remote_work"] == 0
    assert status(s)["acknowledge_required"] is True
    assert disconnect()[0] == 200
    assert not [c for c in s.remote.calls if c[0] == "DELETE"]


def test_unreadable_prior_grant_marks_revocation_unconfirmed(setup):
    s = setup
    connect(s)
    s.db._conn.execute("UPDATE github_grants SET secret=?", (b"tampered",))
    assert s.gateway.github.revoke_token(s.uid, "synthetic-late-token") is False
    assert rows(s, "github_lifecycle")[0]["unconfirmed"] == 1
    assert not [c for c in s.remote.calls if c[0] == "DELETE"]


@pytest.mark.parametrize("phase", ["callback", "confirm", "expiry"])
def test_cleanup_failure_always_settles_exchange_counter(setup, monkeypatch, phase):
    import sqlite3

    s = setup
    status(s)

    def broken(*args):
        raise sqlite3.OperationalError("synthetic cleanup failure")

    if phase == "callback":
        s.remote.scope = "repo"
        flow = begin(s)
        monkeypatch.setattr(s.gateway.github, "revoke_token", broken)
        assert finish(s, flow)[0] == 500
    else:
        finish(s, begin(s))
        assert status(s)["state"] == "confirm"
        monkeypatch.setattr(s.gateway.github, "revoke_token", broken)
        if phase == "confirm":
            assert write(s, "/api/github/confirm", payload={"accept": False})[0] == 500
        else:
            s.db._conn.execute("UPDATE github_confirmations SET expires_at=0")
            assert write(s)[0] == 500
    assert rows(s, "github_lifecycle")[0]["active_exchanges"] == 0
    assert rows(s, "github_lifecycle")[0]["unconfirmed"] == 1
    assert s.gateway.github._live == 0


def test_confirm_fence_rechecked_after_consumption(setup, monkeypatch):
    s = setup
    finish(s, begin(s))
    assert status(s)["state"] == "confirm"
    original_tx = s.db._tx
    count = 0

    def transaction():
        nonlocal count
        count += 1
        if count == 2:
            assert not rows(s, "github_confirmations")
            s.gateway.github.disconnect(s.uid, s.workspace)
        return original_tx()

    monkeypatch.setattr(s.db, "_tx", transaction)
    response = write(s, "/api/github/confirm", payload={"accept": True})
    assert response[0] == 409
    assert not rows(s, "github_grants")
    deletes = [c for c in s.remote.calls if c[0] == "DELETE"]
    assert len(deletes) == 1 and deletes[0][1].endswith("/token")


@pytest.mark.parametrize("target", ["person", "kind"])
def test_aad_binds_valid_ciphertext_with_same_key(setup, target):
    s = setup
    status(s)
    module = importlib.import_module("commonplace.github.store")
    key = b"k" * 32
    raw = module.seal(key, s.uid, "grant", b"synthetic-payload")
    assert module.open_secret(key, s.uid, "grant", raw) == b"synthetic-payload"
    with pytest.raises(providers.SignInError):
        module.open_secret(
            key,
            "other-person" if target == "person" else s.uid,
            "confirmation" if target == "kind" else "grant",
            raw,
        )


@pytest.mark.parametrize("problem", ["location", "owner"])
def test_wrapping_key_location_and_owner_fail_closed(setup, monkeypatch, problem):
    import os
    from pathlib import Path

    s = setup
    status(s)
    if problem == "location":
        key = Path(s.db.path).parent / "synthetic.key"
        key.write_bytes(b"k" * 32)
        key.chmod(0o600)
        monkeypatch.setenv("COMMONPLACE_GITHUB_KEY_FILE", str(key))
    else:
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    assert write(s)[0] == 409
    assert not rows(s, "github_connect_flows")


def test_transport_preserves_repeated_scope_evidence(setup):
    import io
    from email.parser import Parser

    s = setup
    status(s)
    module = importlib.import_module("commonplace.github.client")
    spec = importlib.util.spec_from_file_location(
        "commonplace.github._header_test", module.__file__
    )
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)

    class Response(io.BytesIO):
        status = 200
        headers = Parser().parsestr("X-OAuth-Scopes: \nX-OAuth-Scopes: repo\n\n")

    class Opener:
        def open(self, *args, **kwargs):
            return Response(b"{}")

    transport.build_opener = lambda *args: Opener()
    _, headers, _ = transport.request(
        "GET", "https://api.github.com/user", token="synthetic-issued-token"
    )
    assert hasattr(headers, "get_all")
    assert headers.get_all("X-OAuth-Scopes") == ["", "repo"]


def test_token_scope_evidence_required_without_optional_header(setup, monkeypatch):
    s = setup
    status(s)
    s.remote.scope = None
    original = s.remote.request

    def remote(method, url, **kwargs):
        code, headers, data = original(method, url, **kwargs)
        return code, {} if url.endswith("/user") else headers, data

    monkeypatch.setattr(importlib.import_module("commonplace.github.client"), "request", remote)
    finish(s, begin(s))
    assert status(s)["state"] == "absent"
    deletes = [c for c in s.remote.calls if c[0] == "DELETE"]
    assert len(deletes) == 1 and deletes[0][1].endswith("/token")


def test_worker_lease_heartbeat_and_expired_owner_fail_closed(setup):
    s = setup
    worker = s.gateway.github
    assert worker.ready
    with s.db._lock, s.db._tx() as conn:
        conn.execute("UPDATE github_worker_lease SET expires_at=expires_at-10")
        before = conn.execute("SELECT expires_at FROM github_worker_lease").fetchone()[0]
        worker._fence(conn)
        assert conn.execute("SELECT expires_at FROM github_worker_lease").fetchone()[0] > before
        conn.execute("UPDATE github_worker_lease SET expires_at=0")
    assert not worker.ready
    with pytest.raises(providers.SignInError, match="github_pending"):
        worker.recover()
    assert write(s, "/api/github", "DELETE")[0] == 409


def test_separate_runtime_roots_share_volume_recovery_fence(setup, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from commonplace.control_db import ControlDB

    s = setup
    module = importlib.import_module("commonplace.github")
    s.gateway.github.close()
    module.install(s.gateway)
    begin(s)
    s.db._conn.execute(
        "UPDATE github_lifecycle SET disconnect_pending=1,active_exchanges=1,remote_work=1"
    )
    before = {
        table: rows(s, table)
        for table in ("github_connect_flows", "github_confirmations", "github_lifecycle")
    }
    db2 = ControlDB(s.db.path)
    try:
        second = module.GitHub(SimpleNamespace(db=db2, sign_in=s.gateway.sign_in))
        assert before == {table: rows(s, table) for table in before}, "live state recovered"
        assert not second.ready, "second serve must not claim exclusive ownership"
        with pytest.raises(providers.SignInError, match="github_pending"):
            second.recover()
        assert s.gateway.github.ready
    finally:
        db2.close()


def test_worker_heartbeat_renews_and_stale_completion_cannot_touch_successor(setup, monkeypatch):
    from types import SimpleNamespace

    from commonplace.control_db import ControlDB

    s = setup
    module = importlib.import_module("commonplace.github")
    s.gateway.github.close()
    monkeypatch.setattr(module, "WORKER_HEARTBEAT", 0.02)
    renewed = Event()
    fence = module.GitHub._fence

    def observed_fence(worker, conn):
        fence(worker, conn)
        renewed.set()

    monkeypatch.setattr(module.GitHub, "_fence", observed_fence)
    module.install(s.gateway)
    assert renewed.wait(3), "background heartbeat did not renew"
    old = s.gateway.github
    # Stop only the heartbeat to model a suspended holder, leaving its lease.
    old._stopped.set()
    old._heartbeat_thread.join()
    s.db._conn.execute("UPDATE github_worker_lease SET expires_at=0")
    db2 = ControlDB(s.db.path)
    try:
        successor = module.GitHub(SimpleNamespace(db=db2, sign_in=s.gateway.sign_in))
        assert successor.ready and not old.ready
        s.db._conn.execute(
            "INSERT OR REPLACE INTO github_lifecycle(user_id,active_exchanges,remote_work) VALUES (?,2,1)",
            (s.uid,),
        )
        before = rows(s, "github_lifecycle")
        with pytest.raises(providers.SignInError, match="github_pending"):
            old.finish_exchange(s.uid)
        assert rows(s, "github_lifecycle") == before
        old.close()
        assert successor.ready, "old close must not release a successor's lease"
    finally:
        db2.close()


@pytest.mark.parametrize("entry", ["server", "worker"])
def test_shutdown_releases_lease_once_before_database_close(setup, entry):
    s = setup
    worker = s.gateway.github
    assert worker.ready
    statements = []
    s.db._conn.set_trace_callback(statements.append)
    close = s.server.server_close if entry == "server" else worker.close
    close()
    assert worker._stopped.is_set()
    assert not worker._heartbeat_thread.is_alive()
    assert not s.db._conn.execute("SELECT 1 FROM github_worker_lease").fetchone()
    worker.close()
    assert sum(sql.startswith("DELETE FROM github_worker_lease") for sql in statements) == 1
    s.db.close()
    # A component already closed by the server must remain safe after SQLite closes.
    worker.close()
    s.server.server_close()
    assert sum(sql.startswith("DELETE FROM github_worker_lease") for sql in statements) == 1


def test_clean_serve_shutdown_allows_immediate_restart(setup, monkeypatch, tmp_path):
    import os
    import subprocess
    import sys
    from types import SimpleNamespace

    from commonplace.control_db import ControlDB

    s = setup
    path = s.db.path
    s.gateway.github.close()
    # A real serve process takes SIGTERM through Gateway.serve's installed
    # handler and cmd_serve's teardown; no test-side close releases its lease.
    script = """
import signal
import sys
from types import SimpleNamespace
from commonplace import cli
from commonplace.gateway import Gateway
from commonplace.control_db import ControlDB
from commonplace.rules import checks
from commonplace.backup_pause import PauseServer
checks.deploy = lambda gateway: None
PauseServer.start = lambda self: True
PauseServer.stop = lambda self: None
class Server:
    server_address = ('127.0.0.1', 0)
    def serve_forever(self):
        signal.raise_signal(signal.SIGTERM)
    def server_close(self):
        pass
def build(gateway, *args):
    assert gateway.github.ready
    gateway.db._conn.execute('UPDATE github_lifecycle SET active_exchanges=1,unconfirmed=0')
    return Server()
Gateway.build_server = build
cli._db = lambda args: ControlDB(sys.argv[1])
cli.cmd_serve(SimpleNamespace(data=sys.argv[2],idle_minutes=1,host='127.0.0.1',port=0))
"""
    # Ensure there is state for startup recovery to reconcile after termination.
    s.db._conn.execute("INSERT OR IGNORE INTO github_lifecycle(user_id) VALUES (?)", (s.uid,))
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), str(tmp_path)],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    db2 = ControlDB(path)
    try:
        worker = importlib.import_module("commonplace.github").GitHub(
            SimpleNamespace(db=db2, sign_in=s.gateway.sign_in)
        )
        assert worker.ready, "clean serve shutdown retained its lease"
        assert not db2._conn.execute("SELECT 1 FROM github_connect_flows").fetchone()
        life = db2._conn.execute(
            "SELECT * FROM github_lifecycle WHERE user_id=?", (s.uid,)
        ).fetchone()
        assert life["unconfirmed"] == 1
        assert life["active_exchanges"] == life["remote_work"] == 0
    finally:
        db2.close()


@pytest.mark.parametrize("release", ["close", "expiry"])
def test_standby_heartbeat_retries_after_owner_release(setup, monkeypatch, release):
    from threading import Thread
    from types import SimpleNamespace

    from commonplace.control_db import ControlDB

    s = setup
    module = importlib.import_module("commonplace.github")
    now = module.time.time()
    monkeypatch.setattr(module.time, "time", lambda: now)
    begin(s)
    first = s.gateway.github
    first._stopped.set()
    first._heartbeat_thread.join()
    first._stopped.clear()
    before = rows(s, "github_connect_flows")
    db2 = ControlDB(s.db.path)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Thread, "start", lambda self: None)
            second = module.GitHub(SimpleNamespace(db=db2, sign_in=s.gateway.sign_in))

        # Drive actual heartbeat iterations without sleeps or background scheduling.
        def tick():
            turns = iter([False, True])
            with monkeypatch.context() as patch:
                patch.setattr(second._stopped, "wait", lambda timeout: next(turns))
                second._heartbeat()

        assert not second.ready
        tick()
        assert not second.ready
        assert rows(s, "github_connect_flows") == before, "live owner must block recovery"
        if release == "close":
            first.close()
        else:
            now += module.WORKER_LEASE_TTL + 1
        tick()
        assert second.ready, "standby never retried lease acquisition"
        assert not rows(s, "github_connect_flows")
        assert rows(s, "github_lifecycle")[0]["unconfirmed"] == 1
        first.close()
        assert second.ready, "former owner must not delete successor lease"
    finally:
        # The synthetic heartbeat thread was deliberately never started.
        second._heartbeat_thread = None
        db2.close()
