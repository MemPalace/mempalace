"""Real HTTP OAuth flows through the gateway and its in-process Memory worker."""

import base64
import hashlib
import http.client
import importlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from commonplace import oauth, sessions, urls
from commonplace.control_db import ControlDB
from commonplace.oauth import store
from tests._commonplace_helpers import hub_fixture as hub_fixture

REDIRECT = "https://client.example/callback?fixed=1"
VERIFIER = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGH_~.-"


class Client:
    def __init__(self, hub):
        self.hub = hub
        self.cookies = {}
        oauth.install(hub.gateway)

    def request(self, method, path, data=None, *, cookies=True, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.hub.port, timeout=5)
        hdrs = dict(headers or {})
        if cookies and self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if raw is None and data is not None:
            if path == "/oauth/register":
                raw = json.dumps(data).encode()
                hdrs["Content-Type"] = "application/json"
            else:
                raw = urlencode(data).encode()
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            conn.request(method, path, raw, hdrs)
            resp = conn.getresponse()
            text = resp.read().decode()
            response_headers = dict(resp.getheaders())
            for key, value in resp.getheaders():
                if key.lower() == "set-cookie":
                    name, val = value.split(";", 1)[0].split("=", 1)
                    self.cookies[name] = val
            return resp.status, response_headers, text
        finally:
            conn.close()

    def register(self, **extra):
        data = {"client_name": "Claude Test", "redirect_uris": [REDIRECT], **extra}
        status, headers, text = self.request("POST", "/oauth/register", data)
        assert status == 201, text
        assert headers["Cache-Control"] == "no-store"
        result = json.loads(text)
        assert result["token_endpoint_auth_method"] == "none"
        assert "client_secret" not in result
        return result["client_id"]

    def login(self, email="owner@example.com"):
        token = sessions.create_login_link(self.hub.db, email, self.hub.workspace)
        session = sessions.redeem_login_link(self.hub.db, token)
        assert session
        self.cookies[sessions.COOKIE] = session
        return session

    def authorize_path(self, client_id, **overrides):
        data = {
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "code_challenge": store.challenge(VERIFIER),
            "code_challenge_method": "S256",
            "resource": urls.mcp_url(),
            "state": "opaque + state & = / ✨",
            **overrides,
        }
        data = {key: val for key, val in data.items() if val is not None}
        return "/oauth/authorize?" + urlencode(data)

    def consent(self, client_id, **overrides):
        path = self.authorize_path(client_id, **overrides)
        status, headers, text = self.request("GET", path)
        assert status == 200, text
        assert "Allow" in text and "Deny" in text
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        request = re.search(r'name="request" value="([^"]+)"', text)[1]
        status, headers, text = self.request(
            "POST", path, {"request": request, "decision": "allow"}
        )
        assert status == 303, text
        result = parse_qs(urlsplit(headers["Location"]).query)
        assert result["fixed"] == ["1"]
        assert result["state"] == [overrides.get("state", "opaque + state & = / ✨")]
        assert result["iss"] == [urls.AGENT_URL]
        return result["code"][0]

    def exchange(self, client_id, code, **overrides):
        return self.request(
            "POST",
            "/oauth/token",
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": VERIFIER,
                "redirect_uri": REDIRECT,
                "resource": urls.mcp_url(),
                **overrides,
            },
        )

    def refresh(self, client_id, token, **overrides):
        return self.request(
            "POST",
            "/oauth/token",
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": token,
                "resource": urls.mcp_url(),
                **overrides,
            },
        )

    def grant(self, client_id):
        self.login()
        code = self.consent(client_id)
        status, headers, text = self.exchange(client_id, code)
        assert status == 200, text
        assert headers["Cache-Control"] == "no-store"
        return json.loads(text)


@pytest.fixture
def client(hub):
    return Client(hub)


def assert_error(response, error="invalid_grant", status=400):
    code, headers, text = response
    assert code == status, text
    assert json.loads(text)["error"] == error
    assert headers["Cache-Control"] == "no-store"
    assert "Location" not in headers


def test_full_flow_refresh_replay_and_separate_revocation(client):
    cid = client.register()
    pair = client.grant(cid)
    access, refresh = pair["access_token"], pair["refresh_token"]
    assert pair["token_type"] == "Bearer" and pair["expires_in"] == 3600
    assert pair["resource"] == urls.mcp_url()
    names = client.hub.tool_names(access)
    assert {"mempalace_search", "mempalace_add_drawer"} <= names
    assert "mempalace_kg_query" not in names
    principal = client.hub.gateway.authenticate("Bearer " + access)
    assert principal.auth == "oauth" and principal.client == "claude-ai"
    assert principal.policy.tools == "slim" and principal.policy.trust == "standard"
    assert principal.policy.read_wings == ("shared", "inbox", "project_*")
    assert principal.policy.write_wing == "inbox"
    assert not principal.policy.can_read_wing("private")
    assert principal.policy.can_write_wing("inbox")
    assert not principal.policy.can_write_wing("shared")
    assert client.hub.db.connection(principal.connection_id)["last_used_at"]
    row = client.hub.db.connection(principal.connection_id)
    assert row["token_hash"] is None and row["auth"] == "oauth"
    assert re.fullmatch(r"oauth:claude-test-[0-9a-f]{4}", row["name"])
    with client.hub.db._lock:
        persisted = [tuple(r) for r in client.hub.db._conn.execute("SELECT * FROM oauth_tokens")]
    assert access not in str(persisted) and refresh not in str(persisted)
    assert store.digest(access) in str(persisted)
    status, _, text = client.refresh(cid, refresh)
    assert status == 200, text
    rotated = json.loads(text)
    assert rotated["refresh_token"] != refresh and rotated["access_token"] != access
    assert client.hub.tool_names(rotated["access_token"])
    assert_error(client.refresh(cid, refresh))
    for token in (access, rotated["access_token"]):
        assert client.hub.raw(token, {"method": "tools/list", "id": 1})[0] == 401
    assert_error(client.refresh(cid, rotated["refresh_token"]))
    assert client.hub.db.connection(principal.connection_id)["revoked_at"]

    # Explicit revocation must be exercised on a still-live, independent grant.
    live = client.grant(cid)
    assert client.hub.tool_names(live["access_token"])
    response = client.request(
        "POST", "/oauth/revoke", {"client_id": cid, "token": live["refresh_token"]}
    )
    assert response[0] == 200
    assert client.hub.raw(live["access_token"], {"method": "tools/list", "id": 2})[0] == 401
    assert_error(client.refresh(cid, live["refresh_token"]))
    unknown = client.request("POST", "/oauth/revoke", {"client_id": cid, "token": "unknown"})
    assert (response[0], response[2]) == (unknown[0], unknown[2])


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"code_verifier": "x" * 43}, "invalid_grant"),
        ({"code_verifier": "short"}, "invalid_grant"),
        ({"code_verifier": "!" * 43}, "invalid_grant"),
        ({"redirect_uri": REDIRECT + "&extra=1"}, "invalid_grant"),
        ({"redirect_uri": "https://client.example/callback"}, "invalid_grant"),
        ({"resource": urls.mcp_url() + "/"}, "invalid_target"),
        ({"resource": ""}, "invalid_target"),
    ],
)
def test_rejected_exchange_never_issues_tokens(client, overrides, error):
    cid = client.register()
    client.login()
    code = client.consent(cid)
    assert_error(client.exchange(cid, code, **overrides), error)
    with client.hub.db._lock:
        assert client.hub.db._conn.execute("SELECT count(*) FROM oauth_tokens").fetchone()[0] == 0
    assert client.exchange(cid, code)[0] == 200


def test_code_expiry_and_single_use(client):
    cid = client.register()
    client.login()
    expired = client.consent(cid)
    with client.hub.db._lock:
        client.hub.db._conn.execute("UPDATE oauth_codes SET expires_at = ?", (store.now() - 1,))
    assert_error(client.exchange(cid, expired))
    code = client.consent(cid)
    status, _, text = client.exchange(cid, code)
    assert status == 200
    issued = json.loads(text)
    assert_error(client.exchange(cid, code))
    assert client.hub.gateway.authenticate("Bearer " + issued["access_token"]) is None
    assert_error(client.refresh(cid, issued["refresh_token"]))


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": None}, "invalid_request"),
        ({"code_challenge": "a" * 42}, "invalid_request"),
        ({"code_challenge": "a" * 43}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"resource": "https://wrong.example/mcp"}, "invalid_target"),
        ({"scope": "admin"}, "invalid_scope"),
    ],
)
def test_invalid_authorization_redirects_only_to_registered_uri(client, overrides, error):
    cid = client.register()
    status, headers, _ = client.request("GET", client.authorize_path(cid, **overrides))
    assert status == 302
    location = urlsplit(headers["Location"])
    assert location.hostname == "client.example" and location.path == "/callback"
    values = parse_qs(location.query)
    assert values["error"] == [error]
    assert values["state"] == ["opaque + state & = / ✨"]
    assert not client.hub.db.connections()


def test_unregistered_redirect_and_duplicate_parameters_fail_locally(client):
    cid = client.register()
    for uri in ("https://evil.example/callback", REDIRECT + "/", None):
        assert_error(
            client.request("GET", client.authorize_path(cid, redirect_uri=uri)), "invalid_request"
        )
    assert_error(
        client.request("GET", client.authorize_path(cid) + "&client_id=other"), "invalid_request"
    )


def test_login_token_form_preserves_authorization_without_leaking_credentials(client):
    cid = client.register(client_name='<script>alert("client")</script>')
    path = client.authorize_path(cid)
    status, headers, text = client.request("GET", path)
    assert status == 200 and "Sign in" in text and "Allow" not in text
    assert 'name="login_token"' in text
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Lax" in headers["Set-Cookie"]
    request = re.search(r'name="request" value="([^"]+)"', text)[1]
    login_token = sessions.create_login_link(
        client.hub.db, "owner@example.com", client.hub.workspace
    )
    assert_error(
        client.request(
            "POST", path, {"request": request, "login_token": login_token}, cookies=False
        ),
        "invalid_request",
        401,
    )
    status, headers, _ = client.request(
        "POST", path, {"request": request, "login_token": login_token}
    )
    assert status == 303 and headers["Location"] == path
    assert (
        login_token not in str(headers)
        and client.cookies[sessions.COOKIE] not in headers["Location"]
    )
    assert sessions.redeem_login_link(client.hub.db, login_token) is None
    status, _, text = client.request("GET", headers["Location"])
    assert status == 200 and "Allow" in text
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert unescape(re.search(r'action="([^"]+)"', text)[1]) == path


def test_consent_csrf_session_binding_deny_and_replay(client):
    cid = client.register()
    first_session = client.login()
    path = client.authorize_path(cid)
    status, _, text = client.request("GET", path)
    assert status == 200
    request = re.search(r'name="request" value="([^"]+)"', text)[1]
    assert_error(client.request("POST", path, {"decision": "allow"}), "invalid_request", 403)
    assert_error(
        client.request("POST", path, {"request": request, "decision": "allow"}, cookies=False),
        "invalid_request",
        401,
    )
    client.login()
    assert_error(
        client.request("POST", path, {"request": request, "decision": "allow"}),
        "invalid_request",
        403,
    )
    client.cookies[sessions.COOKIE] = first_session
    status, headers, _ = client.request("POST", path, {"request": request, "decision": "deny"})
    assert status == 303 and parse_qs(urlsplit(headers["Location"]).query)["error"] == [
        "access_denied"
    ]
    assert not client.hub.db.connections()
    assert_error(
        client.request("POST", path, {"request": request, "decision": "allow"}),
        "invalid_request",
        403,
    )


def test_pat_cannot_approve_oauth_consent(client):
    cid = client.register()
    token = client.hub.owner_token()
    status, _, text = client.request(
        "GET", client.authorize_path(cid), headers={"Authorization": "Bearer " + token}
    )
    assert status == 200 and "Sign in" in text and "Allow" not in text


@pytest.mark.parametrize(
    "uri",
    [
        "http://example.com/callback",
        "http://localhost.evil/callback",
        "http://127.0.0.1.evil/callback",
        "https://example.com/callback#fragment",
        "https://example.com/callback#",
        "https://user:pass@example.com/callback",
        "https://example.com:bad/callback",
        "http://[::1]:5555/callback",
        "javascript:alert(1)",
        "/callback",
        "https://example.com/\r\nInjected:x",
        "https://example.com\\evil",
    ],
)
def test_registration_rejects_unsafe_redirects(uri, tmp_path):
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    try:
        store.ensure_schema(db)
        with pytest.raises(oauth.OAuthError) as exc:
            oauth._register(db, {"redirect_uris": [uri]})
        assert exc.value.error == "invalid_redirect_uri"
    finally:
        db.close()


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "https://chatgpt.com/connector_platform/oauth/callback",
        "http://localhost:5555/callback",
        "http://127.0.0.1:5555/callback",
    ],
)
def test_registration_accepts_https_or_loopback_only(uri):
    assert oauth._valid_redirect(uri)


def test_registration_structure_and_auth_method(client):
    for bad in (
        {"redirect_uris": []},
        {"redirect_uris": REDIRECT},
        {"redirect_uris": [None]},
        {"token_endpoint_auth_method": "client_secret_basic"},
        {"client_name": 4},
        {"grant_types": ["implicit"]},
        {"response_types": ["token"]},
    ):
        status, _, text = client.request(
            "POST", "/oauth/register", {"redirect_uris": [REDIRECT], **bad}
        )
        assert status == 400, text
    assert_error(
        client.request(
            "POST",
            "/oauth/register",
            raw=b'{"redirect_uris":[],"redirect_uris":[]}',
            headers={"Content-Type": "application/json"},
        ),
        "invalid_request",
    )


def test_metadata_uses_public_config_and_install_is_idempotent(client, monkeypatch):
    monkeypatch.setattr(urls, "AGENT_URL", "https://cmmn.dev")
    monkeypatch.setattr(urls, "APP_URL", "https://app.cmmn.dev")
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        status, _, text = client.request("GET", path, headers={"Host": "evil.example"})
        assert status == 200
        metadata = json.loads(text)
        assert metadata["resource"] == "https://cmmn.dev/mcp"
        assert metadata["authorization_servers"] == ["https://cmmn.dev"]
        assert metadata["bearer_methods_supported"] == ["header"]
    status, _, text = client.request("GET", "/.well-known/oauth-authorization-server")
    assert status == 200
    metadata = json.loads(text)
    assert metadata["issuer"] == "https://cmmn.dev"
    for key, suffix in (
        ("authorization_endpoint", "authorize"),
        ("token_endpoint", "token"),
        ("registration_endpoint", "register"),
        ("revocation_endpoint", "revoke"),
    ):
        assert metadata[key] == "https://cmmn.dev/oauth/" + suffix
    assert metadata["response_types_supported"] == ["code"]
    assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    authenticators = list(client.hub.gateway.authenticators)
    prefixes = list(client.hub.gateway.prefix_routes)
    oauth.install(client.hub.gateway)
    assert client.hub.gateway.authenticators == authenticators
    assert client.hub.gateway.prefix_routes == prefixes
    assert 'resource_metadata="' in client.hub.gateway.www_authenticate


def test_short_links_persist_scope_redirect_and_collision(client, monkeypatch):
    monkeypatch.setattr(urls, "APP_URL", "https://app.cmmn.dev")
    monkeypatch.setattr(urls, "AGENT_URL", "https://cmmn.dev")
    db = client.hub.db
    workspace = client.hub.workspace
    path = "/app/#/card/145"
    with ThreadPoolExecutor(max_workers=4) as pool:
        links = list(pool.map(lambda _: urls.share_link(db, workspace, path), range(8)))
    assert len(set(links)) == 1
    link = links[0]
    assert re.fullmatch(r"https://cmmn.dev/s/[A-Za-z0-9]{7}", link)
    status, headers, _ = client.request("GET", urlsplit(link).path, cookies=False)
    assert status == 302 and headers["Location"] == "https://app.cmmn.dev" + path
    other = db.create_workspace("other", client.hub.admin)
    assert urls.share_link(db, other, path) != link
    reopened = ControlDB(db.path)
    try:
        assert urls.share_link(reopened, workspace, path) == link
    finally:
        reopened.close()
    for bad in ("/s/unknown", "/s/12345678", "/s/../app"):
        assert client.request("GET", bad)[0] == 404
    with db._lock:
        existing = db._conn.execute("SELECT code FROM short_links LIMIT 1").fetchone()[0]
    choices = iter(existing + "Z9x8Q7r")
    monkeypatch.setattr(urls.secrets, "choice", lambda _: next(choices))
    assert urls.share_link(db, workspace, "/app/#/memory").endswith("/Z9x8Q7r")


@pytest.mark.parametrize(
    "path", ["//evil.example", "https://evil.example", "/%2fexample", "/\\evil", "/x%0d%0aX:y"]
)
def test_app_paths_cannot_redirect_offsite(path):
    with pytest.raises(ValueError):
        urls.app_link(path)


def test_environment_url_precedence(monkeypatch):
    for key in (
        "COMMONPLACE_BASE_URL",
        "COMMONPLACE_APP_URL",
        "COMMONPLACE_AGENT_URL",
        "COMMONPLACE_SITE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    try:
        importlib.reload(urls)
        assert urls.APP_URL == urls.AGENT_URL == urls.SITE_URL == "http://127.0.0.1:8765"
        monkeypatch.setenv("COMMONPLACE_BASE_URL", "https://tail.example/")
        importlib.reload(urls)
        assert urls.APP_URL == urls.AGENT_URL == urls.SITE_URL == "https://tail.example"
        monkeypatch.setenv("COMMONPLACE_APP_URL", "https://app.cmmn.dev/")
        monkeypatch.setenv("COMMONPLACE_AGENT_URL", "https://cmmn.dev/")
        monkeypatch.setenv("COMMONPLACE_SITE_URL", "https://commonplace.run/")
        importlib.reload(urls)
        assert urls.app_link("/app/") == "https://app.cmmn.dev/app/"
        assert urls.mcp_url() == "https://cmmn.dev/mcp"
        assert urls.SITE_URL == "https://commonplace.run"
    finally:
        monkeypatch.undo()
        importlib.reload(urls)


def test_client_binding_and_connection_revocation(client):
    cid = client.register()
    other = client.register(client_name="Other")
    client.login()
    code = client.consent(cid)
    assert_error(client.exchange(other, code))
    status, _, text = client.exchange(cid, code)
    assert status == 200
    pair = json.loads(text)
    assert_error(client.refresh(other, pair["refresh_token"]))
    assert (
        client.request(
            "POST", "/oauth/revoke", {"client_id": other, "token": pair["access_token"]}
        )[0]
        == 200
    )
    principal = client.hub.gateway.authenticate("Bearer " + pair["access_token"])
    assert principal
    client.hub.db.revoke(principal.connection_id, actor=client.hub.admin)
    assert client.hub.gateway.authenticate("Bearer " + pair["access_token"]) is None
    assert_error(client.refresh(cid, pair["refresh_token"]))


def test_token_expiry_resource_binding_narrowing_and_disabled_member(client):
    cid = client.register(client_name="ChatGPT")
    pair = client.grant(cid)
    principal = client.hub.gateway.authenticate("Bearer " + pair["access_token"])
    assert principal.client == "chatgpt"
    assert_error(
        client.refresh(cid, pair["refresh_token"], resource="https://wrong.example/mcp"),
        "invalid_target",
    )
    status, _, text = client.refresh(cid, pair["refresh_token"], scope="memory:read")
    assert status == 200
    narrowed = json.loads(text)
    p = client.hub.gateway.authenticate("Bearer " + narrowed["access_token"])
    assert p.policy.has_scope("memory:read") and not p.policy.has_scope("memory:write")
    assert_error(
        client.refresh(cid, narrowed["refresh_token"], scope="memory:read memory:write"),
        "invalid_scope",
    )
    with client.hub.db._lock:
        client.hub.db._conn.execute(
            "UPDATE oauth_tokens SET expires_at = ? WHERE token_hash = ?",
            (store.now() - 1, store.digest(narrowed["access_token"])),
        )
    assert client.hub.gateway.authenticate("Bearer " + narrowed["access_token"]) is None
    with client.hub.db._lock:
        client.hub.db._conn.execute(
            "UPDATE users SET disabled_at = ? WHERE id = ?", (store.iso(), client.hub.admin)
        )
    assert client.hub.gateway.authenticate("Bearer " + pair["access_token"]) is None
    assert_error(client.refresh(cid, narrowed["refresh_token"]))


def test_no_secrets_persist_in_codes_or_requests(client):
    cid = client.register()
    session = client.login()
    code = client.consent(cid)
    _, _, text = client.request("GET", client.authorize_path(cid))
    request_secret = re.search(r'name="request" value="([^"]+)"', text)[1]
    with client.hub.db._lock:
        codes = str([tuple(r) for r in client.hub.db._conn.execute("SELECT * FROM oauth_codes")])
        requests = str(
            [tuple(r) for r in client.hub.db._conn.execute("SELECT * FROM oauth_requests")]
        )
    assert code not in codes and hashlib.sha256(code.encode()).hexdigest() in codes
    assert session not in requests and store.digest(session) in requests
    assert request_secret not in requests and store.digest(request_secret) in requests


def test_login_can_retry_invalid_token_and_consent_forms_expire(client):
    cid = client.register()
    path = client.authorize_path(cid)
    _, _, text = client.request("GET", path)
    request = re.search(r'name="request" value="([^"]+)"', text)[1]
    status, _, text = client.request("POST", path, {"request": request, "login_token": "invalid"})
    assert status == 401 and 'name="login_token"' in text
    request = re.search(r'name="request" value="([^"]+)"', text)[1]
    login_token = sessions.create_login_link(
        client.hub.db, "owner@example.com", client.hub.workspace
    )
    assert client.request("POST", path, {"request": request, "login_token": login_token})[0] == 303
    _, _, text = client.request("GET", path)
    request = re.search(r'name="request" value="([^"]+)"', text)[1]
    with client.hub.db._lock:
        client.hub.db._conn.execute("UPDATE oauth_requests SET expires_at = ?", (store.now() - 1,))
    assert_error(
        client.request("POST", path, {"request": request, "decision": "allow"}),
        "invalid_request",
        403,
    )
    assert not client.hub.db.connections()


def test_optional_resource_still_binds_tokens_and_refresh_expiry(client):
    cid = client.register()
    client.login()
    code = client.consent(cid, resource=None)
    status, _, text = client.request(
        "POST",
        "/oauth/token",
        {
            "grant_type": "authorization_code",
            "client_id": cid,
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT,
        },
    )
    assert status == 200
    pair = json.loads(text)
    assert pair["resource"] == urls.mcp_url()
    with client.hub.db._lock:
        client.hub.db._conn.execute(
            "UPDATE oauth_tokens SET expires_at = ? WHERE kind = 'refresh'", (store.now() - 1,)
        )
    assert_error(client.refresh(cid, pair["refresh_token"]))
    # Refresh expiry does not retroactively revoke a live access token.
    assert client.hub.gateway.authenticate("Bearer " + pair["access_token"])


def test_refresh_replay_survives_restart_and_access_token_revocation(client):
    cid = client.register()
    pair = client.grant(cid)
    status, _, text = client.refresh(cid, pair["refresh_token"])
    assert status == 200
    rotated = json.loads(text)
    reopened = ControlDB(client.hub.db.path)
    try:
        authenticator = oauth.OAuthAuthenticator(reopened)
        assert authenticator.authenticate(rotated["access_token"])
        with pytest.raises(oauth.OAuthError):
            store.rotate(reopened, {"client_id": cid, "refresh_token": pair["refresh_token"]})
        assert authenticator.authenticate(rotated["access_token"]) is None
    finally:
        reopened.close()
    assert_error(client.refresh(cid, rotated["refresh_token"]))
    live = client.grant(cid)
    assert (
        client.request(
            "POST",
            "/oauth/revoke",
            {
                "client_id": cid,
                "token": live["access_token"],
                "token_type_hint": "refresh_token",
            },
        )[0]
        == 200
    )
    assert client.hub.gateway.authenticate("Bearer " + live["access_token"]) is None
    assert_error(client.refresh(cid, live["refresh_token"]))


def test_concurrent_refresh_cannot_fork_a_grant(client):
    cid = client.register()
    pair = client.grant(cid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.refresh(cid, pair["refresh_token"]), range(2)))
    assert sorted(response[0] for response in responses) == [200, 400]
    issued = json.loads(next(response[2] for response in responses if response[0] == 200))
    assert client.hub.gateway.authenticate("Bearer " + issued["access_token"]) is None
    assert_error(client.refresh(cid, issued["refresh_token"]))


def test_credential_endpoints_refuse_unsupported_bodies_and_authentication(client):
    cid = client.register()
    assert_error(
        client.request(
            "POST", "/oauth/token", raw=b"{}", headers={"Content-Type": "application/json"}
        ),
        "invalid_request",
        415,
    )
    assert_error(
        client.request("POST", "/oauth/token", {"grant_type": "client_credentials"}),
        "unsupported_grant_type",
    )
    assert_error(
        client.request(
            "POST",
            "/oauth/token",
            {"client_id": cid},
            headers={"Authorization": "Basic unsupported"},
        ),
        "invalid_client",
    )
    assert_error(
        client.request("POST", "/oauth/revoke", {"client_id": cid, "token": ""}), "invalid_request"
    )
    assert_error(
        client.request(
            "POST",
            "/oauth/token",
            raw=b"client_id=a&client_id=b",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ),
        "invalid_request",
    )


def test_absolute_request_target_cannot_redirect_login_offsite(client):
    cid = client.register()
    path = client.authorize_path(cid)
    status, _, text = client.request("GET", "https://evil.example" + path)
    assert status == 200
    assert unescape(re.search(r'action="([^"]+)"', text)[1]) == path
    request = re.search(r'name="request" value="([^"]+)"', text)[1]
    login_token = sessions.create_login_link(
        client.hub.db, "owner@example.com", client.hub.workspace
    )
    status, headers, _ = client.request(
        "POST", path, {"request": request, "login_token": login_token}
    )
    assert status == 303 and headers["Location"] == path


def _form_action(headers):
    return next(
        directive.strip().split()[1:]
        for directive in headers["Content-Security-Policy"].split(";")
        if directive.strip().startswith("form-action ")
    )


@pytest.mark.parametrize(
    "uri,origin",
    [
        ("https://MCP.example:8443/callback?fixed=1", "https://mcp.example:8443"),
        ("https://client.example:443/callback", "https://client.example:443"),
        ("http://127.0.0.1:80/callback", "http://127.0.0.1:80"),
        ("https://127.0.0.1:443/callback", "https://127.0.0.1:443"),
        ("http://127.0.0.1:54321/callback", "http://127.0.0.1:54321"),
        ("https://client.example/callback;path='ok',?q=1", "https://client.example"),
    ],
)
def test_consent_form_action_only_exact_validated_origin(client, uri, origin):
    cid = client.register(redirect_uris=[uri, "https://second.example/callback"])
    client.login()
    status, headers, text = client.request("GET", client.authorize_path(cid, redirect_uri=uri))
    assert status == 200 and '<form method="post"' in text and "Allow" in text
    assert _form_action(headers) == ["'self'", origin]
    assert text.count(origin) == 1
    assert f"<p>Code destination: {origin}</p>" in text
    assert "https://second.example" not in text
    # No other directive may change, and the other registered origin is absent.
    style_hash = base64.b64encode(hashlib.sha256(oauth._CSS.encode()).digest()).decode()
    assert headers["Content-Security-Policy"] == (
        "default-src 'none'; style-src 'sha256-" + style_hash + "'; "
        f"form-action 'self' {origin}; base-uri 'none'; frame-ancestors 'none'"
    )


def test_login_and_retry_forms_keep_self_only(client):
    cid = client.register()
    path = client.authorize_path(cid)
    status, headers, text = client.request("GET", path)
    assert status == 200 and '<form method="post"' in text
    assert 'name="login_token"' in text and "Allow" not in text
    assert _form_action(headers) == ["'self'"]
    assert "Code destination:" not in text and "https://client.example" not in text
    request = re.search(r'name="request" value="([^"]+)"', text)[1]
    status, headers, text = client.request(
        "POST", path, {"request": request, "login_token": "invalid"}
    )
    assert status == 401 and '<form method="post"' in text
    assert 'name="login_token"' in text and "Allow" not in text
    assert _form_action(headers) == ["'self'"]
    assert "Code destination:" not in text and "https://client.example" not in text
    # A concurrent sign-in can make a failed login re-render actual consent.
    client.login()
    request = re.search(r'name="request" value="([^"]+)"', text)[1]
    status, headers, text = client.request(
        "POST", path, {"request": request, "login_token": "invalid"}
    )
    assert status == 401 and "Allow" in text and '<form method="post"' in text
    assert _form_action(headers) == ["'self'", "https://client.example"]
    assert text.count("https://client.example") == 1
    assert "<p>Code destination: https://client.example</p>" in text


def test_invalid_authorization_never_renders_external_form_action(client, monkeypatch):
    cid = client.register()
    client.login()
    for path in (
        client.authorize_path("unknown"),
        client.authorize_path(cid, redirect_uri="https://evil.example/"),
    ):
        status, headers, text = client.request("GET", path)
        assert status == 400 and "<form" not in text
        assert "Content-Security-Policy" not in headers  # Existing JSON errors.
    # Generic HTML/error pages have no validated redirect and retain the default.
    captured = {}
    monkeypatch.setattr(oauth, "send", lambda *args, **kwargs: captured.update(kwargs))
    oauth._page(None, "<p>Error</p>", status=400)
    assert _form_action(dict(captured["headers"])) == ["'self'"]


@pytest.mark.parametrize(
    "uri",
    [
        "https://client.example;form-action/callback",
        "https://client.example'unsafe/callback",
        "https://client.example,evil.example/callback",
        "https://bad_label.example/callback",
        "https://client.example:/callback",
        "https://[2001:db8::1]/callback",
        "http://[::1]:54321/callback",
        "https://127.1/callback",
        "https://0x7f000001/callback",
        "https://0177.0.0.1/callback",
        "https://127.0.0.1./callback",
        "https://2130706433/callback",
        "https://client.123/callback",
        "https://client.0xff/callback",
        "https://0x/callback",
        "https://client.0x/callback",
    ],
)
def test_hostile_registered_redirect_fails_closed(client, uri):
    # Simulate an existing registration accepted by the old permissive validator.
    cid = client.register()
    with client.hub.db._lock:
        client.hub.db._conn.execute(
            "UPDATE oauth_clients SET redirect_uris = ? WHERE client_id = ?",
            (json.dumps([uri]), cid),
        )
    client.login()
    path = client.authorize_path(cid, redirect_uri=uri)
    status, headers, text = client.request("GET", path)
    assert status == 400 and "<form" not in text and "Location" not in headers
    assert "Content-Security-Policy" not in headers
    assert_error(
        client.request("POST", "/oauth/register", {"redirect_uris": [uri]}), "invalid_redirect_uri"
    )


@pytest.mark.parametrize(
    "flow,host",
    [
        ("consent", "127.0.0.1"),
        ("login_retry", "127.0.0.1"),
        ("consent_retry", "127.0.0.1"),
        ("consent", "::1"),
    ],
)
def test_consent_chromium_cross_origin_submit(client, flow, host, pytestconfig):
    """Run explicitly with scripts/requirements-release.txt and Chromium installed.

    Only synthetic credentials in an ephemeral DB; never log callback queries.
    A bounded callback wait makes blocked base navigation fail by assertion.
    """
    pytest.importorskip("playwright.sync_api", reason="optional browser regression dependency")
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from playwright.sync_api import sync_playwright

    assert not pytestconfig.getoption("showlocals"), "Use --no-showlocals"
    received = []
    callback_arrived = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            received.append(parse_qs(urlsplit(self.path).query))
            callback_arrived.set()
            payload = b"<title>callback-received</title>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    class CallbackServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if host == "::1" else socket.AF_INET

    target = CallbackServer((host, 0), Callback)
    thread = threading.Thread(target=target.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    origin = f"http://127.0.0.1:{client.hub.port}"
    authority = f"[{host}]" if host == "::1" else host
    callback_origin = f"http://{authority}:{target.server_port}"
    console = []
    violations = []

    def sanitized(message):
        return re.sub(
            r"https?://[^\s'\"]+",
            lambda m: m[0].split("?", 1)[0] + "?<redacted>",
            message,
        )

    try:
        if host == "::1":
            response = client.request(
                "POST", "/oauth/register", {"redirect_uris": [callback_origin + "/callback"]}
            )
            if response[0] == 400:
                assert_error(response, "invalid_redirect_uri")
                print("Chromium IPv6 flow refused before consent: invalid_redirect_uri")
                return
            assert response[0] == 201
            cid = json.loads(response[2])["client_id"]
        else:
            cid = client.register(redirect_uris=[callback_origin + "/callback"])
        path = client.authorize_path(
            cid, redirect_uri=callback_origin + "/callback", state="browser-test"
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                context = browser.new_context()
                page = context.new_page()
                page.on("console", lambda message: console.append(sanitized(message.text)))
                page.expose_function(
                    "recordViolation", lambda directive: violations.append(directive)
                )
                if flow == "consent":
                    client.login()
                    context.add_cookies(
                        [
                            {
                                "name": sessions.COOKIE,
                                "value": client.cookies[sessions.COOKIE],
                                "url": origin,
                            }
                        ]
                    )
                response = page.goto(origin + path)
                assert response.status == 200
                if flow != "consent":
                    assert page.get_by_role("button", name="Sign in", exact=True).count() == 1
                    if flow == "consent_retry":
                        client.login()
                        context.add_cookies(
                            [
                                {
                                    "name": sessions.COOKIE,
                                    "value": client.cookies[sessions.COOKIE],
                                    "url": origin,
                                }
                            ]
                        )
                    page.get_by_label("Sign in token").fill("invalid")
                    with page.expect_response(lambda r: r.request.method == "POST") as retry:
                        page.get_by_role("button", name="Sign in", exact=True).click()
                    assert retry.value.status == 401
                    if flow == "login_retry":
                        token = sessions.create_login_link(
                            client.hub.db, "owner@example.com", client.hub.workspace
                        )
                        page.get_by_label("Sign in token").fill(token)
                        page.get_by_role("button", name="Sign in", exact=True).click()
                        assert page.get_by_role("button", name="Allow", exact=True).count() == 1
                page.evaluate("""document.addEventListener('securitypolicyviolation', e => {
                    window.recordViolation(e.effectiveDirective);
                });""")
                page.get_by_role("button", name="Allow", exact=True).click(no_wait_after=True)
                arrived = callback_arrived.wait(timeout=5)
                # Dispatch queued console/binding events after the server-side wait.
                page.wait_for_timeout(100)
                safe_console = [message for message in console if "form-action" in message]
                evidence = f"Chromium {flow}/{host}: arrived={arrived}; CSP console={safe_console}; violations={violations}"
                print(evidence)
                assert arrived, evidence
                assert host != "::1", "IPv6 redirects must be refused before consent"
                correct_origin = urlsplit(page.url).netloc == urlsplit(callback_origin).netloc
                assert correct_origin and len(received) == 1, (
                    "Must reach the other origin exactly once"
                )
                valid_query = bool(received[0].get("code")) and received[0].get("state") == [
                    "browser-test"
                ]
                assert valid_query, (
                    "Callback must contain an authorization code and preserved state"
                )
                print(
                    f"Chromium {flow}: cross-origin callback reached with code and state (values withheld)"
                )
            finally:
                browser.close()
    finally:
        target.shutdown()
        target.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_registered_redirect_validation_errors_explain_reregistration(client, method):
    cid = client.register()
    client.login()
    path = client.authorize_path(cid)
    _, _, text = client.request("GET", path)
    request = re.search(r'name="request" value="([^"]+)"', text)[1]
    legacy = "https://bad_label.example/callback"
    with client.hub.db._lock:
        client.hub.db._conn.execute(
            "UPDATE oauth_clients SET redirect_uris = ? WHERE client_id = ?",
            (json.dumps([legacy]), cid),
        )
        for row in client.hub.db._conn.execute(
            "SELECT request_hash, parameters FROM oauth_requests"
        ).fetchall():
            parameters = json.loads(row["parameters"])
            parameters["redirect_uri"] = legacy
            client.hub.db._conn.execute(
                "UPDATE oauth_requests SET parameters = ? WHERE request_hash = ?",
                (json.dumps(parameters), row["request_hash"]),
            )
    response = client.request(
        method,
        client.authorize_path(cid, redirect_uri=legacy),
        {"request": request, "decision": "allow"} if method == "POST" else None,
    )
    assert_error(response, "invalid_request")
    description = json.loads(response[2])["error_description"]
    assert description == "Redirect URI no longer passes validation; register the client again"
    assert legacy not in description
    unregistered = client.request("GET", client.authorize_path(cid))
    assert_error(unregistered, "invalid_request")
    assert json.loads(unregistered[2])["error_description"] == "Redirect URI is not registered"


def test_registration_error_names_supported_redirect_rule(client):
    uri = "https://bad_label.example/callback"
    response = client.request("POST", "/oauth/register", {"redirect_uris": [uri]})
    assert_error(response, "invalid_redirect_uri")
    description = json.loads(response[2]).get("error_description", "")
    assert "HTTPS or loopback HTTP" in description
    assert "canonical IPv4" in description and "numeric port" in description
    assert "IPv6" in description and uri not in description


def test_consent_chromium_explicit_default_port_navigation(client, pytestconfig):
    """Observe redirect navigation, with DNS disabled; no TLS server required."""
    pytest.importorskip("playwright.sync_api", reason="optional browser regression dependency")
    from playwright.sync_api import sync_playwright

    assert not pytestconfig.getoption("showlocals"), "Use --no-showlocals"
    uri = "https://CLIENT.example:443/callback"
    destination = "https://client.example:443"
    cid = client.register(redirect_uris=[uri])
    client.login()
    origin = f"http://127.0.0.1:{client.hub.port}"
    started, violations = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage", "--host-resolver-rules=MAP client.example ~NOTFOUND"],
        )
        try:
            context = browser.new_context()
            context.add_cookies(
                [{"name": sessions.COOKIE, "value": client.cookies[sessions.COOKIE], "url": origin}]
            )
            context.expose_function(
                "recordViolation", lambda directive: violations.append(directive)
            )
            context.add_init_script(
                "document.addEventListener('securitypolicyviolation', e => window.recordViolation(e.effectiveDirective));"
            )
            page = context.new_page()

            def navigation(request):
                parts = urlsplit(request.url)
                if request.is_navigation_request() and parts.hostname == "client.example":
                    started.append(parts.scheme == "https" and (parts.port or 443) == 443)

            page.on("request", navigation)
            page.goto(origin + client.authorize_path(cid, redirect_uri=uri))
            shown = (
                page.locator("main > p")
                .all_text_contents()
                .count("Code destination: " + destination)
                == 1
            )
            page.get_by_role("button", name="Allow", exact=True).click(no_wait_after=True)
            page.wait_for_timeout(1500)
            evidence = f"default-port navigation_started={bool(started)}, form-action violations={violations}"
            print(evidence)
            assert started == [True] and "form-action" not in violations, evidence
            assert shown, "Consent must show the validated default-port origin before navigation"
        finally:
            browser.close()
