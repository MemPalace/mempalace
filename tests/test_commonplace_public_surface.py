"""Complete installed route inventory and real-HTTP public-boundary probes."""

import http.client
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from commonplace import ratelimit, sessions, urls, web, webhooks
from commonplace.control_db import ControlDB
from commonplace.gateway import MAX_REQUEST_BYTES, Gateway, _host_name, _write_request
from commonplace.policy import Policy
from commonplace.workers import WorkerSupervisor

# A new registered route must be deliberately classified here. Prefix entries
# include methods without an implemented action: authentication still runs first.
EXACT = {
    ("GET", "/.well-known/oauth-protected-resource"): "public",
    ("GET", "/.well-known/oauth-protected-resource/mcp"): "public",
    ("GET", "/.well-known/oauth-authorization-server"): "public",
    ("POST", "/oauth/register"): "public",
    ("GET", "/oauth/authorize"): "public",
    ("POST", "/oauth/authorize"): "authenticated",
    ("POST", "/oauth/token"): "public",
    ("POST", "/oauth/revoke"): "public",
    ("GET", "/app/login"): "public",
    ("POST", "/app/auth/email"): "public",
    ("GET", "/app/auth/email/verify"): "public",
    ("POST", "/app/auth/email/verify"): "public",
    ("POST", "/app/logout"): "authenticated",
    ("GET", "/api/me"): "authenticated",
    ("GET", "/api/plan"): "authenticated",
    ("GET", "/api/events"): "authenticated",
    ("GET", "/api/connections"): "authenticated",
    ("GET", "/api/voice"): "authenticated",
    ("PATCH", "/api/voice"): "authenticated",
    ("POST", "/api/voice/transcribe"): "authenticated",
    ("POST", "/api/facts"): "authenticated",
    # Context handoff (commonplace/handoff), off by default.
    ("POST", "/api/handoff"): "authenticated",
    ("GET", "/api/handoffs"): "authenticated",
    ("GET", "/api/settings/handoff"): "authenticated",
    ("PUT", "/api/settings/handoff"): "authenticated",
    ("DELETE", "/api/settings/handoff"): "authenticated",
    ("GET", "/app/settings/memory"): "public",
    ("POST", "/app/settings/memory"): "authenticated",
}
PREFIX = {
    ("POST", "/api/connections/"): "authenticated",
    ("DELETE", "/api/connections/"): "authenticated",
    **{
        (method, prefix): "authenticated"
        for method in ("GET", "POST", "PATCH", "DELETE")
        for prefix in ("/api/boards/", "/api/cards/", "/api/needs-me/")
    },
    **{(method, "/api/rules/"): "authenticated" for method in ("GET", "POST", "PATCH")},
    ("GET", "/api/memory/"): "authenticated",
    ("POST", "/api/memory/"): "authenticated",
    ("GET", "/api/webhooks/"): "authenticated",
    ("POST", "/api/webhooks/"): "authenticated",
    ("DELETE", "/api/webhooks/"): "authenticated",
    ("GET", "/app/"): "public",
    ("GET", "/app/auth/"): "public",
    ("POST", "/app/auth/"): "public",
    ("GET", "/s/"): "public",
    ("GET", "/api/handoffs/"): "authenticated",
    ("POST", "/api/handoffs/"): "authenticated",
    ("GET", "/api/threads/"): "authenticated",
    ("POST", "/api/threads/"): "authenticated",
    ("GET", "/app/cards/"): "public",
    ("GET", "/app/threads/"): "public",
    ("GET", "/app/static/"): "public",
}
BUILTIN = {
    ("GET", "/health"): "public",
    ("GET", "/healthz"): "public",
    ("GET", "/mcp"): "authenticated",
    ("POST", "/mcp"): "authenticated",
}


def request(surface, method="GET", path="/api/me", *, headers=None, body=None, source=None):
    conn = http.client.HTTPConnection("127.0.0.1", surface.port, timeout=5, source_address=source)
    try:
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def hardened(response):
    status, headers, body = response
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert not any(key.lower() == "server" for key in headers)
    assert b"Traceback" not in body and b'File "' not in body
    return status


@pytest.fixture
def make_surface(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONPLACE_RATE_LIMITS", raising=False)
    monkeypatch.delenv("COMMONPLACE_ALLOWED_HOSTS", raising=False)
    surfaces = []

    def make():
        root = tmp_path / str(len(surfaces))
        db = ControlDB(str(root / "control.sqlite3"))
        uid = db.create_user("owner@example.com", is_admin=True)
        workspace = db.create_workspace("home", uid, personal=True)
        supervisor = WorkerSupervisor(str(root), launcher=object())
        gateway = Gateway(db, supervisor)
        clock = SimpleNamespace(now=0.0)
        gateway.limiter = ratelimit.TokenBuckets(clock=lambda: clock.now)
        # These tests exercise the gateway, not the memory worker's behavior.
        gateway.mediator.handle = lambda principal, data: {"jsonrpc": "2.0", "id": 1, "result": {}}
        server = gateway.build_server("127.0.0.1", 0)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        surface = SimpleNamespace(
            db=db,
            uid=uid,
            workspace=workspace,
            gateway=gateway,
            port=server.server_address[1],
            server=server,
            thread=thread,
            clock=clock,
        )
        surfaces.append(surface)
        thread.start()
        return surface

    yield make
    for surface in surfaces:
        surface.server.shutdown()
        surface.server.server_close()
        surface.thread.join(timeout=5)
        webhooks.stop(surface.gateway)
        surface.db.close()


@pytest.fixture
def surface(make_surface):
    return make_surface()


def token(surface, name="owner", *, untrusted=False):
    policy = Policy.build(
        profile="full",
        read_wings=["shared", "inbox"],
        write_wing="inbox",
        trust="untrusted" if untrusted else "trusted",
    )
    cid, value = surface.db.create_token(
        workspace_ref=surface.workspace,
        user_ref=surface.uid,
        name=name,
        client="test",
        policy=policy,
        actor=surface.uid,
    )
    return cid, value, {"Authorization": "Bearer " + value}


def test_complete_installed_route_inventory(surface):
    assert set(surface.gateway.routes) == set(EXACT)
    assert {(m, p) for m, p, _h in surface.gateway.prefix_routes} == set(PREFIX)
    assert len(surface.gateway.prefix_routes) == len(PREFIX)
    for index, ((method, path), auth) in enumerate({**EXACT, **PREFIX, **BUILTIN}.items()):
        concrete = path + "unknown" if (method, path) in PREFIX else path
        headers = {
            "X-Forwarded-For": f"192.0.2.{index + 1}",
            "Content-Type": "application/json",
        }
        response = request(surface, method, concrete, headers=headers, body=b"{")
        status = hardened(response)
        if auth == "authenticated":
            assert status == 401, (method, concrete, response)
        else:
            assert status != 401, (method, concrete, response)


def test_protected_prefix_roots_and_concrete_actions_authenticate_before_validation(surface):
    paths = [
        ("GET", "/api/boards"),
        ("POST", "/api/boards/demo/cards"),
        ("PATCH", "/api/cards/1"),
        ("POST", "/api/cards/1/comments"),
        ("POST", "/api/cards/1/handoff"),
        ("POST", "/api/cards/1/decide"),
        ("GET", "/api/needs-me"),
        ("POST", "/api/memory"),
        ("GET", "/api/memory/search"),
        ("GET", "/api/memory/map"),
        ("GET", "/api/webhooks"),
        ("GET", "/api/webhooks/1/deliveries"),
        ("POST", "/api/webhooks/1/test"),
        ("DELETE", "/api/webhooks/1"),
    ]
    for method, path in paths:
        assert hardened(request(surface, method, path, body=b"invalid")) == 401


@pytest.mark.parametrize("header", [None, "Bearer bad", "Basic secret"])
def test_auth_failures_share_ip_budget_but_success_uses_its_own(surface, header):
    headers = {"Authorization": header} if header else {}
    for _ in range(30):
        assert request(surface, headers=headers)[0] == 401
    response = request(surface, headers=headers)
    assert hardened(response) == 429
    assert json.loads(response[2]) == {"error": "slow down"}
    assert response[1]["Retry-After"] == "2"
    _cid, _value, auth = token(surface)
    assert request(surface, headers=auth)[0] == 200
    surface.clock.now = 2
    assert request(surface, headers=headers)[0] == 401


def test_authenticated_limit_is_600_per_connection(surface):
    _cid, raw, auth = token(surface)
    for _ in range(600):
        assert request(surface, headers=auth)[0] == 200
    response = request(surface, headers=auth)
    assert hardened(response) == 429 and response[1]["Retry-After"] == "1"
    assert request(surface, headers=token(surface, "other")[2])[0] == 200
    assert raw not in repr(surface.gateway.limiter._buckets)


def test_sessions_have_separate_600_request_budgets(surface):
    cookies = []
    for _ in range(2):
        link = sessions.create_login_link(surface.db, "owner@example.com", surface.workspace)
        session = sessions.redeem_login_link(surface.db, link)
        cookies.append((session, {"Cookie": sessions.COOKIE + "=" + session}))
    for _ in range(600):
        assert request(surface, headers=cookies[0][1])[0] == 200
    assert request(surface, headers=cookies[0][1])[0] == 429
    assert request(surface, headers=cookies[1][1])[0] == 200
    assert all(session not in repr(surface.gateway.limiter._buckets) for session, _h in cookies)


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/app/login"),
        ("POST", "/oauth/register"),
        ("POST", "/oauth/token"),
        ("POST", "/oauth/authorize"),
        ("GET", "/app/auth/google"),
        ("POST", "/app/auth/apple/callback"),
    ],
)
def test_sensitive_endpoint_ip_limit(surface, method, path):
    for _ in range(10):
        assert request(surface, method, path)[0] != 429
    response = request(surface, method, path)
    assert hardened(response) == 429 and response[1]["Retry-After"] == "6"
    # Atomic rejection did not consume the anonymous bucket a second time.
    for _ in range(20):
        assert request(surface)[0] == 401
    assert request(surface)[0] == 429


def test_authenticated_requests_still_obey_sensitive_ip_limit(surface):
    auth = token(surface)[2]
    for _ in range(10):
        assert request(surface, "GET", "/app/login", headers=auth)[0] == 302
    assert request(surface, "GET", "/app/login", headers=auth)[0] == 429
    assert request(surface, headers=auth)[0] == 200


def test_provider_inventory_does_not_consume_the_sensitive_ip_budget(surface):
    for _ in range(11):
        assert request(surface, "GET", "/app/auth/providers")[0] == 200
    for _ in range(10):
        assert request(surface, "GET", "/app/auth/google")[0] != 429
    assert request(surface, "GET", "/app/auth/google")[0] == 429
    assert request(surface, "GET", "/app/auth/providers")[0] == 200


def test_short_links_have_120_budget_replacing_anonymous(surface):
    for _ in range(120):
        assert request(surface, "GET", "/s/missing")[0] == 404
    response = request(surface, "GET", "/s/missing")
    assert hardened(response) == 429 and response[1]["Retry-After"] == "1"
    assert request(surface)[0] == 401


def test_untrusted_read_post_and_hourly_write_budgets(surface):
    cid, _value, auth = token(surface, untrusted=True)
    rpc_headers = {**auth, "Content-Type": "application/json"}
    read = json.dumps({"method": "tools/call", "params": {"name": "memory_search"}})
    write = json.dumps({"method": "tools/call", "params": {"name": "memory_save"}})
    for _ in range(60):
        assert request(surface, "POST", "/mcp", headers=rpc_headers, body=read)[0] == 200
    assert request(surface, headers=auth)[0] == 429
    assert not any(key[0] == ratelimit.WRITES.name for key in surface.gateway.limiter._buckets)
    surface.clock.now = 60
    assert request(surface, "POST", "/mcp", headers=rpc_headers, body=write)[0] == 200
    key = (ratelimit.WRITES.name, "connection:" + cid)
    assert surface.gateway.limiter._buckets[key][0] == 119  # charged only once
    for _ in range(119):
        assert surface.gateway.limiter.check([(ratelimit.WRITES, "connection:" + cid)]) == 0
    for method, path, body in [
        ("POST", "/mcp", write),
        ("POST", "/api/memory", "{}"),
        ("PATCH", "/api/cards/1", "{}"),
        ("DELETE", "/api/webhooks/1", None),
    ]:
        response = request(surface, method, path, headers=rpc_headers, body=body)
        assert hardened(response) == 429 and response[1]["Retry-After"] == "30"
    assert request(surface, "POST", "/mcp", headers=rpc_headers, body=read)[0] == 200


@pytest.mark.parametrize(
    "name,expected",
    [
        ("memory_save", True),
        ("memory_link", True),
        ("task_add", True),
        ("mempalace_add_drawer", True),
        ("mempalace_kg_add", True),
        ("memory_get", False),
        ("task_list", False),
        ("mempalace_search", False),
        ("unknown", False),
    ],
)
def test_authoritative_write_classification(surface, name, expected):
    assert (
        _write_request("POST", "/mcp", {"method": "tools/call", "params": {"name": name}})
        is expected
    )


@pytest.mark.parametrize(
    "peer,forwarded,expected",
    [
        ("127.0.0.1", "192.0.2.1, 198.51.100.2", "198.51.100.2"),
        ("127.0.0.1", "2001:0db8::1", "2001:db8::1"),
        ("127.0.0.1", "192.0.2.1, invalid", "127.0.0.1"),
        ("127.0.0.1", "192.0.2.1, ", "127.0.0.1"),
        ("127.0.0.1", "[::1]:80", "127.0.0.1"),
        ("127.0.0.1", "fe80::1%eth0", "127.0.0.1"),
        ("127.0.0.2", "192.0.2.1", "127.0.0.2"),
        ("::1", "192.0.2.1", "::1"),
        # The mapped spelling of loopback is normalised but is not the exact
        # 127.0.0.1 peer Caddy uses, so the forwarded header is not trusted.
        ("::ffff:127.0.0.1", "192.0.2.1", "127.0.0.1"),
    ],
)
def test_forwarded_ip_trust(peer, forwarded, expected):
    assert ratelimit.client_ip(peer, forwarded) == expected


def test_forwarded_leftmost_spoofing_cannot_reset_budget(surface):
    for n in range(30):
        assert request(surface, headers={"X-Forwarded-For": f"192.0.2.{n}, 198.51.100.1"})[0] == 401
    assert request(surface, headers={"X-Forwarded-For": "192.0.2.99, 198.51.100.1"})[0] == 429
    assert request(surface, headers={"X-Forwarded-For": "198.51.100.2"})[0] == 401


def test_non_proxy_peer_cannot_spoof_forwarded_ip(surface):
    for n in range(30):
        assert (
            request(surface, headers={"X-Forwarded-For": f"192.0.2.{n}"}, source=("127.0.0.2", 0))[
                0
            ]
            == 401
        )
    assert (
        request(surface, headers={"X-Forwarded-For": "198.51.100.1"}, source=("127.0.0.2", 0))[0]
        == 429
    )


def test_rate_limits_default_on_and_explicit_off(make_surface, monkeypatch):
    assert make_surface().gateway.rate_limits
    monkeypatch.setenv("COMMONPLACE_RATE_LIMITS", "off")
    surface = make_surface()
    for _ in range(40):
        assert request(surface)[0] == 401
    assert not surface.gateway.limiter._buckets


@pytest.mark.parametrize("host", ["cmmn.dev", "CMMN.DEV.:443", "app.cmmn.dev:8765", "[::1]:8765"])
def test_allowed_host_normalization(make_surface, monkeypatch, host):
    monkeypatch.setenv("COMMONPLACE_ALLOWED_HOSTS", "cmmn.dev, app.cmmn.dev, [::1]")
    surface = make_surface()
    assert request(surface, "GET", "/app/", headers={"Host": host})[0] == 200


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "cmmn.dev.attacker.example",
        "cmmn.dev@attacker.example",
        "cmmn.dev:bad",
        "cmmn.dev:0",
        "cmmn.dev:65536",
        "cmmn.dev:",
        "cmmn.dev/path",
        "cmmn.dev#x",
        "cmmn.dev?x",
        "cmmn.dev#",
        "cmmn.dev?",
        "cmmn.dev\\evil",
        "cmmn.dev ",
        "[::1]:bad",
        "[::1]evil",
        "cmmn.dev..",
    ],
)
def test_host_mismatch_or_malformed_is_421(make_surface, monkeypatch, host):
    monkeypatch.setenv("COMMONPLACE_ALLOWED_HOSTS", "cmmn.dev, [::1]")
    surface = make_surface()
    assert hardened(request(surface, headers={"Host": host})) == 421
    for path in ("/health", "/healthz"):
        assert hardened(request(surface, "GET", path, headers={"Host": host})) == 200


def test_health_exempt_from_rate_limits_and_host_check(make_surface, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_ALLOWED_HOSTS", "cmmn.dev")
    surface = make_surface()
    for _ in range(35):
        assert request(surface, "GET", "/health", headers={"Host": "other"})[0] == 200
    assert not surface.gateway.limiter._buckets


def test_metadata_challenge_and_app_links_use_production_config(surface, monkeypatch):
    monkeypatch.setattr(urls, "AGENT_URL", "https://cmmn.dev")
    monkeypatch.setattr(urls, "APP_URL", "https://app.cmmn.dev")
    attacker = {"Host": "attacker.example", "X-Forwarded-Host": "attacker.example"}
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
    ):
        response = request(surface, "GET", path, headers=attacker)
        assert hardened(response) == 200
        data = json.loads(response[2])
        for value in data.values():
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, str) and item.startswith("http"):
                    assert item == "https://cmmn.dev" or item.startswith("https://cmmn.dev/")
    response = request(surface, "POST", "/mcp", headers=attacker)
    assert hardened(response) == 401
    assert (
        response[1]["WWW-Authenticate"]
        == 'Bearer resource_metadata="https://cmmn.dev/.well-known/oauth-protected-resource"'
    )
    link = urls.share_link(surface.db, surface.workspace, "/app/#/card/1")
    response = request(surface, "GET", urlsplit(link).path, headers=attacker)
    assert hardened(response) == 302
    assert response[1]["Location"] == "https://app.cmmn.dev/app/#/card/1"


@pytest.mark.parametrize(
    "path,cap",
    [
        ("/mcp", MAX_REQUEST_BYTES),
        ("/health", 1024 * 1024),
        ("/healthz", 1024 * 1024),
        ("/api/memory", 1024 * 1024),
        ("/api/boards", 1024 * 1024),
        ("/api/webhooks", 1024 * 1024),
        ("/oauth/token", 65536),
        ("/oauth/register", 65536),
        ("/oauth/authorize", 65536),
    ],
)
def test_oversized_body_rejected_at_http_boundary_before_reading(surface, path, cap):
    conn = http.client.HTTPConnection("127.0.0.1", surface.port, timeout=5)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Content-Length", str(cap + 1))
        conn.endheaders()  # No body: a bounded rejection must not wait to read.
        response = conn.getresponse()
        assert hardened((response.status, dict(response.getheaders()), response.read())) == 413
    finally:
        conn.close()


@pytest.mark.parametrize(
    "headers",
    [
        [("Content-Length", "2"), ("Content-Length", "2")],
        [("Content-Length", "-1")],
        [("Content-Length", "wat")],
        [("Transfer-Encoding", "chunked")],
    ],
)
def test_ambiguous_body_framing_is_closed(surface, headers):
    conn = http.client.HTTPConnection("127.0.0.1", surface.port, timeout=5)
    try:
        conn.putrequest("POST", "/api/memory")
        for key, value in headers:
            conn.putheader(key, value)
        conn.endheaders()
        response = conn.getresponse()
        expected = 411 if headers[0][0] == "Transfer-Encoding" else 400
        assert hardened((response.status, dict(response.getheaders()), response.read())) == expected
        assert response.will_close
    finally:
        conn.close()


def test_static_traversal_and_symlinks_never_disclose_outside_file(surface, tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("safe index")
    sentinel = tmp_path / "private.txt"
    sentinel.write_text("private-surface-sentinel")
    (static / "escape.txt").symlink_to(sentinel)
    monkeypatch.setattr(web, "STATIC", static)
    for suffix in (
        "../private.txt",
        "%2e%2e/private.txt",
        "%2e%2e%2fprivate.txt",
        "%2E%2E%5cprivate.txt",
        "..\\private.txt",
        "%2f..%2fprivate.txt",
        "%252e%252e/private.txt",
        "escape.txt",
    ):
        response = request(surface, "GET", "/app/" + suffix)
        assert hardened(response) == 404 and sentinel.read_bytes() not in response[2]
    # SPA fallback must resolve and validate index.html as well.
    (static / "index.html").unlink()
    (static / "index.html").symlink_to(sentinel)
    response = request(surface, "GET", "/app/some-route")
    assert hardened(response) == 404 and sentinel.read_bytes() not in response[2]
    # Every installed prefix is probed, including protected non-file routes.
    for index, (method, prefix) in enumerate(PREFIX):
        response = request(
            surface,
            method,
            prefix + "%2e%2e/private.txt",
            headers={"X-Forwarded-For": f"198.51.100.{index + 1}"},
        )
        hardened(response)
        assert sentinel.read_bytes() not in response[2]


def test_exceptions_and_request_logs_exclude_credentials_and_memory(surface, caplog):
    secret = "sensitive-token-and-memory-text"
    auth = token(surface)[2]

    def fail(h):
        raise RuntimeError(secret)

    surface.gateway.add_route("GET", "/api/fail", fail)
    with caplog.at_level(logging.DEBUG):
        response = request(surface, "GET", "/api/fail", headers=auth)
        assert hardened(response) == 500 and secret.encode() not in response[2]
        request(surface, "GET", "/app/login?t=" + secret)
        request(surface, "GET", "/api/memory/search?q=" + secret)
    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text and "test_commonplace_public_surface.py" in caplog.text


def test_hardening_on_success_redirect_405_404_and_notification(surface):
    auth = token(surface)[2]
    assert hardened(request(surface, headers=auth)) == 200
    assert hardened(request(surface, "GET", "/mcp", headers=auth)) == 405
    assert hardened(request(surface, "GET", "/missing")) == 404
    link = sessions.create_login_link(surface.db, "owner@example.com", surface.workspace)
    assert hardened(request(surface, "GET", "/app/login?t=" + link)) == 302
    surface.gateway.mediator.handle = lambda principal, data: None
    assert (
        hardened(
            request(
                surface,
                "POST",
                "/mcp",
                headers={**auth, "Content-Type": "application/json"},
                body=b"{}",
            )
        )
        == 202
    )


def test_bucket_refill_retry_rounding_atomicity_and_expiration():
    clock = SimpleNamespace(now=0.0)
    buckets = ratelimit.TokenBuckets(clock=lambda: clock.now)
    policies = [(ratelimit.CREDENTIALS, "ip"), (ratelimit.ANONYMOUS, "ip")]
    for _ in range(10):
        assert buckets.check(policies) == 0
    before = dict(buckets._buckets)
    clock.now = 0.1
    assert buckets.check(policies) == 6
    assert buckets._buckets == before
    clock.now = 6
    assert buckets.check(policies) == 0
    clock.now = 3666
    assert buckets.check([(ratelimit.WRITES, "other")]) == 0
    assert len(buckets._buckets) == 1


def test_bucket_state_is_bounded_without_resetting_live_limits():
    clock = SimpleNamespace(now=0.0)
    buckets = ratelimit.TokenBuckets(clock=lambda: clock.now, max_entries=2)
    assert buckets.check([(ratelimit.ANONYMOUS, "one")]) == 0
    assert buckets.check([(ratelimit.ANONYMOUS, "two")]) == 0
    assert buckets.check([(ratelimit.ANONYMOUS, "three")]) == 60
    assert len(buckets._buckets) == 2
    clock.now = 60
    assert buckets.check([(ratelimit.ANONYMOUS, "three")]) == 0


def test_bucket_updates_are_synchronized():
    buckets = ratelimit.TokenBuckets(clock=lambda: 0.0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: buckets.check([(ratelimit.ANONYMOUS, "ip")]), range(100)))
    assert results.count(0) == 30


def test_hourly_write_bucket_refills_after_thirty_seconds():
    clock = SimpleNamespace(now=0.0)
    buckets = ratelimit.TokenBuckets(clock=lambda: clock.now)
    for _ in range(120):
        assert buckets.check([(ratelimit.WRITES, "connection")]) == 0
    assert buckets.check([(ratelimit.WRITES, "connection")]) == 30
    clock.now = 30
    assert buckets.check([(ratelimit.WRITES, "connection")]) == 0


def test_host_normalizer_validates_ports_and_ipv6():
    assert _host_name("[2001:0db8::1]:443") == "2001:db8::1"


def test_keepalive_request_reauthenticates_after_revocation(surface):
    cid, _raw, auth = token(surface)
    conn = http.client.HTTPConnection("127.0.0.1", surface.port, timeout=5)
    try:
        headers = {**auth, "Content-Type": "application/json"}
        conn.request("POST", "/mcp", b"{}", headers)
        response = conn.getresponse()
        assert response.status == 200
        response.read()
        surface.db.revoke(cid, actor=surface.uid)
        conn.request("POST", "/mcp", b"{}", headers)
        response = conn.getresponse()
        assert hardened((response.status, dict(response.getheaders()), response.read())) == 401
    finally:
        conn.close()


@pytest.mark.parametrize("hosts", [[], ["cmmn.dev", "attacker.example"]])
def test_missing_or_duplicate_host_is_421(make_surface, monkeypatch, hosts):
    monkeypatch.setenv("COMMONPLACE_ALLOWED_HOSTS", "cmmn.dev")
    surface = make_surface()
    conn = http.client.HTTPConnection("127.0.0.1", surface.port, timeout=5)
    try:
        conn.putrequest("GET", "/app/", skip_host=True)
        for host in hosts:
            conn.putheader("Host", host)
        conn.endheaders()
        response = conn.getresponse()
        assert hardened((response.status, dict(response.getheaders()), response.read())) == 421
    finally:
        conn.close()


def test_stdlib_error_paths_are_generic_and_hardened(surface):
    response = request(surface, "TRACE", "/unknown?token=never-echo-this")
    assert hardened(response) == 501
    assert b"never-echo-this" not in response[2]


def test_mcp_failure_preserves_rpc_envelope_without_exception_text(surface, caplog):
    auth = token(surface)[2]

    def fail(principal, data):
        raise RuntimeError("private memory and credentials")

    surface.gateway.mediator.handle = fail
    response = request(
        surface,
        "POST",
        "/mcp",
        headers={**auth, "Content-Type": "application/json"},
        body=b'{"id": 42}',
    )
    assert hardened(response) == 500
    assert json.loads(response[2]) == {
        "jsonrpc": "2.0",
        "id": 42,
        "error": {"code": -32603, "message": "Internal error"},
    }
    assert "private memory and credentials" not in caplog.text


REST_TOOLS = [
    ("GET", "/api/boards", "board_list"),
    ("POST", "/api/boards", "board_add"),
    ("GET", "/api/boards/work/cards", "task_list"),
    ("POST", "/api/boards/work/cards", "task_add"),
    ("GET", "/api/cards/1", "task_show"),
    ("PATCH", "/api/cards/1", "task_update"),
    ("POST", "/api/cards/1/comments", "task_comment"),
    ("POST", "/api/cards/1/handoff", "task_handoff"),
    ("POST", "/api/cards/1/decide", "decide"),
    ("GET", "/api/needs-me", "needs_me"),
    ("GET", "/api/memory/search", "memory_search"),
    ("GET", "/api/memory/map", "memory_search"),
    ("GET", "/api/memory/id", "memory_get"),
    ("POST", "/api/memory", "memory_save"),
    ("GET", "/api/webhooks", "webhook_list"),
    ("POST", "/api/webhooks", "webhook_add"),
    ("DELETE", "/api/webhooks/id", "webhook_remove"),
    ("GET", "/api/webhooks/id/deliveries", "webhook_list"),
    ("POST", "/api/webhooks/id/test", "webhook_test"),
]


@pytest.mark.parametrize("method,path,tool", REST_TOOLS)
def test_rest_requires_the_matching_visible_tool(surface, method, path, tool):
    from commonplace.http_util import send_json

    surface.gateway.add_route(method, path, lambda h: send_json(h, 200, {"reached": True}))
    for index, allowed in enumerate(([], [tool])):
        policy = Policy.build(tools=allowed, trust="trusted", read_wings=["shared"])
        _cid, value = surface.db.create_token(
            workspace_ref=surface.workspace,
            user_ref=surface.uid,
            name=f"tools-{index}",
            client="test",
            policy=policy,
            actor=surface.uid,
        )
        assert request(
            surface, method, path, headers={"Authorization": "Bearer " + value}, body=b"{}"
        )[0] == (200 if allowed else 403)
    session = sessions.redeem_login_link(
        surface.db, sessions.create_login_link(surface.db, "owner@example.com", surface.workspace)
    )
    assert (
        request(
            surface,
            method,
            path,
            headers={"Cookie": sessions.COOKIE + "=" + session, "Content-Type": "application/json"},
            body=b"{}",
        )[0]
        == 200
    )


@pytest.mark.parametrize("profile", ["slim", "standard", "full"])
@pytest.mark.parametrize("restricted", [False, True])
def test_rest_profiles_apply_the_same_tool_visibility(surface, profile, restricted):
    from commonplace.http_util import send_json

    surface.gateway.add_route("PATCH", "/api/cards/1", lambda h: send_json(h, 200, {}))
    policy = Policy.build(
        profile=profile, tools=["task_show"] if restricted else None, trust="trusted"
    )
    assert policy.tools == (("task_show",) if restricted else profile)
    _cid, value = surface.db.create_token(
        workspace_ref=surface.workspace,
        user_ref=surface.uid,
        name="slim",
        client="test",
        policy=policy,
        actor=surface.uid,
    )
    assert policy.tool_visible("task_update") is (not restricted)
    assert request(
        surface,
        "PATCH",
        "/api/cards/1",
        headers={"Authorization": "Bearer " + value},
        body=b"{}",
    )[0] == (403 if restricted else 200)


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/boards"),
        ("PATCH", "/api/cards/1"),
        ("DELETE", "/api/webhooks/id"),
        ("POST", "/app/logout"),
    ],
)
@pytest.mark.parametrize(
    "content_type,origin",
    [
        (None, None),
        ("text/plain", None),
        ("application/json", "https://evil.example"),
        ("application/json", "https://app.example.evil"),
        ("application/json", "null"),
        ("application/json", "https://user@app.example"),
        ("application/json", "https://app.example/path"),
        ("application/json", "https://app.example?"),
        ("application/json", "https://app.example#"),
        ("application/json", ""),
    ],
)
def test_session_writes_require_json_and_valid_origin(
    surface, monkeypatch, method, path, content_type, origin
):
    from commonplace.http_util import send_json

    monkeypatch.setattr(urls, "APP_URL", "https://app.example")
    surface.gateway.add_route(method, path, lambda h: send_json(h, 200, {}))
    session = sessions.redeem_login_link(
        surface.db, sessions.create_login_link(surface.db, "owner@example.com", surface.workspace)
    )
    headers = {"Cookie": sessions.COOKIE + "=" + session}
    if content_type is not None:
        headers["Content-Type"] = content_type
    if origin is not None:
        headers["Origin"] = origin
    assert request(surface, method, path, headers=headers, body=b"{}")[0] == 403
    assert request(surface, headers={"Cookie": headers["Cookie"]})[0] == 200
    assert (
        request(surface, method, path, headers={**headers, **token(surface)[2]}, body=b"{}")[0]
        == 200
    )


@pytest.mark.parametrize("origin", [None, "app", "local", "default-port"])
def test_session_json_writes_accept_app_and_request_origins(surface, monkeypatch, origin):
    from commonplace.http_util import send_json

    monkeypatch.setattr(urls, "APP_URL", "https://app.example")
    surface.gateway.add_route("POST", "/api/boards", lambda h: send_json(h, 200, {}))
    session = sessions.redeem_login_link(
        surface.db, sessions.create_login_link(surface.db, "owner@example.com", surface.workspace)
    )
    headers = {
        "Cookie": sessions.COOKIE + "=" + session,
        "Content-Type": "application/json; charset=utf-8",
    }
    if origin:
        headers["Origin"] = {
            "app": "https://app.example",
            "local": f"http://127.0.0.1:{surface.port}",
            "default-port": "https://app.example:443",
        }[origin]
    assert request(surface, "POST", "/api/boards", headers=headers, body=b"{}")[0] == 200


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PATCH", "/api/rules/checks", {"agent": "*", "mode": "warn"}),
        ("POST", "/api/rules/store/accept", {}),
    ],
)
def test_rules_control_routes_require_a_person_owner(surface, method, path, body):
    response = request(
        surface, method, path, body=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    assert hardened(response) == 401
    _cid, _value, headers = token(surface)
    response = request(
        surface,
        method,
        path,
        body=json.dumps(body),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert hardened(response) == 403
    assert surface.db.rule_checks(surface.workspace) == {}
    assert surface.db.rule_stores(surface.workspace) is None
