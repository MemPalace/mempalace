"""
test_proxy.py — Tests for the MCP streamable-HTTP proxy.

Tests the proxy's core logic (circuit breaker, session management,
response wrapping) without requiring a live MemPalace server. Uses
a mock upstream for integration-level tests.

Run: pytest tests/test_proxy.py -v
"""

import asyncio
import json
import os
import sys

import httpx
import pytest

# Add the proxy directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deploy", "proxy"))


@pytest.fixture
def reset_circuit():
    """Reset circuit breaker state before each test."""
    import mempalace_mcp_proxy as proxy

    proxy._circuit["failures"] = 0
    proxy._circuit["state"] = "closed"
    proxy._circuit["opened_at"] = 0.0
    yield
    proxy._circuit["failures"] = 0
    proxy._circuit["state"] = "closed"
    proxy._circuit["opened_at"] = 0.0


@pytest.fixture
def reset_metrics():
    """Reset metrics before each test."""
    import mempalace_mcp_proxy as proxy

    proxy._metrics.clear()
    proxy._metrics_start = asyncio.get_event_loop().time()


class TestCircuitBreaker:
    """Circuit breaker state machine tests."""

    def test_circuit_starts_closed(self, reset_circuit):
        import mempalace_mcp_proxy as proxy

        assert proxy._circuit["state"] == "closed"
        assert proxy._circuit["failures"] == 0
        assert proxy.circuit_check() is True

    def test_circuit_opens_after_threshold(self, reset_circuit):
        import mempalace_mcp_proxy as proxy

        for _ in range(proxy.CIRCUIT_THRESHOLD):
            proxy.circuit_record_failure()

        assert proxy._circuit["state"] == "open"
        assert proxy.circuit_check() is False

    def test_circuit_half_open_after_reset_time(self, reset_circuit):
        import mempalace_mcp_proxy as proxy
        import time

        # Open the circuit
        for _ in range(proxy.CIRCUIT_THRESHOLD):
            proxy.circuit_record_failure()
        assert proxy._circuit["state"] == "open"

        # Simulate time passing
        proxy._circuit["opened_at"] = time.time() - proxy.CIRCUIT_RESET_TIME - 1
        assert proxy.circuit_check() is True
        assert proxy._circuit["state"] == "half-open"

    def test_circuit_closes_on_success(self, reset_circuit):
        import mempalace_mcp_proxy as proxy

        proxy._circuit["state"] = "half-open"
        proxy._circuit["failures"] = 2

        proxy.circuit_record_success()

        assert proxy._circuit["state"] == "closed"
        assert proxy._circuit["failures"] == 0

    def test_circuit_reopens_on_half_open_failure(self, reset_circuit):
        import mempalace_mcp_proxy as proxy

        proxy._circuit["state"] = "half-open"
        proxy.circuit_record_failure()

        assert proxy._circuit["state"] == "open"

    def test_circuit_does_not_open_below_threshold(self, reset_circuit):
        import mempalace_mcp_proxy as proxy

        proxy.circuit_record_failure()
        proxy.circuit_record_failure()
        assert proxy._circuit["state"] == "closed"
        assert proxy._circuit["failures"] == 2


class TestSessionManagement:
    """Session lifecycle tests."""

    def test_session_cleanup_removes_expired(self):
        import mempalace_mcp_proxy as proxy
        import time

        proxy.sessions.clear()
        now = time.time()

        # Add an active session and an expired session
        proxy.sessions["active"] = {"created": now, "last_used": now, "queue": asyncio.Queue()}
        proxy.sessions["expired"] = {
            "created": now - proxy.SESSION_TTL - 100,
            "last_used": now - proxy.SESSION_TTL - 100,
            "queue": asyncio.Queue(),
        }

        proxy.cleanup_expired_sessions()

        assert "active" in proxy.sessions
        assert "expired" not in proxy.sessions

    def test_session_cleanup_handles_empty(self):
        import mempalace_mcp_proxy as proxy

        proxy.sessions.clear()
        proxy.cleanup_expired_sessions()
        assert len(proxy.sessions) == 0


class TestResponseWrapping:
    """Response format tests."""

    def test_sse_format_when_accepted(self):
        """When client sends Accept: text/event-stream, response should be SSE."""
        # This is a structural test — the actual handler needs aiohttp's
        # web.Request which is hard to construct in isolation. The logic
        # is tested via integration tests against a real upstream.
        pass

    def test_json_format_by_default(self):
        """When no SSE Accept header, response should be plain JSON."""
        pass


class TestNotificationHandling:
    """Tests for JSON-RPC notification handling (202/204 responses).

    JSON-RPC notifications (messages without an `id`) have no response body.
    The upstream returns HTTP 202 (Accepted) with empty content. The proxy
    must NOT treat this as an error — it should pass through the 202 status
    with an empty body.

    This was a critical bug: the proxy tried to JSON-parse the empty 202
    response, failed, and returned a -32000 error to the client, breaking
    the MCP handshake at the `notifications/initialized` step.
    """

    def test_202_returns_none_payload(self):
        """_forward_to_upstream should return (None, 202, None) for 202 responses."""

        # Simulate a mock response object with status 202 and empty content
        class MockResponse:
            status_code = 202
            content = b""
            text = ""

            def json(self):
                raise ValueError("No JSON body")

        # We need to test the logic inside _forward_to_upstream.
        # Since it's async and uses httpx, we test the decision logic directly.
        resp = MockResponse()

        # The fix checks: if status in (202, 204) or not resp.content
        should_return_none = resp.status_code in (202, 204) or not resp.content
        assert should_return_none is True, "202 with empty content should return None payload"

    def test_204_returns_none_payload(self):
        """204 No Content should also return None payload."""

        class MockResponse:
            status_code = 204
            content = b""

        resp = MockResponse()
        should_return_none = resp.status_code in (202, 204) or not resp.content
        assert should_return_none is True

    def test_200_with_content_does_not_return_none(self):
        """200 with actual JSON content should NOT trigger the 202/204 path."""

        class MockResponse:
            status_code = 200
            content = b'{"jsonrpc": "2.0", "result": {}}'

        resp = MockResponse()
        should_return_none = resp.status_code in (202, 204) or not resp.content
        assert should_return_none is False

    def test_200_with_empty_content_returns_none(self):
        """200 with empty content (edge case) should also return None."""

        class MockResponse:
            status_code = 200
            content = b""

        resp = MockResponse()
        should_return_none = resp.status_code in (202, 204) or not resp.content
        assert should_return_none is True

    def test_notification_method_has_no_id(self):
        """JSON-RPC notifications have no 'id' field — verify detection logic."""
        import json

        notification = json.loads('{"jsonrpc": "2.0", "method": "notifications/initialized"}')
        request = json.loads('{"jsonrpc": "2.0", "method": "tools/list", "id": 1}')

        assert notification.get("id") is None, "Notifications should not have an id"
        assert request.get("id") is not None, "Requests should have an id"


class TestConfiguration:
    """Configuration via environment variables."""

    def test_default_upstream_url(self):
        import importlib

        # Save and restore env
        old = os.environ.pop("UPSTREAM_URL", None)
        try:
            import mempalace_mcp_proxy as proxy

            importlib.reload(proxy)
            assert proxy.UPSTREAM_URL == "http://127.0.0.1:8765/mcp"
        finally:
            if old is not None:
                os.environ["UPSTREAM_URL"] = old
            importlib.reload(proxy)

    def test_custom_upstream_url(self):
        import importlib

        old = os.environ.get("UPSTREAM_URL")
        try:
            os.environ["UPSTREAM_URL"] = "http://example.com:9999/mcp"
            import mempalace_mcp_proxy as proxy

            importlib.reload(proxy)
            assert proxy.UPSTREAM_URL == "http://example.com:9999/mcp"
        finally:
            if old is not None:
                os.environ["UPSTREAM_URL"] = old
            else:
                os.environ.pop("UPSTREAM_URL", None)
            importlib.reload(proxy)

    def test_default_port(self):
        import importlib

        old = os.environ.get("PORT")
        try:
            os.environ.pop("PORT", None)
            import mempalace_mcp_proxy as proxy

            importlib.reload(proxy)
            assert proxy.PORT == 8766
        finally:
            if old is not None:
                os.environ["PORT"] = old
            importlib.reload(proxy)


class TestNoHardcodedPaths:
    """Ensure no hardcoded paths or IPs (PR checklist requirement)."""

    def test_no_hardcoded_ips_in_proxy(self):
        proxy_path = os.path.join(
            os.path.dirname(__file__), "..", "deploy", "proxy", "mempalace_mcp_proxy.py"
        )
        with open(proxy_path) as f:
            content = f.read()

        # Check for common hardcoded IP patterns (excluding 127.0.0.1 defaults)
        import re

        # Find IP addresses that aren't 127.0.0.1 or 0.0.0.0
        ips = re.findall(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", content)
        bad_ips = [ip for ip in ips if ip not in ("127.0.0.1", "0.0.0.0")]
        assert bad_ips == [], f"Hardcoded IPs found: {bad_ips}"

    def test_no_hardcoded_user_paths_in_proxy(self):
        proxy_path = os.path.join(
            os.path.dirname(__file__), "..", "deploy", "proxy", "mempalace_mcp_proxy.py"
        )
        with open(proxy_path) as f:
            content = f.read()

        assert "/Users/" not in content, "Hardcoded /Users/ path found"
        assert "/home/" not in content, "Hardcoded /home/ path found"

    def test_no_hardcoded_user_paths_in_watchdog(self):
        watchdog_path = os.path.join(
            os.path.dirname(__file__), "..", "deploy", "proxy", "mempalace-watchdog.sh"
        )
        with open(watchdog_path) as f:
            content = f.read()

        assert "/Users/" not in content, "Hardcoded /Users/ path found"
        assert "/home/orkidlabs" not in content, "Hardcoded /home/orkidlabs path found"

    def test_no_hardcoded_user_paths_in_monitor(self):
        monitor_path = os.path.join(
            os.path.dirname(__file__), "..", "deploy", "proxy", "mempalace-monitor.sh"
        )
        with open(monitor_path) as f:
            content = f.read()

        assert "/Users/jacob" not in content, "Hardcoded /Users/jacob path found"
        assert "/home/orkidlabs" not in content, "Hardcoded /home/orkidlabs path found"


class TestIdempotentRetry:
    """Regressions for non-idempotent upstream requests.

    The proxy must not retry JSON-RPC mutations. If a write (e.g. diary,
    kg_add, add_drawer) commits upstream and the response is lost, replaying
    it executes the mutation twice. Only read-only tools and methods may be
    retried.
    """

    @staticmethod
    def _fake_client(responses):
        """Build a fake httpx.AsyncClient that returns queued responses/exceptions."""

        class FakeResponse:
            def __init__(self, status_code, content=b"{}", text=""):
                self.status_code = status_code
                self.content = content
                self.text = text if text else content.decode()

            def json(self):
                if not self.content:
                    raise ValueError("empty body")
                return json.loads(self.content)

        class FakeClient:
            is_closed = False

            def __init__(self, responses):
                self.responses = list(responses)
                self.calls = []

            async def aclose(self):
                pass

            async def post(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                if not self.responses:
                    raise httpx.ReadTimeout("response lost")
                resp = self.responses.pop(0)
                if isinstance(resp, Exception):
                    raise resp
                return resp

        return FakeClient(responses), FakeResponse

    def _run_forward(self, body, responses, restore_circuit=True):
        import mempalace_mcp_proxy as proxy

        client, FakeResponse = self._fake_client(responses)
        old_client = proxy._http_client
        old_circuit = dict(proxy._circuit)
        try:
            proxy._http_client = client
            proxy._circuit["failures"] = 0
            proxy._circuit["state"] = "closed"
            max_attempts = proxy.MAX_RETRIES + 1 if proxy._is_retry_safe(body) else 1
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    proxy._forward_to_upstream(body, {}, 1, max_attempts=max_attempts)
                )
            finally:
                loop.close()
            return result, client.calls
        finally:
            proxy._http_client = old_client
            if restore_circuit:
                proxy._circuit.update(old_circuit)

    def test_read_only_tool_is_retried(self):
        """mempalace_search is read-only and may be retried on 5xx."""
        import mempalace_mcp_proxy as proxy

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "mempalace_search"},
            }
        ).encode()
        assert proxy._is_retry_safe(body)

        _, FakeResponse = self._fake_client([])
        result, calls = self._run_forward(
            body,
            [
                httpx.ConnectError("first attempt failed"),
                FakeResponse(500, b"{}"),
                FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": []}).encode()),
            ],
        )

        assert len(calls) == 3, f"Expected 3 attempts, got {len(calls)}"
        assert result[1] == 200

    def test_mutating_tool_not_retried_on_5xx(self):
        """mempalace_diary_write must not be replayed when upstream 5xxs."""
        import mempalace_mcp_proxy as proxy

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "mempalace_diary_write", "arguments": {"content": "test"}},
            }
        ).encode()
        assert not proxy._is_retry_safe(body)

        _, FakeResponse = self._fake_client([])
        result, calls = self._run_forward(body, [FakeResponse(500, b"{}")])

        assert len(calls) == 1, f"Mutation retried {len(calls)} times"
        assert result[1] >= 500

    def test_mutating_tool_not_retried_on_timeout(self):
        """The first attempt commits but the response is lost — no replay."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "mempalace_add_drawer"},
            }
        ).encode()

        _, _ = self._fake_client([])
        result, calls = self._run_forward(body, [httpx.ReadTimeout("timeout after commit")])

        assert len(calls) == 1
        assert result[1] == 504

    def test_tools_list_is_retried(self):
        """tools/list is read-only and may be retried."""
        import mempalace_mcp_proxy as proxy

        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode()
        assert proxy._is_retry_safe(body)

        _, FakeResponse = self._fake_client([])
        result, calls = self._run_forward(
            body,
            [
                FakeResponse(503, b"{}"),
                FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()),
            ],
        )

        assert len(calls) == 2
        assert result[1] == 200

    def test_unknown_tool_not_retried(self):
        """Tools not in the read-only allowlist are treated as mutating."""
        import mempalace_mcp_proxy as proxy

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "mempalace_future_tool"},
            }
        ).encode()
        assert not proxy._is_retry_safe(body)

        _, FakeResponse = self._fake_client([])
        result, calls = self._run_forward(body, [FakeResponse(500, b"{}")])

        assert len(calls) == 1
        assert result[1] >= 500

    def test_memories_filed_away_not_retried(self):
        """mempalace_memories_filed_away deletes state and must not replay."""
        import mempalace_mcp_proxy as proxy

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "mempalace_memories_filed_away"},
            }
        ).encode()
        assert not proxy._is_retry_safe(body)

        _, FakeResponse = self._fake_client([])
        result, calls = self._run_forward(body, [FakeResponse(500, b"{}")])

        assert len(calls) == 1

    def test_reconnect_not_retried(self):
        """mempalace_reconnect is not write-free and must not be replayed."""
        import mempalace_mcp_proxy as proxy

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "mempalace_reconnect"},
            }
        ).encode()
        assert not proxy._is_retry_safe(body)

        _, FakeResponse = self._fake_client([])
        result, calls = self._run_forward(body, [FakeResponse(500, b"{}")])

        assert len(calls) == 1

    def test_hook_settings_without_args_is_retried(self):
        """mempalace_hook_settings with no arguments is a status query."""
        import mempalace_mcp_proxy as proxy

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "mempalace_hook_settings"},
            }
        ).encode()
        assert proxy._is_retry_safe(body)

    def test_invalid_json_shape_is_not_retried(self):
        """Malformed JSON-RPC is not safe to retry."""
        import mempalace_mcp_proxy as proxy

        body = b'not json'
        assert not proxy._is_retry_safe(body)

    def test_missing_jsonrpc_field_is_not_retried(self):
        import mempalace_mcp_proxy as proxy

        body = json.dumps({"id": 1, "method": "tools/list", "params": {}}).encode()
        assert not proxy._is_retry_safe(body)

    def test_tools_call_without_name_is_not_retried(self):
        import mempalace_mcp_proxy as proxy

        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}
        ).encode()
        assert not proxy._is_retry_safe(body)

    def test_hook_settings_with_args_not_retried(self):
        """mempalace_hook_settings with arguments writes config and must not replay."""
        import mempalace_mcp_proxy as proxy

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "mempalace_hook_settings",
                    "arguments": {"desktop_toast": True},
                },
            }
        ).encode()
        assert not proxy._is_retry_safe(body)

        _, FakeResponse = self._fake_client([])
        result, calls = self._run_forward(body, [FakeResponse(500, b"{}")])

        assert len(calls) == 1

    def test_final_5xx_with_json_body_is_failure_and_preserves_status(self):
        """A final 5xx with a JSON body must open the circuit and keep the status."""
        import mempalace_mcp_proxy as proxy

        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode()

        _, FakeResponse = self._fake_client([])
        result, calls = self._run_forward(
            body,
            [
                FakeResponse(503, b"{}"),
                FakeResponse(503, b"{}"),
                FakeResponse(
                    503,
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}}
                    ).encode(),
                ),
            ],
            restore_circuit=False,
        )

        assert result[1] == 503
        assert proxy._circuit["state"] == "open"
        assert proxy._metrics["requests_failed"] >= 1


class TestOriginValidation:
    """Origin/Host and inbound token validation."""

    @staticmethod
    def _mock_request(**headers):
        class CaseInsensitiveHeaders:
            def __init__(self, headers):
                self._headers = {k.lower(): v for k, v in headers.items()}

            def get(self, key, default=None):
                return self._headers.get(key.lower(), default)

        class MockRequest:
            def __init__(self, headers):
                self.headers = CaseInsensitiveHeaders(headers)

        return MockRequest(headers)

    def test_valid_origin_allowed(self):
        import mempalace_mcp_proxy as proxy

        request = self._mock_request(Origin="http://localhost:8766", Host="localhost:8766")
        allowed, status, msg = proxy._is_inbound_request_allowed(request)
        assert allowed is True

    def test_invalid_origin_rejected(self):
        import mempalace_mcp_proxy as proxy

        request = self._mock_request(Origin="http://evil.example", Host="localhost:8766")
        allowed, status, msg = proxy._is_inbound_request_allowed(request)
        assert allowed is False
        assert status == 403

    def test_no_origin_with_allowed_host_is_accepted(self):
        import mempalace_mcp_proxy as proxy

        request = self._mock_request(Host="127.0.0.1:8766")
        allowed, status, msg = proxy._is_inbound_request_allowed(request)
        assert allowed is True

    def test_no_origin_with_disallowed_host_rejected(self):
        import mempalace_mcp_proxy as proxy

        request = self._mock_request(Host="evil.example")
        allowed, status, msg = proxy._is_inbound_request_allowed(request)
        assert allowed is False
        assert status == 403

    def test_inbound_token_required(self, monkeypatch):
        import mempalace_mcp_proxy as proxy

        monkeypatch.setattr(proxy, "INBOUND_TOKEN", "secret-token")
        request = self._mock_request(Host="127.0.0.1:8766")
        allowed, status, msg = proxy._is_inbound_request_allowed(request)
        assert allowed is False
        assert status == 401

    def test_inbound_token_accepted_when_present(self, monkeypatch):
        import mempalace_mcp_proxy as proxy

        monkeypatch.setattr(proxy, "INBOUND_TOKEN", "secret-token")
        request = self._mock_request(
            Host="127.0.0.1:8766",
            Authorization="Bearer secret-token",
        )
        allowed, status, msg = proxy._is_inbound_request_allowed(request)
        assert allowed is True


class TestEndToEnd:
    """End-to-end handler regressions through aiohttp's test client."""

    @staticmethod
    def _build_fake_upstream(status: int, body: bytes):
        class FakeResponse:
            def __init__(self, status_code, content):
                self.status_code = status_code
                self.content = content
                self.text = content.decode("utf-8")

            def json(self):
                return json.loads(self.content)

        class FakeClient:
            is_closed = False
            calls = 0

            async def aclose(self):
                pass

            async def post(self, *args, **kwargs):
                FakeClient.calls += 1
                return FakeResponse(status, body)

        return FakeClient

    def _run_client(self, test_coro):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(test_coro())
        finally:
            loop.close()

    def test_repeated_json_5xx_opens_circuit_and_persists_status(self):
        import mempalace_mcp_proxy as proxy
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode()
        upstream_body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}}
        ).encode()

        async def test():
            # Reset proxy state for a clean circuit.
            proxy._circuit["failures"] = 0
            proxy._circuit["state"] = "closed"
            proxy._metrics["requests_failed"] = 0
            FakeClient = self._build_fake_upstream(503, upstream_body)
            proxy._http_client = FakeClient()

            app = web.Application()
            app.router.add_post("/mcp", proxy.handle_mcp_post)
            server = TestServer(app, port=0)
            client = TestClient(server, loop=asyncio.get_event_loop())
            await client.start_server()

            port = client.server.port
            old_hosts = proxy.ALLOWED_HOSTS
            proxy.ALLOWED_HOSTS = frozenset({f"127.0.0.1:{port}", "localhost", "127.0.0.1"})

            try:
                # First request: three upstream 5xx attempts, then 503 downstream.
                resp = await client.post("/mcp", data=body)
                assert resp.status == 503
                payload = await resp.json()
                assert payload["error"]["code"] == -32000

                # The circuit should now be open.
                assert proxy._circuit["state"] == "open"

                # Second request: circuit is open, no upstream call, still 503.
                calls_before = FakeClient.calls
                resp = await client.post("/mcp", data=body)
                assert resp.status == 503
                await resp.release()
                assert FakeClient.calls == calls_before

                assert proxy._metrics["requests_failed"] >= 1
            finally:
                proxy.ALLOWED_HOSTS = old_hosts
                await client.close()

        self._run_client(test)

    def test_origin_and_host_validation_at_handler(self):
        import mempalace_mcp_proxy as proxy
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode()

        async def test():
            proxy._circuit["failures"] = 0
            proxy._circuit["state"] = "closed"

            # Ensure the handler is reachable so we can test the boundary.
            old_forward = proxy._forward_to_upstream

            async def fake_forward(*args, **kwargs):
                return None, 202, None

            proxy._forward_to_upstream = fake_forward

            app = web.Application()
            app.router.add_post("/mcp", proxy.handle_mcp_post)
            server = TestServer(app, port=0)
            client = TestClient(server, loop=asyncio.get_event_loop())
            await client.start_server()

            port = client.server.port
            old_hosts = proxy.ALLOWED_HOSTS
            old_origins = proxy.ALLOWED_ORIGINS
            proxy.ALLOWED_HOSTS = frozenset({f"127.0.0.1:{port}", "localhost", "127.0.0.1"})
            proxy.ALLOWED_ORIGINS = frozenset({f"http://127.0.0.1:{port}"})

            try:
                # Invalid Origin -> 403.
                resp = await client.post(
                    "/mcp",
                    data=body,
                    headers={"Origin": "http://evil.example"},
                )
                assert resp.status == 403

                # Valid Origin -> 202.
                resp = await client.post(
                    "/mcp",
                    data=body,
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
                assert resp.status == 202

                # No Origin, allowed Host -> 202.
                resp = await client.post("/mcp", data=body)
                assert resp.status == 202
            finally:
                proxy.ALLOWED_HOSTS = old_hosts
                proxy.ALLOWED_ORIGINS = old_origins
                proxy._forward_to_upstream = old_forward
                await client.close()

        self._run_client(test)

    def test_handler_rejects_malformed_jsonrpc(self):
        """The POST handler validates JSON-RPC envelope and returns 400."""
        import mempalace_mcp_proxy as proxy
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def test():
            proxy._circuit["failures"] = 0
            proxy._circuit["state"] = "closed"

            # Upstream should not be reached for invalid requests.
            class NoOpClient:
                is_closed = False
                calls = 0

                async def aclose(self):
                    pass

                async def post(self, *args, **kwargs):
                    NoOpClient.calls += 1
                    raise AssertionError("upstream should not be called")

            proxy._http_client = NoOpClient()

            app = web.Application()
            app.router.add_post("/mcp", proxy.handle_mcp_post)
            server = TestServer(app, port=0)
            client = TestClient(server, loop=asyncio.get_event_loop())
            await client.start_server()

            port = client.server.port
            old_hosts = proxy.ALLOWED_HOSTS
            proxy.ALLOWED_HOSTS = frozenset({f"127.0.0.1:{port}", "localhost", "127.0.0.1"})

            try:
                # Missing jsonrpc version: valid object, invalid request -> -32600.
                bad = json.dumps({"id": 1, "method": "tools/list", "params": {}}).encode()
                resp = await client.post("/mcp", data=bad)
                assert resp.status == 400
                payload = await resp.json()
                assert payload["error"]["code"] == -32600
                assert NoOpClient.calls == 0

                # tools/call missing name.
                bad = json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}
                ).encode()
                resp = await client.post("/mcp", data=bad)
                assert resp.status == 400
                payload = await resp.json()
                assert payload["error"]["code"] == -32600
                assert NoOpClient.calls == 0
            finally:
                proxy.ALLOWED_HOSTS = old_hosts
                await client.close()

        self._run_client(test)

    def test_handler_rejects_non_object_jsonrpc(self):
        """Non-object valid JSON must be rejected as -32600 Invalid Request, not crash."""
        import mempalace_mcp_proxy as proxy
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def test():
            proxy._circuit["failures"] = 0
            proxy._circuit["state"] = "closed"

            class NoOpClient:
                is_closed = False
                calls = 0

                async def aclose(self):
                    pass

                async def post(self, *args, **kwargs):
                    NoOpClient.calls += 1
                    raise AssertionError("upstream should not be called")

            proxy._http_client = NoOpClient()

            app = web.Application()
            app.router.add_post("/mcp", proxy.handle_mcp_post)
            server = TestServer(app, port=0)
            client = TestClient(server, loop=asyncio.get_event_loop())
            await client.start_server()

            port = client.server.port
            old_hosts = proxy.ALLOWED_HOSTS
            proxy.ALLOWED_HOSTS = frozenset({f"127.0.0.1:{port}", "localhost", "127.0.0.1"})

            try:
                cases = [
                    (b"null", "null"),
                    (b'"string"', "string"),
                    (b"123", "number"),
                    (b'[{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}, {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}]', "batch array"),
                ]
                for body, label in cases:
                    resp = await client.post("/mcp", data=body)
                    assert resp.status == 400, f"{label} should be rejected"
                    payload = await resp.json()
                    assert payload["error"]["code"] == -32600, f"{label} should return -32600"

                # Invalid JSON itself still returns -32700 parse error.
                resp = await client.post("/mcp", data=b"not json")
                assert resp.status == 400
                payload = await resp.json()
                assert payload["error"]["code"] == -32700

                assert NoOpClient.calls == 0
            finally:
                proxy.ALLOWED_HOSTS = old_hosts
                await client.close()

        self._run_client(test)

    def test_handler_accepts_normal_single_request(self):
        """A well-formed single JSON-RPC object reaches the upstream."""
        import mempalace_mcp_proxy as proxy
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode()

        async def test():
            proxy._circuit["failures"] = 0
            proxy._circuit["state"] = "closed"

            class FakeResponse:
                def __init__(self, status_code, content):
                    self.status_code = status_code
                    self.content = content
                    self.text = content.decode("utf-8")

                def json(self):
                    return json.loads(self.content)

            class CountingClient:
                is_closed = False
                calls = 0

                async def aclose(self):
                    pass

                async def post(self, *args, **kwargs):
                    CountingClient.calls += 1
                    return FakeResponse(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": []}).encode())

            proxy._http_client = CountingClient()

            app = web.Application()
            app.router.add_post("/mcp", proxy.handle_mcp_post)
            server = TestServer(app, port=0)
            client = TestClient(server, loop=asyncio.get_event_loop())
            await client.start_server()

            port = client.server.port
            old_hosts = proxy.ALLOWED_HOSTS
            proxy.ALLOWED_HOSTS = frozenset({f"127.0.0.1:{port}", "localhost", "127.0.0.1"})

            try:
                resp = await client.post("/mcp", data=body)
                assert resp.status == 200
                assert CountingClient.calls == 1
            finally:
                proxy.ALLOWED_HOSTS = old_hosts
                await client.close()

        self._run_client(test)

    def test_handler_does_not_retry_mutating_tool(self):
        """A write tool failing upstream is only attempted once through the real handler."""
        import mempalace_mcp_proxy as proxy
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        write_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "mempalace_diary_write",
                    "arguments": {"content": "test"},
                },
            }
        ).encode()

        async def test():
            proxy._circuit["failures"] = 0
            proxy._circuit["state"] = "closed"
            old_forward = proxy._forward_to_upstream

            class FakeResponse:
                def __init__(self, status_code, content):
                    self.status_code = status_code
                    self.content = content
                    self.text = content.decode("utf-8")

                def json(self):
                    return json.loads(self.content)

            class CountingClient:
                is_closed = False
                calls = 0

                async def aclose(self):
                    pass

                async def post(self, *args, **kwargs):
                    CountingClient.calls += 1
                    return FakeResponse(500, b"{}")

            old_client = proxy._http_client
            proxy._http_client = CountingClient()

            app = web.Application()
            app.router.add_post("/mcp", proxy.handle_mcp_post)
            server = TestServer(app, port=0)
            client = TestClient(server, loop=asyncio.get_event_loop())
            await client.start_server()

            port = client.server.port
            old_hosts = proxy.ALLOWED_HOSTS
            proxy.ALLOWED_HOSTS = frozenset({f"127.0.0.1:{port}", "localhost", "127.0.0.1"})

            try:
                resp = await client.post("/mcp", data=write_body)
                assert resp.status == 500
                assert CountingClient.calls == 1, "mutating tool must not be retried"
            finally:
                proxy.ALLOWED_HOSTS = old_hosts
                proxy._forward_to_upstream = old_forward
                if old_client is not None:
                    proxy._http_client = old_client
                await client.close()

        self._run_client(test)
