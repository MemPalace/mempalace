#!/usr/bin/env python3
"""
MCP Streamable-HTTP proxy for MemPalace's HTTP transport.

MemPalace's `--transport http` mode speaks plain JSON over HTTP
(BaseHTTPRequestHandler, Connection: close, no SSE). MCP clients
that expect the streamable-http transport protocol (POST /mcp with
Mcp-Session-Id, GET /mcp with text/event-stream, DELETE /mcp)
cannot connect directly. This proxy bridges the gap.

Features:
  - Persistent httpx.AsyncClient with connection pooling
  - Circuit breaker (3 consecutive failures -> open, 30s half-open probe)
  - Retry with exponential backoff for transient failures
  - Bearer token forwarding (MEMPALACE_MCP_HTTP_TOKEN)
  - /health endpoint with upstream liveness check
  - /metrics endpoint (Prometheus-style)
  - Session management with TTL cleanup
  - Structured logging with request IDs
  - Graceful shutdown on SIGTERM/SIGINT

Requirements:
  pip install aiohttp httpx

Usage:
  python mempalace_mcp_proxy.py

Environment variables (all have sensible defaults):
  UPSTREAM_URL       MemPalace HTTP endpoint (default: http://127.0.0.1:8765/mcp)
  UPSTREAM_TOKEN     Bearer token for upstream auth (optional, matches MEMPALACE_MCP_HTTP_TOKEN)
  HOST               Bind host (default: 127.0.0.1)
  PORT               Bind port (default: 8766)
  UPSTREAM_TIMEOUT   Per-request timeout in seconds (default: 120)
  MAX_RETRIES        Max retry attempts for transient failures (default: 2)
  SESSION_TTL        Session expiry in seconds (default: 1800)
  LOG_LEVEL          DEBUG/INFO/WARNING/ERROR (default: INFO)
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from collections import defaultdict

try:
    import httpx
    from aiohttp import web
except ImportError:
    print("Missing dependencies. Install with: pip install aiohttp httpx", file=sys.stderr)
    sys.exit(1)

# ── Configuration ──────────────────────────────────────────────────────────────

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "http://127.0.0.1:8765/mcp")
UPSTREAM_TOKEN = os.environ.get("UPSTREAM_TOKEN", os.environ.get("MEMPALACE_MCP_HTTP_TOKEN", ""))
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8766"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "120"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))
SESSION_TTL = float(os.environ.get("SESSION_TTL", "1800"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Inbound request security (Streamable-HTTP / DNS rebinding protection).
# ALLOWED_ORIGINS: comma-separated list of permitted Origin headers.
# ALLOWED_HOSTS: comma-separated list of permitted Host headers (no Origin).
# INBOUND_TOKEN: optional bearer token required for all /mcp endpoints.
ALLOWED_ORIGINS = frozenset(
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if o.strip()
)
ALLOWED_HOSTS = frozenset(
    h.strip().lower()
    for h in os.environ.get(
        "ALLOWED_HOSTS",
        f"127.0.0.1,127.0.0.1:{PORT},localhost,localhost:{PORT},{HOST}",
    ).split(",")
    if h.strip()
)
INBOUND_TOKEN = os.environ.get("INBOUND_TOKEN", "")

# ── Idempotency / retry safety ─────────────────────────────────────────────────
#
# The proxy must not retry non-idempotent MCP tool calls: if a mutation
# (add/update/delete/diary/...) commits upstream and the response is lost,
# replaying the request can execute the mutation twice. Only the JSON-RPC
# methods and tools listed below are proven read-only and therefore safe to
# retry on transient 5xx / timeout / connect failures.

_READ_ONLY_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "tools/list",
    }
)

_READ_ONLY_TOOLS = frozenset(
    {
        "mempalace_status",
        "mempalace_list_wings",
        "mempalace_list_rooms",
        "mempalace_get_taxonomy",
        "mempalace_get_aaak_spec",
        "mempalace_kg_query",
        "mempalace_kg_timeline",
        "mempalace_kg_stats",
        "mempalace_traverse",
        "mempalace_find_tunnels",
        "mempalace_graph_stats",
        "mempalace_list_tunnels",
        "mempalace_list_hallways",
        "mempalace_follow_tunnels",
        "mempalace_search",
        "mempalace_check_duplicate",
        "mempalace_get_drawer",
        "mempalace_list_drawers",
        "mempalace_diary_read",
    }
)

# Tools whose retry safety depends on the request payload.  These are NOT in
# _READ_ONLY_TOOLS and are evaluated by _is_retry_safe on a per-call basis.
_CONDITIONALLY_READ_ONLY_TOOLS = frozenset(
    {
        "mempalace_hook_settings",
    }
)

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mempalace-proxy")

# ── State ──────────────────────────────────────────────────────────────────────

sessions: dict[str, dict] = {}

_circuit = {
    "failures": 0,
    "state": "closed",  # closed / open / half-open
    "opened_at": 0.0,
}
CIRCUIT_THRESHOLD = 3
CIRCUIT_RESET_TIME = 30.0

_metrics = defaultdict(int)
_metrics_start = time.time()

# ── HTTP Client (persistent, pooled) ───────────────────────────────────────────

_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=UPSTREAM_TIMEOUT,
                write=10.0,
                pool=5.0,
            ),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=60.0,
            ),
            http2=False,
        )
        log.info("HTTP client created (pool: 20 max, 10 keepalive)")
    return _http_client


async def close_http_client():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        log.info("HTTP client closed")


# ── Circuit Breaker ────────────────────────────────────────────────────────────


def circuit_check() -> bool:
    if _circuit["state"] == "closed":
        return True
    if _circuit["state"] == "open":
        elapsed = time.time() - _circuit["opened_at"]
        if elapsed > CIRCUIT_RESET_TIME:
            _circuit["state"] = "half-open"
            log.warning(f"Circuit breaker: open -> half-open (after {elapsed:.1f}s)")
            return True
        return False
    return True


def circuit_record_success():
    if _circuit["state"] != "closed":
        log.info("Circuit breaker: half-open -> closed (recovered)")
    _circuit["failures"] = 0
    _circuit["state"] = "closed"


def circuit_record_failure():
    _circuit["failures"] += 1
    if _circuit["failures"] >= CIRCUIT_THRESHOLD:
        _circuit["state"] = "open"
        _circuit["opened_at"] = time.time()
        log.error(f"Circuit breaker: -> open ({_circuit['failures']} consecutive failures)")
    elif _circuit["state"] == "half-open":
        _circuit["state"] = "open"
        _circuit["opened_at"] = time.time()
        log.error("Circuit breaker: half-open -> open (probe failed)")


# ── Session Management ─────────────────────────────────────────────────────────


def cleanup_expired_sessions():
    now = time.time()
    expired = [sid for sid, s in sessions.items() if now - s["last_used"] > SESSION_TTL]
    for sid in expired:
        del sessions[sid]
    if expired:
        log.info(f"Cleaned up {len(expired)} expired sessions")


# ── Request Forwarding ─────────────────────────────────────────────────────────


def _is_valid_jsonrpc_request(body: bytes) -> tuple[bool, str | None, int]:
    """Validate the JSON-RPC 2.0 envelope and return (ok, error_message, error_code).

    The proxy relies on a well-formed envelope to make retry-safety and
    routing decisions.  Malformed requests are rejected before forwarding.

    Error codes follow JSON-RPC 2.0:
      - -32700 Parse error: body is not valid JSON.
      - -32600 Invalid Request: valid JSON but not a valid JSON-RPC request object.
    """
    try:
        req = json.loads(body)
    except Exception as exc:
        return False, f"Invalid JSON: {exc}", -32700

    if not isinstance(req, dict):
        return False, "JSON-RPC request must be an object", -32600

    if req.get("jsonrpc") != "2.0":
        return False, "Invalid or missing jsonrpc version (expected '2.0')", -32600

    method = req.get("method")
    if not isinstance(method, str) or not method:
        return False, "method must be a non-empty string", -32600

    # id may be absent (notification), but if present must be a valid type.
    if "id" in req and not (
        isinstance(req["id"], (str, int, float)) or req["id"] is None
    ):
        return False, "id must be a string, number, or null", -32600

    params = req.get("params")
    if params is not None and not isinstance(params, (dict, list)):
        return False, "params must be an object or array", -32600

    if method == "tools/call":
        if not isinstance(params, dict):
            return False, "tools/call requires a params object", -32600
        tool = params.get("name")
        if not isinstance(tool, str) or not tool:
            return False, "tools/call params must contain a non-empty 'name' string", -32600
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return False, "tools/call 'arguments' must be an object", -32600

    return True, None, 0


def _is_retry_safe(body: bytes) -> bool:
    """Return True if a JSON-RPC request is idempotent and safe to retry.

    The upstream MCP server handles mutations (add/update/delete/...) that may
    commit even when the response is lost. Replaying those requests can execute
    the mutation twice, so we only retry methods and tools that are proven
    read-only.  Only well-formed JSON-RPC requests can be considered safe.
    """
    ok, _, _ = _is_valid_jsonrpc_request(body)
    if not ok:
        return False

    req = json.loads(body)
    method = req.get("method", "")
    if method in _READ_ONLY_METHODS:
        return True

    if method == "tools/call":
        params = req.get("params") or {}
        tool = params.get("name", "")
        if tool in _READ_ONLY_TOOLS:
            return True

        # Request-sensitive retry safety: `mempalace_hook_settings` is read-only
        # only when called with no arguments (a status query).  Any arguments
        # mean the caller is mutating configuration and must not be replayed.
        if tool in _CONDITIONALLY_READ_ONLY_TOOLS:
            arguments = params.get("arguments") or {}
            return not arguments

    # Notifications and other methods are not retried by default.
    return False


async def _forward_to_upstream(
    body: bytes, headers: dict, req_id: any, max_attempts: int | None = None
) -> tuple[dict | None, int, str | None]:
    """Forward request to upstream with retry + circuit breaker."""
    if not circuit_check():
        return None, 503, "Circuit breaker open — upstream unavailable"

    attempts = max_attempts if max_attempts is not None else MAX_RETRIES + 1
    client = await get_http_client()
    last_error = None

    for attempt in range(attempts):
        try:
            resp = await client.post(UPSTREAM_URL, content=body, headers=headers)

            if resp.status_code >= 500:
                last_error = f"Upstream returned {resp.status_code}"
                circuit_record_failure()

                if _circuit["state"] == "open" or attempt >= attempts - 1:
                    _metrics["requests_failed"] += 1

                    if resp.content:
                        try:
                            payload = resp.json()
                            return payload, resp.status_code, None
                        except Exception:
                            pass

                    return (
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {
                                "code": -32000,
                                "message": f"Upstream returned {resp.status_code}",
                                "data": resp.text[:500],
                            },
                        },
                        resp.status_code,
                        last_error,
                    )

                log.warning(
                    f"Upstream {resp.status_code} (attempt {attempt + 1}/{attempts}), retrying..."
                )
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            # 2xx / 3xx / 4xx are not retried. Record success for 2xx/3xx.
            if 200 <= resp.status_code < 400:
                circuit_record_success()
                _metrics["requests_success"] += 1

            # 202 Accepted / 204 No Content — valid for JSON-RPC notifications
            # (notifications have no response body). Return None to signal the
            # handler to return an empty success response.
            if resp.status_code in (202, 204) or not resp.content:
                return None, resp.status_code, None

            try:
                payload = resp.json()
                return payload, resp.status_code, None
            except Exception:
                return (
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32000,
                            "message": f"Upstream returned non-JSON (status {resp.status_code})",
                            "data": resp.text[:500],
                        },
                    },
                    resp.status_code,
                    "non-json response",
                )

        except httpx.ConnectError as exc:
            last_error = str(exc)
            _metrics["connect_errors"] += 1
            if attempt < attempts - 1:
                log.warning(f"Connect error (attempt {attempt + 1}/{attempts}): {exc}")
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            circuit_record_failure()
            return None, 502, f"Upstream connect error: {exc}"

        except httpx.ReadTimeout:
            last_error = f"Read timeout after {UPSTREAM_TIMEOUT}s"
            _metrics["timeout_errors"] += 1
            if attempt < attempts - 1:
                log.warning(f"Read timeout (attempt {attempt + 1}/{attempts})")
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            circuit_record_failure()
            return None, 504, last_error

        except httpx.PoolTimeout as exc:
            last_error = str(exc)
            _metrics["pool_errors"] += 1
            if attempt < attempts - 1:
                await asyncio.sleep(0.2 * (attempt + 1))
                continue
            return None, 503, f"Connection pool timeout: {exc}"

        except Exception as exc:
            last_error = str(exc)
            _metrics["unexpected_errors"] += 1
            log.error(f"Unexpected upstream error: {type(exc).__name__}: {exc}")
            if attempt < attempts - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            circuit_record_failure()
            return None, 502, f"Unexpected error: {exc}"

    circuit_record_failure()
    return None, 502, last_error or "All retries exhausted"


def _is_inbound_request_allowed(request: web.Request) -> tuple[bool, int, str]:
    """Validate Origin/Host and inbound bearer token for MCP endpoints.

    - If the request includes an ``Origin`` header, it must match the request
      ``Host`` or one of ``ALLOWED_ORIGINS``.
    - If no ``Origin`` is present (non-browser clients), the ``Host`` header
      must be in ``ALLOWED_HOSTS``.
    - If ``INBOUND_TOKEN`` is configured, the request must carry a matching
      ``Authorization: Bearer <token>`` header.
    """
    if INBOUND_TOKEN:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != INBOUND_TOKEN:
            return False, 401, "Unauthorized — invalid or missing inbound token"

    origin = request.headers.get("Origin")
    host = request.headers.get("Host", "").lower()

    if origin:
        # Browser / cross-origin client: Origin must match Host or the allowlist.
        if origin in ALLOWED_ORIGINS:
            return True, 200, ""
        if origin == f"http://{host}" or origin == f"https://{host}":
            return True, 200, ""
        return False, 403, f"Forbidden — Origin {origin!r} not allowed"

    # Non-browser client with no Origin header: restrict by Host.
    if host not in ALLOWED_HOSTS:
        return False, 403, f"Forbidden — Host {host!r} not allowed"

    return True, 200, ""


# ── Handlers ───────────────────────────────────────────────────────────────────


async def handle_mcp_post(request: web.Request) -> web.StreamResponse:
    """Handle POST /mcp — forward JSON-RPC to upstream and wrap response."""
    allowed, status, msg = _is_inbound_request_allowed(request)
    if not allowed:
        return web.json_response(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": msg}},
            status=status,
        )

    req_start = time.time()
    request_id = uuid.uuid4().hex[:8]
    _metrics["requests_total"] += 1

    if _metrics["requests_total"] % 50 == 0:
        cleanup_expired_sessions()

    body = await request.read()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    # Forward bearer token if configured
    if UPSTREAM_TOKEN:
        headers["Authorization"] = f"Bearer {UPSTREAM_TOKEN}"

    session_id = request.headers.get("Mcp-Session-Id")
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    is_valid, validation_error, validation_code = _is_valid_jsonrpc_request(body)
    if not is_valid:
        log.warning(f"[{request_id}] Rejected malformed JSON-RPC: {validation_error}")
        _metrics["requests_failed"] += 1
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": validation_code, "message": validation_error},
            },
            status=400,
        )

    req_json = json.loads(body)
    is_initialize = req_json.get("method") == "initialize"
    req_id = req_json.get("id")
    method = req_json.get("method", "?")

    retry_safe = _is_retry_safe(body)
    max_attempts = MAX_RETRIES + 1 if retry_safe else 1
    if not retry_safe:
        log.info(f"[{request_id}] Non-idempotent request; no retries method={method}")

    log.debug(f"[{request_id}] POST /mcp method={method} retry_safe={retry_safe}")

    payload, status, error = await _forward_to_upstream(
        body, headers, req_id, max_attempts=max_attempts
    )

    if payload is None:
        # 202/204 with no payload = successful notification (no response body)
        if status in (202, 204):
            elapsed = time.time() - req_start
            log.info(
                f"[{request_id}] OK method={method} status={status} "
                f"elapsed={elapsed:.3f}s (notification accepted)"
            )
            notif_headers = {}
            if session_id and session_id in sessions:
                notif_headers["Mcp-Session-Id"] = session_id
            accept = request.headers.get("Accept", "")
            if "text/event-stream" in accept:
                return web.Response(
                    status=status,
                    content_type="text/event-stream",
                    headers=notif_headers,
                )
            return web.Response(status=status, headers=notif_headers)

        # Actual failure
        _metrics["requests_failed"] += 1
        elapsed = time.time() - req_start
        log.error(
            f"[{request_id}] FAILED method={method} status={status} "
            f"error={error} elapsed={elapsed:.3f}s"
        )
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32000,
                    "message": error or "Unknown error",
                },
            },
            status=status,
        )

    resp_headers = {}
    if is_initialize:
        new_session_id = uuid.uuid4().hex
        sessions[new_session_id] = {
            "created": time.time(),
            "last_used": time.time(),
            "queue": asyncio.Queue(),
        }
        resp_headers["Mcp-Session-Id"] = new_session_id
        log.info(f"[{request_id}] New session: {new_session_id}")

    if session_id and session_id in sessions:
        sessions[session_id]["last_used"] = time.time()

    elapsed = time.time() - req_start
    _metrics["request_duration_sum"] += elapsed

    if isinstance(payload, dict) and "error" in payload:
        _metrics["mcp_errors"] += 1
        log.warning(
            f"[{request_id}] MCP error method={method} "
            f"code={payload['error'].get('code')} "
            f"msg={payload['error'].get('message', '')[:100]}"
        )
    else:
        log.info(f"[{request_id}] OK method={method} status={status} elapsed={elapsed:.3f}s")

    accept = request.headers.get("Accept", "")
    if "text/event-stream" in accept:
        data = json.dumps(payload, ensure_ascii=False)
        sse_body = f"event: message\ndata: {data}\n\n"
        return web.Response(
            body=sse_body,
            status=status,
            content_type="text/event-stream",
            headers=resp_headers,
        )
    else:
        return web.json_response(payload, status=status, headers=resp_headers)


async def handle_mcp_get(request: web.Request) -> web.StreamResponse:
    """Handle GET /mcp — SSE keep-alive stream."""
    allowed, status, msg = _is_inbound_request_allowed(request)
    if not allowed:
        return web.json_response(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": msg}},
            status=status,
        )

    session_id = request.headers.get("Mcp-Session-Id")
    if not session_id or session_id not in sessions:
        return web.Response(status=400, text="Invalid or missing Mcp-Session-Id")

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await resp.prepare(request)

    try:
        while True:
            await asyncio.sleep(15)
            await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass

    return resp


async def handle_mcp_delete(request: web.Request) -> web.Response:
    """Handle DELETE /mcp — terminate a session."""
    allowed, status, msg = _is_inbound_request_allowed(request)
    if not allowed:
        return web.json_response(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": msg}},
            status=status,
        )

    session_id = request.headers.get("Mcp-Session-Id")
    if session_id and session_id in sessions:
        del sessions[session_id]
        log.info(f"Session terminated: {session_id}")
        return web.Response(status=200, text="session terminated")
    return web.Response(status=404, text="session not found")


async def handle_health(request: web.Request) -> web.Response:
    """Health check — tests both proxy and upstream connectivity."""
    upstream_ok = False
    upstream_latency = 0.0
    try:
        client = await get_http_client()
        start = time.time()
        headers = {"Content-Type": "application/json"}
        if UPSTREAM_TOKEN:
            headers["Authorization"] = f"Bearer {UPSTREAM_TOKEN}"
        resp = await client.post(
            UPSTREAM_URL,
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
            headers=headers,
            timeout=httpx.Timeout(10.0),
        )
        upstream_latency = time.time() - start
        if resp.status_code == 200:
            data = resp.json()
            upstream_ok = "result" in data
    except Exception:
        upstream_ok = False
        upstream_latency = -1

    status = "ok" if upstream_ok else "degraded"
    return web.json_response(
        {
            "status": status,
            "upstream": UPSTREAM_URL,
            "upstream_ok": upstream_ok,
            "upstream_latency_ms": round(upstream_latency * 1000, 1),
            "circuit_state": _circuit["state"],
            "circuit_failures": _circuit["failures"],
            "active_sessions": len(sessions),
            "uptime_seconds": round(time.time() - _metrics_start, 1),
        },
        status=200 if upstream_ok else 503,
    )


async def handle_metrics(request: web.Request) -> web.Response:
    """Prometheus-style metrics endpoint."""
    uptime = time.time() - _metrics_start
    lines = [
        "# HELP mempalace_proxy_requests_total Total requests processed",
        "# TYPE mempalace_proxy_requests_total counter",
        f"mempalace_proxy_requests_total {_metrics['requests_total']}",
        "# HELP mempalace_proxy_requests_success Total successful requests",
        "# TYPE mempalace_proxy_requests_success counter",
        f"mempalace_proxy_requests_success {_metrics['requests_success']}",
        "# HELP mempalace_proxy_requests_failed Total failed requests",
        "# TYPE mempalace_proxy_requests_failed counter",
        f"mempalace_proxy_requests_failed {_metrics['requests_failed']}",
        "# HELP mempalace_proxy_mcp_errors Total MCP-level errors",
        "# TYPE mempalace_proxy_mcp_errors counter",
        f"mempalace_proxy_mcp_errors {_metrics['mcp_errors']}",
        "# HELP mempalace_proxy_connect_errors Total connection errors",
        "# TYPE mempalace_proxy_connect_errors counter",
        f"mempalace_proxy_connect_errors {_metrics['connect_errors']}",
        "# HELP mempalace_proxy_timeout_errors Total timeout errors",
        "# TYPE mempalace_proxy_timeout_errors counter",
        f"mempalace_proxy_timeout_errors {_metrics['timeout_errors']}",
        "# HELP mempalace_proxy_active_sessions Active MCP sessions",
        "# TYPE mempalace_proxy_active_sessions gauge",
        f"mempalace_proxy_active_sessions {len(sessions)}",
        "# HELP mempalace_proxy_uptime_seconds Proxy uptime",
        "# TYPE mempalace_proxy_uptime_seconds gauge",
        f"mempalace_proxy_uptime_seconds {uptime:.1f}",
        "# HELP mempalace_proxy_circuit_state Circuit breaker state (0=closed, 1=half-open, 2=open)",
        "# TYPE mempalace_proxy_circuit_state gauge",
        f'mempalace_proxy_circuit_state {{state="{_circuit["state"]}"}} {0 if _circuit["state"] == "closed" else 1 if _circuit["state"] == "half-open" else 2}',
    ]
    return web.Response(
        text="\n".join(lines) + "\n",
        content_type="text/plain; version=0.0.4",
    )


# ── Lifecycle ──────────────────────────────────────────────────────────────────


async def on_startup(app):
    log.info(f"MCP proxy starting: {HOST}:{PORT} -> {UPSTREAM_URL}")
    log.info(
        f"Config: timeout={UPSTREAM_TIMEOUT}s retries={MAX_RETRIES} session_ttl={SESSION_TTL}s"
    )
    if UPSTREAM_TOKEN:
        log.info("Bearer token configured for upstream auth")
    await get_http_client()


async def on_cleanup(app):
    log.info("MCP proxy shutting down...")
    await close_http_client()


def main():
    app = web.Application()
    app.router.add_post("/mcp", handle_mcp_post)
    app.router.add_get("/mcp", handle_mcp_get)
    app.router.add_delete("/mcp", handle_mcp_delete)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/metrics", handle_metrics)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(_shutdown(app, s)))
        except NotImplementedError:
            pass  # Windows

    log.info(f"MCP streamable-http proxy: {HOST}:{PORT} -> {UPSTREAM_URL}")
    web.run_app(app, host=HOST, port=PORT, print=None)


async def _shutdown(app, sig):
    log.info(f"Received signal {sig.name}, shutting down...")
    await close_http_client()


if __name__ == "__main__":
    main()
