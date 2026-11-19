"""Credentialed requests never follow a redirect.

The stdlib redirect handler copies ``Authorization`` (and every other
non-content header) onto the redirected request, whatever host ``Location``
names. ``mempalace._net.urlopen`` refuses the redirect instead, so a peer, an
LLM endpoint or a Qdrant server that answers 3xx cannot collect the
credential meant for it.
"""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError

import pytest

from mempalace import _net, transport


class _Sink(BaseHTTPRequestHandler):
    """Where the redirect points: records every request it receives."""

    seen: list = []

    def do_GET(self):
        self.seen.append(dict(self.headers))
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *_args):
        pass


class _Redirector(BaseHTTPRequestHandler):
    target = ""
    code = 302

    def do_GET(self):
        self.send_response(self.code)
        self.send_header("Location", self.target + "/landed")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *_args):
        pass


def _serve(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def sink():
    _Sink.seen = []
    server, url = _serve(_Sink)
    yield url
    server.shutdown()
    server.server_close()


@pytest.fixture
def redirector(sink):
    _Redirector.target = sink
    _Redirector.code = 302
    server, url = _serve(_Redirector)
    yield url
    server.shutdown()
    server.server_close()


def _request(url, **headers):
    return urllib.request.Request(url, headers={"Authorization": "Bearer s3cret", **headers})


def test_stdlib_urlopen_forwards_the_credential(redirector):
    """The hazard this module exists for: the control case."""
    with urllib.request.urlopen(_request(redirector), timeout=5) as response:
        assert response.status == 200
    assert _Sink.seen and _Sink.seen[0].get("Authorization") == "Bearer s3cret"


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_net_urlopen_refuses_every_redirect(redirector, code):
    _Redirector.code = code
    with pytest.raises(HTTPError) as caught:
        _net.urlopen(_request(redirector), timeout=5)
    assert caught.value.code == code
    assert "not followed" in str(caught.value.reason)
    assert "/landed" in str(caught.value.reason)
    assert _Sink.seen == []


def test_net_urlopen_refuses_a_redirected_post(redirector):
    with pytest.raises(HTTPError):
        _net.urlopen(_request(redirector), data=b'{"k": 1}', timeout=5)
    assert _Sink.seen == []


def test_net_urlopen_serves_a_direct_reply(sink):
    with _net.urlopen(_request(sink), timeout=5) as response:
        assert json.loads(response.read()) == {"ok": True}
    assert _Sink.seen[0].get("Authorization") == "Bearer s3cret"


def test_peer_transport_reports_the_redirect_and_keeps_the_token(redirector):
    with pytest.raises(transport.TransportError, match="not followed"):
        transport.http_request(redirector, "peer-token", "/sync/version_vector")
    assert _Sink.seen == []
