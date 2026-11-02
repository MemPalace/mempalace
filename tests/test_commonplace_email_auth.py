"""Email sign-in over public HTTP, starting with no users or workspaces."""

import http.client
import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
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
        self.changed = threading.Condition()
        self.releases = []

    def send(self, to, subject, text, html=None):
        with self.changed:
            self.messages.append((to, subject, text, html))
            self.changed.notify_all()

    def link(self, index=-1, *, wait=True):
        if wait:
            delivered(self.auth)
        with self.changed:
            count = index + 1 if index >= 0 else -index
            assert self.changed.wait_for(lambda: len(self.messages) >= count, timeout=3)
        return re.search(r"https?://\S+", self.messages[index][2])[0]


def delivered(auth):
    """Wait for processing, including failure cleanup, with a finite watchdog."""
    pending = auth._pending
    with pending.all_tasks_done:
        assert pending.all_tasks_done.wait_for(lambda: pending.unfinished_tasks == 0, timeout=3)


@contextmanager
def held_mail(mailbox, error=None):
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    mailbox.releases.append(release)
    original = mailbox.send

    def send(*args):
        original(*args)
        entered.set()
        try:
            assert release.wait(5), "test did not release mail delivery"
            if error:
                raise error(args[0] + args[2])
        finally:
            returned.set()

    mailbox.send = send
    try:
        yield SimpleNamespace(entered=entered, release=release, returned=returned)
    finally:
        release.set()
        mailbox.send = original


class Background:
    def __init__(self, call):
        self.done = threading.Event()
        self.error = None

        def run():
            try:
                self.value = call()
            except BaseException as exc:
                self.error = exc
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def result(self):
        assert self.done.wait(3), "background operation did not finish"
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()
        if self.error:
            raise self.error
        return self.value


class Client:
    def __init__(self, gateway, port):
        self.gateway, self.port = gateway, port
        self.requests = 0
        self.backgrounds = []

    def background(self, call):
        job = Background(call)
        self.backgrounds.append(job)
        return job

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

    def verify(self, link, *, headers=None):
        parts = urlsplit(link)
        return self.request(
            parts.path, "POST", data={"t": parse_qs(parts.query)["t"][0]}, headers=headers
        )

    def signin(self, mailbox, email="fresh@example.test", **kwargs):
        assert self.ask(email, **kwargs)[0] == 202
        link = mailbox.link()
        parts = urlsplit(link)
        assert self.request(parts.path + "?" + parts.query)[0] == 200
        status, headers, content = self.verify(link, headers={"Origin": urls.APP_URL})
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
        mailbox.auth = gateway.email_auth
        clock = SimpleNamespace(now=0.0)
        gateway.limiter = ratelimit.TokenBuckets(clock=lambda: clock.now)
        server = gateway.build_server("127.0.0.1", 0)
        monkeypatch.setattr(urls, "APP_URL", f"http://127.0.0.1:{server.server_address[1]}")
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        client = Client(gateway, server.server_address[1])
        client.db, client.clock, client.server = db, clock, server
        servers.append((server, thread, supervisor, db, client, mailbox))
        return client, mailbox

    yield make
    for server, thread, supervisor, db, client, mailbox in servers:
        for release in mailbox.releases:
            release.set()
        for job in client.backgrounds:
            job.thread.join(timeout=3)
            assert not job.thread.is_alive()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
        gateway = client.gateway
        gateway.webhooks.stop()
        handoff = getattr(gateway, "handoff", None)
        if handoff is not None:
            handoff.close()
        supervisor.stop_all()
        db.close()
        if gateway.email_auth._started:
            gateway.email_auth._worker.join(timeout=3)
            assert not gateway.email_auth._worker.is_alive()


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
    assert status == 303 and headers["Location"] == "/app/#/board/commonplace" and not content
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
    delivered(client.gateway.email_auth)
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
    delivered(client.gateway.email_auth)
    assert len(mailbox.messages) == 2 and counts(client) == before
    assert client.verify(mailbox.link())[0] == 303
    assert client.me(cookie)["email"] == "fresh@example.test"


def test_signup_rechecks_guard_at_verification(fresh, monkeypatch):
    client, mailbox = fresh()
    assert client.ask()[0] == 202
    mailbox.link()
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
    mailbox.link()
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
    assert client.verify(mailbox.link())[1]["Location"] == "/app/#/board/commonplace"


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
    delivered(client.gateway.email_auth)
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
    delivered(client.gateway.email_auth)
    assert counts(client) == (0, 0) and not mailbox.messages
    assert not challenges(client)


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
    mailbox.link()
    with client.db._lock:
        client.db._conn.execute("UPDATE users SET disabled_at='2026-01-01T00:00:00Z'")
    refused(client.verify(mailbox.link()))
    assert client.ask()[0] == 202
    delivered(client.gateway.email_auth)
    assert len(mailbox.messages) == 2
    assert not challenges(client)
    assert client.request("/api/me", cookie=cookie)[0] == 401


def test_unconfigured_mail_is_neutral_and_creates_nothing(fresh):
    client, mailbox = fresh()
    client.gateway.email_auth.mailer = mailer.DisabledMailer()
    assert client.ask()[0] == 202
    delivered(client.gateway.email_auth)
    assert counts(client) == (0, 0) and not mailbox.messages
    assert not challenges(client)


def known_user(client, mailbox):
    """Bootstrap only through public GET confirmation and same-origin POST."""
    assert counts(client) == (0, 0)
    cookie = client.signin(mailbox)
    me = client.me(cookie)
    assert counts(client) == (1, 1)
    assert client.db.user(me["user"])["is_admin"] == 0
    assert not client.db.user(me["user"])["disabled_at"]
    refused(client.verify(mailbox.link()))
    return me


def challenges(client):
    with client.db._lock:
        return list(client.db._conn.execute("SELECT * FROM email_challenges"))


class SQLBoundary:
    """Observe invocations, even ones SQLite would refuse after connection close."""

    def __init__(self, conn):
        self.conn = conn
        self.forbidden = False
        self.late = []
        self.before_execute = lambda sql: None

    def execute(self, sql, *args):
        if self.forbidden:
            self.late.append(sql)
        self.before_execute(sql)
        return self.conn.execute(sql, *args)

    def executescript(self, sql):
        if self.forbidden:
            self.late.append(sql)
        return self.conn.executescript(sql)

    def close(self):
        self.conn.close()
        self.forbidden = True

    def __getattr__(self, name):
        return getattr(self.conn, name)


def observe_sql(client):
    # Stop unrelated dispatch before measuring the email-delivery lifecycle.
    client.gateway.webhooks.stop()
    with client.db._lock:
        boundary = SQLBoundary(client.db._conn)
        client.db._conn = boundary
    return boundary


def test_known_and_unknown_return_before_held_delivery(fresh, monkeypatch):
    client, mailbox = fresh()
    known_user(client, mailbox)
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    auth = client.gateway.email_auth
    worker = auth._worker
    with held_mail(mailbox) as hold:
        known = client.background(client.ask)
        assert hold.entered.wait(3)
        unknown = client.background(lambda: client.ask("unknown@example.test"))
        known_response, unknown_response = known.result(), unknown.result()
        assert not hold.release.is_set() and not hold.returned.is_set()
        assert known_response[0] == unknown_response[0] == 202
        assert (
            known_response[2]
            == unknown_response[2]
            == json.dumps({"message": email_auth.MESSAGE}).encode()
        )
        assert counts(client) == (1, 1) and len(challenges(client)) == 1
        hold.release.set()
        delivered(auth)
    link = mailbox.link()
    parts = urlsplit(link)
    assert client.request(parts.path + "?" + parts.query)[0] == 200
    assert client.verify(link, headers={"Origin": "https://another.example"})[0] == 403
    assert client.verify(link, headers={"Origin": urls.APP_URL})[0] == 303
    refused(client.verify(link))
    assert len(mailbox.messages) == 2 and auth._worker is worker and worker.is_alive()
    assert counts(client) == (1, 1)


def test_request_admission_does_not_wait_for_account_sql(fresh, monkeypatch):
    client, mailbox = fresh()
    known_user(client, mailbox)
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    # The worker cannot look up either account until both HTTP responses finish.
    with client.db._lock:
        known = client.background(client.ask).result()
        unknown = client.background(lambda: client.ask("unknown@example.test")).result()
        assert known[0] == unknown[0] == 202 and known[2] == unknown[2]
        assert len(mailbox.messages) == 1
    delivered(client.gateway.email_auth)
    assert len(mailbox.messages) == 2


def test_pending_deliveries_are_fifo_and_do_not_provision_accounts(fresh):
    client, mailbox = fresh()
    known_user(client, mailbox)
    with held_mail(mailbox) as hold:
        assert client.ask()[0] == 202
        assert hold.entered.wait(3)
        for name in ("first", "second", "third"):
            assert client.ask(f"{name}@example.test")[0] == 202
        assert counts(client) == (1, 1) and len(challenges(client)) == 1
        hold.release.set()
        delivered(client.gateway.email_auth)
    assert [message[0] for message in mailbox.messages[2:]] == [
        "first@example.test",
        "second@example.test",
        "third@example.test",
    ]
    assert counts(client) == (1, 1)


@pytest.mark.parametrize("error", [mailer.MailError, RuntimeError])
def test_held_delivery_failure_cleans_up_and_same_worker_recovers(
    fresh, monkeypatch, caplog, error
):
    client, mailbox = fresh()
    known_user(client, mailbox)
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    auth = client.gateway.email_auth
    worker = auth._worker
    with held_mail(mailbox, error) as hold:
        assert client.ask()[0] == 202
        assert hold.entered.wait(3)
        failed_link = mailbox.link(wait=False)
        assert len(challenges(client)) == 1
        hold.release.set()
        delivered(auth)
    assert not challenges(client) and auth._active_hash is None
    refused(client.verify(failed_link))
    warning = (
        "Email sign-in mail could not be sent"
        if error is mailer.MailError
        else "Email sign-in delivery could not be completed"
    )
    assert warning in caplog.text
    assert "fresh@example.test" not in caplog.text
    assert failed_link not in caplog.text
    assert parse_qs(urlsplit(failed_link).query)["t"][0] not in caplog.text
    assert client.ask()[0] == 202
    assert client.verify(mailbox.link())[0] == 303
    assert auth._worker is worker and worker.is_alive()
    # Failed deliveries still spent the normal email budget.
    assert client.ask()[0] == 429


def test_full_fifo_drops_uniformly_charges_limits_and_known_delivery_recovers(fresh, monkeypatch):
    client, mailbox = fresh()
    known_user(client, mailbox)
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    auth = client.gateway.email_auth
    worker = auth._worker
    assert auth._pending.maxsize == email_auth.QUEUE_CAPACITY == 16
    assert worker.daemon
    with held_mail(mailbox) as hold:
        assert client.ask()[0] == 202
        assert hold.entered.wait(3)
        for n in range(email_auth.QUEUE_CAPACITY):
            assert client.ask(f"queued{n}@example.test")[0] == 202
        assert auth._pending.qsize() == 16
        known = client.ask()
        unknown = client.ask("dropped@example.test")
        assert known[0] == unknown[0] == 202 and known[2] == unknown[2]
        assert auth._pending.qsize() == 16 and len(challenges(client)) == 1
        assert client.ask()[0] == 429
        for _ in range(2):
            assert client.ask("dropped@example.test")[0] == 202
        assert client.ask("dropped@example.test")[0] == 429
        for n in range(10):
            assert (
                client.request(
                    "/app/auth/email",
                    "POST",
                    data={"email": f"ipdrop{n}@example.test"},
                    headers={"X-Forwarded-For": "192.0.2.200"},
                )[0]
                == 202
            )
        assert (
            client.request(
                "/app/auth/email",
                "POST",
                data={"email": "ipdrop-final@example.test"},
                headers={"X-Forwarded-For": "192.0.2.200"},
            )[0]
            == 429
        )
        hold.release.set()
        delivered(auth)
    assert len(mailbox.messages) == 2
    assert auth._pending.empty() and counts(client) == (1, 1)
    client.clock.now += 300
    assert client.ask()[0] == 202
    link = mailbox.link()
    assert len(mailbox.messages) == 3
    assert mailbox.messages[-1][0] == "fresh@example.test"
    assert client.verify(link)[0] == 303
    refused(client.verify(link))
    assert auth._worker is worker and worker.is_alive()


@pytest.mark.parametrize("before,after", [("closed", "open"), ("open", "closed")])
def test_signup_changes_do_not_authorize_queued_unknown_requests(fresh, monkeypatch, before, after):
    client, mailbox = fresh()
    known_user(client, mailbox)
    with held_mail(mailbox) as hold:
        assert client.ask()[0] == 202
        assert hold.entered.wait(3)
        monkeypatch.setenv("COMMONPLACE_SIGNUP", before)
        assert client.ask("queued@example.test")[0] == 202
        monkeypatch.setenv("COMMONPLACE_SIGNUP", after)
        hold.release.set()
        delivered(client.gateway.email_auth)
    assert len(mailbox.messages) == 2 and counts(client) == (1, 1)
    assert [row["email"] for row in challenges(client)] == ["fresh@example.test"]


def test_disabling_queued_and_inflight_accounts_is_enforced(fresh):
    client, mailbox = fresh()
    known_user(client, mailbox)
    client.signin(mailbox, "second@example.test")
    with held_mail(mailbox) as hold:
        assert client.ask()[0] == 202
        assert hold.entered.wait(3)
        assert client.ask("second@example.test")[0] == 202
        with client.db._lock:
            client.db._conn.execute("UPDATE users SET disabled_at='2026-01-01T00:00:00Z'")
        hold.release.set()
        delivered(client.gateway.email_auth)
    assert len(mailbox.messages) == 3
    refused(client.verify(mailbox.link()))
    assert counts(client) == (2, 2)


def test_waiting_in_fifo_does_not_spend_challenge_lifetime(fresh, monkeypatch):
    client, mailbox = fresh()
    known_user(client, mailbox)
    now = sessions._now()
    monkeypatch.setattr(sessions, "_now", lambda: now)
    with held_mail(mailbox) as hold:
        assert client.ask()[0] == 202
        assert hold.entered.wait(3)
        assert client.ask("queued@example.test")[0] == 202
        assert len(challenges(client)) == 1
        monkeypatch.setattr(
            sessions, "_now", lambda: now + timedelta(minutes=sessions.LINK_MINUTES + 1)
        )
        hold.release.set()
        delivered(client.gateway.email_auth)
    assert mailbox.messages[-1][0] == "queued@example.test"
    assert client.verify(mailbox.link())[0] == 303


@pytest.mark.parametrize("entry", ["server", "db"])
@pytest.mark.parametrize("error", [None, mailer.MailError])
def test_close_revokes_active_preserves_finalized_and_prevents_late_sql(
    fresh, monkeypatch, entry, error
):
    client, mailbox = fresh()
    known_user(client, mailbox)
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    assert client.ask()[0] == 202
    preserved = sessions._hash(parse_qs(urlsplit(mailbox.link()).query)["t"][0])
    auth = client.gateway.email_auth
    boundary = observe_sql(client)
    with held_mail(mailbox, error) as hold:
        assert client.ask()[0] == 202
        assert hold.entered.wait(3)
        active = auth._active_hash
        assert active and active != preserved
        assert client.ask("pending@example.test")[0] == 202
        joins = []
        original_join = auth._worker.join

        def join(*, timeout):
            assert not client.db._lock._is_owned()
            joins.append(timeout)
            original_join(timeout=timeout)
            assert auth._worker.is_alive() and not hold.release.is_set()

        with monkeypatch.context() as patch:
            patch.setattr(auth._worker, "join", join)
            close = client.server.server_close if entry == "server" else client.db.close
            close()
            assert joins == [email_auth.SHUTDOWN_WAIT] == [1.0]
            assert auth._stopped.is_set() and auth._active_hash is None
            if entry == "server":
                boundary.forbidden = True
            client.db.close()
            client.db.close()
            client.server.server_close()
            assert joins == [1.0]
        with closing(sqlite3.connect(client.db.path)) as conn:
            hashes = [row[0] for row in conn.execute("SELECT token_hash FROM email_challenges")]
        assert hashes == [preserved]
        assert auth._pending.qsize() <= 1  # Only an idle-worker wake can remain.
        hold.release.set()
        auth._worker.join(timeout=3)
        assert not auth._worker.is_alive()
        assert not boundary.late


def test_accepted_handler_after_server_and_db_close_returns_neutral_without_sql(fresh, monkeypatch):
    client, mailbox = fresh()
    known_user(client, mailbox)
    boundary = observe_sql(client)
    entered, release = threading.Event(), threading.Event()
    mailbox.releases.append(release)
    original = email_auth._next

    def before_admission(value):
        entered.set()
        assert release.wait(5)
        return original(value)

    monkeypatch.setattr(email_auth, "_next", before_admission)
    request = client.background(client.ask)
    try:
        assert entered.wait(3)
        client.server.server_close()
        boundary.forbidden = True
        client.db.close()
    finally:
        release.set()
    response = request.result()
    assert response[0] == 202
    assert response[2] == json.dumps({"message": email_auth.MESSAGE}).encode()
    assert not boundary.late and len(mailbox.messages) == 1


def test_close_racing_challenge_insertion_revokes_published_token(fresh):
    client, mailbox = fresh()
    known_user(client, mailbox)
    auth = client.gateway.email_auth
    boundary = observe_sql(client)
    entered, release = threading.Event(), threading.Event()
    mailbox.releases.append(release)

    def before_insert(sql):
        if sql.startswith("INSERT INTO email_challenges"):
            entered.set()
            assert release.wait(5)

    boundary.before_execute = before_insert
    assert client.ask()[0] == 202
    try:
        assert entered.wait(3)
        close = client.background(client.db.close)
        assert auth._stopped.wait(3)
    finally:
        release.set()
    close.result()
    auth._worker.join(timeout=3)
    assert not auth._worker.is_alive() and auth._active_hash is None
    with closing(sqlite3.connect(client.db.path)) as conn:
        assert conn.execute("SELECT count(*) FROM email_challenges").fetchone()[0] == 0
    assert not boundary.late


def test_close_wins_before_success_finalization_without_losing_revocation(fresh):
    client, mailbox = fresh()
    known_user(client, mailbox)
    auth = client.gateway.email_auth
    boundary = observe_sql(client)
    with held_mail(mailbox) as hold:
        assert client.ask()[0] == 202
        assert hold.entered.wait(3)
        with client.db._lock:
            active = auth._active_hash
            hold.release.set()
            assert hold.returned.wait(3)
            close = client.background(client.db.close)
            assert auth._stopped.wait(3)
            assert auth._active_hash == active
        close.result()
    auth._worker.join(timeout=3)
    assert not auth._worker.is_alive() and auth._active_hash is None
    with closing(sqlite3.connect(client.db.path)) as conn:
        assert conn.execute("SELECT count(*) FROM email_challenges").fetchone()[0] == 0
    assert not boundary.late


def test_idle_and_duplicate_close_are_safe(fresh):
    client, _ = fresh()
    auth = client.gateway.email_auth
    client.server.server_close()
    client.server.server_close()
    client.db.close()
    client.db.close()
    assert auth._stopped.is_set() and not auth._worker.is_alive()


def test_worker_start_failure_is_uniformly_unavailable_without_replacement(
    fresh, monkeypatch, caplog
):
    original = threading.Thread.start
    attempts = []

    def start(thread):
        if thread.name == "email-signin":
            auth = thread._target.__self__
            assert auth.close in auth.db._pre_close and not auth._started
            attempts.append(thread)
            raise RuntimeError("cannot start")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    client, mailbox = fresh()
    first = client.ask()
    second = client.ask("other@example.test")
    assert first[0] == second[0] == 202 and first[2] == second[2]
    auth = client.gateway.email_auth
    assert attempts == [auth._worker] and not auth._started
    assert auth._stopped.is_set() and auth._pending.empty()
    assert not challenges(client) and not mailbox.messages
    assert "Email sign-in delivery is unavailable" in caplog.text
    client.server.server_close()
    client.db.close()


@pytest.mark.parametrize("failures", [1, 3])
@pytest.mark.parametrize("error", [sqlite3.OperationalError, RuntimeError])
def test_revocation_sql_errors_retain_failed_link_and_same_worker_recovers(
    fresh, monkeypatch, caplog, failures, error
):
    client, mailbox = fresh()
    known_user(client, mailbox)
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    auth = client.gateway.email_auth
    worker = auth._worker
    boundary = observe_sql(client)
    remaining = failures

    def fail_cleanup(sql):
        nonlocal remaining
        if sql.startswith("DELETE FROM email_challenges WHERE token_hash") and remaining:
            remaining -= 1
            raise error("synthetic cleanup failure")

    boundary.before_execute = fail_cleanup
    with held_mail(mailbox, mailer.MailError) as hold:
        assert client.ask()[0] == 202
        assert hold.entered.wait(3)
        failed_link = mailbox.link(wait=False)
        failed_hash = sessions._hash(parse_qs(urlsplit(failed_link).query)["t"][0])
        hold.release.set()
        delivered(auth)
    assert worker.is_alive() and auth._worker is worker
    assert auth._active_hash == failed_hash and auth._revocation_pending
    assert [row["token_hash"] for row in challenges(client)] == [failed_hash]
    parts = urlsplit(failed_link)
    assert client.request(parts.path + "?" + parts.query)[0] == 400
    refused(client.verify(failed_link))
    for number in range(failures - 1):
        assert client.ask(f"cleanup{number}@example.test")[0] == 202
        delivered(auth)
        assert auth._active_hash == failed_hash and auth._revocation_pending
        assert len(mailbox.messages) == 2 and worker.is_alive()
        assert [row["token_hash"] for row in challenges(client)] == [failed_hash]
    assert remaining == 0
    assert client.ask()[0] == 202
    recovered_link = mailbox.link()
    assert auth._active_hash is None and not auth._revocation_pending
    assert failed_hash not in [row["token_hash"] for row in challenges(client)]
    assert client.verify(recovered_link)[0] == 303
    refused(client.verify(recovered_link))
    assert auth._worker is worker and worker.is_alive()
    assert len(mailbox.messages) == 3 and counts(client) == (1, 1)
    assert client.ask()[0] == 429
    assert "Email sign-in challenge cleanup is unavailable" in caplog.text
    assert "fresh@example.test" not in caplog.text and failed_link not in caplog.text
    assert parse_qs(urlsplit(failed_link).query)["t"][0] not in caplog.text


@pytest.mark.parametrize("error", [None, mailer.MailError])
def test_close_with_revocation_sql_failure_reports_error_and_never_uses_late_sql(
    fresh, monkeypatch, error
):
    client, mailbox = fresh()
    known_user(client, mailbox)
    auth = client.gateway.email_auth
    boundary = observe_sql(client)

    def fail_cleanup(sql):
        if sql.startswith("DELETE FROM email_challenges WHERE token_hash"):
            raise sqlite3.OperationalError("synthetic cleanup failure")

    with held_mail(mailbox, error) as hold:
        assert client.ask()[0] == 202
        assert hold.entered.wait(3)
        active = auth._active_hash
        boundary.before_execute = fail_cleanup
        joins = []
        original_join = auth._worker.join

        def join(*, timeout):
            assert not client.db._lock._is_owned()
            joins.append(timeout)
            original_join(timeout=timeout)
            assert auth._worker.is_alive() and not hold.release.is_set()

        with monkeypatch.context() as patch:
            patch.setattr(auth._worker, "join", join)
            with pytest.raises(RuntimeError, match="challenge cleanup is unavailable"):
                client.db.close()
            assert joins == [email_auth.SHUTDOWN_WAIT]
        assert boundary.forbidden and auth._stopped.is_set()
        assert auth._active_hash == active and auth._revocation_pending
        client.server.server_close()
        client.db.close()
        hold.release.set()
        auth._worker.join(timeout=3)
        assert not auth._worker.is_alive() and not boundary.late
        with closing(sqlite3.connect(client.db.path)) as conn:
            # Failed cleanup is reported, never described as successful revocation.
            assert conn.execute("SELECT token_hash FROM email_challenges").fetchone()[0] == active
