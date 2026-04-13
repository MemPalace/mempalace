"""HTTP transport — FastAPI app mounting the MCP JSON-RPC dispatcher."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from mempalace.auth import AuthError, AuthMiddleware
from mempalace.concurrency import writer_lock
from mempalace.mcp_server import WRITE_TOOL_NAMES, handle_request

logger = logging.getLogger("mempalace.transport.http")


def build_app(auth: AuthMiddleware | None) -> FastAPI:
    app = FastAPI(title="MemPalace MCP", version="http-transport")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        # Future: verify chromadb client + sqlite + JWKS reachability.
        return {"status": "ready"}

    @app.post("/mcp")
    async def mcp(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
        if auth is None:
            identity = "anonymous"
        else:
            try:
                identity = auth.validate(authorization)
            except AuthError as e:
                logger.warning(f"auth_failure reason={e.reason}")
                return JSONResponse(
                    status_code=401,
                    content={"error": "unauthorized", "reason": e.reason},
                )

        payload: Any = await request.json()
        tool_name = _extract_tool_name(payload)

        if tool_name in WRITE_TOOL_NAMES:
            async with writer_lock:
                response = handle_request(payload, identity=identity)
        else:
            response = handle_request(payload, identity=identity)

        return JSONResponse(content=response)

    return app


def _extract_tool_name(payload: dict[str, Any]) -> str | None:
    if not isinstance(payload, dict):
        return None
    params = payload.get("params") or {}
    if isinstance(params, dict):
        return params.get("name")
    return None
