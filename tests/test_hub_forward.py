"""Hub discovery plus CLI and stdio forwarding.

A long-lived HTTP hub (``mempalace serve``) holds the MCP writer lease for
its lifetime, which locks the save hooks' spawned ``mempalace mine`` CLI out
of the palace — transcript capture would silently stop on the hub machine.
These tests cover the fix: the HTTP transport records a per-palace
serverinfo file, and ``cmd_mine`` forwards forwardable mines to the live hub
over HTTP instead of colliding with the lease.

Read-only ``mempalace search`` also forwards to the live hub so agent shell
commands do not cold-load a private copy of a large HNSW index per process.
"""

import argparse
import builtins
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mempalace import cli, mcp_proxy, server_registry
from mempalace.config import MempalaceConfig


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("MEMPALACE_HUB_FORWARD", raising=False)
    monkeypatch.delenv("MEMPALACE_MCP_HTTP_TOKEN", raising=False)
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


def _search_args(**overrides):
    defaults = dict(
        query="needle",
        palace=None,
        backend=None,
        global_backend=None,
        wing=None,
        room=None,
        results=5,
        since=None,
        before=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── server_registry ──────────────────────────────────────────────────


class TestServerRegistry:
    def test_write_then_read_live_roundtrip(self, isolated_home):
        palace = str(isolated_home / "palace")
        path = server_registry.write_serverinfo(
            palace,
            host="127.0.0.1",
            port=8765,
            scheme="http",
            read_only=False,
            capabilities=["search_cli_compatible"],
            search_config_fingerprint="config-digest",
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
        assert info["capabilities"] == ["search_cli_compatible"]
        assert info["search_config_fingerprint"] == "config-digest"

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

    def test_target_palace_token_precedes_process_environment(self, isolated_home, monkeypatch):
        palace = str(isolated_home / "palace-b")
        token_path = server_registry.server_token_path(palace)
        token_path.parent.mkdir(parents=True)
        token_path.write_text("palace-b-token\n")
        monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "palace-a-token")

        assert server_registry.load_server_token(palace) == "palace-b-token"

    def test_process_environment_is_fallback_without_palace_token(self, isolated_home, monkeypatch):
        palace = str(isolated_home / "palace")
        monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "explicit-token")

        assert server_registry.load_server_token(palace) == "explicit-token"


# ── mine forwarding ──────────────────────────────────────────────────


class _FakeHub:
    """Minimal /healthz + /mcp endpoint standing in for `mempalace serve`."""

    def __init__(self, mine_result=None, search_result=None, rpc_error=None, required_token=None):
        self.requests = []
        self.auth_headers = []
        outer = self

        mine_result = mine_result or {"success": True, "mode": "convos", "output": "filed 1"}
        search_result = search_result or {
            "query": "needle",
            "filters": {"wing": None, "room": None},
            "results": [
                {
                    "text": "matching drawer",
                    "wing": "project",
                    "room": "decisions",
                    "source_file": "notes.md",
                    "similarity": 0.91,
                    "bm25_score": 1.25,
                }
            ],
        }

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
                authorization = self.headers.get("Authorization")
                outer.auth_headers.append(authorization)
                if required_token is not None and authorization != f"Bearer {required_token}":
                    self.send_error(401)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                outer.requests.append(request)
                if rpc_error is not None:
                    payload = {"jsonrpc": "2.0", "id": request.get("id"), "error": rpc_error}
                else:
                    tool_name = request.get("params", {}).get("name")
                    tool_result = search_result if tool_name == "mempalace_search" else mine_result
                    payload = {
                        "jsonrpc": "2.0",
                        "id": request.get("id"),
                        "result": {"content": [{"type": "text", "text": json.dumps(tool_result)}]},
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


def _register_hub(palace, hub, read_only=False, capabilities=None, search_config_fingerprint=None):
    if capabilities is None:
        capabilities = ["search_cli_compatible"]
    if search_config_fingerprint is None:
        search_config_fingerprint = MempalaceConfig(palace_path=palace).search_config_fingerprint
    server_registry.write_serverinfo(
        palace,
        host="127.0.0.1",
        port=hub.port,
        scheme="http",
        read_only=read_only,
        capabilities=capabilities,
        search_config_fingerprint=search_config_fingerprint,
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

    def test_uses_target_palace_token_instead_of_unrelated_environment_token(
        self, isolated_home, tmp_path, monkeypatch
    ):
        palace = str(isolated_home / "palace-b")
        hub = _FakeHub(required_token="palace-b-token")
        try:
            _register_hub(palace, hub)
            server_registry.server_token_path(palace).write_text("palace-b-token")
            monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "palace-a-token")

            assert cli._forward_mine_to_hub(_mine_args(tmp_path), palace) is True
            assert hub.auth_headers == ["Bearer palace-b-token"]
            assert len(hub.requests) == 1
        finally:
            hub.stop()

    def test_retries_process_token_after_stale_palace_token_401(
        self, isolated_home, tmp_path, monkeypatch
    ):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(required_token="current-token")
        try:
            _register_hub(palace, hub)
            server_registry.server_token_path(palace).write_text("stale-token")
            monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "current-token")

            assert cli._forward_mine_to_hub(_mine_args(tmp_path), palace) is True
            assert hub.auth_headers == ["Bearer stale-token", "Bearer current-token"]
            assert len(hub.requests) == 1
        finally:
            hub.stop()

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


class TestForwardSearchToHub:
    def test_forwards_to_read_only_hub_and_prints_cli_results(self, isolated_home, capsys):
        palace = str(isolated_home / "palace")
        hub = _FakeHub()
        try:
            _register_hub(palace, hub, read_only=True)
            args = _search_args(
                wing="project",
                room="decisions",
                results=8,
                since="2026-08-01",
                before="2026-09-01",
            )
            assert cli._forward_search_to_hub(args, palace) is True
            (request,) = hub.requests
            assert request["params"]["name"] == "mempalace_search"
            assert request["params"]["arguments"] == {
                "query": "needle",
                "limit": 8,
                "cli_compatible": True,
                "wing": "project",
                "room": "decisions",
                "since": "2026-08-01",
                "before": "2026-09-01",
            }
            out = capsys.readouterr().out
            assert 'Results for: "needle"' in out
            assert "project / decisions" in out
            assert "matching drawer" in out
        finally:
            hub.stop()

    def test_prints_cli_compatible_hub_output_verbatim(self, isolated_home, capsys):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(search_result={"query": "needle", "cli_output": "exact CLI output\n"})
        try:
            _register_hub(palace, hub)
            assert cli._forward_search_to_hub(_search_args(), palace) is True
            assert capsys.readouterr().out == "exact CLI output\n"
        finally:
            hub.stop()

    def test_prints_cli_compatible_hub_stderr_verbatim(self, isolated_home, capsys):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(
            search_result={
                "query": "needle",
                "cli_output": "exact CLI output\n",
                "cli_error_output": "legacy metric warning\n",
            }
        )
        try:
            _register_hub(palace, hub)
            assert cli._forward_search_to_hub(_search_args(), palace) is True
            captured = capsys.readouterr()
            assert captured.out == "exact CLI output\n"
            assert "forwarding search to palace hub" in captured.err
            assert captured.err.endswith("legacy metric warning\n")
        finally:
            hub.stop()

    @pytest.mark.parametrize("local_token", [None, "stale-token"])
    def test_authenticated_hub_without_matching_token_keeps_direct_path(
        self, isolated_home, local_token
    ):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(required_token="actual-token")
        try:
            _register_hub(palace, hub)
            if local_token is not None:
                server_registry.server_token_path(palace).write_text(local_token)

            assert cli._forward_search_to_hub(_search_args(), palace) is False
            assert hub.requests == []
        finally:
            hub.stop()

    def test_authenticated_hub_with_matching_token_forwards(self, isolated_home):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(required_token="actual-token")
        try:
            _register_hub(palace, hub)
            server_registry.server_token_path(palace).write_text("actual-token")

            assert cli._forward_search_to_hub(_search_args(), palace) is True
            assert len(hub.requests) == 1
            assert hub.auth_headers == ["Bearer actual-token"]
        finally:
            hub.stop()

    def test_authenticated_hub_uses_matching_environment_token(self, isolated_home, monkeypatch):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(required_token="environment-token")
        try:
            _register_hub(palace, hub)
            monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "environment-token")

            assert cli._forward_search_to_hub(_search_args(), palace) is True
            assert len(hub.requests) == 1
            assert hub.auth_headers == ["Bearer environment-token"]
        finally:
            hub.stop()

    def test_authenticated_hub_prefers_target_token_over_unrelated_environment(
        self, isolated_home, monkeypatch
    ):
        palace = str(isolated_home / "palace-b")
        hub = _FakeHub(required_token="palace-b-token")
        try:
            _register_hub(palace, hub)
            server_registry.server_token_path(palace).write_text("palace-b-token")
            monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "palace-a-token")

            assert cli._forward_search_to_hub(_search_args(), palace) is True
            assert hub.auth_headers == ["Bearer palace-b-token"]
            assert len(hub.requests) == 1
        finally:
            hub.stop()

    def test_authenticated_hub_retries_process_token_after_stale_palace_token(
        self, isolated_home, monkeypatch
    ):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(required_token="current-token")
        try:
            _register_hub(palace, hub)
            server_registry.server_token_path(palace).write_text("stale-token")
            monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "current-token")

            assert cli._forward_search_to_hub(_search_args(), palace) is True
            assert hub.auth_headers == ["Bearer stale-token", "Bearer current-token"]
            assert len(hub.requests) == 1
        finally:
            hub.stop()

    def test_no_hub_or_kill_switch_keeps_direct_path(self, isolated_home, monkeypatch, fake_hub):
        palace = str(isolated_home / "palace")
        assert cli._forward_search_to_hub(_search_args(), palace) is False
        _register_hub(palace, fake_hub)
        monkeypatch.setenv("MEMPALACE_HUB_FORWARD", "0")
        assert cli._forward_search_to_hub(_search_args(), palace) is False
        assert fake_hub.requests == []

    def test_old_hub_without_cli_compatible_capability_keeps_direct_path(
        self, isolated_home, fake_hub
    ):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub, capabilities=[])
        assert cli._forward_search_to_hub(_search_args(), palace) is False
        assert fake_hub.requests == []

    def test_persisted_config_drift_keeps_direct_path(self, isolated_home, fake_hub):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub)
        config_dir = isolated_home / ".mempalace"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.json").write_text(
            json.dumps({"backend": "qdrant", "palace_path": palace})
        )

        assert cli._forward_search_to_hub(_search_args(), palace) is False
        assert fake_hub.requests == []

    def test_artifact_detected_backend_config_drift_keeps_direct_path(
        self, isolated_home, fake_hub, monkeypatch
    ):
        monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
        monkeypatch.delenv("MEMPALACE_BACKEND_EXPLICIT", raising=False)
        palace_path = isolated_home / "palace"
        palace_path.mkdir()
        (palace_path / "qdrant_backend.json").write_text("{}")
        palace = str(palace_path)
        config_dir = isolated_home / ".mempalace"
        config_dir.mkdir(exist_ok=True)
        config_path = config_dir / "config.json"
        config_path.write_text(json.dumps({"qdrant_timeout": 5}))
        _register_hub(palace, fake_hub)
        config_path.write_text(json.dumps({"qdrant_timeout": 15}))

        assert cli._forward_search_to_hub(_search_args(), palace) is False
        assert fake_hub.requests == []

    def test_hook_setting_write_keeps_hub_path(self, isolated_home, fake_hub):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub)

        MempalaceConfig().set_hook_setting("silent_save", True)

        assert cli._forward_search_to_hub(_search_args(), palace) is True
        assert len(fake_hub.requests) == 1

    def test_invalid_inactive_backend_setting_keeps_hub_path(self, isolated_home, fake_hub):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub)
        config_dir = isolated_home / ".mempalace"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps({"milvus_consistency_level": "invalid"}))

        assert cli._forward_search_to_hub(_search_args(), palace) is True
        assert len(fake_hub.requests) == 1

    @pytest.mark.parametrize(
        ("setting", "value"),
        [
            ("embedding_api_key", "rotated-secret"),
            ("embedding_api_model", "new-api-model"),
            ("embedding_api_url", "https://embeddings.example.test"),
        ],
    )
    def test_dormant_embedding_api_config_keeps_hub_path(
        self, isolated_home, fake_hub, setting, value
    ):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub)
        config_dir = isolated_home / ".mempalace"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.json").write_text(json.dumps({setting: value}))

        assert cli._forward_search_to_hub(_search_args(), palace) is True
        assert len(fake_hub.requests) == 1

    def test_active_embedding_api_config_drift_keeps_direct_path(self, isolated_home, fake_hub):
        palace = str(isolated_home / "palace")
        config_dir = isolated_home / ".mempalace"
        config_dir.mkdir(exist_ok=True)
        config_path = config_dir / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "embedding_model": "openai-compat",
                    "embedding_api_model": "embed-v1",
                    "embedding_api_url": "https://old.example.test",
                }
            )
        )
        _register_hub(palace, fake_hub)
        config_path.write_text(
            json.dumps(
                {
                    "embedding_model": "openai-compat",
                    "embedding_api_model": "embed-v1",
                    "embedding_api_url": "https://new.example.test",
                }
            )
        )

        assert cli._forward_search_to_hub(_search_args(), palace) is False
        assert fake_hub.requests == []

    @pytest.mark.parametrize(
        ("setting", "value"),
        [("embedding_device", "cpu"), ("embedding_threads", 2)],
    )
    def test_dormant_local_embedding_config_keeps_hub_path(
        self, isolated_home, fake_hub, setting, value
    ):
        palace = str(isolated_home / "palace")
        config_dir = isolated_home / ".mempalace"
        config_dir.mkdir(exist_ok=True)
        config_path = config_dir / "config.json"
        config = {
            "embedding_model": "openai-compat",
            "embedding_api_model": "embed-v1",
            "embedding_api_url": "https://embeddings.example.test",
        }
        config_path.write_text(json.dumps(config))
        _register_hub(palace, fake_hub)
        config[setting] = value
        config_path.write_text(json.dumps(config))

        assert cli._forward_search_to_hub(_search_args(), palace) is True
        assert len(fake_hub.requests) == 1

    def test_hub_start_environment_drift_keeps_direct_path(
        self, isolated_home, fake_hub, monkeypatch
    ):
        palace = str(isolated_home / "palace")
        monkeypatch.setenv("MEMPALACE_BACKEND", "qdrant")
        hub_fingerprint = MempalaceConfig(palace_path=palace).search_config_fingerprint
        monkeypatch.delenv("MEMPALACE_BACKEND")
        _register_hub(
            palace,
            fake_hub,
            search_config_fingerprint=hub_fingerprint,
        )

        assert cli._forward_search_to_hub(_search_args(), palace) is False
        assert fake_hub.requests == []

    def test_hub_rpc_error_exits_without_local_fallback(self, isolated_home, capsys):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(rpc_error={"code": -32000, "message": "search failed"})
        try:
            _register_hub(palace, hub)
            with pytest.raises(SystemExit) as exc:
                cli._forward_search_to_hub(_search_args(), palace)
            assert exc.value.code == 1
            assert "search failed" in capsys.readouterr().err
        finally:
            hub.stop()

    def test_cmd_search_does_not_open_local_searcher_when_hub_handles_request(
        self, isolated_home, fake_hub, monkeypatch
    ):
        palace = str(isolated_home / "palace")
        _register_hub(palace, fake_hub)
        args = _search_args(palace=palace)

        real_import = builtins.__import__

        def import_without_local_searcher(name, *args, **kwargs):
            if name == "mempalace.searcher":
                raise AssertionError("forwarded search must not import the local storage stack")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", import_without_local_searcher)
        cli.cmd_search(args)
        assert len(fake_hub.requests) == 1

    def test_explicit_backend_is_not_forwardable(self):
        assert cli._search_args_forwardable(_search_args()) is True
        assert cli._search_args_forwardable(_search_args(backend="sqlite_exact")) is False

    @pytest.mark.parametrize("length", [201, 250, 251])
    def test_query_the_hub_would_sanitize_is_not_forwardable(self, length):
        assert cli._search_args_forwardable(_search_args(query="x" * 200)) is True
        assert cli._search_args_forwardable(_search_args(query="x" * length)) is False

    def test_query_the_hub_would_strip_is_not_forwardable(self):
        assert cli._search_args_forwardable(_search_args(query=" needle ")) is False

    @pytest.mark.parametrize("results", [0, -1, 101, 500])
    def test_result_count_outside_mcp_range_is_not_forwardable(self, results):
        assert cli._search_args_forwardable(_search_args(results=results)) is False

    @pytest.mark.parametrize("results", [1, 100])
    def test_result_count_at_mcp_boundaries_is_forwardable(self, results):
        assert cli._search_args_forwardable(_search_args(results=results)) is True

    @pytest.mark.parametrize(
        "overrides",
        [
            {"wing": "sales/2026"},
            {"room": " release_notes "},
            {"wing": ""},
        ],
    )
    def test_name_filter_the_hub_rejects_or_rewrites_is_not_forwardable(self, overrides):
        assert cli._search_args_forwardable(_search_args(**overrides)) is False

    def test_valid_name_filters_are_forwardable(self):
        assert (
            cli._search_args_forwardable(_search_args(wing="sales_2026", room="release_notes"))
            is True
        )

    @pytest.mark.parametrize(
        "env_name",
        [
            "MEMPALACE_LANG",
            "MEMPAL_LANG",
            "MEMPALACE_BACKEND",
            "MEMPALACE_EMBEDDING_MODEL",
            "MEMPALACE_EMBEDDING_API_URL",
            "MEMPALACE_QDRANT_URL",
        ],
    )
    def test_per_invocation_search_override_is_not_forwardable(self, monkeypatch, env_name):
        for name in cli._SEARCH_OVERRIDE_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(env_name, "en")
        assert cli._search_args_forwardable(_search_args()) is False

    def test_blank_language_override_does_not_disable_forwarding(self, monkeypatch):
        monkeypatch.setenv("MEMPALACE_LANG", "  ")
        monkeypatch.setenv("MEMPAL_LANG", "")
        assert cli._search_args_forwardable(_search_args()) is True


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

    def test_retries_process_token_after_stale_palace_token_401(self, proxied_palace, monkeypatch):
        from mempalace import mcp_server

        hub = _FakeHub(required_token="current-token")
        try:
            local_calls = self._local_sentinel(monkeypatch)
            _register_hub(proxied_palace, hub)
            self._disown_record(proxied_palace)
            server_registry.server_token_path(proxied_palace).write_text("stale-token")
            monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "current-token")
            request = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}

            response = mcp_server._dispatch_stdio_request(request)

            assert local_calls == []
            assert response["id"] == 7
            assert hub.auth_headers == ["Bearer stale-token", "Bearer current-token"]
            assert hub.requests == [request]
        finally:
            hub.stop()

    def test_dynamic_proxy_status_adds_local_client_update_state(self, proxied_palace, monkeypatch):
        from mempalace import mcp_server, mcp_proxy

        hub = _FakeHub(mine_result={"updates": {"server": {"enabled": True, "installed": "3.9.0"}}})
        try:
            self._local_sentinel(monkeypatch)
            _register_hub(proxied_palace, hub)
            self._disown_record(proxied_palace)
            monkeypatch.setattr(
                mcp_proxy,
                "cached_update_status",
                lambda: {"enabled": True, "installed": "3.8.0"},
            )
            monkeypatch.setattr(mcp_proxy, "schedule_update_check", lambda: False)
            request = {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "mempalace_status", "arguments": {}},
            }

            response = mcp_server._dispatch_stdio_request(request)

            payload = json.loads(response["result"]["content"][0]["text"])
            assert payload["updates"] == {
                "server": {"enabled": True, "installed": "3.9.0"},
                "client": {"enabled": True, "installed": "3.8.0"},
            }
        finally:
            hub.stop()

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


class TestThinStdioProxyTokenRetry:
    def test_retries_process_token_after_stale_palace_token_401(self, isolated_home, monkeypatch):
        palace = str(isolated_home / "palace")
        hub = _FakeHub(required_token="current-token")
        try:
            server_registry.server_token_path(palace).parent.mkdir(parents=True, exist_ok=True)
            server_registry.server_token_path(palace).write_text("stale-token")
            monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", "current-token")
            request = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}

            response = mcp_proxy._forward(
                f"http://127.0.0.1:{hub.port}",
                {"Content-Type": "application/json"},
                request,
                palace,
            )

            assert response["id"] == 7
            assert hub.auth_headers == ["Bearer stale-token", "Bearer current-token"]
            assert hub.requests == [request]
        finally:
            hub.stop()


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
