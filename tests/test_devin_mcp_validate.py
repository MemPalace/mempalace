"""Tests for the Devin PreToolUse MCP validation hook."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOOK_PATH = Path(__file__).parent.parent / ".devin" / "hooks" / "mcp_validate.py"


def load_hook_module():
    spec = importlib.util.spec_from_file_location("mcp_validate", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSchemaValidation:
    def test_missing_required_field(self):
        module = load_hook_module()
        schema = {
            "type": "object",
            "properties": {"entity": {"type": "string"}},
            "required": ["entity"],
        }
        errors = module.validate_against_schema({}, schema)
        assert errors == ["missing required field: entity"]

    def test_unknown_field(self):
        module = load_hook_module()
        schema = {
            "type": "object",
            "properties": {"entity": {"type": "string"}},
            "required": ["entity"],
        }
        errors = module.validate_against_schema({
            "entity": "Max",
            "entitty": "Max",
        }, schema)
        assert "unknown field: entitty" in errors

    def test_wrong_type(self):
        module = load_hook_module()
        schema = {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        }
        errors = module.validate_against_schema({"limit": "10"}, schema)
        assert errors == ["field limit should be integer, got str"]

    def test_approve_valid(self):
        module = load_hook_module()
        schema = {
            "type": "object",
            "properties": {"entity": {"type": "string"}},
            "required": ["entity"],
        }
        errors = module.validate_against_schema({"entity": "Max"}, schema)
        assert errors == []


class TestConfigDiscovery:
    def test_loads_user_mcp_config(self, monkeypatch, tmp_path):
        module = load_hook_module()

        home = tmp_path / "home"
        home.mkdir()
        config_dir = home / ".config" / "devin"
        config_dir.mkdir(parents=True)
        (config_dir / "mcp_config.json").write_text(json.dumps({
            "mcpServers": {
                "mempalace": {"url": "http://localhost:8766/mcp"}
            }
        }))

        monkeypatch.setenv("HOME", str(home))
        # Force Path.home() to re-evaluate the env var.
        os.environ["HOME"] = str(home)

        cfg = module.load_server_config("mempalace")
        assert cfg == {"url": "http://localhost:8766/mcp"}

    def test_user_config_overrides_mcp_config(self, monkeypatch, tmp_path):
        module = load_hook_module()

        home = tmp_path / "home"
        home.mkdir()
        config_dir = home / ".config" / "devin"
        config_dir.mkdir(parents=True)
        (config_dir / "mcp_config.json").write_text(json.dumps({
            "mcpServers": {
                "mempalace": {"url": "http://old:8766/mcp"}
            }
        }))
        (config_dir / "config.json").write_text(json.dumps({
            "mcpServers": {
                "mempalace": {"url": "http://new:8766/mcp"}
            }
        }))

        monkeypatch.setenv("HOME", str(home))
        os.environ["HOME"] = str(home)

        cfg = module.load_server_config("mempalace")
        assert cfg == {"url": "http://new:8766/mcp"}


class _McpJsonRpcHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        req = json.loads(body.decode())

        if req.get("method") == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "test", "version": "0.1"},
                },
            }
        elif req.get("method") == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "result": {
                    "tools": [
                        {
                            "name": "test_tool",
                            "description": "A test tool",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"}
                                },
                                "required": ["name"],
                            },
                        }
                    ]
                },
            }
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "error": {"code": -32601, "message": "Unknown method"},
            }

        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        # The reachability probe uses HEAD; 405 still means the server is alive,
        # but returning 200 keeps the probe simple.
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args, **kwargs):
        pass


class TestHttpToolList:
    def test_http_list_tools_and_validation(self):
        module = load_hook_module()

        server = HTTPServer(("127.0.0.1", 0), _McpJsonRpcHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            url = f"http://127.0.0.1:{port}/mcp"
            tools = module._http_list_tools({"url": url})
            assert any(t["name"] == "test_tool" for t in tools)

            tool_def = next(t for t in tools if t["name"] == "test_tool")
            errors = module.validate_against_schema({}, tool_def["inputSchema"])
            assert errors == ["missing required field: name"]
        finally:
            server.shutdown()

    def test_main_blocks_invalid_http_call(self, monkeypatch, tmp_path):
        load_hook_module()

        server = HTTPServer(("127.0.0.1", 0), _McpJsonRpcHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            home = tmp_path / "home"
            home.mkdir()
            config_dir = home / ".config" / "devin"
            config_dir.mkdir(parents=True)
            (config_dir / "mcp_config.json").write_text(json.dumps({
                "mcpServers": {
                    "testserver": {"url": f"http://127.0.0.1:{port}/mcp"}
                }
            }))

            monkeypatch.setenv("HOME", str(home))
            os.environ["HOME"] = str(home)

            # Clear any existing cache so the hook has to talk to the HTTP server.
            cache_dir = home / ".devin" / "cache" / "mcp_schemas"
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "testserver.json").unlink(missing_ok=True)

            stdin = json.dumps({
                "tool_name": "mcp_call_tool",
                "tool_input": {
                    "server_name": "testserver",
                    "tool_name": "test_tool",
                    "arguments": {},
                },
            })
            proc = subprocess.run(
                [sys.executable, str(HOOK_PATH)],
                input=stdin,
                capture_output=True,
                text=True,
                env={**os.environ, "HOME": str(home)},
            )
            assert proc.returncode == 1
            result = json.loads(proc.stdout)
            assert result["decision"] == "block"
            assert "missing required field: name" in result["reason"]
        finally:
            server.shutdown()
