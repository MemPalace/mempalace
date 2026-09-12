"""The thin stdio front end (mempalace.mcp_proxy).

``mempalace-mcp`` is spawned once per agent session and, whenever a hub is
running, does nothing but forward JSON-RPC over HTTP. Importing the full
server to do that costs ~77 MB (chromadb alone is ~61 MB), so a 50-agent
fleet paid ~3.9 GB to hold proxies that never touch storage. These tests
guard the two properties that make the thin path worth having: it must stay
light, and losing the hub must still leave a working -- and visibly degraded
-- session rather than a broken one.
"""

import json
import subprocess
import sys
import urllib.error

import pytest

from mempalace import mcp_proxy


class TestInvocationRouting:
    @pytest.mark.parametrize(
        "argv",
        [[], ["--transport", "stdio"], ["--transport=stdio"], ["--palace", "/tmp/p"]],
    )
    def test_plain_stdio_invocations_can_be_proxied(self, argv):
        assert mcp_proxy._is_plain_stdio_invocation(argv) is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["--transport", "http"],
            ["--transport=http"],
            ["--transport"],
            ["--host", "127.0.0.1"],
            ["--port", "8765"],
            ["--read-only"],
            ["--some-future-flag"],
        ],
    )
    def test_non_stdio_or_unknown_invocations_go_to_the_full_server(self, argv):
        """Unknown flags must not be silently dropped by the thin path.

        Guessing wrong this way only costs the old startup weight; guessing
        the other way would run a server the operator did not ask for.
        """
        assert mcp_proxy._is_plain_stdio_invocation(argv) is False

    def test_explicit_palace_flag_wins_over_config(self):
        assert mcp_proxy._palace_path(["--palace", "/tmp/explicit"]) == "/tmp/explicit"
        assert mcp_proxy._palace_path(["--palace=/tmp/eq"]) == "/tmp/eq"


class TestDegradedAnnotation:
    def _tool_response(self):
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": '{"results": []}'}]},
        }

    def test_notice_is_prepended_to_tool_content(self):
        """The agent only ever sees result.content; a log line is not a warning."""
        out = mcp_proxy._annotate_degraded(self._tool_response())
        blocks = out["result"]["content"]
        assert len(blocks) == 2
        assert "WITHOUT its shared hub" in blocks[0]["text"]
        # The real payload must survive untouched, and stay parseable.
        assert json.loads(blocks[1]["text"]) == {"results": []}

    @pytest.mark.parametrize(
        "response",
        [
            None,
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "x"}},
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
            "not-a-dict",
        ],
    )
    def test_shapes_without_tool_content_are_left_alone(self, response):
        assert mcp_proxy._annotate_degraded(response) == response


class _FakeServer:
    """Stand-in for the lazily-imported mcp_server."""

    def __init__(self, mutating=False):
        self.calls = []
        self._mutating = mutating

    def _request_is_mutating(self, request):
        return self._mutating

    def handle_request(self, request):
        self.calls.append(request)
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {"content": [{"type": "text", "text": "{}"}]},
        }


class _LoadedLocal:
    def __init__(self, server):
        self.server = server
        self.load_count = 0

    def load(self):
        self.load_count += 1
        return self.server


class TestRouting:
    _REQUEST = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "x"}}

    def test_live_hub_is_used_and_the_server_is_never_loaded(self, monkeypatch):
        """The point of the module: a proxied session pays nothing for storage."""
        forwarded = {"jsonrpc": "2.0", "id": 7, "result": {"content": []}}
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: ("http://hub", {}))
        monkeypatch.setattr(mcp_proxy, "_forward", lambda *a: forwarded)
        local = _LoadedLocal(_FakeServer())

        assert mcp_proxy._handle(dict(self._REQUEST), "/p", local) is forwarded
        assert local.load_count == 0

    def test_proxied_status_distinguishes_hub_and_local_update_state(self, monkeypatch):
        request = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "mempalace_status", "arguments": {}},
        }
        hub_payload = {
            "total_drawers": 10,
            "updates": {"server": {"enabled": True, "installed": "3.9.0"}},
        }
        forwarded = {
            "jsonrpc": "2.0",
            "id": 8,
            "result": {"content": [{"type": "text", "text": json.dumps(hub_payload)}]},
        }
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: ("http://hub", {}))
        monkeypatch.setattr(mcp_proxy, "_forward", lambda *a: forwarded)
        monkeypatch.setattr(
            mcp_proxy,
            "cached_update_status",
            lambda: {"enabled": True, "installed": "3.8.0"},
            raising=False,
        )
        monkeypatch.setattr(mcp_proxy, "schedule_update_check", lambda: False, raising=False)
        local = _LoadedLocal(_FakeServer())

        out = mcp_proxy._handle(request, "/p", local)

        payload = json.loads(out["result"]["content"][0]["text"])
        assert payload["updates"] == {
            "server": {"enabled": True, "installed": "3.9.0"},
            "client": {"enabled": True, "installed": "3.8.0"},
        }
        assert local.load_count == 0

    def test_no_hub_falls_back_locally_and_warns(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: None)
        server = _FakeServer()
        local = _LoadedLocal(server)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", local)
        assert server.calls, "request was not served locally"
        assert "WITHOUT its shared hub" in out["result"]["content"][0]["text"]

    def test_unreachable_hub_falls_back_for_a_read(self, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: ("http://hub", {}))

        def boom(*a):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(mcp_proxy, "_forward", boom)
        server = _FakeServer(mutating=False)
        local = _LoadedLocal(server)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", local)
        assert server.calls
        assert "WITHOUT its shared hub" in out["result"]["content"][0]["text"]

    def test_mutating_call_that_failed_mid_flight_is_not_replayed(self, monkeypatch):
        """The hub may still be executing it — a local retry could double-write."""
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: ("http://hub", {}))

        def boom(*a):
            raise urllib.error.URLError("connection reset")

        monkeypatch.setattr(mcp_proxy, "_forward", boom)
        server = _FakeServer(mutating=True)
        local = _LoadedLocal(server)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", local)
        assert server.calls == [], "a mutating call was replayed locally"
        assert out["error"]["code"] == -32000

    def test_hub_http_error_is_not_replayed_even_for_a_read(self, monkeypatch):
        """An HTTP status means the hub received it; re-running it here is wrong."""
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: ("http://hub", {}))

        def boom(*a):
            raise urllib.error.HTTPError("http://hub/mcp", 500, "boom", {}, None)

        monkeypatch.setattr(mcp_proxy, "_forward", boom)
        server = _FakeServer(mutating=False)
        local = _LoadedLocal(server)

        out = mcp_proxy._handle(dict(self._REQUEST), "/p", local)
        assert server.calls == []
        assert out["error"]["code"] == -32000

    def test_hub_forward_kill_switch_disables_proxying(self, monkeypatch):
        monkeypatch.setenv(mcp_proxy._HUB_FORWARD_ENV, "0")
        assert mcp_proxy._hub_target("/p") is None


def test_importing_the_proxy_does_not_import_the_storage_stack():
    """The whole reason this module exists.

    A regression here is invisible in behaviour and only shows up as memory
    across a fleet, so it is asserted directly. Runs in a subprocess because
    the test session has already imported everything.
    """
    code = (
        "import sys; import mempalace.mcp_proxy; "
        "print(','.join(m for m in ('chromadb','numpy','mempalace.mcp_server') "
        "if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"heavy modules imported by the proxy: {out.stdout.strip()}"
