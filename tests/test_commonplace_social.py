"""Provider sign-in through a real gateway, with every remote request faked."""

import base64
import http.client
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
from threading import Barrier
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from commonplace import plans, sessions, social, urls
from commonplace.oauth import store
from commonplace.social import accounts, providers
from tests.test_commonplace_oauth import Client
from tests.test_commonplace_public_surface import make_surface as make_surface

IDS = ("google", "github", "apple", "x", "discord")
NAMES = ("Google", "GitHub", "Apple", "X", "Discord")
AUTHORIZE = (
    "https://accounts.google.com/o/oauth2/v2/auth",
    "https://github.com/login/oauth/authorize",
    "https://appleid.apple.com/auth/authorize",
    "https://x.com/i/oauth2/authorize",
    "https://discord.com/oauth2/authorize",
)
TOKENS = (
    "https://oauth2.googleapis.com/token",
    "https://github.com/login/oauth/access_token",
    "https://appleid.apple.com/auth/token",
    "https://api.x.com/2/oauth2/token",
    "https://discord.com/api/oauth2/token",
)
SCOPES = (
    "openid email profile",
    "read:user user:email",
    "name email",
    "tweet.read users.read users.email",
    "identify email",
)
_FETCH = providers.fetch


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def jwt(claims):
    return b64(b'{"alg":"RS256"}') + "." + b64(json.dumps(claims).encode()) + ".test-signature"


def decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.fixture
def apple_key():
    return ec.generate_private_key(ec.SECP256R1())


def pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def enable(monkeypatch, provider_id, key=None):
    values = {"CLIENT_ID": "test-client-id", "CLIENT_SECRET": "test-client-secret"}
    if provider_id == "apple":
        values = {
            "CLIENT_ID": "test-client-id",
            "TEAM_ID": "test-team-id",
            "KEY_ID": "test-key-id",
            "PRIVATE_KEY": pem(key),
        }
    for name, value in values.items():
        monkeypatch.setenv(f"COMMONPLACE_{provider_id.upper()}_{name}", value)


class Remote:
    def __init__(self):
        self.calls = []
        self.nonce = ""
        self.claims = {}
        self.token = None
        self.failure = False
        self.github = {"id": 123, "name": "Test Person", "login": "test-person"}
        self.emails = [{"email": "owner@example.com", "verified": True, "primary": True}]
        self.x = {
            "data": {
                "id": "test-x-subject",
                "name": "Test Person",
                "confirmed_email": "owner@example.com",
            }
        }
        self.discord = {
            "id": "test-discord-subject",
            "global_name": "Test Person",
            "username": "test-person",
            "email": "owner@example.com",
            "verified": True,
        }

    def fetch(self, url, *, form=None, headers=None):
        self.calls.append((url, form, headers))
        if self.failure:
            raise providers.SignInError("failed")
        if url in TOKENS:
            issuer = (
                "https://appleid.apple.com" if url == TOKENS[2] else "https://accounts.google.com"
            )
            return (
                self.token
                if self.token is not None
                else {
                    "access_token": "test-access-token",
                    "id_token": jwt(
                        {
                            "iss": issuer,
                            "aud": "test-client-id",
                            "exp": int(time.time()) + 600,
                            "nonce": self.nonce,
                            "sub": "test-oidc-subject",
                            "email": "owner@example.com",
                            "email_verified": True,
                            "name": "Test Person",
                            **self.claims,
                        }
                    ),
                }
            )
        return {
            "https://api.github.com/user": self.github,
            "https://api.github.com/user/emails": self.emails,
            "https://api.x.com/2/users/me?user.fields=confirmed_email": self.x,
            "https://discord.com/api/v10/users/@me": self.discord,
        }[url]


@pytest.fixture(autouse=True)
def remote(monkeypatch):
    for provider_id in IDS:
        for key in ("CLIENT_ID", "CLIENT_SECRET", "TEAM_ID", "KEY_ID", "PRIVATE_KEY"):
            monkeypatch.delenv(f"COMMONPLACE_{provider_id.upper()}_{key}", raising=False)
    monkeypatch.delenv("COMMONPLACE_SIGNUP", raising=False)
    monkeypatch.delenv("COMMONPLACE_X_DAILY_CAP", raising=False)
    monkeypatch.setattr(urls, "APP_URL", "https://app.example")
    monkeypatch.setattr(urls, "AGENT_URL", "https://agent.example")
    remote = Remote()
    monkeypatch.setattr(providers, "fetch", remote.fetch)
    # Accidentally bypassing the fake must never make a network request.
    monkeypatch.setattr(providers, "build_opener", lambda *a: pytest.fail("unexpected network"))
    return remote


@pytest.fixture
def make_signin(make_surface, monkeypatch, apple_key):
    def make(ids=("google",), *, signup=None):
        for provider_id in ids:
            enable(monkeypatch, provider_id, apple_key)
        if signup is not None:
            monkeypatch.setenv("COMMONPLACE_SIGNUP", " OPEN " if signup else "closed")
        surface = make_surface()
        surface.gateway.rate_limits = False
        return surface

    return make


def request(surface, path, method="GET", *, data=None, cookie=None, raw=None, headers=None):
    hdrs = dict(headers or {})
    if cookie:
        hdrs["Cookie"] = cookie
    if data is not None:
        raw = urlencode(data).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    conn = http.client.HTTPConnection("127.0.0.1", surface.port, timeout=5)
    try:
        conn.request(method, path, raw, hdrs)
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


def start(surface, remote, provider_id="google", *, next_path="", cookie=None):
    path = f"/app/auth/{provider_id}"
    if next_path:
        path += "?" + urlencode({"next": next_path})
    status, headers, _ = request(surface, path, cookie=cookie)
    assert status == 302
    params = {key: value[0] for key, value in parse_qs(urlsplit(headers["Location"]).query).items()}
    remote.nonce = params.get("nonce", "")
    return SimpleNamespace(
        params=params,
        cookie=headers["Set-Cookie"].split(";", 1)[0],
        headers=headers,
        id=provider_id,
    )


def finish(surface, flow, *, cookie=None, **parameters):
    parameters = {"state": flow.params["state"], "code": "test-code", **parameters}
    path = f"/app/auth/{flow.id}/callback"
    if flow.id == "apple":
        return request(surface, path, "POST", data=parameters, cookie=cookie or flow.cookie)
    return request(surface, path + "?" + urlencode(parameters), cookie=cookie or flow.cookie)


def refused(response, code, next_path=""):
    status, headers, raw = response
    assert status == 303, raw
    location = urlsplit(headers["Location"])
    assert location.path == "/app/"
    assert parse_qs(location.query) == {
        "signin": [code],
        **({"next": [next_path]} if next_path else {}),
    }
    assert "Set-Cookie" not in headers


def rows(surface, table):
    with surface.db._lock:
        return [dict(row) for row in surface.db._conn.execute("SELECT * FROM " + table)]


def signed_in(surface, response, uid=None, landing="/app/"):
    status, headers, raw = response
    assert status == 303, raw
    assert headers["Location"] == landing
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    assert cookie.startswith(sessions.COOKIE + "=")
    status, _, raw = request(surface, "/api/me", cookie=cookie)
    assert status == 200, raw
    me = json.loads(raw)
    assert me["user"] == (uid or surface.uid)
    return me


def test_no_configuration_preserves_token_signin(make_signin):
    surface = make_signin(())
    status, headers, raw = request(surface, "/app/auth/providers")
    assert status == 200 and json.loads(raw) == {"providers": [], "paused": []}
    assert headers["Cache-Control"] == "no-store"
    for provider_id in IDS:
        assert request(surface, f"/app/auth/{provider_id}")[0] == 404
        assert (
            request(
                surface, f"/app/auth/{provider_id}/callback", "POST", data={"code": "test-code"}
            )[0]
            == 404
        )
    status, _, raw = request(surface, "/app/")
    assert status == 200 and b'class="provider"' not in raw
    assert surface.gateway.sign_in.links() == []
    oauth = Client(surface)
    cid = oauth.register()
    assert 'class="provider"' not in oauth.request("GET", oauth.authorize_path(cid))[2]


@pytest.mark.parametrize("provider_id", IDS)
def test_partial_configuration_is_hidden(make_signin, monkeypatch, caplog, provider_id):
    monkeypatch.setenv(f"COMMONPLACE_{provider_id.upper()}_CLIENT_ID", "test-client-id")
    surface = make_signin(())
    assert json.loads(request(surface, "/app/auth/providers")[2]) == {"providers": [], "paused": []}
    assert request(surface, f"/app/auth/{provider_id}")[0] == 404
    assert len(caplog.records) == 1
    assert f"COMMONPLACE_{provider_id.upper()}_" in caplog.text
    assert "test-client-id" not in caplog.text


def test_provider_inventory_order_and_install_is_idempotent(make_signin):
    surface = make_signin(IDS)
    assert json.loads(request(surface, "/app/auth/providers")[2]) == {
        "providers": [
            {"id": pid, "name": name, "url": f"https://app.example/app/auth/{pid}"}
            for pid, name in zip(IDS, NAMES)
        ],
        "paused": [],
    }
    before = list(surface.gateway.prefix_routes)
    social.install(surface.gateway)
    assert surface.gateway.prefix_routes == before


@pytest.mark.parametrize("provider_id", IDS)
@pytest.mark.parametrize("secure", [False, True])
def test_start_redirect_cookie_and_server_side_flow(make_signin, remote, provider_id, secure):
    surface = make_signin((provider_id,))
    surface.gateway.secure_cookies = secure
    # Read canonical URLs at call time, not when providers are configured.
    flow = start(surface, remote, provider_id)
    params = flow.params
    index = IDS.index(provider_id)
    assert flow.headers["Location"].split("?")[0] == AUTHORIZE[index]
    assert params["response_type"] == "code"
    assert params["client_id"] == "test-client-id"
    assert params["redirect_uri"] == f"https://app.example/app/auth/{provider_id}/callback"
    assert params["scope"] == SCOPES[index]
    assert "%20" in flow.headers["Location"] and "+" not in flow.headers["Location"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", params["state"])
    [row] = rows(surface, "signin_flows")
    assert row["state_hash"] == store.digest(params["state"])
    assert row["browser_hash"] == store.digest(flow.cookie.split("=", 1)[1])
    assert row["provider"] == provider_id and row["next"] == ""
    assert 590 <= row["expires_at"] - time.time() <= 600
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", row["verifier"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", row["nonce"])
    if provider_id != "apple":
        assert params["code_challenge"] == store.challenge(row["verifier"])
        assert params["code_challenge_method"] == "S256"
    else:
        assert "code_challenge" not in params and "code_challenge_method" not in params
        assert params["response_mode"] == "form_post"
    if provider_id in ("google", "apple"):
        assert params["nonce"] == row["nonce"]
    else:
        assert "nonce" not in params
    if provider_id == "google":
        assert params["prompt"] == "select_account"
    cookie = flow.headers["Set-Cookie"]
    assert "Path=/app/auth/" in cookie and "HttpOnly" in cookie and "Max-Age=600" in cookie
    assert ("SameSite=None; Secure" if secure else "SameSite=Lax") in cookie
    assert ("Secure" in cookie) == secure
    assert "Domain=" not in cookie
    assert not remote.calls


def test_start_reuses_valid_binding_and_prunes_expired_flows(make_signin, remote):
    surface = make_signin()
    first = start(surface, remote, cookie="cmmn_signin=invalid")
    with surface.db._lock:
        surface.db._conn.execute("UPDATE signin_flows SET expires_at = 0")
    second = start(surface, remote, cookie=first.cookie)
    assert first.cookie == second.cookie
    assert first.params["state"] != second.params["state"]
    assert len(rows(surface, "signin_flows")) == 1
    third = start(surface, remote, cookie="cmmn_signin=" + "!" * 43)
    assert third.cookie != "cmmn_signin=" + "!" * 43


@pytest.mark.parametrize(
    "next_path",
    [
        "/app/",
        "/app/#/card/123",
        "/app/boards/work?view=mine",
        "/oauth/authorize?client_id=test&state=a + b",
        "/app/" + "a" * 4091,
    ],
)
def test_next_accepts_app_and_authorization_paths(make_signin, remote, next_path):
    surface = make_signin()
    flow = start(surface, remote, next_path=next_path)
    assert rows(surface, "signin_flows")[0]["next"] == next_path
    refused(finish(surface, flow, error="access_denied"), "cancelled", next_path)


@pytest.mark.parametrize(
    "next_path",
    [
        "https://evil.example/app/",
        "//evil.example/app/",
        "/%2fexample",
        "/app/\\evil",
        "/app/%0d%0aLocation:evil",
        "/app/auth/google",
        "/app/auth/apple/callback",
        "/app/login?t=test",
        "/app/logout",
        "/oauth/authorize",
        "/oauth/token?x=y",
        "/other",
        "app/",
        "/app/" + "a" * 4092,
    ],
)
def test_next_rejects_other_and_unsafe_paths(make_signin, remote, next_path):
    surface = make_signin()
    flow = start(surface, remote, next_path=next_path)
    assert rows(surface, "signin_flows")[0]["next"] == ""
    refused(finish(surface, flow, error="access_denied"), "cancelled")


@pytest.mark.parametrize("signup", [None, "", "closed", " CLOSED ", "maybe"])
def test_closed_signup_refuses_unknown_email_without_creating_anything(
    make_signin, remote, monkeypatch, caplog, signup
):
    if signup is not None:
        monkeypatch.setenv("COMMONPLACE_SIGNUP", signup)
    surface = make_signin()
    social.install(surface.gateway)
    remote.claims["email"] = "new@example.com"
    refused(finish(surface, start(surface, remote)), "account")
    assert len(rows(surface, "users")) == len(rows(surface, "workspaces")) == 1
    assert rows(surface, "user_identities") == rows(surface, "sessions") == []
    audit = surface.db.audit_entries()[0]
    assert (audit["action"], audit["decision"], audit["reason"]) == (
        "signin.refuse",
        "deny",
        "google:account",
    )
    if signup == "maybe":
        [warning] = caplog.records
        assert warning.levelname == "WARNING"
        assert "COMMONPLACE_SIGNUP" in warning.message
        assert "open" in warning.message and "closed" in warning.message
        assert signup not in warning.message
    else:
        assert not caplog.records


@pytest.mark.parametrize("signup", ["open", " OPEN "])
def test_open_signup_creates_user_personal_workspace_identity_and_session(
    make_signin, remote, monkeypatch, signup
):
    if signup is not None:
        monkeypatch.setenv("COMMONPLACE_SIGNUP", signup)
    calls = []

    def assign_signup_plan(db, uid, workspace_id, *, via):
        assert db.user(uid)["id"] == uid
        assert db.workspace(workspace_id)["personal_of"] == uid
        calls.append((db, uid, workspace_id, via))

    monkeypatch.setattr(plans, "assign_signup_plan", assign_signup_plan)
    surface = make_signin()
    remote.claims.update(email="new@example.com", name="  Test\tPerson\x00\n " + "x" * 120)
    response = finish(surface, start(surface, remote))
    user = surface.db.user("new@example.com")
    assert user["name"] == ("Test Person " + "x" * 120)[:100]
    me = signed_in(surface, response, user["id"])
    workspace = surface.db.workspace(me["workspace"])
    assert workspace["name"] == "new@example.com" and workspace["personal_of"] == user["id"]
    assert calls == [(surface.db, user["id"], workspace["id"], "google")]
    assert surface.db.role(workspace["id"], user["id"]) == "owner"
    [identity] = rows(surface, "user_identities")
    assert identity["provider"] == "google" and identity["subject"] == "test-oidc-subject"
    assert identity["user_id"] == user["id"]
    assert set(identity) == {"provider", "subject", "user_id", "created_at", "last_used_at"}
    audits = surface.db.audit_entries()
    assert {a["action"] for a in audits} >= {"user.create", "identity.link", "session.create"}
    assert next(a for a in audits if a["action"] == "user.create")["reason"] == "google"
    assert next(a for a in audits if a["action"] == "identity.link")["reason"] == "google"
    assert next(a for a in audits if a["action"] == "session.create")["reason"] == "google"
    assert rows(surface, "signin_flows") == []
    signed_in(surface, finish(surface, start(surface, remote)), user["id"])
    assert calls == [(surface.db, user["id"], workspace["id"], "google")]


def test_verified_email_links_case_insensitively_then_subject_survives_email_change(
    make_signin, remote, monkeypatch
):
    monkeypatch.setattr(
        plans, "assign_signup_plan", lambda *a, **kw: pytest.fail("unexpected plan")
    )
    surface = make_signin(signup=False)
    remote.claims["email"] = "OWNER@EXAMPLE.COM"
    signed_in(surface, finish(surface, start(surface, remote)))
    assert len(rows(surface, "users")) == len(rows(surface, "user_identities")) == 1
    with surface.db._lock:
        surface.db._conn.execute(
            "UPDATE users SET email='changed@example.com' WHERE id = ?", (surface.uid,)
        )
        surface.db._conn.execute("UPDATE user_identities SET last_used_at='2000-01-01T00:00:00Z'")
    remote.claims.update(email="unrelated@example.com", email_verified=False)
    signed_in(surface, finish(surface, start(surface, remote)))
    assert rows(surface, "user_identities")[0]["last_used_at"] != "2000-01-01T00:00:00Z"
    assert len(rows(surface, "users")) == 1


@pytest.mark.parametrize("signup", [False, True])
@pytest.mark.parametrize(
    "changes",
    [
        {"email_verified": False},
        {"email_verified": "true"},
        {"email_verified": 1},
        {"email": None},
        {"email": ""},
        {"email": "invalid"},
        {"email_verified": None},
    ],
)
def test_unverified_or_missing_email_never_links_or_creates(make_signin, remote, signup, changes):
    surface = make_signin(signup=signup)
    remote.claims.update(changes)
    before = rows(surface, "users")
    refused(finish(surface, start(surface, remote)), "email")
    assert rows(surface, "users") == before
    assert rows(surface, "user_identities") == rows(surface, "sessions") == []
    assert surface.db.audit_entries()[0]["reason"] == "google:email"


@pytest.mark.parametrize("known", [False, True])
def test_disabled_user_is_refused_by_email_and_subject(make_signin, remote, known):
    surface = make_signin()
    if known:
        signed_in(surface, finish(surface, start(surface, remote)))
        remote.claims.update(email=None, email_verified=False)
    with surface.db._lock:
        surface.db._conn.execute("UPDATE users SET disabled_at='2026-01-01T00:00:00Z'")
    refused(finish(surface, start(surface, remote)), "disabled")
    assert len(rows(surface, "sessions")) == int(known)
    assert surface.db.audit_entries()[0]["reason"] == "google:disabled"


def test_user_without_workspace_is_refused(make_signin, remote):
    surface = make_signin()
    with surface.db._lock:
        surface.db._conn.execute("DELETE FROM memberships")
    refused(finish(surface, start(surface, remote)), "disabled")
    assert rows(surface, "sessions") == []
    assert surface.db.audit_entries()[0]["reason"] == "google:disabled"


@pytest.mark.parametrize(
    "problem", ["missing", "wrong-browser", "no-browser", "expired", "wrong-provider", "replay"]
)
def test_invalid_flow_is_consumed_and_never_exchanges_code(make_signin, remote, problem):
    surface = make_signin(("google", "github"))
    next_path = "/app/#/card/123"
    flow = start(surface, remote, next_path=next_path)
    if problem == "expired":
        with surface.db._lock:
            surface.db._conn.execute("UPDATE signin_flows SET expires_at = ?", (int(time.time()),))
    if problem == "missing":
        flow.params["state"] = "unknown"
    if problem == "wrong-provider":
        flow.id = "github"
    if problem == "replay":
        signed_in(surface, finish(surface, flow), landing=next_path)
        remote.calls.clear()
    cookie = (
        "cmmn_signin=" + "a" * 43
        if problem == "wrong-browser"
        else "unrelated=value"
        if problem == "no-browser"
        else None
    )
    refused(
        finish(surface, flow, cookie=cookie), "expired", next_path if problem == "expired" else ""
    )
    assert remote.calls == []
    audits = [a for a in surface.db.audit_entries() if a["action"] == "signin.refuse"]
    assert [a["reason"] for a in audits] == (["google:expired"] if problem == "expired" else [])
    if problem != "missing":
        refused(finish(surface, flow), "expired")
        assert [a for a in surface.db.audit_entries() if a["action"] == "signin.refuse"] == audits


@pytest.mark.parametrize(
    "error,code",
    [
        ("access_denied", "cancelled"),
        ("user_cancelled_authorize", "cancelled"),
        ("server_error", "failed"),
        ("<script>private@example.com</script>", "failed"),
        ("", "failed"),
    ],
)
def test_provider_errors_are_fixed_messages_and_audited(make_signin, remote, caplog, error, code):
    surface = make_signin()
    refused(
        finish(
            surface,
            start(surface, remote),
            error=error,
            error_description="test-private-description",
        ),
        code,
    )
    assert not remote.calls
    audit = surface.db.audit_entries()[0]
    assert audit["reason"] == "google:" + code
    assert "test-private-description" not in caplog.text + repr(dict(audit))
    assert "private@example.com" not in caplog.text + repr(dict(audit))


@pytest.mark.parametrize("provider_id", IDS)
@pytest.mark.parametrize(
    "token",
    [
        {"error": "bad_verification_code", "error_description": "test-private-description"},
        {"access_token": "test-access-token", "error": "bad_code"},
        {},
        [],
        {"access_token": 123},
        {"access_token": ""},
    ],
)
def test_bad_token_responses_are_refused_including_github_http_200_error(
    make_signin, remote, caplog, provider_id, token
):
    surface = make_signin((provider_id,))
    remote.token = token
    refused(finish(surface, start(surface, remote, provider_id)), "failed")
    assert rows(surface, "sessions") == rows(surface, "user_identities") == []
    assert len(remote.calls) == 1
    assert len(caplog.records) == 1
    endpoint = urlsplit(TOKENS[IDS.index(provider_id)])
    assert endpoint.hostname + endpoint.path in caplog.text and "status=200" in caplog.text
    error = token.get("error") if isinstance(token, dict) else None
    assert "error=" + (error or "-") in caplog.text
    assert "test-private-description" not in caplog.text
    assert "test-access-token" not in caplog.text
    assert surface.db.audit_entries()[0]["reason"] == provider_id + ":failed"


def test_token_request_failure_is_refused(make_signin, remote):
    surface = make_signin()
    remote.failure = True
    refused(finish(surface, start(surface, remote)), "failed")
    assert rows(surface, "signin_flows") == []
    assert surface.db.audit_entries()[0]["reason"] == "google:failed"


@pytest.mark.parametrize("code", ["", "a" * 2049])
def test_invalid_code_is_rejected_before_exchange(make_signin, remote, code):
    surface = make_signin()
    refused(finish(surface, start(surface, remote), code=code), "failed")
    assert not remote.calls


@pytest.mark.parametrize("provider_id", ["google", "apple"])
@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://evil.example"},
        {"aud": "wrong-client"},
        {"aud": ["wrong-client"]},
        {"exp": 0},
        {"exp": "9999999999"},
        {"exp": True},
        {"exp": float("nan")},
        {"exp": float("inf")},
        {"nonce": "wrong-nonce"},
        {"nonce": None},
        {"sub": ""},
        {"sub": 123},
    ],
)
def test_oidc_claims_are_checked(make_signin, remote, provider_id, changes):
    surface = make_signin((provider_id,))
    remote.claims.update(changes)
    refused(finish(surface, start(surface, remote, provider_id)), "failed")
    assert rows(surface, "sessions") == rows(surface, "user_identities") == []


@pytest.mark.parametrize("token", [None, "bad", "a.!!.c", "a.W10.c", 123])
def test_malformed_id_token_is_refused(make_signin, remote, token):
    surface = make_signin()
    remote.token = {"access_token": "test-access-token", "id_token": token}
    refused(finish(surface, start(surface, remote)), "failed")


def test_oidc_list_audience_and_google_short_issuer(make_signin, remote):
    surface = make_signin()
    remote.claims.update(iss="accounts.google.com", aud=["other-client", "test-client-id"])
    signed_in(surface, finish(surface, start(surface, remote)))


def test_github_links_verified_non_primary_match_and_never_unverified(make_signin, remote):
    surface = make_signin(("github",))
    remote.emails = [
        {"email": "unmatched@example.com", "verified": True, "primary": True},
        {"email": "OWNER@EXAMPLE.COM", "verified": True, "primary": False},
        {"email": "unverified@example.com", "verified": False, "primary": False},
    ]
    signed_in(surface, finish(surface, start(surface, remote, "github")))
    assert len(rows(surface, "users")) == 1
    assert rows(surface, "user_identities")[0]["subject"] == "123"
    for url, _, headers in remote.calls[1:]:
        assert url.startswith("https://api.github.com/")
        assert headers == {
            "Authorization": "Bearer test-access-token",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


def test_github_primary_verified_email_is_first_for_signup(make_signin, remote):
    surface = make_signin(("github",), signup=True)
    remote.emails = [
        {"email": "secondary@example.com", "verified": True, "primary": False},
        {"email": "primary@example.com", "verified": True, "primary": True},
    ]
    response = finish(surface, start(surface, remote, "github"))
    signed_in(surface, response, surface.db.user("primary@example.com")["id"])


@pytest.mark.parametrize("provider_id", ["github", "x", "discord"])
def test_non_oidc_missing_or_unverified_email_is_refused(make_signin, remote, provider_id):
    surface = make_signin((provider_id,), signup=True)
    remote.emails = [{"email": "owner@example.com", "verified": False, "primary": True}]
    remote.x["data"].pop("confirmed_email")
    remote.discord["verified"] = False
    refused(finish(surface, start(surface, remote, provider_id)), "email")
    assert len(rows(surface, "users")) == 1 and rows(surface, "user_identities") == []


@pytest.mark.parametrize("provider_id", ["x", "discord"])
def test_x_confirmed_email_and_discord_verified_email_link(make_signin, remote, provider_id):
    surface = make_signin((provider_id,))
    signed_in(surface, finish(surface, start(surface, remote, provider_id)))
    assert rows(surface, "user_identities")[0]["subject"] == f"test-{provider_id}-subject"
    assert remote.calls[-1][2] == {"Authorization": "Bearer test-access-token"}


@pytest.mark.parametrize("provider_id", IDS)
def test_exchange_form_and_client_authentication(make_signin, remote, provider_id, apple_key):
    surface = make_signin((provider_id,))
    flow = start(surface, remote, provider_id)
    [row] = rows(surface, "signin_flows")
    signed_in(surface, finish(surface, flow))
    url, form, headers = remote.calls[0]
    assert url == TOKENS[IDS.index(provider_id)]
    expected = {
        "grant_type": "authorization_code",
        "code": "test-code",
        "redirect_uri": f"https://app.example/app/auth/{provider_id}/callback",
    }
    if provider_id != "apple":
        expected["code_verifier"] = row["verifier"]
    if provider_id == "x":
        assert headers == {
            "Authorization": "Basic "
            + base64.b64encode(b"test-client-id:test-client-secret").decode()
        }
        assert form == expected
    else:
        expected.update(
            client_id="test-client-id",
            client_secret=form["client_secret"] if provider_id == "apple" else "test-client-secret",
        )
        assert form == expected and headers == {}
    if provider_id == "apple":
        head, claims, signature = form["client_secret"].split(".")
        assert json.loads(decode(head)) == {"alg": "ES256", "kid": "test-key-id"}
        claims = json.loads(decode(claims))
        assert claims["iss"] == "test-team-id" and claims["sub"] == "test-client-id"
        assert claims["aud"] == "https://appleid.apple.com"
        assert claims["exp"] - claims["iat"] == 300
        assert abs(time.time() - claims["iat"]) < 10
        sig = decode(signature)
        assert len(sig) == 64
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        apple_key.public_key().verify(
            der,
            (head + "." + form["client_secret"].split(".")[1]).encode(),
            ec.ECDSA(hashes.SHA256()),
        )


def test_x_basic_credentials_use_form_encoding(make_signin, monkeypatch, remote):
    enable(monkeypatch, "x")
    monkeypatch.setenv("COMMONPLACE_X_CLIENT_ID", "test id:+é")
    monkeypatch.setenv("COMMONPLACE_X_CLIENT_SECRET", "test secret:&+")
    surface = make_signin(())
    signed_in(surface, finish(surface, start(surface, remote, "x")))
    _, form, headers = remote.calls[0]
    expected = quote_plus("test id:+é") + ":" + quote_plus("test secret:&+")
    assert base64.b64decode(headers["Authorization"].removeprefix("Basic ")).decode() == expected
    assert "client_id" not in form and "client_secret" not in form


def test_apple_form_post_name_and_string_verified_email(make_signin, remote):
    surface = make_signin(("apple",), signup=True)
    surface.gateway.secure_cookies = True
    remote.claims.update(email="new@example.com", email_verified="true")
    flow = start(surface, remote, "apple")
    response = finish(
        surface,
        flow,
        user=json.dumps(
            {
                "name": {"firstName": "  Test\t", "lastName": " Person\x00 "},
                "email": "ignored@example.com",
            }
        ),
    )
    user = surface.db.user("new@example.com")
    signed_in(surface, response, user["id"])
    assert user["name"] == "Test Person"
    assert "Secure" in response[1]["Set-Cookie"]


@pytest.mark.parametrize("user", ["", "{", "[]", '{"name":null}', '{"name":{"firstName":5}}'])
def test_apple_name_is_optional_on_repeat_authorizations(make_signin, remote, user):
    surface = make_signin(("apple",))
    signed_in(surface, finish(surface, start(surface, remote, "apple"), user=user))


@pytest.mark.parametrize("encoding", ["pem", "literal-newlines", "base64-body"])
def test_apple_accepts_supported_key_encodings(monkeypatch, apple_key, encoding):
    enable(monkeypatch, "apple", apple_key)
    value = pem(apple_key)
    if encoding == "literal-newlines":
        value = value.replace("\n", "\\n")
    elif encoding == "base64-body":
        value = "".join(value.splitlines()[1:-1])
    monkeypatch.setenv("COMMONPLACE_APPLE_PRIVATE_KEY", value)
    [provider] = providers.configured()
    assert (
        provider.id == "apple"
        and provider.private_key.public_key().public_numbers()
        == apple_key.public_key().public_numbers()
    )


@pytest.mark.parametrize("kind", ["unreadable", "wrong-curve", "rsa", "missing-package"])
def test_invalid_apple_key_or_missing_crypto_disables_only_apple(
    monkeypatch, apple_key, caplog, kind
):
    enable(monkeypatch, "apple", apple_key)
    enable(monkeypatch, "google")
    if kind == "missing-package":
        import builtins

        original = builtins.__import__

        def missing(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("test missing cryptography")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", missing)
    else:
        value = (
            "test-unreadable-key"
            if kind == "unreadable"
            else pem(
                ec.generate_private_key(ec.SECP384R1())
                if kind == "wrong-curve"
                else rsa.generate_private_key(public_exponent=65537, key_size=2048)
            )
        )
        monkeypatch.setenv("COMMONPLACE_APPLE_PRIVATE_KEY", value)
    assert [p.id for p in providers.configured()] == ["google"]
    assert "apple" in caplog.text and "test-unreadable-key" not in caplog.text


@pytest.mark.parametrize(
    "method,path,status",
    [
        ("GET", "/app/auth/apple/callback", 405),
        ("POST", "/app/auth/google/callback", 405),
        ("POST", "/app/auth/google", 405),
        ("POST", "/app/auth/providers", 405),
        ("GET", "/app/auth/google/extra", 404),
        ("GET", "/app/auth/google/callback/extra", 404),
        ("GET", "/app/auth/unknown", 404),
        ("GET", "/app/auth", 404),
    ],
)
def test_route_dispatch_and_wrong_methods_are_json(make_signin, method, path, status):
    surface = make_signin(("google", "apple"))
    actual, headers, raw = request(surface, path, method)
    assert actual == status and headers["Content-Type"] == "application/json"
    assert set(json.loads(raw)) == {"error"}


def test_malformed_callback_parameters_fail_without_provider_requests(make_signin, remote):
    surface = make_signin(("google", "apple"))
    refused(request(surface, "/app/auth/google/callback?state=a&state=b"), "failed")
    refused(
        request(
            surface,
            "/app/auth/apple/callback",
            "POST",
            raw=b"{}",
            headers={"Content-Type": "application/json"},
        ),
        "failed",
    )
    refused(
        request(
            surface,
            "/app/auth/apple/callback",
            "POST",
            raw=b"state=a&state=b",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ),
        "failed",
    )
    assert not remote.calls
    assert not [a for a in surface.db.audit_entries() if a["action"] == "signin.refuse"]


def test_malformed_start_parameters_are_not_audited(make_signin, remote):
    surface = make_signin()
    refused(request(surface, "/app/auth/google?next=a&next=b"), "failed")
    assert not remote.calls
    assert rows(surface, "signin_flows") == []
    assert not [a for a in surface.db.audit_entries() if a["action"] == "signin.refuse"]


def test_consent_links_preserve_next_and_success_returns_to_agent_origin(make_signin, remote):
    surface = make_signin(IDS)
    client = Client(surface)
    cid = client.register()
    authorize_path = client.authorize_path(cid)
    status, _, html = client.request("GET", authorize_path)
    assert status == 200
    links = [unescape(url) for url in re.findall(r'class="provider" href="([^"]+)"', html)]
    assert len(links) == 5
    for provider_id, name, link in zip(IDS, NAMES, links):
        parts = urlsplit(link)
        assert parts.scheme + "://" + parts.netloc == urls.APP_URL
        assert parts.path == f"/app/auth/{provider_id}"
        assert parse_qs(parts.query) == {"next": [authorize_path]}
        assert f"Continue with {name}</a>" in html
    assert html.index('class="provider"') < html.index('name="login_token"')
    flow = start(surface, remote, next_path=authorize_path)
    response = finish(surface, flow)
    signed_in(surface, response, landing=urls.AGENT_URL + authorize_path)
    client.cookies[sessions.COOKIE] = response[1]["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]
    html = client.request("GET", authorize_path)[2]
    assert "Allow" in html and 'class="provider"' not in html
    del surface.gateway.sign_in
    client.cookies.clear()
    assert 'class="provider"' not in client.request("GET", authorize_path)[2]


def test_consent_escapes_provider_names_and_links(make_signin, monkeypatch):
    surface = make_signin()
    monkeypatch.setattr(
        surface.gateway.sign_in,
        "links",
        lambda next_path: [
            ('<script>"name"</script>', '/app/auth/google?next=" onfocus="test&x=1')
        ],
    )
    client = Client(surface)
    cid = client.register()
    html = client.request("GET", client.authorize_path(cid))[2]
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "&quot; onfocus=&quot;test&amp;x=1" in html


def test_success_lands_on_app_next(make_signin, remote):
    surface = make_signin()
    signed_in(
        surface,
        finish(surface, start(surface, remote, next_path="/app/#/card/123")),
        landing="/app/#/card/123",
    )


def test_identities_and_audit_never_store_profile_or_credentials(make_signin, remote, caplog):
    surface = make_signin()
    flow = start(surface, remote)
    [row] = rows(surface, "signin_flows")
    assert flow.params["state"] not in repr(row) and flow.cookie.split("=")[1] not in repr(row)
    signed_in(surface, finish(surface, flow))
    persisted = repr(
        rows(surface, "user_identities")
        + rows(surface, "signin_flows")
        + [dict(entry) for entry in surface.db.audit_entries()]
    )
    for private in (
        "test-code",
        "test-access-token",
        "test-client-secret",
        "owner@example.com",
        "Test Person",
        jwt({"nonce": remote.nonce}),
    ):
        assert private not in persisted + caplog.text


def test_concurrent_linking_creates_only_one_user_and_identity(make_signin):
    surface = make_signin(signup=True)
    profile = providers.Profile("test-new-subject", "Test Person", ("new@example.com",))
    with ThreadPoolExecutor(max_workers=2) as pool:
        users = list(
            pool.map(
                lambda _: accounts.resolve(surface.db, "google", profile, signup=True), range(2)
            )
        )
    assert len(set(users)) == 1
    assert len(rows(surface, "users")) == len(rows(surface, "workspaces")) == 2
    assert len(rows(surface, "user_identities")) == 1


def test_concurrent_callback_consumes_state_once(make_signin, remote):
    surface = make_signin()
    flow = start(surface, remote)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: finish(surface, flow), range(2)))
    success = next(r for r in responses if "Set-Cookie" in r[1])
    signed_in(surface, success)
    refused(next(r for r in responses if "Set-Cookie" not in r[1]), "expired")
    assert len(remote.calls) == len(rows(surface, "sessions")) == 1


def test_fetch_is_direct_bounded_and_form_encoded(monkeypatch):
    seen = {}

    class Opener:
        def open(self, req, timeout):
            seen.update(request=req, timeout=timeout)
            response = io.BytesIO(b'{"ok":true}')
            response.status = 200
            return response

    def opener(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(providers, "build_opener", opener)
    assert _FETCH(
        "https://test.example/token", form={"code": "test code+"}, headers={"X-Test": "yes"}
    ) == {"ok": True}
    assert seen["timeout"] == 10
    assert seen["handlers"][0].proxies == {}
    assert isinstance(seen["handlers"][1], providers._NoRedirect)
    req = seen["request"]
    assert req.data == b"code=test%20code%2B"
    assert req.get_header("User-agent") == "Commonplace"
    assert req.get_header("Accept") == "application/json"
    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert req.get_header("X-test") == "yes"


@pytest.mark.parametrize(
    "problem",
    ["redirect", "http-error", "timeout", "too-large", "bad-json", "bad-encoding", "status"],
)
def test_fetch_failures_are_generic_and_do_not_log_responses(monkeypatch, caplog, problem):

    class Opener:
        def open(self, req, timeout):
            if problem in ("redirect", "http-error"):
                raise HTTPError(
                    req.full_url,
                    302 if problem == "redirect" else 400,
                    "test-private-description",
                    {},
                    None,
                )
            if problem == "timeout":
                raise TimeoutError("test-private-description")
            raw = (
                b"x" * (providers.MAX_RESPONSE + 1)
                if problem == "too-large"
                else b"{"
                if problem == "bad-json"
                else b"\xff"
                if problem == "bad-encoding"
                else b"{}"
            )
            response = io.BytesIO(raw)
            response.status = 201 if problem == "status" else 200
            return response

    monkeypatch.setattr(providers, "build_opener", lambda *args: Opener())
    with pytest.raises(providers.SignInError) as exc:
        _FETCH("https://test.example/token")
    assert exc.value.code == "failed" and str(exc.value) == "failed"
    assert "test-private-description" not in caplog.text
    assert len(caplog.records) == 1
    assert "test.example/token" in caplog.text


@pytest.mark.parametrize(
    "error", ["redirect_uri_mismatch", "a" * 64, "a" * 65, "a@b.example", "bad code", "", 123, None]
)
def test_fetch_http_error_logs_only_safe_diagnostics(monkeypatch, caplog, error):
    sizes = []

    class ErrorBody(io.BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)

    body = ErrorBody(
        json.dumps(
            {
                "error": error,
                "error_description": "test-private-description a@b.example",
                "access_token": "test-access-token",
            }
        ).encode()
        + b" " * 8192
    )

    class Opener:
        def open(self, req, timeout):
            raise HTTPError(req.full_url, 400, "test-private-description", {}, body)

    monkeypatch.setattr(providers, "build_opener", lambda *args: Opener())
    with pytest.raises(providers.SignInError, match="failed"):
        _FETCH(
            "https://test.example/token?code=test-private-code",
            form={"client_secret": "test-client-secret"},
        )
    assert sizes == [4096] and body.closed
    assert len(caplog.records) == 1
    assert "test.example/token" in caplog.text and "status=400" in caplog.text
    expected = (
        error if isinstance(error, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", error) else "-"
    )
    assert "error=" + expected in caplog.text
    for private in (
        "test-private-description",
        "a@b.example",
        "test-access-token",
        "test-private-code",
        "test-client-secret",
        "?",
    ):
        assert private not in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        OSError,
        URLError,
        TimeoutError,
        http.client.HTTPException,
        ValueError,
        UnicodeError,
        RecursionError,
    ],
)
def test_fetch_expected_failures_are_warned_once(monkeypatch, caplog, failure):
    class Opener:
        def open(self, req, timeout):
            raise failure("test-private-description")

    monkeypatch.setattr(providers, "build_opener", lambda *args: Opener())
    with pytest.raises(providers.SignInError, match="failed"):
        _FETCH("https://test.example/token")
    assert len(caplog.records) == 1 and "test.example/token" in caplog.text
    assert "test-private-description" not in caplog.text


@pytest.mark.parametrize("failure", [AttributeError, TypeError, RuntimeError])
def test_fetch_does_not_mask_programming_errors(monkeypatch, caplog, failure):
    class Opener:
        def open(self, req, timeout):
            raise failure("test programming error")

    monkeypatch.setattr(providers, "build_opener", lambda *args: Opener())
    with pytest.raises(failure, match="test programming error"):
        _FETCH("https://test.example/token")
    assert not caplog.records


@pytest.mark.parametrize(
    "value,cap,warnings",
    [
        (None, 6, 0),
        ("", 6, 0),
        ("  ", 6, 0),
        ("2", 2, 0),
        ("0", 0, 0),
        ("invalid", 6, 1),
        ("-1", 6, 1),
        ("1.5", 6, 1),
    ],
)
def test_x_daily_cap_configuration(make_signin, monkeypatch, caplog, value, cap, warnings):
    if value is not None:
        monkeypatch.setenv("COMMONPLACE_X_DAILY_CAP", value)
    surface = make_signin(IDS)
    configured = surface.gateway.sign_in.providers
    assert configured["x"].daily_cap == cap
    assert all(p.daily_cap is None for pid, p in configured.items() if pid != "x")
    assert len(caplog.records) == warnings
    if warnings:
        assert caplog.records[0].levelname == "WARNING"
        assert "COMMONPLACE_X_DAILY_CAP" in caplog.text
        assert "using 6" in caplog.text
    inventory = json.loads(request(surface, "/app/auth/providers")[2])
    assert [p["id"] for p in inventory["providers"]] == [
        pid for pid in IDS if pid != "x" or cap > 0
    ]
    assert [p["id"] for p in inventory["paused"]] == (["x"] if cap == 0 else [])


def test_seventh_x_callback_pauses_all_surfaces_without_provider_requests(
    make_signin, remote, monkeypatch
):
    now = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(social, "_now", lambda: now)
    surface = make_signin(("x", "github"))
    next_path = "/app/#/card/123"
    flows = [start(surface, remote, "x", next_path=next_path) for _ in range(7)]
    assert rows(surface, "signin_usage") == []
    for flow in flows[:6]:
        signed_in(surface, finish(surface, flow), landing=next_path)
    assert len(remote.calls) == 12
    remote.calls.clear()
    refused(finish(surface, flows[6]), "paused", next_path)
    assert not remote.calls
    assert rows(surface, "signin_usage") == [{"provider": "x", "day": "2026-10-31", "count": 6}]
    assert surface.db.audit_entries()[0]["reason"] == "x:paused"
    assert json.loads(request(surface, "/app/auth/providers")[2]) == {
        "providers": [
            {"id": "github", "name": "GitHub", "url": "https://app.example/app/auth/github"}
        ],
        "paused": [{"id": "x", "name": "X", "until": "2026-11-01T00:00:00Z"}],
    }
    assert surface.gateway.sign_in.links() == [("GitHub", "https://app.example/app/auth/github")]
    client = Client(surface)
    cid = client.register()
    html = client.request("GET", client.authorize_path(cid))[2]
    assert "Continue with X" not in html and "Continue with GitHub" in html
    audits = [dict(a) for a in surface.db.audit_entries()]
    for destination in ("", next_path, "https://evil.example/app/"):
        refused(
            request(surface, "/app/auth/x?" + urlencode({"next": destination})),
            "paused",
            next_path if destination == next_path else "",
        )
        assert rows(surface, "signin_flows") == []
        assert [dict(a) for a in surface.db.audit_entries()] == audits
    signed_in(surface, finish(surface, start(surface, remote, "github")))
    assert rows(surface, "signin_usage")[0]["count"] == 6


@pytest.mark.parametrize(
    "problem", ["missing", "wrong-provider", "wrong-browser", "expired", "error", "no-code"]
)
def test_x_rejected_callbacks_do_not_count(make_signin, remote, problem):
    surface = make_signin(("x", "github"))
    flow = start(surface, remote, "x")
    parameters = {}
    cookie = None
    if problem == "missing":
        flow.params["state"] = "unknown"
    elif problem == "wrong-provider":
        flow.id = "github"
    elif problem == "wrong-browser":
        cookie = "cmmn_signin=" + "a" * 43
    elif problem == "expired":
        with surface.db._lock:
            surface.db._conn.execute("UPDATE signin_flows SET expires_at = 0")
    elif problem == "error":
        parameters["error"] = "access_denied"
    else:
        parameters["code"] = ""
    code = "cancelled" if problem == "error" else "failed" if problem == "no-code" else "expired"
    refused(finish(surface, flow, cookie=cookie, **parameters), code)
    assert rows(surface, "signin_usage") == []
    assert not remote.calls


def test_x_failed_provider_request_keeps_reservation(make_signin, remote, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_X_DAILY_CAP", "1")
    surface = make_signin(("x",))
    remote.failure = True
    refused(finish(surface, start(surface, remote, "x")), "failed")
    assert len(remote.calls) == 1
    assert rows(surface, "signin_usage")[0]["count"] == 1
    refused(request(surface, "/app/auth/x"), "paused")


def test_x_utc_rollover_and_usage_pruning(make_signin, remote, monkeypatch):
    now = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(social, "_now", lambda: now)
    monkeypatch.setenv("COMMONPLACE_X_DAILY_CAP", "1")
    surface = make_signin(("x",))
    with surface.db._lock:
        surface.db._conn.executemany(
            "INSERT INTO signin_usage(provider, day, count) VALUES (?,?,?)",
            [("x", (now.date() - timedelta(days=days)).isoformat(), 1) for days in (40, 41)],
        )
    signed_in(surface, finish(surface, start(surface, remote, "x")))
    assert {r["day"] for r in rows(surface, "signin_usage")} == {"2026-09-21", "2026-10-31"}
    assert json.loads(request(surface, "/app/auth/providers")[2])["providers"] == []
    now += timedelta(seconds=1)
    inventory = json.loads(request(surface, "/app/auth/providers")[2])
    assert [p["id"] for p in inventory["providers"]] == ["x"] and inventory["paused"] == []
    signed_in(surface, finish(surface, start(surface, remote, "x")))
    assert rows(surface, "signin_usage") == [
        {"provider": "x", "day": "2026-10-31", "count": 1},
        {"provider": "x", "day": "2026-11-01", "count": 1},
    ]


def test_x_concurrent_reservations_grant_only_remaining_unit(make_signin, monkeypatch):
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    monkeypatch.setattr(social, "_now", lambda: now)
    surface = make_signin(("x",))
    signin = surface.gateway.sign_in
    with surface.db._lock:
        surface.db._conn.execute(
            "INSERT INTO signin_usage(provider, day, count) VALUES ('x','2026-10-31',5)"
        )
    barrier = Barrier(8)

    def reserve(_):
        barrier.wait(timeout=5)
        return signin._reserve(signin.providers["x"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        granted = list(pool.map(reserve, range(8)))
    assert granted.count(True) == 1 and granted.count(False) == 7
    assert rows(surface, "signin_usage")[0]["count"] == 6


@pytest.mark.parametrize("provider_id", IDS)
@pytest.mark.parametrize("secure", [False, True])
def test_success_sets_readable_provider_hint_cookie(make_signin, remote, provider_id, secure):
    surface = make_signin((provider_id,))
    surface.gateway.secure_cookies = secure
    response = finish(surface, start(surface, remote, provider_id))
    signed_in(surface, response)
    cookies = response[1].get_all("Set-Cookie")
    assert len(cookies) == 2 and cookies[0].startswith(sessions.COOKIE + "=")
    assert cookies[1] == (
        f"cmmn_via={provider_id}; Path=/app/; Max-Age=34560000; SameSite=Lax"
        + ("; Secure" if secure else "")
    )
    assert "HttpOnly" not in cookies[1]
