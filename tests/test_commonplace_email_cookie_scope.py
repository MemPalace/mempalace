"""Email sign-in with HTTPS domain cookies and a pre-existing host cookie."""

import json
import os
from email.message import Message
from http.cookiejar import CookieJar
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import Request

import pytest

from commonplace import urls
from tests.test_commonplace_email_auth import fresh as fresh


class BrowserCookies:
    """Use HTTPS cookie rules while transporting requests over loopback HTTP."""

    origin = os.environ.get("EMAIL_COOKIE_TEST_ORIGIN", "https://app.example.test")
    host = urlsplit(origin).netloc
    domain = urlsplit(origin).hostname.partition(".")[2]

    def __init__(self, client):
        self.client = client
        self.jar = CookieJar()

    def accept(self, path, headers):
        message = Message()
        for name, value in headers:
            message.add_header(name, value)
        self.jar.extract_cookies(SimpleNamespace(info=lambda: message), Request(self.origin + path))

    def request(self, path, method="GET", *, data=None, raw=None):
        request = Request(self.origin + path)
        self.jar.add_cookie_header(request)
        headers = {"Host": self.host}
        if method == "POST":
            headers["Origin"] = self.origin
        if raw is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        response = self.client.request(
            path,
            method,
            data=data,
            raw=raw,
            cookie=request.get_header("Cookie"),
            headers=headers,
        )
        self.accept(path, self.client.response_headers)
        return response


@pytest.mark.parametrize("old_host_cookie", [False, True])
@pytest.mark.parametrize("shared_domain", [False, True])
def test_email_verify_domain_cookie_authenticates_after_redirect(
    fresh, monkeypatch, old_host_cookie, shared_domain
):
    monkeypatch.setenv("COMMONPLACE_APP_URL", BrowserCookies.origin)
    client, mailbox = fresh()
    # The shared fixture clears COOKIE_DOMAIN and points links at loopback.
    if shared_domain:
        monkeypatch.setenv("COMMONPLACE_COOKIE_DOMAIN", BrowserCookies.domain)
    monkeypatch.setattr(urls, "APP_URL", BrowserCookies.origin)
    assert client.gateway.secure_cookies
    browser = BrowserCookies(client)
    if old_host_cookie:
        browser.accept(
            "/app/", [("Set-Cookie", "cmmn_session=stale; Path=/; HttpOnly; Secure; SameSite=Lax")]
        )
    assert browser.request("/api/me")[0] == 401
    status, _, content = browser.request(
        "/app/auth/email", "POST", data={"email": "cookie-user@example.test"}
    )
    assert status == 202
    assert "link is on its way" in json.loads(content)["message"]
    link = urlsplit(mailbox.link())
    assert link.scheme + "://" + link.netloc == BrowserCookies.origin
    path = link.path + "?" + link.query
    status, _, page = browser.request(path)
    assert status == 200 and b'<form method="post">' in page
    status, headers, _ = browser.request(path, "POST", raw="confirm=1")
    assert status == 303
    cookie_header_count = sum(name.lower() == "set-cookie" for name, _ in client.response_headers)
    cookies = [
        c for c in browser.jar if c.name == "cmmn_session" and c.domain_specified == shared_domain
    ]
    assert len(cookies) == 1
    cookie = cookies[0]
    expected_domain = "." + BrowserCookies.domain if shared_domain else BrowserCookies.host
    assert cookie.domain == expected_domain and cookie.path == "/" and cookie.secure
    assert cookie.has_nonstandard_attr("HttpOnly")
    assert cookie.get_nonstandard_attr("SameSite") == "Lax"
    assert "Max-Age=2592000" in headers["Set-Cookie"]
    destination = headers["Location"].split("#", 1)[0]
    assert destination == "/app/"
    assert browser.request(destination)[0] == 200
    status, _, content = browser.request("/api/me")
    assert status == 200, "successful verify must authenticate the redirected browser"
    assert json.loads(content)["email"] == "cookie-user@example.test"
    assert len([c for c in browser.jar if c.name == "cmmn_session"]) == 1
    if not shared_domain:
        assert cookie_header_count == 1
    # A used link is visibly refused, without replacing or clearing the session.
    status, headers, content = browser.request(path, "POST", raw="confirm=1")
    assert status == 400 and b"invalid or expired" in content
    assert "Set-Cookie" not in headers and "Location" not in headers
    assert browser.request("/api/me")[0] == 200
