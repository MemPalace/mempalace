"""The HTTP hub keeps its port to itself.

Windows ``SO_REUSEADDR`` lets a second socket bind a port that is already
listening, and the newer socket then receives connections meant for the first.
Python's ``http.server.HTTPServer`` sets that option, so a static preview
server started on the hub's port (``python -m http.server 8765``) silently took
over loopback requests meant for the hub: authenticated MCP calls failed with
``RemoteDisconnected`` while the hub itself was healthy. On Windows the hub now
binds with ``SO_EXCLUSIVEADDRUSE``, so the later server fails to bind instead.

These tests bind real sockets on OS-assigned ports.
"""

import http.client
import json
import socket
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler

import pytest

from mempalace import mcp_server as mcp

pytestmark = pytest.mark.usefixtures("tokenless_http_hub")

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="SO_REUSEADDR only shares a listening port on Windows"
)


def _serve(httpd):
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    return thread


def _stop(httpd, thread):
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture
def hub():
    """The production hub on an ephemeral loopback port."""
    httpd = mcp._build_http_server("127.0.0.1", 0)
    thread = _serve(httpd)
    try:
        yield httpd
    finally:
        _stop(httpd, thread)


def _tools_list(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode("utf-8")
        conn.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


@windows_only
def test_a_preview_server_cannot_bind_the_hub_port(hub):
    """``python -m http.server <port>`` fails rather than sharing the hub's port."""
    port = hub.server_address[1]
    with pytest.raises(OSError):
        HTTPServer(("127.0.0.1", port), SimpleHTTPRequestHandler).server_close()
    status, payload = _tools_list(port)
    assert status == 200
    assert "tools" in payload["result"]


@windows_only
def test_a_loopback_server_cannot_bind_beside_a_wildcard_hub(monkeypatch):
    """A hub on 0.0.0.0 also refuses a later server on 127.0.0.1 and the same port."""
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "port-exclusive-test-token")
    httpd = mcp._build_http_server("0.0.0.0", 0)
    thread = _serve(httpd)
    try:
        port = httpd.server_address[1]
        with pytest.raises(OSError):
            HTTPServer(("127.0.0.1", port), SimpleHTTPRequestHandler).server_close()
    finally:
        _stop(httpd, thread)


@windows_only
def test_the_hub_refuses_a_port_another_server_already_holds():
    """The hub fails to start on a taken port instead of joining it."""
    squatter = HTTPServer(("127.0.0.1", 0), SimpleHTTPRequestHandler)
    try:
        with pytest.raises(OSError):
            mcp._build_http_server("127.0.0.1", squatter.server_address[1]).server_close()
    finally:
        squatter.server_close()


def test_the_hub_rebinds_its_port_right_after_serving_a_request(hub):
    """A supervisor restart reuses the port at once, after real traffic."""
    port = hub.server_address[1]
    status, _ = _tools_list(port)
    assert status == 200
    hub.shutdown()
    hub.server_close()

    again = mcp._build_http_server("127.0.0.1", port)
    thread = _serve(again)
    try:
        status, payload = _tools_list(port)
        assert status == 200
        assert "tools" in payload["result"]
    finally:
        _stop(again, thread)


def test_the_listening_socket_options_match_the_platform(hub):
    """Windows binds exclusively; POSIX keeps SO_REUSEADDR for TIME_WAIT rebinds."""
    if sys.platform == "win32":
        assert hub.socket.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1
        assert hub.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
    else:
        assert hub.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) != 0
