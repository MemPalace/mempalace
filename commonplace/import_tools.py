"""Consented, verbatim Grok fact imports through the shared import batch path."""

from __future__ import annotations

import hashlib
from io import StringIO

from . import import_grant
from .extensions import ALL_PROFILES, LocalTool, ToolError, register

MAX_JSONL_BYTES = 1024 * 1024
MAX_LINES = 50


def facts_import(ctx, args):
    if not isinstance(args, dict) or args.keys() - {"jsonl"}:
        raise ToolError("no: invalid arguments")
    jsonl = args.get("jsonl")
    if not isinstance(jsonl, str):
        raise ToolError("no: jsonl must be a string")
    if len(jsonl) > MAX_JSONL_BYTES:
        raise ToolError(f"no: jsonl must be at most {MAX_JSONL_BYTES} bytes")
    try:
        encoded = jsonl.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolError("no: jsonl must be valid UTF-8") from None
    if len(encoded) > MAX_JSONL_BYTES:
        raise ToolError(f"no: jsonl must be at most {MAX_JSONL_BYTES} bytes")

    # Match the HTTP client's identities, including CRLF and a final terminator.
    lines = [raw for raw in StringIO(jsonl, newline="") if raw.strip()]
    if not 1 <= len(lines) <= MAX_LINES:
        raise ToolError(f"no: send between 1 and {MAX_LINES} nonblank JSONL lines")
    items = [
        {
            "thread_id": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "transcript": raw,
            "format": "auto",
        }
        for raw in lines
    ]
    handoff = getattr(ctx.gateway, "handoff", None)
    admit = getattr(handoff, "grant_admit", None)
    import_batch = getattr(handoff, "import_batch", None)
    if not callable(admit) or not callable(import_batch):
        raise ToolError("no: import unavailable")
    digest = import_grant.request_digest("mcp:facts_import", {"jsonl": jsonl})
    wait = admit(ctx.principal, digest)
    if wait > 0:
        raise ToolError(f"no: import not due; retry in {wait} seconds")
    return import_batch(ctx.principal, "grok-bot", items, path="grant")


register(
    LocalTool(
        name="facts_import",
        description="Import 1–50 Grok memory facts as verbatim JSONL into your private workspace.",
        input_schema={
            "type": "object",
            "properties": {"jsonl": {"type": "string", "maxLength": MAX_JSONL_BYTES}},
            "required": ["jsonl"],
            "additionalProperties": False,
        },
        handler=facts_import,
        scope="memory:import",
        kind="write",
        profiles=ALL_PROFILES,
        available=lambda p, g: import_grant.grant_path(g.db, p),
    )
)
