"""HTTP transport — FastAPI app mounting the MCP JSON-RPC dispatcher."""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from mempalace.auth import AuthError, AuthMiddleware
from mempalace.concurrency import writer_lock
from mempalace.mcp_server import WRITE_TOOL_NAMES, handle_request

logger = logging.getLogger("mempalace.transport.http")

REQUESTS = Counter(
    "mempalace_http_requests_total",
    "Total HTTP requests",
    ["method", "tool", "identity", "status"],
)
DURATION = Histogram(
    "mempalace_tool_duration_seconds",
    "Tool call duration",
    ["tool"],
)
AUTH_FAILURES = Counter(
    "mempalace_auth_failures_total",
    "Auth failures",
    ["reason"],
)


def build_app(auth: AuthMiddleware | None) -> FastAPI:
    app = FastAPI(title="MemPalace MCP", version="http-transport")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        # Future: verify chromadb client + sqlite + JWKS reachability.
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/mcp/sse")
    async def mcp_sse() -> PlainTextResponse:
        # Placeholder — streaming transport reserved for future implementation.
        return PlainTextResponse(
            ":reserved-for-future-streaming\n\n",
            media_type="text/event-stream",
            status_code=200,
        )

    @app.post("/mcp")
    async def mcp(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        tool_name: str | None = None
        identity = "anonymous"

        if auth is not None:
            try:
                identity = auth.validate(authorization)
            except AuthError as e:
                logger.warning(f"auth_failure reason={e.reason}")
                AUTH_FAILURES.labels(reason=e.reason).inc()
                REQUESTS.labels(
                    method="POST",
                    tool="unknown",
                    identity="anonymous",
                    status="401",
                ).inc()
                return JSONResponse(
                    status_code=401,
                    content={"error": "unauthorized", "reason": e.reason},
                )

        payload: Any = await request.json()
        tool_name = _extract_tool_name(payload)
        tool_label = tool_name or "unknown"

        start = time.perf_counter()
        try:
            if tool_name in WRITE_TOOL_NAMES:
                async with writer_lock:
                    response = handle_request(payload, identity=identity)
            else:
                response = handle_request(payload, identity=identity)
        finally:
            DURATION.labels(tool=tool_label).observe(time.perf_counter() - start)

        REQUESTS.labels(
            method="POST",
            tool=tool_label,
            identity=identity,
            status="200",
        ).inc()
        return JSONResponse(content=response)

    return app


def _extract_tool_name(payload: dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None
    params = payload.get("params") or {}
    if isinstance(params, dict):
        return params.get("name")
    return None
