"""Small authenticated client for forwarding JSON-RPC to a live Palace hub."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Mapping

logger = logging.getLogger(__name__)

HUB_FORWARD_ENV = "MEMPALACE_HUB_FORWARD"
HUB_PROXY_TIMEOUT_S = 600.0
# A hook-budgeted caller cannot wait the proxy's 600 s, but a palace write
# queues behind whatever the hub is already running, so this is long enough
# that a slow-but-live hub is not mistaken for a dead one.
HUB_TOOL_TIMEOUT_S = 120.0
HUB_HEALTH_TIMEOUT_S = 0.75


def _forwarding_disabled() -> bool:
    return os.environ.get(HUB_FORWARD_ENV, "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def discover_hub(
    palace_path: str | None, *, require_writable: bool = False
) -> tuple[str, dict[str, str]] | None:
    """Return the authenticated endpoint for another live hub, if available.

    ``require_writable`` skips a hub serving the palace read-only, which can
    answer reads but refuses every palace write.
    """

    if _forwarding_disabled() or not palace_path:
        return None
    try:
        from . import server_registry

        info = server_registry.read_live_serverinfo(palace_path)
        if not info or info.get("pid") == os.getpid():
            return None
        if require_writable and info.get("read_only"):
            return None
        base_url = server_registry.client_base_url(info)
        headers = {"Content-Type": "application/json"}
        token = server_registry.load_server_token(palace_path)
    except Exception:
        logger.debug("hub discovery failed", exc_info=True)
        return None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return base_url, headers


def _hub_is_serving(base_url: str, headers: Mapping[str, str]) -> bool:
    """Whether the hub answers its credential-free liveness probe.

    The registry record only proves the recorded pid is alive, not that the
    HTTP listener is up: a wedged or part-torn-down hub leaves both. Probing
    before a write is what separates "no hub, do the work here" from "the hub
    owns this write", so a caller never double-files a payload the hub may
    already have accepted.
    """
    try:
        request = urllib.request.Request(f"{base_url}/healthz", headers=dict(headers))
        with urllib.request.urlopen(request, timeout=HUB_HEALTH_TIMEOUT_S) as response:
            return response.status == 200
    except Exception:
        logger.debug("hub health probe failed", exc_info=True)
        return False


def forward_tool_call(
    palace_path: str | None,
    tool_name: str,
    arguments: Mapping[str, object],
    *,
    timeout: float = HUB_TOOL_TIMEOUT_S,
) -> tuple[bool, dict | None]:
    """Run one MCP tool in the palace's live hub.

    Returns ``(handled, result)`` and the first element is the whole contract:

    - ``(False, None)`` — no hub owns this palace, so the caller still has the
      write to do itself.
    - ``(True, result)`` — the hub answered. ``result`` is the tool's own
      return value, including a ``{"success": False, ...}`` refusal.
    - ``(True, None)`` — the hub accepted the request but did not answer in a
      shape this client can use. The write may have landed, so the caller must
      not repeat it.

    A live hub holds the palace writer lease for its lifetime, so once one is
    answering there is no in-process write left to fall back to: it would be
    refused anyway. That is why a failure after the probe reports
    ``handled=True`` rather than inviting a retry that could file the same
    verbatim content twice. An HTTP 401 is the one failure that is *not* in
    that group — the hub's auth gate rejects it before dispatch — so it reports
    ``handled=False`` and leaves the write to the caller.
    """
    from . import server_registry

    target = discover_hub(palace_path, require_writable=True)
    if target is None or not _hub_is_serving(*target):
        return False, None
    base_url, headers = target

    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": dict(arguments)},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        # Retries a pre-acceptance 401 with the second local credential, so a
        # hub restarted with an explicit token is not lost to a stale one.
        with server_registry.urlopen_with_server_tokens(
            palace_path, f"{base_url}/mcp", data=body, headers=headers, timeout=timeout
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, None
        return True, {"success": False, "error": f"hub refused {tool_name} ({exc.code})"}
    except Exception:
        logger.debug("hub tool call %s did not complete", tool_name, exc_info=True)
        return True, None

    if not raw:
        return True, None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (AttributeError, TypeError, ValueError):
        logger.debug("hub returned an undecodable %s response", tool_name)
        return True, None
    if not isinstance(payload, dict):
        logger.debug("hub returned a non-object %s response", tool_name)
        return True, None
    if payload.get("error"):
        return True, {"success": False, "error": payload["error"].get("message", "unknown error")}
    try:
        result = json.loads(payload["result"]["content"][0]["text"])
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        logger.debug("hub returned an unrecognized %s response", tool_name)
        return True, None
    if not isinstance(result, dict):
        logger.debug("hub returned a non-mapping %s result", tool_name)
        return True, None
    return True, result


def forward_json_rpc(
    base_url: str,
    headers: Mapping[str, str],
    request: Mapping,
    *,
    timeout: float = HUB_PROXY_TIMEOUT_S,
):
    """POST one JSON-RPC request to the hub; return ``None`` for an empty body."""

    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(f"{base_url}/mcp", data=body, headers=dict(headers))
    with urllib.request.urlopen(http_request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))
