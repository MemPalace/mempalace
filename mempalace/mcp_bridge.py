"""stdio bridge from MCP clients into the MemPalace local daemon.

This is the production/package complement to the daemon+bridge direction from
#1270. It intentionally reuses mempalace.daemon instead of starting a second
daemon type:

* the existing daemon already owns a per-palace queue, token-authenticated
  loopback HTTP endpoint, and cross-platform process spawning;
* CLI/hook write paths can already submit mine/sync/diary jobs there;
* this bridge adds the missing MCP stdio transport so normal MCP clients also
  route through the same local owner.

The bridge itself does not import ChromaDB or mempalace.mcp_server. It reads
JSON-RPC lines from stdin and forwards them to the daemon's /mcp endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

from . import daemon
from .config import MempalaceConfig

_DISABLE_ENV = "MEMPALACE_MCP_DISABLE_DAEMON"
_REQUEST_TIMEOUT_ENV = "MEMPALACE_MCP_DAEMON_REQUEST_TIMEOUT_SECONDS"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0 * 60.0


class BridgeError(RuntimeError):
    """Raised when the bridge cannot connect to or relay through the daemon."""


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _normalize_backend(value: Any) -> str:
    return str(value or "").strip().lower()


def _effective_read_only(args: argparse.Namespace) -> bool:
    return bool(args.read_only) or _truthy_env("MEMPALACE_MCP_READ_ONLY")


def _request_timeout_seconds() -> float:
    raw = os.environ.get(_REQUEST_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_REQUEST_TIMEOUT_SECONDS

    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_REQUEST_TIMEOUT_SECONDS

    return max(0.1, value)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MemPalace MCP stdio daemon bridge")
    parser.add_argument("--palace", default=None, help="Path to the palace directory")
    parser.add_argument("--backend", default=None, help="Storage backend")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio uses the daemon bridge; non-stdio delegates to raw mcp_server",
    )
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8765, help=argparse.SUPPRESS)
    parser.add_argument("--tls-cert", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--tls-key", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--read-only", action="store_true", help="Run MCP tools read-only")
    parser.add_argument(
        "--no-auto-start",
        action="store_true",
        help="Do not start the MemPalace daemon automatically",
    )
    args, unknown = parser.parse_known_args(argv)
    args.unknown = unknown
    return args


def _exec_raw_stdio_server(argv: list[str]) -> None:  # pragma: no cover
    cmd = [sys.executable, "-m", "mempalace.mcp_server", *argv]
    env = os.environ.copy()
    if os.name == "posix":
        os.execvpe(sys.executable, cmd, env)
    raise SystemExit(subprocess.call(cmd, env=env))


def build_daemon_identity(args: argparse.Namespace) -> dict[str, Any]:
    palace_path = daemon.canonical_palace_path(args.palace)
    collection_name = MempalaceConfig().collection_name
    return {
        "palace_path": palace_path,
        "backend": _normalize_backend(
            args.backend
            or os.environ.get("MEMPALACE_BACKEND_EXPLICIT")
            or os.environ.get("MEMPALACE_BACKEND")
        ),
        "read_only": _effective_read_only(args),
        "collection_name": collection_name,
    }


def validate_daemon_health(health: dict[str, Any], identity: dict[str, Any]) -> None:
    actual_palace = daemon.canonical_palace_path(str(health.get("palace_path") or ""))
    expected_palace = daemon.canonical_palace_path(str(identity["palace_path"]))
    if actual_palace != expected_palace:
        raise BridgeError(
            "MemPalace daemon is serving a different palace: "
            f"expected={expected_palace!r} actual={actual_palace!r}"
        )

    expected_backend = _normalize_backend(identity.get("backend"))
    actual_backend = _normalize_backend(health.get("backend"))

    # A blank expected backend means "use config/env/default", so it is
    # compatible with a daemon that reports None/blank.
    if expected_backend and actual_backend != expected_backend:
        raise BridgeError(
            "MemPalace daemon is serving a different backend: "
            f"expected={expected_backend!r} actual={actual_backend!r}"
        )

    expected_collection = str(identity.get("collection_name") or "").strip()
    actual_collection = str(health.get("collection_name") or "").strip()
    if expected_collection and actual_collection != expected_collection:
        raise BridgeError(
            "MemPalace daemon is serving a different collection: "
            f"expected={expected_collection!r} actual={actual_collection!r}"
        )


def connect_daemon(args: argparse.Namespace, identity: dict[str, Any]) -> daemon.DaemonClient:
    client = daemon.ensure_client(
        str(identity["palace_path"]),
        backend=args.backend,
        auto_start=not bool(args.no_auto_start),
    )
    validate_daemon_health(client.health(timeout=5.0), identity)
    return client


def request_expects_response(request: Any) -> bool:
    """Return False for JSON-RPC notifications."""
    if not isinstance(request, dict):
        return True
    if request.get("method", "").startswith("notifications/"):
        return False
    return request.get("id") is not None


def bridge_error_response(req_id: Any, message: str) -> str:
    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32000,
            "message": "MemPalace MCP bridge error",
            "data": {"message": message},
        },
    }
    return json.dumps(payload, ensure_ascii=False) + "\n"


def forward_request(
    client: daemon.DaemonClient,
    request: Any,
    identity: dict[str, Any],
    *,
    timeout: float,
) -> Any:
    payload = client.request(
        "POST",
        "/mcp",
        {"request": request, "identity": identity},
        timeout=timeout,
    )
    return payload.get("response")


def run_bridge(args: argparse.Namespace) -> int:
    identity = build_daemon_identity(args)
    client = connect_daemon(args, identity)
    timeout = _request_timeout_seconds()

    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue

        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc.msg}"},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            sys.stdout.flush()
            continue

        expects_response = request_expects_response(request)
        req_id = request.get("id") if isinstance(request, dict) else None

        try:
            response = forward_request(client, request, identity, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - convert bridge failures to JSON-RPC
            if expects_response:
                sys.stdout.write(bridge_error_response(req_id, str(exc)))
                sys.stdout.flush()
            else:
                print(f"mempalace-mcp bridge: {exc}", file=sys.stderr)
            continue

        if not expects_response:
            continue

        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    return 0


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    # Preserve the old direct server for HTTP mode and emergency rollback.
    if args.transport != "stdio" or _truthy_env(_DISABLE_ENV):
        _exec_raw_stdio_server(sys.argv[1:] if argv is None else argv)

    try:
        raise SystemExit(run_bridge(args))
    except BridgeError as exc:
        print(f"mempalace-mcp bridge: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
