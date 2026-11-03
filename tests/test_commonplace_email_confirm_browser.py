"""Email confirmation under a proxy that overwrites the referrer response header.

The fast test needs no Playwright. Browser evidence uses Playwright 1.64.1 and
an existing Chromium cache via PLAYWRIGHT_BROWSERS_PATH set before pytest starts.
Run with --no-showlocals; token-bearing URLs and requests stay in memory only.
"""

import asyncio
import http.client
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from commonplace import sessions, urls
from tests.test_commonplace_email_auth import Client, challenges, counts, fresh as fresh, refused

VERIFY = "/app/auth/email/verify"
RESOURCES = {"/app/app.css", "/app/ui.css", "/app/brand/logo-small-dark.svg"}


def _private_output(pytestconfig):
    safe_output = not pytestconfig.getoption("showlocals")
    assert safe_output, (
        "email confirmation tests require --no-showlocals before creating credentials"
    )


def _challenge_rows(client):
    return [tuple(row) for row in challenges(client)]


def _unchanged(client, before):
    assert counts(client) == (0, 0)
    assert _challenge_rows(client) == before


def test_confirmation_referrer_meta_precedes_resources(fresh, monkeypatch, pytestconfig):
    _private_output(pytestconfig)
    monkeypatch.delenv("COMMONPLACE_APP_URL", raising=False)
    monkeypatch.delenv("COMMONPLACE_BASE_URL", raising=False)
    client, mailbox = fresh()
    try:
        assert client.ask()[0] == 202
        link = urlsplit(mailbox.link())
        token = parse_qs(link.query)["t"][0]
        before = _challenge_rows(client)
        assert len(before) == 1
        status, headers, page = client.request(link.path + "?" + link.query)
        assert status == 200
        meta = b'<meta name="referrer" content="strict-origin">'
        present = meta in page
        assert present, "confirmation document must declare strict-origin referrer policy"
        after_charset = b'<meta charset="utf-8">' + meta in page
        assert after_charset, "referrer meta must follow the charset declaration"
        for marker in (b"<link", b"<img", b"<form"):
            early = page.index(meta) < page.index(marker)
            assert early, "referrer meta must precede confirmation resources and form"
        leaked = token.encode() in page or token in str(headers)
        assert not leaked, "confirmation response must not expose the token"
        _unchanged(client, before)
    except AssertionError:
        raise
    except Exception:
        raise AssertionError(
            "confirmation meta check failed; credential details suppressed"
        ) from None


@contextmanager
def _overwriting_proxy(upstream_port):
    requests, errors = [], []

    class Proxy(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # The default traceback can include a token-bearing HTTP request.
            errors.append(True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def forward(self):
            request_headers = list(self.headers.items())
            record = [self.command, self.path, request_headers, None, []]
            requests.append(record)
            payload = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            connection = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=3)
            try:
                connection.putrequest(
                    self.command, self.path, skip_host=True, skip_accept_encoding=True
                )
                for name, value in request_headers:
                    connection.putheader(name, value)
                connection.endheaders(payload)
                response = connection.getresponse()
                content = response.read()
                response_headers = [
                    (name, value)
                    for name, value in response.getheaders()
                    if name.lower() != "referrer-policy"
                ]
                response_headers.append(("Referrer-Policy", "no-referrer"))
                # Fill the response before sending so expect_response cannot race it.
                record[3:] = [response.status, response_headers]
                try:
                    self.send_response_only(response.status)
                    for name, value in response_headers:
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError):
                    # Closing Chromium can cancel a landing-page resource request.
                    pass
            finally:
                connection.close()

        do_GET = forward
        do_POST = forward

    server = Proxy(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1], requests
        assert not errors, "confirmation proxy failed; credential details suppressed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive(), "confirmation proxy did not stop"


def _origin_refused(response):
    status, headers, _ = response
    denied = status == 403 and "Set-Cookie" not in headers and "Location" not in headers
    assert denied, "invalid Origin must be refused without a session or redirect"


def _duplicate_origin(port, path, origin):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.putrequest("POST", path)
        connection.putheader("Content-Type", "application/x-www-form-urlencoded")
        connection.putheader("Content-Length", "9")
        connection.putheader("Origin", origin)
        connection.putheader("Origin", origin)
        connection.endheaders(b"confirm=1")
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _audit_token(token, requests):
    for method, path, headers, _, response_headers in requests:
        verification = method in ("GET", "POST") and urlsplit(path).path == VERIFY
        leaked = token in path and not verification
        assert not leaked, "token must appear only in verification request lines"
        leaked = any(token in value for _, value in headers)
        assert not leaked, "browser request headers must not contain the token"
        leaked = any(token in value for _, value in response_headers)
        assert not leaked, "response headers and redirects must not contain the token"


async def _confirm_in_chromium(client, proxied, origin, link, token, requests, before):
    from playwright.async_api import async_playwright

    expected_referer = origin + "/"
    browser_log_start = len(requests)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            context = await browser.new_context()
            page = await context.new_page()
            page.set_default_timeout(15000)
            response = await page.goto(link)
            loaded = response.status == 200
            assert loaded, "confirmation GET must succeed"
            overwritten = await response.header_value("referrer-policy") == "no-referrer"
            assert overwritten, "proxy must overwrite the confirmation response policy"
            _unchanged(client, before)
            no_session = not await context.cookies()
            assert no_session, "confirmation GET must not create a session"
            pre_post = requests[browser_log_start:]
            async with page.expect_response(
                lambda r: r.request.method == "POST" and r.url.split("?", 1)[0].endswith(VERIFY)
            ) as confirmed:
                await page.locator("button.email-confirm").click()
            response = await confirmed.value
            post_origin = await response.request.header_value("origin")
            successful = response.status == 303 and post_origin == origin
            # This is the first post-click assertion: base must fail as 403/null.
            assert successful, f"confirmation POST status={response.status}, Origin={post_origin}"
            await page.wait_for_url(origin + "/app/**")
            await page.locator(".app").wait_for()

            # Only the confirmation document's requests retain this exact policy.
            for method, path, headers, _, _ in pre_post:
                referers = [value for name, value in headers if name.lower() == "referer"]
                if method == "GET" and urlsplit(path).path == VERIFY:
                    absent = not referers
                    assert absent, "initial confirmation navigation must not send a Referer"
                elif urlsplit(path).path in RESOURCES:
                    origin_only = referers == [expected_referer]
                    assert origin_only, "confirmation resources must send an origin-only Referer"
            post_referer = await response.request.header_value("referer")
            origin_only = post_referer == expected_referer
            assert origin_only, "confirmation POST must send an origin-only Referer"
            cookies = await context.cookies()
            session = [cookie for cookie in cookies if cookie["name"] == sessions.COOKIE]
            one_session = len(session) == 1
            assert one_session, "successful confirmation must set the session cookie"
            cookie = session[0]["name"] + "=" + session[0]["value"]
        finally:
            await browser.close()
    _audit_token(token, requests[browser_log_start:])
    me = proxied.me(cookie)
    correct_account = me["email"] == "browser-user@example.test"
    assert correct_account, "confirmation must sign in the captured-mail recipient"
    assert counts(client) == (1, 1)
    assert not _challenge_rows(client), "successful confirmation must consume the challenge"
    assert client.db.user(me["user"])["is_admin"] == 0


async def _browser_confirmation(client, proxied, origin, link, token, requests, before):
    try:
        await _confirm_in_chromium(client, proxied, origin, link, token, requests, before)
    except AssertionError:
        raise
    except Exception:
        # Playwright navigation errors include the URL; suppress their context here.
        raise AssertionError("browser confirmation failed; credential details suppressed") from None


def test_chromium_confirmation_with_overwritten_referrer_header(fresh, monkeypatch, pytestconfig):
    _private_output(pytestconfig)
    pytest.importorskip("playwright.async_api", reason="optional browser regression dependency")
    monkeypatch.delenv("COMMONPLACE_APP_URL", raising=False)
    monkeypatch.delenv("COMMONPLACE_BASE_URL", raising=False)
    client, mailbox = fresh()
    try:
        with _overwriting_proxy(client.port) as (port, requests):
            origin = f"http://127.0.0.1:{port}"
            # fresh() sets APP_URL; override it only after that factory returns.
            monkeypatch.setattr(urls, "APP_URL", origin)
            proxied = Client(client.gateway, port)
            assert proxied.ask("browser-user@example.test")[0] == 202
            link = mailbox.link()
            parts = urlsplit(link)
            path = parts.path + "?" + parts.query
            token = parse_qs(parts.query)["t"][0]
            before = _challenge_rows(client)
            assert len(before) == 1
            _unchanged(client, before)

            for bad_origin in ("https://another.example", "null", "not-an-origin"):
                _origin_refused(proxied.verify(link, headers={"Origin": bad_origin}))
                _unchanged(client, before)
            _origin_refused(_duplicate_origin(port, path, origin))
            _unchanged(client, before)
            for data in ({"t": "x" * 43}, {}):
                refused(proxied.request(VERIFY, "POST", data=data, headers={"Origin": origin}))
                _unchanged(client, before)

            asyncio.run(
                _browser_confirmation(client, proxied, origin, link, token, requests, before)
            )
    except AssertionError:
        raise
    except Exception:
        # Playwright navigation errors include the URL; never expose that context.
        raise AssertionError("browser confirmation failed; credential details suppressed") from None
