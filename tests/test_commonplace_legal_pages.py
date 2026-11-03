"""Public legal content, rendered sign-in links, and real web-server routing.

Set NGINX_BIN and CADDY_BIN to exercise the deployment examples locally.
The servers bind only to loopback and never contact a real app or mailbox.
"""

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.test_commonplace_email_auth import delivered, fresh as fresh

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "landing/public"
PAGES = ("/privacy", "/terms", "/privacy/nl", "/terms/nl")


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)

    def links(self):
        return {attrs.get("href") for tag, attrs in self.tags if tag == "a"}


@pytest.mark.parametrize("path", PAGES)
def test_legal_pages_are_complete_static_documents(path):
    file = PUBLIC / path.strip("/") / "index.html"
    assert file.is_file(), f"Public legal page missing: {path}"
    html = file.read_text()
    doc = Document(html)
    text = " ".join(doc.text)
    assert ("html", {"lang": "nl" if path.endswith("/nl") else "en"}) in doc.tags
    assert any(tag == "meta" and attrs.get("name") == "viewport" for tag, attrs in doc.tags)
    assert any(tag == "main" for tag, _ in doc.tags)
    assert any(tag == "link" and attrs.get("href") == "/legal.css" for tag, attrs in doc.tags)
    assert not re.search(r"\[CHECK|\[TODO|\bTBD\b|placeholder|\[.*?\]", text, re.I)
    assert not re.search(
        r"\b(?:KvK|postcode|postal address|street address|huisadres)\b", text, re.I
    )
    assert not re.search(r"\b\d{4}\s?[A-Z]{2}\b", text)
    assert "privacy@cmmn.dev" in text
    assert "2026-11-03" in html
    language_suffix = "/nl" if path.endswith("/nl") else ""
    assert {"/privacy" + language_suffix, "/terms" + language_suffix} <= doc.links()
    translation = path.removesuffix("/nl") if path.endswith("/nl") else path + "/nl"
    assert translation in doc.links()
    assert not any(tag in {"script", "iframe", "object", "embed"} for tag, _ in doc.tags)
    for tag, attrs in doc.tags:
        if tag != "a":
            for attr in ("src", "href", "srcset"):
                assert not re.match(r"(?:https?:)?//", attrs.get(attr, ""))
    if "privacy" in path:
        assert text.count("Jeffrey Endeman") == 1
        assert "cmmn_session" in text and "cmmn_signin" in text and "cmmn_via" in text
        assert "tweet.read users.read users.email" in text
        assert "16" in text and "30" in text
        assert not re.search(
            r"the disk is encrypted|encrypt the server disk|DPA is accepted", text, re.I
        )
        assert "end-to-end" not in text.lower()
    else:
        assert "16" in text and "EUR 100" in text and "14" in text and "30" in text
        assert "250" in text


@pytest.mark.parametrize("path", ("/privacy/nl", "/terms/nl"))
def test_dutch_legal_footer_preserves_language(path):
    file = PUBLIC / path.strip("/") / "index.html"
    assert file.is_file(), f"Public legal page missing: {path}"
    footer = Document(file.read_text().split("<footer", 1)[1])
    assert {"/privacy/nl", "/terms/nl"} <= footer.links()
    assert "Voorwaarden" in footer.text
    assert "/privacy" not in footer.links() and "/terms" not in footer.links()


def test_dutch_terms_allow_voluntary_end_of_service_use():
    file = PUBLIC / "terms/nl/index.html"
    assert file.is_file(), "Dutch terms must be available"
    text = " ".join(Document(file.read_text()).text)
    assert "Je kunt op elk moment stoppen met het gebruik van de dienst." in text
    assert "Je kunt de dienst op elk moment niet meer gebruiken." not in text


def test_landing_does_not_advertise_unimplemented_encryption():
    html = (PUBLIC / "index.html").read_text()
    assert not re.search(r"sealed|ciphertext|your key only|three privacy tiers", html, re.I)
    assert "Connection permissions" in html


@pytest.mark.parametrize("page", ("index.html", "connect/index.html"))
def test_site_footers_link_to_both_legal_pages(page):
    html = (PUBLIC / page).read_text()
    footer = html.split("<footer", 1)[1]
    assert {"/privacy", "/terms"} <= Document(footer).links()


def test_rendered_signin_has_legal_navigation_without_changing_email_form():
    node = shutil.which("node")
    assert node, "Node is required for the rendered sign-in check"
    program = """
      const { readFileSync } = await import('node:fs');
      const url = s => 'data:text/javascript;base64,' + Buffer.from(s).toString('base64');
      const api = url(readFileSync('commonplace/web/static/api.js', 'utf8'));
      const source = readFileSync('commonplace/web/static/signin.js', 'utf8');
      const { signinView } = await import(url(source.replace('"./api.js"', JSON.stringify(api))));
      console.log(signinView({ next: '/app/#/settings' }));
    """
    html = subprocess.check_output(
        [node, "--input-type=module", "-e", program], cwd=ROOT, text=True, timeout=10
    )
    doc = Document(html)
    assert {"https://cmmn.dev/privacy", "https://cmmn.dev/terms"} <= doc.links()
    assert any(
        tag == "form" and attrs.get("action") == "/app/auth/email" and attrs.get("method") == "post"
        for tag, attrs in doc.tags
    )
    assert any(
        attrs.get("name") == "next" and attrs.get("value") == "/app/#/settings"
        for _, attrs in doc.tags
    )


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(port, host, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        conn.request("GET", path, headers={"Host": host})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        conn.close()


@pytest.fixture(params=("nginx", "caddy"))
def public_server(request, tmp_path):
    kind = request.param
    binary = os.environ.get(f"{kind.upper()}_BIN") or shutil.which(kind)
    if not binary:
        pytest.skip(f"Set {kind.upper()}_BIN to exercise real {kind} routing")
    port = _port()
    upstream = None
    if kind == "nginx":
        site = (ROOT / "landing/nginx.conf").read_text()
        site = site.replace("/usr/share/nginx/html", str(PUBLIC))
        site = site.replace("listen 80", f"listen 127.0.0.1:{port}")
        temps = "\n".join(
            f"{name}_temp_path {tmp_path}/{name};"
            for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi")
        )
        config = tmp_path / "nginx.conf"
        config.write_text(
            f"pid {tmp_path}/nginx.pid; error_log stderr notice; events {{}}\n"
            f"http {{ access_log off; {temps}\n"
            "types { text/html html; text/css css; image/svg+xml svg; }\n" + site + "\n}"
        )
        command = [binary, "-p", str(tmp_path), "-c", str(config), "-g", "daemon off;"]
        ready = "start worker processes"
    else:

        class Agent(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(("agent:" + self.path).encode())

            def log_message(self, *args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Agent)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        source = (ROOT / "deploy/commonplace-public.caddy.example").read_text()
        source = source.replace("bind 192.0.2.30 2001:db8::30", "bind 127.0.0.1")
        source = source.replace("/path/to/commonplace/app/landing/public", str(PUBLIC))
        source = source.replace("127.0.0.1:8765", f"127.0.0.1:{upstream.server_port}")
        for host in ("www.commonplace.run", "commonplace.run", "app.cmmn.dev", "cmmn.dev"):
            source = re.sub(
                rf"(?m)(^|, ){re.escape(host)}(?=,| \{{)", rf"\1http://{host}:{port}", source
            )
        config = tmp_path / "Caddyfile"
        config.write_text("{\n admin off\n auto_https off\n}\n" + source)
        command = [binary, "run", "--config", str(config), "--adapter", "caddyfile"]
        ready = "serving initial configuration"
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    startup = []

    def await_ready():
        for line in process.stderr:
            startup.append(line)
            if ready in line:
                return

    reader = threading.Thread(target=await_ready, daemon=True)
    reader.start()
    reader.join(timeout=10)
    try:
        assert not reader.is_alive() and process.poll() is None, "".join(startup)
        yield kind, port
    finally:
        process.terminate()
        process.wait(timeout=5)
        reader.join(timeout=1)
        process.stderr.close()
        if upstream:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=3)


@pytest.mark.parametrize("host", ("cmmn.dev", "commonplace.run"))
def test_logged_out_legal_routes_serve_documents_without_redirects(public_server, host):
    _, port = public_server
    for path in PAGES:
        for suffix in ("", "/"):
            status, headers, html = _get(port, host, path + suffix)
            assert status == 200, (host, path + suffix, status, headers.get("Location"))
            assert "Location" not in headers and "Set-Cookie" not in headers
            assert "<main" in html and "privacy@cmmn.dev" in html
            assert "text/html" in headers.get("Content-Type", "")
            assert "Content-Security-Policy" in headers
    for asset in ("/legal.css", "/favicon.svg"):
        status, headers, content = _get(port, host, asset)
        assert status == 200 and content, (host, asset, status)
        assert "Location" not in headers and "Set-Cookie" not in headers


def test_short_links_fallback_and_caddy_agent_routes_are_preserved(public_server):
    kind, port = public_server
    github_redirect = re.search(
        r"^\s*~\^/gh/\?\$\s+(https://github\.com/[^\s;]+);",
        (ROOT / "landing/nginx.conf").read_text(),
        re.MULTILINE,
    )
    assert github_redirect, "The configured /gh project redirect must remain explicit"
    assert _get(port, "commonplace.run", "/")[0] == 200
    assert _get(port, "commonplace.run", "/not-a-page")[0] == 404
    for path, destination in (
        ("/unknown", "https://commonplace.run/"),
        ("/", "https://commonplace.run/"),
        ("/gh", github_redirect.group(1)),
        ("/connect", "https://commonplace.run/connect/"),
        ("/codex", "https://commonplace.run/connect/#codex"),
    ):
        status, headers, _ = _get(port, "cmmn.dev", path)
        assert (status, headers.get("Location")) == (302, destination)
    if kind == "caddy":
        for path in (
            "/mcp",
            "/oauth/authorize",
            "/.well-known/oauth-authorization-server",
            "/s/example",
            "/healthz",
        ):
            status, _, content = _get(port, "cmmn.dev", path)
            assert (status, content) == (200, "agent:" + path)
        assert _get(port, "app.cmmn.dev", "/api/me")[2] == "agent:/api/me"


def test_closed_signup_remains_neutral_and_provisions_nothing(fresh, monkeypatch):
    client, mailbox = fresh()
    monkeypatch.setenv("COMMONPLACE_SIGNUP", "closed")
    status, _, content = client.ask("new@example.test")
    assert status == 202 and json.loads(content)["message"]
    delivered(client.gateway.email_auth)
    assert mailbox.messages == []
    assert client.db.users() == [] and client.db.workspaces() == []
