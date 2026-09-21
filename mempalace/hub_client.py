"""Small authenticated client for forwarding JSON-RPC to a live Palace hub."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Mapping

logger = logging.getLogger(__name__)

HUB_FORWARD_ENV = "MEMPALACE_HUB_FORWARD"
HUB_PROXY_TIMEOUT_S = 600.0


def _forwarding_disabled() -> bool:
    return os.environ.get(HUB_FORWARD_ENV, "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def discover_hub(palace_path: str | None) -> tuple[str, dict[str, str]] | None:
    """Return the authenticated endpoint for another live hub, if available."""

    if _forwarding_disabled() or not palace_path:
        return None
    try:
        from . import server_registry

        info = server_registry.read_live_serverinfo(palace_path)
        if not info or info.get("pid") == os.getpid():
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
