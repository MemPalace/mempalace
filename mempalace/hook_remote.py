"""Send save-hook writes to a remote MemPalace server over HTTP.

The save hooks normally write to the palace on this machine. When a remote
server is configured they call its ``/mcp`` endpoint instead, and never touch
the local palace: a write that fails stays failed, because a local fallback is
exactly the drift a central server exists to prevent.

Kept import-light, like the rest of the hook foreground. Nothing here puts the
token in a message, a ``repr`` or an exception: every failure is reported by
HTTP status or error class.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from . import _net
from ._env import env_float
from .hub_client import tool_result

REMOTE_URL_ENV = "MEMPALACE_HOOK_REMOTE_URL"
REMOTE_TOKEN_ENV = "MEMPALACE_HOOK_REMOTE_TOKEN"
REMOTE_TIMEOUT_ENV = "MEMPALACE_HOOK_REMOTE_TIMEOUT"
REMOTE_TIMEOUT_DEFAULT_S = 30.0

_MAX_REASON_CHARS = 200


class RemoteConfigError(ValueError):
    """The remote settings cannot be used. The message never holds a secret."""


class RemoteCallFailed(Exception):
    """A remote write did not succeed. The message never holds a secret."""


@dataclass(frozen=True)
class RemoteTarget:
    """One validated remote server."""

    url: str
    token: str = field(default="", repr=False)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_remote_target(url: str | None, token: str | None) -> RemoteTarget | None:
    """Validate the configured URL and token.

    Returns None when no URL is configured. Raises ``RemoteConfigError`` when
    one is configured but cannot be used, so the caller blocks the write and
    does not fall back to the local palace.

    The bearer token must not travel in clear text, so plain ``http`` is
    accepted for a loopback host only.
    """
    url = (url or "").strip()
    token = (token or "").strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        raise RemoteConfigError("the hook remote URL is malformed") from None
    if parts.scheme not in ("http", "https") or not host:
        raise RemoteConfigError("the hook remote URL must be an http(s) URL with a host")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise RemoteConfigError(
            "the hook remote URL must not hold credentials, a query or a fragment; "
            f"set the token with {REMOTE_TOKEN_ENV}"
        )
    loopback = _is_loopback(host)
    if parts.scheme == "http" and not loopback:
        raise RemoteConfigError(
            "the hook remote URL must use https, because plain http would send "
            "the bearer token unencrypted (http is accepted on loopback only)"
        )
    if not token and not loopback:
        raise RemoteConfigError(f"a remote server needs a bearer token; set {REMOTE_TOKEN_ENV}")
    if any(char.isspace() or not char.isprintable() for char in token):
        raise RemoteConfigError("the hook remote token holds whitespace or control characters")
    path = parts.path.rstrip("/") or "/mcp"
    return RemoteTarget(url=urlunsplit((parts.scheme, parts.netloc, path, "", "")), token=token)


def remote_timeout() -> float:
    """Seconds to wait for the server; a non-positive setting uses the default."""
    value = env_float(REMOTE_TIMEOUT_ENV, REMOTE_TIMEOUT_DEFAULT_S)
    return value if value > 0 else REMOTE_TIMEOUT_DEFAULT_S


def _short(text: object) -> str:
    return str(text)[:_MAX_REASON_CHARS]


def call_tool(target: RemoteTarget, name: str, arguments: Mapping[str, object]) -> dict:
    """Run one MCP tool on the server and return its successful result.

    Raises ``RemoteCallFailed`` on any failure. The request is sent once: after
    a timeout the write may have landed, so repeating it here could file the
    same entry twice.
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments)},
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if target.token:
        headers["Authorization"] = f"Bearer {target.token}"
    request = urllib.request.Request(target.url, data=body, headers=headers, method="POST")
    try:
        # ``_net.urlopen`` follows no redirect, so the token reaches only this URL.
        with _net.urlopen(request, timeout=remote_timeout()) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RemoteCallFailed(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        raise RemoteCallFailed(type(exc).__name__) from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise RemoteCallFailed("unreadable response") from None
    if not isinstance(payload, dict):
        raise RemoteCallFailed("unreadable response")
    error = payload.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else None
        raise RemoteCallFailed(_short(message or "server error"))
    try:
        result = tool_result(payload)
    except (KeyError, IndexError, TypeError, ValueError):
        raise RemoteCallFailed("unreadable response") from None
    if not isinstance(result, dict):
        raise RemoteCallFailed("unreadable response")
    if not result.get("success"):
        raise RemoteCallFailed(_short(result.get("error") or "the tool reported failure"))
    return result
