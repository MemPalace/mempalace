"""Hub discovery + mine forwarding.

A long-lived HTTP hub (``mempalace serve``) holds the MCP writer lease for
its lifetime, which locks the save hooks' spawned ``mempalace mine`` CLI out
of the palace — transcript capture would silently stop on the hub machine.
These tests cover the fix: the HTTP transport records a per-palace
serverinfo file, and ``cmd_mine`` forwards forwardable mines to the live hub
over HTTP instead of colliding with the lease.
"""

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mempalace import cli, server_registry


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MEMPALACE_HUB_FORWARD", raising=False)
    return tmp_path


def _mine_args(source_dir, **overrides):
    defaults = dict(
        dir=str(source_dir),
        palace=None,
        backend=None,
        global_backend=None,
        mode="convos",
        wing=None,
        no_gitignore=False,
        include_ignored=None,
        agent="mempalace",
        limit=0,
        redetect_origin=False,
        dry_run=False,
        daemon=False,
        background=False,
        extract="exchange",
        max_chunks_per_file=None,
        kg_extract=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── server_registry ──────────────────────────────────────────────────


class TestServerRegistry:
    def test_write_then_read_live_roundtrip(self, isolated_home):
        palace = str(isolated_home / "palace")
        path = server_registry.write_serverinfo(
            palace, host="127.0.0.1", port=8765, scheme="http", read_only=False
        )
        if os.name != "nt":
            assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        else:
            assert path.exists()
        info = server_registry.read_live_serverinfo(palace)
        assert info is not None
        assert info["pid"] == os.getpid()
        assert info["port"] == 8765
        assert info["read_only"] is False

    def test_shares_directory_with_server_token(self, isolated_home):
        palace = str(isolated_home / "palace")
        assert (
            server_registry.serverinfo_path(palace).parent == cli._server_token_path(palace).parent
        )

    def test_dead_pid_record_is_ignored(self, isolated_home):
        palace = str(isolated_home / "palace")
        path = server_registry.serverinfo_path(palace)
        path.parent.mkdir(parents=True)
        # PID 2**22+5 is above the default macOS/Linux pid_max — never alive.
        path.write_text(
            json.dumps({"pid": 2**22 + 5, "host": "127.0.0.1", "port": 8765, "scheme": "http"})
        )
        assert server_registry.read_live_serverinfo(palace) is None

    def test_missing_or_corrupt_record_is_ignored(self, isolated_home):
        palace = str(isolated_home / "palace")
        assert server_registry.read_live_serverinfo(palace) is None
        path = server_registry.serverinfo_path(palace)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert server_registry.read_live_serverinfo(palace) is None

    def test_clear_only_removes_own_record(self, isolated_home):
        palace = str(isolated_home / "palace")
        server_registry.write_serverinfo(
            palace, host="127.0.0.1", port=8765, scheme="http", read_only=False
        )
        path = server_registry.serverinfo_path(palace)
        # Another (newer) hub's record must survive our atexit cleanup.
        other = json.loads(path.read_text())
        other["pid"] = os.getpid() + 1
        path.write_text(json.dumps(other))
        server_registry.clear_serverinfo(palace)
        assert path.exists()
        # Our own record is removed.
        own = json.loads(path.read_text())
        own["pid"] = os.getpid()
        path.write_text(json.dumps(own))
        server_registry.clear_serverinfo(palace)
        assert not path.exists()

    def test_wildcard_bind_dialed_via_loopback(self):
        info = {"host": "0.0.0.0", "port": 9999, "scheme": "http"}
        assert server_registry.client_base_url(info) == "http://127.0.0.1:9999"
        info = {"host": "192.168.0.7", "port": 9999, "scheme": "https"}
        assert server_registry.client_base_url(info) == "https://192.168.0.7:9999"


# ── mine forwarding ──────────────────────────────────────────────────


class _FakeHub:
    """Minimal /healthz + /mcp endpoint standing in for `mempalace serve`."""

    def __init__(self, mine_result=None, rpc_error=None):
        self.requests = []
        self.auth_headers = []
        outer = self

        mine_result = mine_result or {"success": True, "mode": "convos", "output": "filed 1"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/healthz":
                    body = b"ok\n"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                outer.requests.append(request)
                outer.auth_headers.append(self.headers.get("Authorization"))
                if rpc_error is not None:
                    payload = {"jsonrpc": "2.0", "id": request.get("id"), "error": rpc_error}
                else:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "result": {"content": [{"type": "text", "text": json.dumps(mine_result)}]},
                    }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_hub(isolated_home):
    hub = _FakeHub()
    yield hub
    hub.stop()


def _register_hub(palace, hub, read_only=False):
    server_registry.write_serverinfo(
        palace, host="127.0.0.1", port=hub.port, scheme="http", read_only=read_only
    )


class TestForwardMineToHub:
    def test_forwards_when_hub_alive(self, isolated_home, tmp_path, fake_hub, capsys):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub)
        args = _mine_args(tmp_path / "convos", wing="myproj")
        handled = cli._forward_mine_to_hub(args, palace)
        assert handled is True
        (request,) = fake_hub.requests
        assert request["params"]["name"] == "mempalace_mine"
        arguments = request["params"]["arguments"]
        assert arguments["mode"] == "convos"
        assert arguments["wing"] == "myproj"
        assert arguments["source"] == str(tmp_path / "convos")
        out = capsys.readouterr().out
        assert "forwarding mine to palace hub" in out
        assert "filed 1" in out

    def test_attaches_bearer_token_when_present(self, isolated_home, tmp_path, fake_hub):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub)
        token_path = server_registry.server_token_path(palace)
        token_path.write_text("sekrit\n")
        cli._forward_mine_to_hub(_mine_args(tmp_path), palace)
        assert fake_hub.auth_headers == ["Bearer sekrit"]

    def test_no_hub_returns_false(self, isolated_home, tmp_path):
        palace = str(isolated_home / "palace")
        assert cli._forward_mine_to_hub(_mine_args(tmp_path), palace) is False

    def test_read_only_hub_not_forwarded(self, isolated_home, tmp_path, fake_hub):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub, read_only=True)
        assert cli._forward_mine_to_hub(_mine_args(tmp_path), palace) is False
        assert fake_hub.requests == []

    def test_env_kill_switch(self, isolated_home, tmp_path, fake_hub, monkeypatch):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub)
        monkeypatch.setenv("MEMPALACE_HUB_FORWARD", "0")
        assert cli._forward_mine_to_hub(_mine_args(tmp_path), palace) is False
        assert fake_hub.requests == []

    def test_unreachable_hub_falls_back(self, isolated_home, tmp_path):
        palace = str(isolated_home / "palace")
        # Bind-then-close: the port is real but nothing listens on it.
        probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        dead_port = probe.server_address[1]
        probe.server_close()
        server_registry.write_serverinfo(
            palace, host="127.0.0.1", port=dead_port, scheme="http", read_only=False
        )
        assert cli._forward_mine_to_hub(_mine_args(tmp_path), palace) is False

    def test_hub_mine_failure_exits_nonzero(self, isolated_home, tmp_path, capsys):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(mine_result={"success": False, "error": "boom"})
        try:
            _register_hub(palace, hub)
            with pytest.raises(SystemExit) as exc:
                cli._forward_mine_to_hub(_mine_args(tmp_path), palace)
            assert exc.value.code == 1
            assert "boom" in capsys.readouterr().err
        finally:
            hub.stop()

    def test_hub_rpc_error_exits_nonzero_without_direct_fallback(
        self, isolated_home, tmp_path, capsys
    ):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(rpc_error={"code": -32003, "message": "read-only server"})
        try:
            _register_hub(palace, hub)
            with pytest.raises(SystemExit):
                cli._forward_mine_to_hub(_mine_args(tmp_path), palace)
            assert "read-only server" in capsys.readouterr().err
        finally:
            hub.stop()


class TestForwardability:
    def test_plain_convo_mine_is_forwardable(self, tmp_path):
        assert cli._mine_args_forwardable(_mine_args(tmp_path), []) is True

    @pytest.mark.parametrize(
        "overrides,include_ignored",
        [
            (dict(kg_extract=True), []),
            (dict(redetect_origin=True), []),
            (dict(no_gitignore=True), []),
            (dict(max_chunks_per_file=10), []),
            (dict(backend="qdrant"), []),
            (dict(), ["*.log"]),
        ],
    )
    def test_hub_incapable_flags_stay_direct(self, tmp_path, overrides, include_ignored):
        args = _mine_args(tmp_path, **overrides)
        assert cli._mine_args_forwardable(args, include_ignored) is False


class TestStdioProxy:
    """`mempalace-mcp` (stdio) must delegate to a live hub instead of opening
    its own Chroma handles — this is what lets stdio-only harnesses (plugins,
    desktop apps) share one writer with zero client-side reconfiguration."""

    @pytest.fixture
    def proxied_palace(self, isolated_home, monkeypatch):
        palace = str(isolated_home / "palace")
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", palace)
        return palace

    def _local_sentinel(self, monkeypatch):
        from mempalace import mcp_server

        calls = []

        def fake_local(request):
            calls.append(request)
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": "local"}

        monkeypatch.setattr(mcp_server, "handle_request", fake_local)
        return calls

    @staticmethod
    def _disown_record(palace):
        """Re-stamp the serverinfo pid so the record looks like another
        process's hub — write_serverinfo records our own pid, which the
        proxy correctly refuses to dial."""
        path = server_registry.serverinfo_path(palace)
        record = json.loads(path.read_text())
        parent_pid = os.getppid()
        assert parent_pid != os.getpid()
        assert server_registry._pid_alive(parent_pid)
        record["pid"] = parent_pid
        path.write_text(json.dumps(record))

    def test_forwards_request_to_live_hub(self, proxied_palace, fake_hub, monkeypatch):
        from mempalace import mcp_server

        local_calls = self._local_sentinel(monkeypatch)
        _register_hub(proxied_palace, fake_hub)
        self._disown_record(proxied_palace)
        request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "mempalace_search", "arguments": {"query": "x"}},
        }
        response = mcp_server._dispatch_stdio_request(request)
        assert local_calls == [], "must not handle locally while a hub is live"
        assert fake_hub.requests == [request]
        assert response["id"] == 7
        assert "result" in response

    def test_no_hub_handles_locally(self, proxied_palace, monkeypatch):
        from mempalace import mcp_server

        local_calls = self._local_sentinel(monkeypatch)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        response = mcp_server._dispatch_stdio_request(request)
        assert local_calls == [request]
        assert response["result"] == "local"

    def test_own_process_record_is_not_a_proxy_target(self, proxied_palace, monkeypatch):
        from mempalace import mcp_server

        local_calls = self._local_sentinel(monkeypatch)
        server_registry.write_serverinfo(
            proxied_palace, host="127.0.0.1", port=1, scheme="http", read_only=False
        )
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        mcp_server._dispatch_stdio_request(request)
        assert local_calls == [request], "the hub itself must never proxy to itself"

    def test_kill_switch_disables_proxying(self, proxied_palace, fake_hub, monkeypatch):
        from mempalace import mcp_server

        local_calls = self._local_sentinel(monkeypatch)
        _register_hub(proxied_palace, fake_hub)
        self._disown_record(proxied_palace)
        monkeypatch.setenv("MEMPALACE_HUB_FORWARD", "0")
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        mcp_server._dispatch_stdio_request(request)
        assert local_calls == [request]
        assert fake_hub.requests == []

    def _register_dead_hub(self, palace):
        probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        dead_port = probe.server_address[1]
        probe.server_close()
        server_registry.write_serverinfo(
            palace, host="127.0.0.1", port=dead_port, scheme="http", read_only=False
        )
        self._disown_record(palace)

    def test_unreachable_hub_read_request_falls_back_locally(self, proxied_palace, monkeypatch):
        from mempalace import mcp_server

        local_calls = self._local_sentinel(monkeypatch)
        self._register_dead_hub(proxied_palace)
        request = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
        response = mcp_server._dispatch_stdio_request(request)
        assert local_calls == [request]
        assert response["result"] == "local"

    def test_unreachable_hub_mutating_request_errors_without_local_replay(
        self, proxied_palace, monkeypatch
    ):
        from mempalace import mcp_server

        local_calls = self._local_sentinel(monkeypatch)
        self._register_dead_hub(proxied_palace)
        request = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "mempalace_add_drawer", "arguments": {"content": "x"}},
        }
        response = mcp_server._dispatch_stdio_request(request)
        assert local_calls == [], "a mutating call must never be replayed locally"
        assert response["error"]["code"] == -32000
        assert "hub" in response["error"]["message"]

    def test_unreachable_hub_notification_returns_none(self, proxied_palace, monkeypatch):
        from mempalace import mcp_server

        self._local_sentinel(monkeypatch)
        self._register_dead_hub(proxied_palace)
        notification = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "mempalace_add_drawer", "arguments": {"content": "x"}},
        }
        assert mcp_server._dispatch_stdio_request(notification) is None


class TestServeHttpRegistersServerinfo:
    def test_serve_http_writes_then_clears_serverinfo(self, isolated_home, monkeypatch):
        from mempalace import mcp_server

        palace = str(isolated_home / "palace")
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", palace)
        observed = {}

        class DummyHTTPd:
            scheme = "http"
            server_address = ("127.0.0.1", 12345)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def serve_forever(self, poll_interval=0.5):
                observed["during"] = server_registry.read_live_serverinfo(palace)
                raise KeyboardInterrupt

        monkeypatch.setattr(mcp_server, "_build_http_server", lambda h, p: DummyHTTPd())
        mcp_server._serve_http("127.0.0.1", 12345)
        assert observed["during"] is not None, "hub must be discoverable while serving"
        assert observed["during"]["port"] == 12345
        assert observed["during"]["read_only"] is mcp_server._READ_ONLY
        # After shutdown the record is gone — no stale forwarding target.
        assert server_registry.read_live_serverinfo(palace) is None


class TestCmdMineIntegration:
    def test_cmd_mine_routes_through_hub(self, isolated_home, tmp_path, fake_hub, monkeypatch):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub)
        convos = tmp_path / "convos"
        convos.mkdir()
        args = _mine_args(convos, palace=palace)
        cli.cmd_mine(args)
        (request,) = fake_hub.requests
        assert request["params"]["arguments"]["source"] == str(convos)
