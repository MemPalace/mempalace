# tests/test_mcp_http_transport.py
"""
Tests for the opt-in HTTP transport added for #1801.

These exercise the *production* server built by
``mempalace.mcp_server._build_http_server`` over a real loopback socket on an
ephemeral port — the earlier version of this file reimplemented the endpoint in
Starlette and guarded on ``pytest.importorskip("starlette")``/``uvicorn``,
neither of which is a project dependency, so it was silently skipped in CI and
the real ``_serve_http`` handler had zero coverage.

Design constraints
------------------
* Real sockets, but bound to ``127.0.0.1:0`` (OS-assigned port) so there is no
  port conflict on any CI runner.
* Pure stdlib (``http.client``, ``threading``) — no third-party deps.
* Server runs in a daemon thread and is shut down in fixture teardown.
"""

import http.client
import json
import logging
import os
import socketserver
import socket
import ssl
import threading
import time

import anyio
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from mempalace import mcp_server as mcp


def _post(port, path, body, headers=None, host_header=None):
    """Raw POST with full control over Host / Origin / Authorization headers."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        raw = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
        headers = headers or {}
        conn.putrequest("POST", path, skip_host=(host_header is not None))
        if host_header is not None:
            conn.putheader("Host", host_header)
        conn.putheader("Content-Type", "application/json")
        # Let a caller override Content-Length (used to fake an oversized body)
        # instead of emitting a second, conflicting header.
        if not any(k.lower() == "content-length" for k in headers):
            conn.putheader("Content-Length", str(len(raw)))
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        conn.send(raw)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _post_response(port, body, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    raw = json.dumps(body).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **(headers or {}),
    }
    try:
        conn.request("POST", "/mcp", body=raw, headers=request_headers)
        response = conn.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        conn.close()


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


@pytest.fixture
def http_server():
    """A running production MCP HTTP server on an ephemeral loopback port."""
    httpd = mcp._build_http_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield port, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_official_sdk_streamable_http_handshake_and_tools_list():
    """Exercise initialize, initialized, and tools/list on the SDK transport."""
    app, state = mcp._build_sdk_http_app("127.0.0.1", 0)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    state.server_address = ("127.0.0.1", port)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)

    async def exercise():
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                initialized = await session.initialize()
                assert initialized.server_info.name == "mempalace"
                tools = await session.list_tools()
                assert "mempalace_search" in {tool.name for tool in tools.tools}
                result = await session.call_tool("mempalace_get_aaak_spec", {})
                assert "AAAK" in result.content[0].text

    try:
        anyio.run(exercise)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_official_sdk_notification_returns_202_with_empty_body():
    app, state = mcp._build_sdk_http_app("127.0.0.1", 0)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    state.server_address = ("127.0.0.1", port)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    try:
        status, _, headers = _post_response(
            port,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "contract-test", "version": "1"},
                },
            },
        )
        assert status == 200
        session_id = {key.lower(): value for key, value in headers.items()}["mcp-session-id"]
        status, body, _ = _post_response(
            port,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={
                "Mcp-Session-Id": session_id,
                "MCP-Protocol-Version": "2025-11-25",
            },
        )
        assert status == 202
        assert body == b""
        assert _get(port, "/healthz", headers={"User-Agent": "sdk-contract"}) == (200, b"ok\n")
        status, body = _get(port, "/statusz")
        assert status == 200
        status_payload = json.loads(body)
        assert status_payload["requests"]["total"] >= 3
        assert status_payload["requests"]["by_status"]["202"] >= 1
        assert any(
            client["user_agent"] == "sdk-contract" for client in status_payload["clients"]["recent"]
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_official_sdk_http_security_guards(monkeypatch):
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "sdk-secret")
    monkeypatch.setenv(mcp._HTTP_EXTRA_ALLOWED_HOSTS_ENV, "palace.example")
    app, _state = mcp._build_sdk_http_app("127.0.0.1", 8765)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "security-test", "version": "1"},
        },
    }
    common = {
        "host": "127.0.0.1:8765",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.post("/mcp", json=initialize, headers=common).status_code == 401
        authorized = {**common, "authorization": "Bearer sdk-secret"}
        assert client.post("/mcp", json=initialize, headers=authorized).status_code == 200
        assert (
            client.post(
                "/mcp",
                json=initialize,
                headers={**authorized, "host": "palace.example:8765"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/mcp", json=initialize, headers={**authorized, "host": "evil.example"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/mcp",
                json=initialize,
                headers={**authorized, "origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert client.get("/healthz", headers={"host": "127.0.0.1:8765"}).status_code == 200
        assert client.get("/statusz", headers={"host": "127.0.0.1:8765"}).status_code == 401


def test_official_sdk_non_loopback_accepts_cluster_hosts_with_bearer(monkeypatch):
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "cluster-secret")
    app, _state = mcp._build_sdk_http_app("0.0.0.0", 8765)
    with TestClient(app, base_url="http://mempalace.hermes.svc:8765") as client:
        assert client.get("/statusz", headers={"host": "10.42.1.9:8765"}).status_code == 401
        headers = {"host": "mempalace.hermes.svc:8765", "authorization": "Bearer cluster-secret"}
        assert client.get("/statusz", headers=headers).status_code == 200


def test_official_sdk_sync_and_sse_routes(monkeypatch):
    class FakeLogstream:
        replica_id = "replica-a"

        def version_vector(self):
            return {"replica-a": 2}

        def list_ops(self, origin, after_seq=0, limit=500):
            return [{"origin": origin, "seq": after_seq + 1}][:limit]

        def get_artifact(self, artifact_id):
            return {"id": artifact_id, "content": "verbatim"} if artifact_id == "known" else None

        def latest_event_id(self):
            return None

        def list_events(self, **_kwargs):
            return []

    fake = FakeLogstream()
    monkeypatch.setattr(mcp, "_call_logstream", lambda operation: operation(fake))
    monkeypatch.setattr(mcp, "_node_profile", lambda: {"roles": []})
    monkeypatch.setattr(mcp, "_known_profiles_snapshot", lambda: {})
    monkeypatch.setattr(
        mcp, "_mesh_peers_payload", lambda: {"replica_id": "replica-a", "peers": []}
    )
    monkeypatch.setenv(mcp._SSE_MAX_CLIENTS_ENV, "0")
    app, _state = mcp._build_sdk_http_app("127.0.0.1", 8765)
    headers = {"host": "127.0.0.1:8765"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/sync/version_vector", headers=headers).json()["version_vector"] == {
            "replica-a": 2
        }
        assert (
            client.get("/sync/ops?origin=replica-a&after=2&limit=5", headers=headers).json()[
                "events"
            ][0]["seq"]
            == 3
        )
        assert (
            client.get("/sync/artifact?id=known", headers=headers).json()["artifact"]["content"]
            == "verbatim"
        )
        assert client.get("/sync/artifact?id=missing", headers=headers).status_code == 404
        assert client.get("/sync/peers", headers=headers).json()["peers"] == []
        response = client.get("/logstream/stream", headers=headers)
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"


@pytest.mark.parametrize("protocol_version", ["2025-03-26", "2025-06-18", "2025-11-25"])
def test_official_sdk_negotiates_supported_protocol_versions(protocol_version):
    app, _state = mcp._build_sdk_http_app("127.0.0.1", 8765)
    headers = {
        "host": "127.0.0.1:8765",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "version-test", "version": "1"},
        },
    }
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/mcp", json=request, headers=headers)
        assert response.status_code == 200
        assert response.json()["result"]["protocolVersion"] == protocol_version


def test_official_sdk_rejects_malformed_and_oversized_json():
    app, _state = mcp._build_sdk_http_app("127.0.0.1", 8765)
    headers = {
        "host": "127.0.0.1:8765",
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        malformed = client.post("/mcp", content=b"{bad", headers=headers)
        assert malformed.status_code == 400
        oversized = client.post(
            "/mcp", content=b"x" * (mcp._HTTP_MAX_REQUEST_BYTES + 1), headers=headers
        )
        assert oversized.status_code == 413


def test_sdk_activity_refreshes_idle_clock(monkeypatch):
    import anyio
    import mcp.types as mcp_types

    monkeypatch.setattr(mcp, "_last_request_time", 1.0)

    async def exercise():
        await mcp._sdk_list_tools(None, None)
        listed_at = mcp._last_request_time
        await mcp._sdk_call_http_tool(
            None, mcp_types.CallToolRequestParams(name="mempalace_get_aaak_spec", arguments={})
        )
        assert listed_at > 1.0
        assert mcp._last_request_time >= listed_at

    anyio.run(exercise)


def test_official_sdk_sse_emits_bounded_heartbeat(monkeypatch):
    import anyio
    from starlette.requests import Request

    class FakeLogstream:
        def latest_event_id(self):
            return None

        def list_events(self, **_kwargs):
            return []

    fake = FakeLogstream()
    monkeypatch.setattr(mcp, "_call_logstream", lambda operation: operation(fake))
    monkeypatch.setattr(mcp, "_SSE_HEARTBEAT_S", 0.0)
    monkeypatch.setattr(mcp, "_SSE_POLL_BASE_S", 0.0)
    monkeypatch.setattr(mcp, "_SSE_POLL_JITTER_S", 0.0)
    app, _state = mcp._build_sdk_http_app("127.0.0.1", 8765)
    route = next(
        route for route in app.wrapped.routes if getattr(route, "path", None) == "/logstream/stream"
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/logstream/stream",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8765),
            "scheme": "http",
        }
    )

    async def connected():
        await anyio.sleep(0)
        return False

    monkeypatch.setattr(request, "is_disconnected", connected)

    async def exercise():
        response = await route.endpoint(request)
        frame = await anext(response.body_iterator)
        assert frame == ": ping\n\n"
        await response.body_iterator.aclose()

    anyio.run(exercise)


def test_official_sdk_serve_port_zero_uses_actual_bound_port(monkeypatch):
    from types import SimpleNamespace

    captured = {}
    monkeypatch.setattr(mcp, "_start_peer_sync_thread", lambda: None)
    monkeypatch.setattr(mcp, "_config", SimpleNamespace(palace_path=None))
    monkeypatch.setattr(
        uvicorn.Server,
        "run",
        lambda self, sockets=None: captured.update(
            run_port=self.config.port, socket_port=sockets[0].getsockname()[1]
        ),
    )
    mcp._serve_http("127.0.0.1", 0)
    assert captured["run_port"] > 0
    assert captured["socket_port"] == captured["run_port"]


def test_official_sdk_serve_pins_tls_1_2(monkeypatch):
    from types import SimpleNamespace

    captured = {}
    monkeypatch.setattr(mcp, "_start_peer_sync_thread", lambda: None)
    monkeypatch.setattr(mcp, "_config", SimpleNamespace(palace_path=None))
    monkeypatch.setattr(mcp, "_resolve_tls_paths", lambda: ("cert.pem", "key.pem"))

    def fake_load(config):
        config.ssl = SimpleNamespace(minimum_version=None)
        config.loaded = True

    monkeypatch.setattr(uvicorn.Config, "load", fake_load)
    monkeypatch.setattr(
        uvicorn.Server,
        "run",
        lambda self, sockets=None: captured.update(minimum_version=self.config.ssl.minimum_version),
    )
    mcp._serve_http("127.0.0.1", 0)
    assert captured["minimum_version"] == ssl.TLSVersion.TLSv1_2


def test_post_dispatches_to_handle_request(http_server):
    """A real POST to /mcp reaches handle_request and returns its JSON-RPC reply."""
    port, _ = http_server
    status, body = _post(port, "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200
    payload = json.loads(body)
    assert payload["id"] == 1
    names = {t["name"] for t in payload["result"]["tools"]}
    assert "mempalace_search" in names


def test_initialize_reports_server_info(http_server):
    port, _ = http_server
    status, body = _post(port, "/mcp", {"jsonrpc": "2.0", "id": 7, "method": "initialize"})
    assert status == 200
    assert json.loads(body)["result"]["serverInfo"]["name"] == "mempalace"


def test_healthz_ok(http_server):
    port, _ = http_server
    status, body = _get(port, "/healthz")
    assert status == 200
    assert body == b"ok\n"


def test_statusz_reports_machine_readable_server_and_client_state(http_server, monkeypatch):
    monkeypatch.setattr(mcp, "_sqlite_integrity_payload", lambda: {"ok": True, "errors": []})
    port, _ = http_server

    assert _get(port, "/healthz", headers={"User-Agent": "codex-test"})[0] == 200
    status, body = _get(port, "/statusz", headers={"User-Agent": "codex-test"})

    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["server"]["name"] == "mempalace"
    assert payload["server"]["transport"] == "http"
    assert payload["server"]["port"] == port
    assert payload["requests"]["total"] >= 1
    assert payload["requests"]["by_status"]["200"] >= 1
    assert payload["clients"]["active_window_seconds"] == mcp._HTTP_ACTIVE_CLIENT_WINDOW_S
    assert payload["clients"]["recent"]
    first = payload["clients"]["recent"][0]
    assert first["peer"] == "127.0.0.1"
    assert first["peer_hint"] == "127.0.0.1"
    assert first["user_agent"] == "codex-test"
    assert first["last_path"] == "/healthz"
    assert "Authorization" not in json.dumps(payload)
    if mcp._config.palace_path:
        assert mcp._config.palace_path not in json.dumps(payload)


def test_unknown_path_404(http_server):
    port, _ = http_server
    assert _post(port, "/nope", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 404
    assert _get(port, "/nope")[0] == 404


def test_invalid_json_returns_parse_error(http_server):
    port, _ = http_server
    status, body = _post(port, "/mcp", b"{not valid json")
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32700


def test_oversized_request_rejected_413(http_server):
    """A declared Content-Length over the cap is rejected before the body is read."""
    port, _ = http_server
    # Lie about the length: the handler checks the header and returns 413 before
    # reading the (tiny) body, so we never have to ship 16 MiB.
    status, body = _post(
        port,
        "/mcp",
        b"{}",
        headers={"Content-Length": str(mcp._HTTP_MAX_REQUEST_BYTES + 1)},
    )
    assert status == 413
    assert json.loads(body)["error"]["code"] == -32600


def test_notification_returns_202_no_body(http_server):
    port, _ = http_server
    status, body = _post(port, "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202
    assert body == b""


def test_rejects_foreign_host_header(http_server):
    """DNS-rebinding guard: a request carrying an attacker domain in Host is 403."""
    port, _ = http_server
    status, _ = _post(
        port,
        "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        host_header="evil.example.com",
    )
    assert status == 403


def test_rejects_cross_origin(http_server):
    """A browser Origin from a non-loopback page is 403 (rebinding/SSRF guard)."""
    port, _ = http_server
    status, _ = _post(
        port,
        "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403


def test_allows_loopback_origin(http_server):
    port, _ = http_server
    status, _ = _post(
        port,
        "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Origin": "http://localhost:5173"},
    )
    assert status == 200


def test_bearer_token_enforced_when_configured(monkeypatch):
    """With MEMPALACE_MCP_HTTP_TOKEN set, /mcp requires a matching bearer token."""
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "s3cret")
    monkeypatch.setattr(mcp, "_sqlite_integrity_payload", lambda: {"ok": True, "errors": []})
    httpd = mcp._build_http_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        # No token → 401.
        assert _post(port, "/mcp", ping)[0] == 401
        # Wrong token → 401.
        assert _post(port, "/mcp", ping, headers={"Authorization": "Bearer nope"})[0] == 401
        # Correct token → 200.
        assert _post(port, "/mcp", ping, headers={"Authorization": "Bearer s3cret"})[0] == 200
        # /healthz never requires the token (orchestrator liveness probes).
        assert _get(port, "/healthz")[0] == 200
        # /statusz exposes server/client metadata, so it follows the auth policy.
        assert _get(port, "/statusz")[0] == 401
        status, body = _get(port, "/statusz", headers={"Authorization": "Bearer s3cret"})
        assert status == 200
        assert "recent" in json.loads(body)["clients"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_read_only_hides_and_refuses_mutating_tools(http_server, monkeypatch):
    """Read-only mode (#1877): the refused tools are hidden from tools/list AND
    refused at dispatch with -32003, while read tools still work."""
    monkeypatch.setattr(mcp, "_READ_ONLY", True)
    port, _ = http_server

    status, body = _post(port, "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200
    names = {t["name"] for t in json.loads(body)["result"]["tools"]}
    assert "mempalace_search" in names  # read tool stays
    assert "mempalace_add_drawer" not in names  # mutating tool hidden
    assert names.isdisjoint(mcp._READ_ONLY_REFUSED_TOOLS)

    status, body = _post(
        port,
        "/mcp",
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "mempalace_add_drawer", "arguments": {"content": "x"}},
        },
    )
    assert status == 200
    assert json.loads(body)["error"]["code"] == -32003


def test_read_only_off_exposes_mutating_tools(http_server):
    """Sanity: without read-only, mutating tools are present (guards the test above)."""
    port, _ = http_server
    status, body = _post(port, "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in json.loads(body)["result"]["tools"]}
    assert "mempalace_add_drawer" in names


def test_writable_http_refuses_startup_without_writer_lease(monkeypatch):
    monkeypatch.setattr(mcp, "_READ_ONLY", False)
    monkeypatch.setattr(
        mcp,
        "_acquire_mcp_writer_lock",
        lambda: (False, "another writer owns the palace"),
    )
    monkeypatch.setattr(
        mcp,
        "_serve_http",
        lambda *args: pytest.fail("server must not bind without the writer lease"),
    )

    with pytest.raises(SystemExit) as exc_info:
        mcp._run_http_loop()

    assert exc_info.value.code == 2


def test_read_only_http_skips_writer_lease(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp, "_READ_ONLY", True)
    monkeypatch.setattr(
        mcp,
        "_acquire_mcp_writer_lock",
        lambda: pytest.fail("read-only HTTP must not acquire the writer lease"),
    )
    monkeypatch.setattr(mcp, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp, "_start_idle_exit_watchdog", lambda: None)
    monkeypatch.setattr(mcp, "_serve_http", lambda host, port: calls.append((host, port)))

    mcp._run_http_loop()

    assert calls == [(mcp._args.host, mcp._args.port)]


def test_writable_http_releases_writer_lease_when_serving_ends(monkeypatch):
    events = []

    class DummyLease:
        def __exit__(self, *exc):
            events.append("lease-exit")
            return False

    lease = DummyLease()

    def acquire_writer():
        mcp._MCP_WRITER_LOCK_CM = lease
        return True, ""

    monkeypatch.setattr(mcp, "_READ_ONLY", False)
    monkeypatch.setattr(mcp, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(mcp, "_acquire_mcp_writer_lock", acquire_writer)
    monkeypatch.setattr(mcp, "_discard_mcp_storage_handles", lambda: events.append("discard"))
    monkeypatch.setattr(mcp, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp, "_start_idle_exit_watchdog", lambda: None)
    monkeypatch.setattr(mcp, "_serve_http", lambda host, port: events.append("serve"))

    mcp._run_http_loop()

    assert events == ["serve", "discard", "lease-exit"]
    assert mcp._MCP_WRITER_LOCK_CM is None


def test_writable_http_releases_writer_lease_after_bind_failure(monkeypatch):
    events = []

    class DummyLease:
        def __exit__(self, *exc):
            events.append("lease-exit")
            return False

    lease = DummyLease()

    def acquire_writer():
        mcp._MCP_WRITER_LOCK_CM = lease
        return True, ""

    def fail_bind(host, port):
        events.append("bind-failed")
        raise SystemExit(1)

    monkeypatch.setattr(mcp, "_READ_ONLY", False)
    monkeypatch.setattr(mcp, "_MCP_WRITER_LOCK_CM", None)
    monkeypatch.setattr(mcp, "_acquire_mcp_writer_lock", acquire_writer)
    monkeypatch.setattr(mcp, "_discard_mcp_storage_handles", lambda: events.append("discard"))
    monkeypatch.setattr(mcp, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp, "_start_idle_exit_watchdog", lambda: None)
    monkeypatch.setattr(mcp, "_serve_http", fail_bind)

    with pytest.raises(SystemExit) as exc_info:
        mcp._run_http_loop()

    assert exc_info.value.code == 1
    assert events == ["bind-failed", "discard", "lease-exit"]
    assert mcp._MCP_WRITER_LOCK_CM is None


def _hook_settings_call(req_id):
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {
            "name": "mempalace_hook_settings",
            "arguments": {"silent_save": False, "desktop_toast": True},
        },
    }


def test_read_only_refuses_the_hook_settings_config_write(http_server, monkeypatch, tmp_path):
    """mempalace_hook_settings writes the server's ~/.mempalace/config.json.

    It touches no palace state, so it is correctly absent from _MUTATING_TOOLS,
    the palace-write set the peer-writer lease arbitrates. Read-only gated on
    that set, which let a read-only server persist a config change on behalf of
    a client that is supposed to have no write access at all.

    The first half is the control: it proves the write really does land here, so
    the "unchanged" assertion in the second half cannot pass vacuously.
    """
    home = tmp_path / "home"
    (home / ".mempalace").mkdir(parents=True)
    cfg_file = home / ".mempalace" / "config.json"
    cfg_file.write_text(
        json.dumps({"hooks": {"silent_save": True, "desktop_toast": False}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOMEDRIVE", os.path.splitdrive(str(home))[0] or "C:")
    monkeypatch.setenv("HOMEPATH", os.path.splitdrive(str(home))[1] or str(home))
    pristine = cfg_file.read_bytes()

    port, _ = http_server

    # Control: the gate is off, so the very same call rewrites config.json.
    # _READ_ONLY is resolved at import from the environment, so pin it rather
    # than inherit whatever the suite was started with.
    monkeypatch.setattr(mcp, "_READ_ONLY", False)
    status, body = _post(port, "/mcp", _hook_settings_call(1))
    assert status == 200
    # The handler reports its own failures inside `result` as {"success": false},
    # not as a JSON-RPC error, so check the payload rather than just the envelope.
    payload = json.loads(body)
    assert "error" not in payload
    assert json.loads(payload["result"]["content"][0]["text"])["success"] is True
    assert cfg_file.read_bytes() != pristine
    cfg_file.write_bytes(pristine)

    # Gate on: hidden from tools/list, refused at dispatch, file left alone.
    monkeypatch.setattr(mcp, "_READ_ONLY", True)

    status, body = _post(port, "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in json.loads(body)["result"]["tools"]}
    assert "mempalace_hook_settings" not in names

    status, body = _post(port, "/mcp", _hook_settings_call(3))
    assert status == 200
    assert json.loads(body)["error"]["code"] == -32003
    assert cfg_file.read_bytes() == pristine


def test_read_only_refuses_the_checkpoint_ack_delete(http_server, monkeypatch, tmp_path):
    """mempalace_memories_filed_away unlinks the Stop hook's checkpoint ack file.

    Consuming that file is the contract of the tool, but it is still a delete of
    state that outlives the process, done for a client with no write access. Same
    two-phase shape as the config test: the control proves the delete lands, so
    the survival assertion afterwards cannot pass vacuously.
    """
    home = tmp_path / "home"
    state_dir = home / ".mempalace" / "hook_state"
    state_dir.mkdir(parents=True)
    ack = state_dir / "last_checkpoint"
    ack.write_text(json.dumps({"msgs": 7, "ts": "2026-01-01T00:00:00"}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOMEDRIVE", os.path.splitdrive(str(home))[0] or "C:")
    monkeypatch.setenv("HOMEPATH", os.path.splitdrive(str(home))[1] or str(home))

    port, _ = http_server
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_memories_filed_away", "arguments": {}},
    }

    # Control: the gate is off, so the call consumes the ack file.
    monkeypatch.setattr(mcp, "_READ_ONLY", False)
    status, body = _post(port, "/mcp", call)
    assert status == 200
    assert json.loads(json.loads(body)["result"]["content"][0]["text"])["count"] == 7
    assert not ack.exists()

    # Gate on: refused, and a fresh ack file survives untouched.
    ack.write_text(json.dumps({"msgs": 7, "ts": "2026-01-01T00:00:00"}), encoding="utf-8")
    pristine = ack.read_bytes()
    monkeypatch.setattr(mcp, "_READ_ONLY", True)

    status, body = _post(port, "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in json.loads(body)["result"]["tools"]}
    assert "mempalace_memories_filed_away" not in names

    status, body = _post(port, "/mcp", dict(call, id=3))
    assert status == 200
    assert json.loads(body)["error"]["code"] == -32003
    assert ack.read_bytes() == pristine


@pytest.mark.parametrize(
    "disconnect_exc",
    [
        ConnectionResetError(104, "connection reset by peer"),
        BrokenPipeError(32, "broken pipe"),
        ssl.SSLEOFError("unexpected eof while reading"),
    ],
    ids=["connreset", "brokenpipe", "ssleof"],
)
def test_handle_error_quiets_client_disconnect(caplog, monkeypatch, disconnect_exc):
    """Regression for #2003: a client that hangs up mid-response makes the send
    path raise ConnectionError (BrokenPipeError / ConnectionResetError), or
    ssl.SSLEOFError on the TLS transport. The server must log that quietly at
    DEBUG instead of routing it to the default handler's per-request traceback.
    """
    httpd = mcp._build_http_server("127.0.0.1", 0)
    try:
        delegated = []
        monkeypatch.setattr(
            socketserver.BaseServer,
            "handle_error",
            lambda self, request, addr: delegated.append(addr),
        )
        addr = ("127.0.0.1", 51234)

        with caplog.at_level(logging.DEBUG, logger="mempalace_mcp"):
            try:
                raise disconnect_exc
            except type(disconnect_exc):
                httpd.handle_error(None, addr)

        assert delegated == []  # noisy default handler NOT invoked
        rec = next(r for r in caplog.records if "disconnect" in r.getMessage().lower())
        assert rec.levelno == logging.DEBUG
        assert rec.name == "mempalace_mcp"
    finally:
        httpd.server_close()


def test_handle_error_delegates_real_errors(monkeypatch):
    """A genuine error is NOT misclassified as a disconnect: it reaches the
    default handler, so its traceback is still surfaced.
    """
    httpd = mcp._build_http_server("127.0.0.1", 0)
    try:
        delegated = []
        monkeypatch.setattr(
            socketserver.BaseServer,
            "handle_error",
            lambda self, request, addr: delegated.append(addr),
        )
        addr = ("127.0.0.1", 51234)
        try:
            raise ValueError("boom")
        except ValueError:
            httpd.handle_error(None, addr)
        assert delegated == [addr]
    finally:
        httpd.server_close()


def _make_self_signed_cert(tmp_path):
    """Write a throwaway self-signed cert/key via openssl; skip if unavailable."""
    import shutil
    import subprocess

    if shutil.which("openssl") is None:
        pytest.skip("openssl not available to generate a test certificate")
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def test_tls_serves_https(tmp_path, monkeypatch):
    """With --tls-cert/--tls-key (via env), the server speaks TLS: a plain HTTP
    client cannot read it, and an HTTPS client trusting the cert can."""
    import ssl

    cert, key = _make_self_signed_cert(tmp_path)
    monkeypatch.setenv("MEMPALACE_MCP_TLS_CERT", str(cert))
    monkeypatch.setenv("MEMPALACE_MCP_TLS_KEY", str(key))

    httpd = mcp._build_http_server("127.0.0.1", 0)
    assert getattr(httpd, "scheme", "http") == "https"
    port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        # Full verification on: trust the self-signed cert as the CA and dial
        # "localhost" (the cert CN, resolves to 127.0.0.1) so hostname checking
        # passes without being disabled.
        ctx = ssl.create_default_context(cafile=str(cert))
        conn = http.client.HTTPSConnection("localhost", port, context=ctx, timeout=5)
        try:
            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.read() == b"ok\n"
        finally:
            conn.close()

        # A plaintext HTTP client must NOT be able to talk to the TLS socket.
        with pytest.raises(Exception):
            plain = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            plain.request("GET", "/healthz")
            plain.getresponse()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_tls_requires_both_cert_and_key(tmp_path, monkeypatch):
    """A cert without a key (or vice versa) is a startup error, not a silent skip."""
    cert, _key = _make_self_signed_cert(tmp_path)
    monkeypatch.setenv("MEMPALACE_MCP_TLS_CERT", str(cert))
    monkeypatch.delenv("MEMPALACE_MCP_TLS_KEY", raising=False)
    with pytest.raises(ValueError, match="both"):
        mcp._build_http_server("127.0.0.1", 0)


def test_loopback_and_origin_helpers():
    assert mcp._http_is_loopback("127.0.0.1")
    assert mcp._http_is_loopback("localhost")
    assert not mcp._http_is_loopback("0.0.0.0")
    assert not mcp._http_is_loopback("192.168.1.10")
    assert mcp._http_origin_allowed("http://127.0.0.1:8765")
    assert mcp._http_origin_allowed("http://localhost")
    assert not mcp._http_origin_allowed("https://evil.example")
    assert not mcp._http_origin_allowed("garbage")
    allowed = mcp._http_allowed_host_values("127.0.0.1", 8765)
    assert "127.0.0.1:8765" in allowed and "localhost" in allowed


def test_extra_allowed_hosts_extend_the_loopback_pin(monkeypatch):
    """A loopback-bound server behind a fronting proxy (tailscale serve, nginx)
    receives the public name in Host; the operator allowlists it via env."""
    monkeypatch.setenv(
        "MEMPALACE_MCP_EXTRA_ALLOWED_HOSTS",
        "mybox.tail1234.ts.net, Proxy.Example:8443",
    )
    allowed = mcp._http_allowed_host_values("127.0.0.1", 8765)
    # Bare hostname matches with and without the bound port.
    assert "mybox.tail1234.ts.net" in allowed
    assert "mybox.tail1234.ts.net:8765" in allowed
    # host:port entries match exactly (lowercased); no bound-port variant added.
    assert "proxy.example:8443" in allowed
    assert "proxy.example" not in allowed
    # The loopback pin itself is unchanged.
    assert "127.0.0.1:8765" in allowed


def test_extra_allowed_hosts_default_empty(monkeypatch):
    monkeypatch.delenv("MEMPALACE_MCP_EXTRA_ALLOWED_HOSTS", raising=False)
    allowed = mcp._http_allowed_host_values("127.0.0.1", 8765)
    assert not any("ts.net" in v for v in allowed)
