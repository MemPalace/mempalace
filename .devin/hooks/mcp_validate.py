#!/usr/bin/env python3
"""Validate mcp_call_tool input against the server's declared JSON schema.

This script is invoked by a Devin PreToolUse hook. It reads the pending
mcp_call_tool invocation from stdin, loads the target MCP server's tool
schemas, and verifies that the provided arguments match the tool's input
schema.

Optimizations for performance / health:
  - Tool schemas are cached on disk with a TTL so we do not spawn a fresh
    server process for every single MCP call.
  - HTTP-based servers are probed first; if reachable we skip the stdio spawn
    entirely and validate from cache.
  - stdio servers get a spawn timeout so a slow/failed server cannot block
    every tool call.

If the arguments are invalid, it prints a JSON decision with a block reason
and exits non-zero. If valid or validation cannot be performed, it prints
{"decision": "approve"}.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CACHE_DIR = Path.home() / ".devin" / "cache" / "mcp_schemas"
CACHE_TTL_SECONDS = 300  # 5 minutes
SPAWN_TIMEOUT_SECONDS = 5
HTTP_PROBE_TTL_SECONDS = 30  # short TTL so a server restart is noticed quickly


def _http_reachability_cache_path(server_name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{server_name}.reachable"


def _http_server_is_reachable_cached(server_name: str, server_config: dict) -> bool:
    """Check reachability with a short-lived file cache to avoid probing every call."""
    path = _http_reachability_cache_path(server_name)
    if path.exists():
        try:
            mtime = path.stat().st_mtime
            if time.time() - mtime <= HTTP_PROBE_TTL_SECONDS:
                return True
        except Exception:
            pass
    reachable = _http_server_is_alive(server_config)
    try:
        if reachable:
            path.write_text(str(time.time()))
        else:
            path.unlink(missing_ok=True)
    except Exception:
        pass
    return reachable


def load_server_config(server_name: str) -> dict:
    """Find the MCP server configuration in Devin config files.

    Devin historically splits server configs into a dedicated
    ``mcp_config.json`` (supplementary) and the main ``config.json``
    (overrides). Project-level configs live in ``.devin/``. Check both files
    at each level so validation works for HTTP servers defined in
    ``~/.config/devin/mcp_config.json`` and not only in ``config.json``.
    """
    candidates = [
        # Project-level; main config overrides MCP-specific config.
        Path(".devin/config.json"),
        Path(".devin/config.local.json"),
        Path(".devin/mcp_config.json"),
        Path(".devin/mcp_config.local.json"),
        # User-level; same precedence.
        Path.home() / ".config/devin/config.json",
        Path.home() / ".config/devin/config.local.json",
        Path.home() / ".config/devin/mcp_config.json",
        Path.home() / ".config/devin/mcp_config.local.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
            servers = data.get("mcpServers", {})
            if server_name in servers:
                return servers[server_name]
        except Exception:
            continue
    return {}


def _cache_path(server_name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{server_name}.json"


def _load_cached_tools(server_name: str) -> list | None:
    path = _cache_path(server_name)
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
        if time.time() - mtime > CACHE_TTL_SECONDS:
            return None
        data = json.loads(path.read_text())
        return data.get("tools", [])
    except Exception:
        return None


def _save_cached_tools(server_name: str, tools: list) -> None:
    path = _cache_path(server_name)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tools": tools, "cached_at": time.time()}))
    except Exception:
        pass


def _http_server_is_alive(server_config: dict) -> bool:
    """Return True if an HTTP/SSE MCP endpoint appears to be reachable."""
    url = server_config.get("url", "")
    if not url or not url.startswith("http"):
        return False
    try:
        # We only need to know if the TCP endpoint is listening.  A HEAD or
        # GET on the SSE endpoint is enough; the server may return 405 for
        # HEAD but that still means it is alive.
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status < 500
    except urllib.request.HTTPError as e:
        # 4xx means the server is alive even if HEAD is not allowed.
        return e.code < 500
    except Exception:
        return False


def _http_request(url: str, payload: dict, headers: dict | None = None) -> dict:
    """POST a JSON-RPC request to an HTTP/SSE MCP endpoint and parse the response."""
    data = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.request.HTTPError as e:
        body = e.read().decode("utf-8")

    content_type = resp.headers.get("Content-Type", "") if "resp" in locals() else ""
    if "text/event-stream" in content_type or body.startswith("event:") or body.startswith("data:"):
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                try:
                    return json.loads(data)
                except json.JSONDecodeError:
                    continue
        raise RuntimeError("No parseable data: line in SSE response")

    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Non-JSON HTTP response: {body[:200]}") from e


def _http_list_tools(server_config: dict) -> list:
    """Call tools/list on an HTTP/SSE MCP server and return the tool definitions."""
    url = server_config.get("url", "")
    headers = server_config.get("headers", {})

    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "devin-mcp-validator", "version": "0.3.0"},
        },
    }
    init_resp = _http_request(url, init_req, headers)
    if "error" in init_resp:
        raise RuntimeError(f"MCP init failed: {init_resp.get('error')}")

    # Send initialized notification (fire-and-forget; no response expected).
    try:
        _http_request(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
    except Exception:
        pass

    tools_resp = _http_request(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers)
    if "error" in tools_resp:
        raise RuntimeError(f"tools/list failed: {tools_resp.get('error')}")

    result = tools_resp.get("result", {})
    return result.get("tools", [])


def list_tools(server_config: dict) -> list:
    """Call tools/list on an MCP server and return the tool definitions.

    For HTTP/SSE servers this currently returns an empty list because we do not
    speak the streaming protocol from a subprocess.  The caller should treat an
    empty list as "unable to validate; allow the call".
    """
    command = server_config.get("command")
    args = server_config.get("args", [])
    env = os.environ.copy()
    env.update(server_config.get("env", {}))

    if not command:
        raise RuntimeError("MCP server config missing command")

    proc = subprocess.Popen(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    try:
        # MCP initialize handshake
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "devin-mcp-validator", "version": "0.2.0"},
            },
        }
        proc.stdin.write(json.dumps(init_req) + "\n")
        proc.stdin.flush()

        # Read initialize response with timeout
        init_line = proc.stdout.readline()
        if not init_line:
            raise RuntimeError("MCP server closed stdout before initialize response")
        init_resp = json.loads(init_line)
        if "error" in init_resp:
            raise RuntimeError(f"MCP init failed: {init_resp.get('error')}")

        # Send initialized notification
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        # Request tools/list
        tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        proc.stdin.write(json.dumps(tools_req) + "\n")
        proc.stdin.flush()

        tools_line = proc.stdout.readline()
        if not tools_line:
            raise RuntimeError("MCP server closed stdout before tools/list response")
        tools_resp = json.loads(tools_line)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=SPAWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    if "error" in tools_resp:
        raise RuntimeError(f"tools/list failed: {tools_resp.get('error')}")

    result = tools_resp.get("result", {})
    return result.get("tools", [])


def validate_against_schema(arguments: dict, schema: dict) -> list:
    """Basic JSON Schema validation. Returns a list of error strings."""
    errors = []
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    # Check required fields
    for field in required:
        if field not in arguments:
            errors.append(f"missing required field: {field}")

    # Check known fields
    for key in arguments:
        if key not in properties:
            errors.append(f"unknown field: {key}")
        else:
            prop = properties[key]
            value = arguments[key]
            expected_type = prop.get("type")
            if expected_type and not check_type(value, expected_type):
                errors.append(f"field {key} should be {expected_type}, got {type(value).__name__}")

    return errors


def check_type(value, expected_type: str) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    return True


def main():
    data = json.load(sys.stdin)
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if tool_name != "mcp_call_tool":
        print(json.dumps({"decision": "approve"}))
        return 0

    server_name = tool_input.get("server_name", "")
    requested_tool = tool_input.get("tool_name", "")
    arguments = tool_input.get("arguments", {})

    if not server_name or not requested_tool:
        print(json.dumps({"decision": "block", "reason": "mcp_call_tool missing server_name or tool_name"}))
        return 1

    server_config = load_server_config(server_name)
    if not server_config:
        print(json.dumps({"decision": "approve", "reason": f"no config found for server {server_name}; skipping validation"}))
        return 0

    is_http = bool(server_config.get("url", "").startswith("http"))

    # Fast path: try the on-disk schema cache first.
    tools = _load_cached_tools(server_name)
    if tools is None:
        if is_http:
            # HTTP/SSE servers are already running; fetch the schema directly
            # rather than spawning a subprocess. If the server is unreachable,
            # skip validation and let the call fail with a real connectivity error
            # rather than masking it with validation.
            if not _http_server_is_reachable_cached(server_name, server_config):
                print(json.dumps({"decision": "approve", "reason": f"{server_name} not reachable; skipping validation"}))
                return 0
            try:
                tools = _http_list_tools(server_config)
                _save_cached_tools(server_name, tools)
            except Exception as e:
                print(json.dumps({"decision": "approve", "reason": f"could not list tools for HTTP {server_name}: {e}; allowing call"}))
                return 0
        else:
            # stdio servers: spawn once, fetch tools/list, cache, and shut down.
            try:
                tools = list_tools(server_config)
                _save_cached_tools(server_name, tools)
            except Exception as e:
                print(json.dumps({"decision": "approve", "reason": f"could not list tools for {server_name}: {e}; allowing call"}))
                return 0

    tool_def = next((t for t in tools if t.get("name") == requested_tool), None)
    if not tool_def:
        print(json.dumps({"decision": "approve", "reason": f"tool {requested_tool} not found on server {server_name}; allowing call"}))
        return 0

    schema = tool_def.get("inputSchema", {})
    errors = validate_against_schema(arguments, schema)
    if errors:
        msg = f"Invalid arguments for {server_name}/{requested_tool}: " + "; ".join(errors)
        print(json.dumps({"decision": "block", "reason": msg}))
        return 1

    print(json.dumps({"decision": "approve"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
