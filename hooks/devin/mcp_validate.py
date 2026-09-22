#!/usr/bin/env python3

"""Validate MemPalace MCP tool calls against the server's declared JSON schema.

Invoked by a Devin PreToolUse hook. Reads the pending invocation from stdin,
resolves the target server's configuration the way Devin does, fetches the
server's tool schemas over streamable HTTP, and checks the arguments.

Scope (per MemPalace review policy):
  - Only servers identified as MemPalace are validated; all other MCP
    servers are approved untouched.
  - stdio servers are never spawned by this hook — if the resolved config
    is command-based, the call is approved (fail open).
  - Validation matches the server's actual leniency: we enforce `required`
    parameters, `enum` membership, and `additionalProperties: false` —
    nothing else. The MemPalace server coerces scalar types ("5" -> 5) and
    accepts extra fields where the schema permits, so a stricter local
    check would block calls the server accepts.
  - Anything this hook cannot resolve (no config, unreachable server,
    unexpected payload, non-JSONC file, timeouts) approves the call.

Blocking prints {"decision": "block", ...} and exits 2 — Devin's documented
blocking exit code. All other exits are 0 with {"decision": "approve"}.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CACHE_TTL_SECONDS = 300  # 5 minutes
HTTP_TIMEOUT_SECONDS = 0.45  # whole-request budget; hook must stay under ~500ms
PROTOCOL_VERSION = "2025-03-26"

_MEMPALACE_HINT = re.compile(r"mempalace", re.IGNORECASE)


def _is_mempalace_server(server_name: str, server_config: dict) -> bool:
    """Only validate servers that are clearly MemPalace instances."""
    if _MEMPALACE_HINT.search(server_name or ""):
        return True
    for key in ("url", "command", "serverUrl"):
        if _MEMPALACE_HINT.search(str(server_config.get(key, ""))):
            return True
    for arg in server_config.get("args", []) or []:
        if _MEMPALACE_HINT.search(str(arg)):
            return True
    return False


# ---------------------------------------------------------------------------
# Devin config resolution
# ---------------------------------------------------------------------------


def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments without touching string literals."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _read_config(path: Path) -> dict:
    try:
        return json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except Exception:
        return {}


def _config_candidates() -> list[Path]:
    """Devin's effective precedence: project > user; local > main;
    config.* overrides mcp_config.* within the same level."""
    project_dir = Path(os.environ.get("DEVIN_PROJECT_DIR") or os.getcwd())
    if sys.platform == "win32":
        user_dir = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "devin"
    else:
        user_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "devin"
    candidates: list[Path] = []
    for base in (project_dir / ".devin", user_dir):
        candidates += [
            base / "config.local.json",
            base / "config.json",
            base / "mcp_config.local.json",
            base / "mcp_config.json",
        ]
    return candidates


def load_server_config(server_name: str) -> tuple[dict, Path | None]:
    """Return (server_config, defining_file) honoring Devin precedence."""
    for path in _config_candidates():
        if not path.exists():
            continue
        data = _read_config(path)
        servers = data.get("mcpServers") or data.get("mcp_servers") or {}
        if server_name in servers:
            return servers[server_name], path
    return {}, None


# ---------------------------------------------------------------------------
# Schema cache (keyed by resolved-config fingerprint, not server name)
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))) / "devin"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "devin"
    return base / "mcp_schemas"


def _cache_key(server_name: str, server_config: dict) -> str:
    project = os.environ.get("DEVIN_PROJECT_DIR") or os.getcwd()
    fingerprint = json.dumps(
        {"project": project, "server": server_name, "config": server_config},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:24]


def _load_cached_tools(key: str) -> list | None:
    path = _cache_dir() / f"{key}.json"
    try:
        if time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        return json.loads(path.read_text()).get("tools", [])
    except Exception:
        return None


def _save_cached_tools(key: str, tools: list) -> None:
    try:
        _cache_dir().mkdir(parents=True, exist_ok=True)
        (_cache_dir() / f"{key}.json").write_text(
            json.dumps({"tools": tools, "cached_at": time.time()})
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Streamable-HTTP tools/list (initialize handshake doubles as liveness probe)
# ---------------------------------------------------------------------------


def _http_request(
    url: str, payload: dict, headers: dict, session_id: str | None, protocol: str | None
) -> tuple[dict, str | None, str | None]:
    """POST one JSON-RPC message. Returns (body, session_id, protocol_version)."""
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    req_headers.update(headers or {})
    if session_id:
        req_headers["mcp-session-id"] = session_id
    if protocol:
        req_headers["mcp-protocol-version"] = protocol

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=req_headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        body = resp.read().decode("utf-8", "replace")
        resp_session = resp.headers.get("mcp-session-id")
        content_type = resp.headers.get("Content-Type", "")

    if "text/event-stream" in content_type or body.lstrip().startswith(("event:", "data:")):
        parsed = None
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    parsed = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
        if parsed is None:
            raise RuntimeError("no parseable data line in SSE response")
    else:
        parsed = json.loads(body) if body.strip() else {}

    negotiated = None
    if isinstance(parsed, dict):
        negotiated = (parsed.get("result") or {}).get("protocolVersion")
    return parsed, resp_session or session_id, negotiated


def _http_list_tools(server_config: dict) -> list:
    url = server_config.get("url") or server_config.get("serverUrl") or ""
    if not url.startswith("http"):
        raise RuntimeError("not an HTTP server config")
    headers = server_config.get("headers") or {}

    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "devin-mempalace-validator", "version": "0.4.0"},
        },
    }
    init_resp, session_id, protocol = _http_request(url, init, headers, None, None)
    if init_resp.get("error"):
        raise RuntimeError(f"initialize failed: {init_resp['error']}")

    try:
        _http_request(
            url,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers,
            session_id,
            protocol,
        )
    except Exception:
        pass

    tools_resp, _, _ = _http_request(
        url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers,
        session_id,
        protocol,
    )
    if tools_resp.get("error"):
        raise RuntimeError(f"tools/list failed: {tools_resp['error']}")
    return (tools_resp.get("result") or {}).get("tools", [])


# ---------------------------------------------------------------------------
# Leniency-matched validation
# ---------------------------------------------------------------------------


def validate_arguments(arguments: dict, schema: dict) -> list:
    """Enforce only what the MemPalace server itself rejects:
    missing required params, enum violations, and unknown params when the
    schema sets additionalProperties: false. Scalar coercion ("5" -> 5) is
    the server's job — do not duplicate a stricter contract here."""
    errors = []
    properties = schema.get("properties") or {}

    for field in schema.get("required") or []:
        if field not in arguments:
            errors.append(f"missing required parameter: {field}")

    if schema.get("additionalProperties") is False:
        for key in arguments:
            if key not in properties:
                errors.append(f"unknown parameter: {key}")

    for key, value in arguments.items():
        prop = properties.get(key)
        if prop and "enum" in prop and value not in prop["enum"]:
            errors.append(f"{key} must be one of {prop['enum']}")

    return errors


# ---------------------------------------------------------------------------
# Hook entry
# ---------------------------------------------------------------------------


def _resolve_call(data: dict) -> tuple[str, str, dict] | None:
    """Return (server_name, tool_name, arguments) for both Devin MCP forms:
    the mcp_call_tool wrapper and the direct mcp__<server>__<tool> name."""
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}

    if tool_name == "mcp_call_tool":
        return (
            tool_input.get("server_name", ""),
            tool_input.get("tool_name", ""),
            tool_input.get("arguments") or {},
        )

    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) == 3:
            _, server, tool = parts
            arguments = tool_input.get("arguments")
            if not isinstance(arguments, dict):
                arguments = tool_input if isinstance(tool_input, dict) else {}
            return server, tool, arguments
    return None


def _approve(reason: str = "") -> int:
    out = {"decision": "approve"}
    if reason:
        out["reason"] = reason
    print(json.dumps(out))
    return 0


def _block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}))
    return 2


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return _approve("unparseable hook payload")

    resolved = _resolve_call(data)
    if resolved is None:
        return _approve()
    server_name, requested_tool, arguments = resolved
    if not server_name or not requested_tool:
        return _approve("incomplete MCP call payload")

    server_config, _ = load_server_config(server_name)
    if not server_config:
        return _approve(f"no config for {server_name}")

    if not _is_mempalace_server(server_name, server_config):
        return _approve()

    url = server_config.get("url") or server_config.get("serverUrl") or ""
    if not str(url).startswith("http"):
        # stdio/command configs: never spawn a server from a hook.
        return _approve(f"{server_name} is stdio-configured; not spawning")

    key = _cache_key(server_name, server_config)
    tools = _load_cached_tools(key)
    if tools is None:
        try:
            tools = _http_list_tools(server_config)
            _save_cached_tools(key, tools)
        except Exception as e:
            return _approve(f"could not reach {server_name}: {e}")

    tool_def = next((t for t in tools if t.get("name") == requested_tool), None)
    if not tool_def:
        return _approve(f"{requested_tool} not in {server_name} schema")

    errors = validate_arguments(arguments, tool_def.get("inputSchema") or {})
    if errors:
        return _block(f"invalid arguments for {server_name}/{requested_tool}: " + "; ".join(errors))
    return _approve()


if __name__ == "__main__":
    sys.exit(main())
