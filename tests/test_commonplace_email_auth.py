"""Email sign-in over public HTTP, starting with no users or workspaces."""

import http.client
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from commonplace import email_auth, mailer, plans, ratelimit, sessions, urls
from commonplace.control_db import ControlDB
from commonplace.gateway import Gateway
from commonplace.workers import WorkerSupervisor
from tests.test_commonplace_mailer import smtp_env as smtp_env, smtp_server as smtp_server


class Mailbox:
    configured = True

    def __init__(self):
        self.messages = []

    def send(self, to, subject, text, html=None):
        self.messages.append((to, subject, text, html))

    def link(self, index=-1):
        return re.search(r"https?://\S+", self.messages[index][2])[0]


class Client:
    def __init__(self, gateway, port):
        self.gateway, self.port = gateway, port
        self.requests = 0

    def request(self, path, method="GET", *, data=None, cookie=None, headers=None, raw=None):
        self.requests += 1
        hdrs = {"X-Forwarded-For": f"192.0.2.{self.requests}"}
        hdrs.update(headers or {})
        if data is not None:
            raw = json.dumps(data)
            hdrs["Content-Type"] = "application/json"
        if cookie:
            hdrs["Cookie"] = cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method, path, body=raw, headers=hdrs)
            response = conn.getresponse()
            self.response_headers = response.getheaders()
            return response.status, dict(self.response_headers), response.read()
        finally:
            conn.close()

    def ask(self, email="fresh@example.test", **kwargs):
        return self.request("/app/auth/email", "POST", data={"email": email, **kwargs})

    def verify(self, link):
        parts = urlsplit(link)
        return self.request(parts.path, "POST", data={"t": parse_qs(parts.query)["t"][0]})

    def signin(self, mailbox, email="fresh@example.test", **kwargs):
        assert self.ask(email, **kwargs)[0] == 202
        status, headers, content = self.verify(mailbox.link())
        assert status == 303 and not content
        return headers["Set-Cookie"].split(";", 1)[0]

    def me(self, cookie):
        status, _, content = self.request("/api/me", cookie=cookie)
        assert status == 200
        return json.loads(content)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "open")
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    monkeypatch.delenv("COMMONPLACE_RATE_LIMITS", raising=False)
    monkeypatch.delenv("COMMONPLACE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("COMMONPLACE_COOKIE_DOMAIN", raising=False)
    for provider in ("GOOGLE", "GITHUB", "APPLE", "X", "DISCORD"):
        for key in ("CLIENT_ID", "CLIENT_SECRET", "TEAM_ID", "KEY_ID", "PRIVATE_KEY"):
            monkeypatch.delenv(f"COMMONPLACE_{provider}_{key}", raising=False)
    servers = []

    def make(*, smtp=False):
        mailbox = Mailbox()
        if not smtp:
            monkeypatch.setattr(mailer, "from_env", lambda: mailbox)
        root = tmp_path / str(len(servers))
        db = ControlDB(str(root / "control.sqlite3"))
        supervisor = WorkerSupervisor(str(root), launcher=object())
        gateway = Gateway(db, supervisor)
        clock = SimpleNamespace(now=0.0)
        gateway.limiter = ratelimit.TokenBuckets(clock=lambda: clock.now)
        server = gateway.build_server("127.0.0.1", 0)
        monkeypatch.setattr(urls, "APP_URL", f"http://127.0.0.1:{server.server_address[1]}")
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        servers.append((server, thread, supervisor, db))
        client = Client(gateway, server.server_address[1])
        client.db, client.clock = db, clock
        return client, mailbox

    yield make
    for server, thread, supervisor, db in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        supervisor.stop_all()
        db.close()


def counts(client):
    return len(client.db.users()), len(client.db.workspaces())


def refused(response):
    status, headers, content = response
    assert status == 400
    assert "Set-Cookie" not in headers and "Location" not in headers
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert content == b"Sign in link is invalid or expired"


def test_fresh_signup_isolation_logout_and_closed_signup_relogin(fresh, monkeypatch, caplog):
    client, mailbox = fresh()
    assert counts(client) == (0, 0)
    status, _, page = client.request("/app/")
    assert status == 200 and b'aria-label="Email"' in page
    assert b'action="/app/auth/email"' in page
    assert client.ask()[0] == 202
    assert counts(client) == (0, 0)
    link = mailbox.link()
    token = parse_qs(urlsplit(link).query)["t"][0]
    assert mailbox.messages[0][0] == "fresh@example.test"
    with client.db._lock:
        pending = [tuple(row) for row in client.db._conn.execute("SELECT * FROM email_challenges")]
        assert len(pending) == 1 and token not in str(pending)
    status, headers, content = client.verify(link)
    assert status == 303 and headers["Location"] == "/app/" and not content
    assert headers["Referrer-Policy"] == "no-referrer"
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    for attr in ("HttpOnly", "SameSite=Lax", "Path=/"):
        assert attr in headers["Set-Cookie"]
    assert token not in str(headers) and token not in caplog.text
    me = client.me(cookie)
    assert me["email"] == "fresh@example.test"
    assert counts(client) == (1, 1)
    assert client.db.workspace(me["workspace"])["personal_of"] == me["user"]
    assert plans.get_plan(client.db, me["user"]) == plans.FREE
    assert client.db.user(me["user"])["is_admin"] == 0
    refused(client.verify(link))
    # Only the public signup journey creates the second account too.
    other_cookie = client.signin(mailbox, "second@example.test")
    other = client.me(other_cookie)
    assert other["workspace"] != me["workspace"] and other["user"] != me["user"]
    response = client.request(
        "/api/boards", "POST", cookie=cookie, data={"slug": "private-space", "name": "Private"}
    )
    assert response[0] == 201, response
    assert b"private-space" in client.request("/api/boards", cookie=cookie)[2]
    assert b"private-space" not in client.request("/api/boards", cookie=other_cookie)[2]
    assert client.request("/api/boards/private-space", cookie=other_cookie)[0] == 404
    old_cookie = cookie
    status, headers, _ = client.request(
        "/app/logout", "POST", cookie=cookie, data={}, headers={"Origin": urls.APP_URL}
    )
    assert status == 204 and "Max-Age=0" in headers["Set-Cookie"]
    assert client.request("/api/me", cookie=old_cookie)[0] == 401
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    cookie = client.signin(mailbox, "FRESH@EXAMPLE.TEST")
    assert client.me(cookie) == me and cookie != old_cookie
    assert counts(client) == (2, 2)
    # Only hashes are persisted, including legacy login tokens minted internally.
    with client.db._lock:
        for table in ("email_challenges", "sessions", "login_links", "audit_log"):
            rows = [tuple(row) for row in client.db._conn.execute(f"SELECT * FROM {table}")]
            assert token not in str(rows)
    assert token not in caplog.text and link not in caplog.text


def test_real_smtp_capture_verifies_a_fresh_account(fresh, smtp_env, smtp_server):
    client, _ = fresh(smtp=True)
    assert counts(client) == (0, 0)
    assert client.ask("smtp-user@example.test")[0] == 202
    assert counts(client) == (0, 0)
    [message] = smtp_server.messages
    assert str(message["To"]) == "smtp-user@example.test"
    link = re.search(r"https?://\S+", message.get_content())[0]
    status, headers, _ = client.verify(link)
    assert status == 303
    assert client.me(headers["Set-Cookie"].split(";", 1)[0])["email"] == "smtp-user@example.test"


def test_prefetch_does_not_consume_link_or_create_account(fresh):
    client, mailbox = fresh()
    assert client.ask()[0] == 202
    link = urlsplit(mailbox.link())
    path = link.path + "?" + link.query
    token = parse_qs(link.query)["t"][0]
    for _ in range(2):
        status, headers, page = client.request(path)
        assert status == 200 and "Set-Cookie" not in headers
        assert headers["Cache-Control"] == "no-store"
        assert headers["Referrer-Policy"] == "strict-origin"
        assert [
            value for name, value in client.response_headers if name.lower() == "referrer-policy"
        ] == ["strict-origin"]
        assert b'<form method="post">' in page and b'value="1">Sign in</button>' in page
        assert token.encode() not in page
        assert counts(client) == (0, 0)

    def challenge_rows():
        with client.db._lock:
            return [tuple(row) for row in client.db._conn.execute("SELECT * FROM email_challenges")]

    for stylesheet in ("/app/app.css", "/app/ui.css"):
        assert stylesheet.encode() in page
        status, headers, _ = client.request(stylesheet)
        assert status == 200 and "text/css" in headers["Content-Type"]
    assert client.request("/app/brand/logo-small-dark.svg")[0] == 200
    before = challenge_rows()
    assert len(before) == 1
    for origin_headers in (
        {"Origin": "https://another.example"},
        {"Origin": "null"},
        {"Origin": "not-an-origin"},
        {"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    ):
        denied = client.request(
            path,
            "POST",
            raw="confirm=1",
            headers={"Content-Type": "application/x-www-form-urlencoded", **origin_headers},
        )
        assert denied[0] == 403 and "Set-Cookie" not in denied[1]
        assert counts(client) == (0, 0)
        assert challenge_rows() == before
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=3)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/x-www-form-urlencoded")
        conn.putheader("Content-Length", "9")
        conn.putheader("Origin", urls.APP_URL)
        conn.putheader("Origin", urls.APP_URL)
        conn.endheaders(b"confirm=1")
        response = conn.getresponse()
        assert response.status == 403 and response.getheader("Set-Cookie") is None
        response.read()
    finally:
        conn.close()
    assert counts(client) == (0, 0) and challenge_rows() == before
    status, headers, _ = client.request(
        path,
        "POST",
        raw="confirm=1",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": urls.APP_URL},
    )
    assert status == 303
    assert client.me(headers["Set-Cookie"].split(";", 1)[0])["email"] == "fresh@example.test"
    refused(client.request(path))
    refused(client.verify(mailbox.link()))
    assert client.ask("no-origin@example.test")[0] == 202
    assert client.verify(mailbox.link())[0] == 303


@pytest.mark.parametrize("signup", [None, "", "closed", " CLOSED ", "invalid"])
def test_closed_signup_is_neutral_and_creates_nothing(fresh, monkeypatch, signup):
    client, mailbox = fresh()
    cookie = client.signin(mailbox)
    before = counts(client)
    if signup is None:
        monkeypatch.delenv("COMMONPLACE_SIGNUP", raising=False)
    else:
        monkeypatch.setenv("COMMONPLACE_SIGNUP", signup)
    known = client.ask()
    unknown = client.ask("unknown@example.test")
    assert known[0] == unknown[0] == 202
    assert known[2] == unknown[2] == json.dumps({"message": email_auth.MESSAGE}).encode()
    assert len(mailbox.messages) == 2 and counts(client) == before
    assert client.verify(mailbox.link())[0] == 303
    assert client.me(cookie)["email"] == "fresh@example.test"


def test_signup_rechecks_guard_at_verification(fresh, monkeypatch):
    client, mailbox = fresh()
    assert client.ask()[0] == 202
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    refused(client.verify(mailbox.link()))
    assert counts(client) == (0, 0)
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "open")
    refused(client.verify(mailbox.link()))


@pytest.mark.parametrize("query", ["", "?t=bad", "?t=" + "x" * 44, "?t=a&t=b"])
def test_invalid_challenges_create_no_accounts(fresh, query):
    client, _ = fresh()
    refused(client.request("/app/auth/email/verify" + query))
    assert counts(client) == (0, 0)


def test_expired_challenge_fails_without_provisioning(fresh, monkeypatch):
    client, mailbox = fresh()
    now = sessions._now()
    monkeypatch.setattr(sessions, "_now", lambda: now)
    assert client.ask()[0] == 202
    monkeypatch.setattr(sessions, "_now", lambda: now + timedelta(minutes=sessions.LINK_MINUTES))
    refused(client.verify(mailbox.link()))
    assert counts(client) == (0, 0)


def test_concurrent_challenge_redemption_has_one_winner(fresh):
    client, mailbox = fresh()
    assert client.ask()[0] == 202
    path = urlsplit(mailbox.link())
    path = path.path + "?" + path.query
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.request(path, "POST", data={}), range(2)))
    assert sorted(response[0] for response in responses) == [303, 400]
    assert counts(client) == (1, 1)


@pytest.mark.parametrize(
    "next_path",
    [
        "https://evil.example/",
        "//evil.example/",
        "/%2fevil.example/",
        "/app/\\evil",
        "/app/%0d%0aX:x",
        "/app/auth/email/verify?t=bad",
    ],
)
def test_unsafe_destinations_do_not_redirect_or_affect_mailed_origin(fresh, next_path):
    client, mailbox = fresh()
    assert client.ask(next=next_path)[0] == 202
    assert mailbox.link().startswith(urls.APP_URL + "/app/auth/email/verify?t=")
    assert client.verify(mailbox.link())[1]["Location"] == "/app/"


def test_safe_destination_is_bound_to_challenge_and_secure_cookie(fresh):
    client, mailbox = fresh()
    client.gateway.secure_cookies = True
    assert client.ask(next="/app/#/board/work")[0] == 202
    status, headers, _ = client.verify(mailbox.link() + "&next=https://evil.example")
    assert status == 303 and headers["Location"] == "/app/#/board/work"
    assert "; Secure" in headers["Set-Cookie"]


@pytest.mark.parametrize("signup", ["open", "closed"])
def test_email_rate_limit_normalizes_addresses_across_ips_and_refills(fresh, monkeypatch, signup):
    client, mailbox = fresh()
    monkeypatch.setenv("COMMONPLACE_SIGNUP", signup)
    for address in ("fresh@example.test", "FRESH@EXAMPLE.TEST", " fresh@example.test "):
        assert client.ask(address)[0] == 202
    status, headers, content = client.ask()
    assert status == 429 and int(headers["Retry-After"]) == 300
    assert b"fresh" not in content
    assert len(mailbox.messages) == (3 if signup == "open" else 0)
    assert "fresh@example.test" not in str(client.gateway.limiter._buckets)
    client.clock.now += 300
    assert client.ask()[0] == 202


def test_ip_rate_limit_covers_unknown_addresses(fresh, monkeypatch):
    client, mailbox = fresh()
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    for n in range(10):
        assert (
            client.request(
                "/app/auth/email",
                "POST",
                data={"email": f"n{n}@example.test"},
                headers={"X-Forwarded-For": "192.0.2.200"},
            )[0]
            == 202
        )
    status, headers, _ = client.request(
        "/app/auth/email",
        "POST",
        data={"email": "another@example.test"},
        headers={"X-Forwarded-For": "192.0.2.200"},
    )
    assert status == 429 and headers["Retry-After"] == "6"
    assert counts(client) == (0, 0) and not mailbox.messages


@pytest.mark.parametrize(
    "email",
    [
        None,
        123,
        [],
        "invalid",
        "a@example.test\nBcc:evil@example.test",
        "Name <a@example.test>",
        "a@example.test,b@example.test",
        ".a@example.test",
    ],
)
def test_malformed_email_is_bounded_and_never_reflected(fresh, email):
    client, mailbox = fresh()
    status, _, content = client.ask(email)
    assert status == 400 and content == b'{"error": "Enter a valid email address"}'
    assert counts(client) == (0, 0) and not mailbox.messages


def test_form_encoded_request_uses_same_journey(fresh):
    client, mailbox = fresh()
    response = client.request(
        "/app/auth/email",
        "POST",
        raw=urlencode({"email": "form@example.test"}),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Host": "evil.example"},
    )
    assert response[0] == 202
    assert mailbox.link().startswith(urls.APP_URL + "/app/auth/email/verify?t=")
    assert client.verify(mailbox.link())[0] == 303


def test_delivery_failure_discards_challenge_and_never_logs_secret(fresh, caplog):
    client, mailbox = fresh()

    def fail(to, subject, text, html=None):
        mailbox.messages.append((to, subject, text, html))
        raise mailer.MailError(text)

    mailbox.send = fail
    assert client.ask()[0] == 202
    refused(client.verify(mailbox.link()))
    assert mailbox.link() not in caplog.text and "fresh@example.test" not in caplog.text
    assert counts(client) == (0, 0)


def test_disabled_account_cannot_request_or_redeem_challenge(fresh):
    client, mailbox = fresh()
    cookie = client.signin(mailbox)
    assert client.ask()[0] == 202
    with client.db._lock:
        client.db._conn.execute("UPDATE users SET disabled_at='2026-01-01T00:00:00Z'")
    refused(client.verify(mailbox.link()))
    assert client.ask()[0] == 202 and len(mailbox.messages) == 2
    assert client.request("/api/me", cookie=cookie)[0] == 401


def test_unconfigured_mail_is_neutral_and_creates_nothing(fresh):
    client, mailbox = fresh()
    client.gateway.email_auth.mailer = mailer.DisabledMailer()
    assert client.ask()[0] == 202
    assert counts(client) == (0, 0) and not mailbox.messages
