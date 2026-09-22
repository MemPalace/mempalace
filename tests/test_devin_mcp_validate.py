"""Tests for the Devin PreToolUse MemPalace MCP validation hook.

Contract under test (from review of PR #2261):
  - exit 2 + {"decision": "block"} for blocked calls (Devin blocking code)
  - both `mcp_call_tool` wrapper and `mcp__<server>__<tool>` namespaced form
  - only MemPalace servers are validated; everything else approves
  - no HEAD probe (MemPalace's HTTP transport returns 501 to HEAD)
  - no stdio spawning; fail open on anything unresolvable
  - validation matches server leniency: required, enum, and
    additionalProperties:false only — scalar coercion passes
"""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOOK_PATH = Path(__file__).parent.parent / "hooks" / "devin" / "mcp_validate.py"


def load_hook_module():
    spec = importlib.util.spec_from_file_location("mcp_validate", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook(payload: dict, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def isolated_env(tmp_path) -> dict:
    """Env with a scratch HOME and project dir so real Devin configs never leak in."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".config" / "devin").mkdir(parents=True)
    (project / ".devin").mkdir(parents=True)
    return {
        **{k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT")},
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "DEVIN_PROJECT_DIR": str(project),
    }


class TestResolveCall:
    def test_wrapper_form(self):
        module = load_hook_module()
        resolved = module._resolve_call(
            {
                "tool_name": "mcp_call_tool",
                "tool_input": {
                    "server_name": "mempalace",
                    "tool_name": "mempalace_search",
                    "arguments": {"query": "x"},
                },
            }
        )
        assert resolved == ("mempalace", "mempalace_search", {"query": "x"})

    def test_namespaced_form(self):
        module = load_hook_module()
        resolved = module._resolve_call(
            {
                "tool_name": "mcp__mempalace__mempalace_kg_query",
                "tool_input": {"entity": "orkid"},
            }
        )
        assert resolved == ("mempalace", "mempalace_kg_query", {"entity": "orkid"})

    def test_non_mcp_tool(self):
        module = load_hook_module()
        assert module._resolve_call({"tool_name": "exec", "tool_input": {}}) is None


class TestMempalaceDetection:
    def test_by_name(self):
        module = load_hook_module()
        assert module._is_mempalace_server("mempalace", {"url": "http://x/mcp"})

    def test_by_url(self):
        module = load_hook_module()
        assert module._is_mempalace_server("memory", {"url": "http://mempalace.local/mcp"})

    def test_by_command(self):
        module = load_hook_module()
        assert module._is_mempalace_server("mem", {"command": "mempalace-mcp"})

    def test_negative(self):
        module = load_hook_module()
        assert not module._is_mempalace_server("github", {"url": "http://api.github.com/mcp"})


class TestValidation:
    """Validation matches server leniency — only required, enum, and
    additionalProperties:false are enforced."""

    def test_missing_required(self):
        module = load_hook_module()
        schema = {"properties": {"query": {"type": "string"}}, "required": ["query"]}
        assert module.validate_arguments({}, schema) == ["missing required parameter: query"]

    def test_enum_violation(self):
        module = load_hook_module()
        schema = {"properties": {"direction": {"enum": ["in", "out"]}}}
        errors = module.validate_arguments({"direction": "sideways"}, schema)
        assert len(errors) == 1 and "enum" not in errors[0] and "must be one of" in errors[0]

    def test_unknown_param_rejected_only_when_forbidden(self):
        module = load_hook_module()
        strict = {
            "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        }
        errors = module.validate_arguments({"query": "x", "entitty": "y"}, strict)
        assert errors == ["unknown parameter: entitty"]

        permissive = {"properties": {"query": {"type": "string"}}}
        # The MemPalace server accepts extra params like wait_for_previous.
        assert (
            module.validate_arguments({"query": "x", "wait_for_previous": True}, permissive) == []
        )

    def test_scalar_coercion_passes(self):
        module = load_hook_module()
        schema = {"properties": {"limit": {"type": "integer"}}}
        # The server coerces "5" -> 5; the hook must not block it.
        assert module.validate_arguments({"limit": "5"}, schema) == []


class _McpJsonRpcHandler(BaseHTTPRequestHandler):
    """Streamable-HTTP fake. Deliberately has NO do_HEAD — MemPalace's real
    transport returns 501 for HEAD, which must not break validation."""

    session_seen = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length).decode())
        self.session_seen.append(self.headers.get("mcp-session-id"))

        if req.get("method") == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mempalace", "version": "0.1"},
                },
            }
            session = "test-session-1"
        elif req.get("method") == "notifications/initialized":
            resp = None
            session = self.headers.get("mcp-session-id")
        elif req.get("method") == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": {
                    "tools": [
                        {
                            "name": "mempalace_search",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                    "limit": {"type": "integer"},
                                },
                                "required": ["query"],
                            },
                        }
                    ]
                },
            }
            session = self.headers.get("mcp-session-id")
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "error": {"code": -32601, "message": "?"},
            }
            session = self.headers.get("mcp-session-id")

        if resp is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            if session:
                self.send_header("mcp-session-id", session)
            self.end_headers()
            return

        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if session:
            self.send_header("mcp-session-id", session)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args, **kwargs):
        pass


class TestHttpDiscovery:
    def test_tools_list_without_head_probe(self):
        module = load_hook_module()
        server = HTTPServer(("127.0.0.1", 0), _McpJsonRpcHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            tools = module._http_list_tools(
                {"url": f"http://127.0.0.1:{server.server_address[1]}/mcp"}
            )
            assert [t["name"] for t in tools] == ["mempalace_search"]
            # Session id negotiated on initialize was forwarded on tools/list.
            assert "test-session-1" in self._sessions()
        finally:
            server.shutdown()

    def _sessions(self):
        return _McpJsonRpcHandler.session_seen

    def test_unreachable_approves(self, tmp_path):
        env = isolated_env(tmp_path)
        cfg_dir = Path(env["DEVIN_PROJECT_DIR"]) / ".devin"
        (cfg_dir / "config.json").write_text(
            json.dumps({"mcpServers": {"mempalace": {"url": "http://127.0.0.1:1/mcp"}}})
        )
        proc = run_hook(
            {
                "tool_name": "mcp_call_tool",
                "tool_input": {
                    "server_name": "mempalace",
                    "tool_name": "mempalace_search",
                    "arguments": {},
                },
            },
            env,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["decision"] == "approve"


class TestEndToEnd:
    def _write_config(self, env, port):
        cfg_dir = Path(env["DEVIN_PROJECT_DIR"]) / ".devin"
        (cfg_dir / "config.json").write_text(
            json.dumps({"mcpServers": {"mempalace": {"url": f"http://127.0.0.1:{port}/mcp"}}})
        )

    def test_blocks_missing_required_exit_2(self, tmp_path):
        env = isolated_env(tmp_path)
        server = HTTPServer(("127.0.0.1", 0), _McpJsonRpcHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self._write_config(env, server.server_address[1])
            proc = run_hook(
                {
                    "tool_name": "mcp_call_tool",
                    "tool_input": {
                        "server_name": "mempalace",
                        "tool_name": "mempalace_search",
                        "arguments": {},
                    },
                },
                env,
            )
            assert proc.returncode == 2
            result = json.loads(proc.stdout)
            assert result["decision"] == "block"
            assert "missing required parameter: query" in result["reason"]
        finally:
            server.shutdown()

    def test_namespaced_call_validates(self, tmp_path):
        env = isolated_env(tmp_path)
        server = HTTPServer(("127.0.0.1", 0), _McpJsonRpcHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self._write_config(env, server.server_address[1])
            proc = run_hook(
                {
                    "tool_name": "mcp__mempalace__mempalace_search",
                    "tool_input": {},
                },
                env,
            )
            assert proc.returncode == 2
            assert json.loads(proc.stdout)["decision"] == "block"
        finally:
            server.shutdown()

    def test_coerced_scalar_approved(self, tmp_path):
        env = isolated_env(tmp_path)
        server = HTTPServer(("127.0.0.1", 0), _McpJsonRpcHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            self._write_config(env, server.server_address[1])
            proc = run_hook(
                {
                    "tool_name": "mcp_call_tool",
                    "tool_input": {
                        "server_name": "mempalace",
                        "tool_name": "mempalace_search",
                        "arguments": {"query": "orkid", "limit": "5"},
                    },
                },
                env,
            )
            assert proc.returncode == 0
            assert json.loads(proc.stdout)["decision"] == "approve"
        finally:
            server.shutdown()

    def test_non_mempalace_server_approved_unvalidated(self, tmp_path):
        env = isolated_env(tmp_path)
        cfg_dir = Path(env["DEVIN_PROJECT_DIR"]) / ".devin"
        # Even with an unreachable URL and missing args, a non-mempalace
        # server is outside this hook's scope.
        (cfg_dir / "config.json").write_text(
            json.dumps({"mcpServers": {"github": {"url": "http://127.0.0.1:1/mcp"}}})
        )
        proc = run_hook(
            {
                "tool_name": "mcp__github__create_issue",
                "tool_input": {},
            },
            env,
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["decision"] == "approve"

    def test_stdio_config_never_spawns(self, tmp_path):
        env = isolated_env(tmp_path)
        cfg_dir = Path(env["DEVIN_PROJECT_DIR"]) / ".devin"
        (cfg_dir / "config.json").write_text(
            json.dumps({"mcpServers": {"mempalace": {"command": "mempalace-mcp"}}})
        )
        proc = run_hook(
            {
                "tool_name": "mcp__mempalace__mempalace_search",
                "tool_input": {},
            },
            env,
        )
        assert proc.returncode == 0
        result = json.loads(proc.stdout)
        assert result["decision"] == "approve"
        assert "stdio" in result.get("reason", "")


class TestConfigResolution:
    def test_local_overrides_main(self, tmp_path):
        module = load_hook_module()
        env = isolated_env(tmp_path)
        cfg_dir = Path(env["DEVIN_PROJECT_DIR"]) / ".devin"
        (cfg_dir / "config.json").write_text(
            json.dumps({"mcpServers": {"mempalace": {"url": "http://main:1/mcp"}}})
        )
        (cfg_dir / "config.local.json").write_text(
            json.dumps({"mcpServers": {"mempalace": {"url": "http://local:1/mcp"}}})
        )
        os.environ["DEVIN_PROJECT_DIR"] = env["DEVIN_PROJECT_DIR"]
        try:
            cfg, _ = module.load_server_config("mempalace")
            assert cfg == {"url": "http://local:1/mcp"}
        finally:
            del os.environ["DEVIN_PROJECT_DIR"]

    def test_jsonc_and_mcp_config(self, tmp_path):
        module = load_hook_module()
        env = isolated_env(tmp_path)
        cfg_dir = Path(env["DEVIN_PROJECT_DIR"]) / ".devin"
        (cfg_dir / "mcp_config.json").write_text(
            '{\n  // mempalace endpoint\n  "mcpServers": {"mempalace": {"url": "http://jsonc:1/mcp"}}\n}'
        )
        os.environ["DEVIN_PROJECT_DIR"] = env["DEVIN_PROJECT_DIR"]
        try:
            cfg, _ = module.load_server_config("mempalace")
            assert cfg == {"url": "http://jsonc:1/mcp"}
        finally:
            del os.environ["DEVIN_PROJECT_DIR"]

    def test_cache_key_depends_on_config(self):
        module = load_hook_module()
        k1 = module._cache_key("mempalace", {"url": "http://a/mcp"})
        k2 = module._cache_key("mempalace", {"url": "http://b/mcp"})
        assert k1 != k2
