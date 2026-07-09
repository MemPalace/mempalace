"""Single-writer MCP daemon for MemPalace.

Issue #1963 tracks the architectural problem: many stdio MCP processes can
open the same local ChromaDB PersistentClient. ChromaDB's local persistence
model is safe only when one process owns the client.

This module is deliberately transport-only. The MCP protocol, tool schemas,
validation, read-only mode, SQLite integrity gates, and tool implementation
remain in mempalace.mcp_server. The daemon imports handle_request() once and
serves every stdio bridge over a private Unix domain socket.

Concurrency contract:
* initialize / ping / notifications / tools/list stay lock-free.
* every tools/call request is serialized through one process-wide lock.
  That matches the daemon + bridge proposal in #1270 and keeps both read
  calls that may lazily open Chroma and mutating calls inside the same owner.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Callable

STATE_ROOT_ENV = "MEMPALACE_MCP_DAEMON_STATE_ROOT"
SOCKET_ENV = "MEMPALACE_MCP_SOCKET"
MAX_LINE_BYTES = int(os.environ.get("MEMPALACE_MCP_MAX_LINE_BYTES", str(16 * 1024 * 1024)))

_REQUEST_LOCK = threading.RLock()
_STOP_EVENT = threading.Event()

logger = logging.getLogger("mempalace_mcp_daemon")


def canonical_palace_path(path: str | None = None) -> str:
    """Return the canonical palace path used to key one daemon per palace."""
    if path:
        value = path
    else:
        from .config import MempalaceConfig

        value = MempalaceConfig().palace_path
    return os.path.abspath(os.path.realpath(os.path.expanduser(value)))


def palace_key(palace_path: str) -> str:
    """Stable short key for filesystem state paths."""
    normalized = os.path.normcase(canonical_palace_path(palace_path))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def state_root() -> Path:
    raw = os.environ.get(STATE_ROOT_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".mempalace" / "mcp"


def state_dir_for_palace(palace_path: str) -> Path:
    return state_root() / palace_key(palace_path)


def socket_path_for_palace(palace_path: str) -> Path:
    """Return the private Unix socket path for a palace."""
    override = os.environ.get(SOCKET_ENV)
    if override:
        return Path(override).expanduser()
    return state_dir_for_palace(palace_path) / "mcp.sock"


def _chmod_private(path: Path, mode: int) -> None:
    try:
        os.chmod(str(path), mode)
    except OSError:
        pass


def _json_error(req_id: Any, code: int, message: str, data: dict[str, Any] | None = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def request_needs_serial_writer(request: Any) -> bool:
    """True when the request must run under the single-writer gate."""
    if not isinstance(request, dict):
        return False
    return request.get("method") == "tools/call"


def dispatch_request(request: Any, handler: Callable[[Any], Any]) -> Any:
    """Dispatch one JSON-RPC request, serializing all tools/call traffic."""
    if request_needs_serial_writer(request):
        with _REQUEST_LOCK:
            return handler(request)
    return handler(request)


def _load_mcp_handler() -> tuple[Callable[[Any], Any], Any]:  # pragma: no cover
    """Import the existing MCP server after daemon args/env are resolved."""
    from . import mcp_server

    for name in (
        "_refresh_sqlite_integrity_status",
        "_refresh_vector_disabled_flag",
        "_maybe_eager_warmup_embedder",
    ):
        fn = getattr(mcp_server, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                logger.debug("%s failed during daemon startup", name, exc_info=True)

    return mcp_server.handle_request, mcp_server


def _close_mcp_client_best_effort(mcp_server_module: Any) -> None:
    """Best-effort Chroma drain/close when the daemon exits."""
    client = getattr(mcp_server_module, "_client_cache", None)
    if client is None:
        return

    for name in ("persist", "close"):
        fn = getattr(client, name, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                logger.debug("client.%s() failed during daemon shutdown", name, exc_info=True)


def _socket_accepting(path: Path) -> bool:
    if not hasattr(socket, "AF_UNIX"):
        return False

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _prepare_socket_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private(path.parent, 0o700)

    if not path.exists():
        return

    if _socket_accepting(path):
        raise SystemExit(f"MemPalace MCP daemon already listening at {path}")

    try:
        path.unlink()
    except OSError as exc:
        raise SystemExit(f"Cannot remove stale MCP socket {path}: {exc}") from exc


def _handle_client(conn: socket.socket, handler: Callable[[Any], Any]) -> None:  # pragma: no cover
    with conn:
        try:
            reader = conn.makefile("r", encoding="utf-8", newline="\n")
            writer = conn.makefile("w", encoding="utf-8", newline="\n")
        except OSError:
            return

        with reader, writer:
            for line in reader:
                if len(line.encode("utf-8", errors="replace")) > MAX_LINE_BYTES:
                    response = _json_error(
                        None,
                        -32600,
                        "Request too large",
                        {"max_line_bytes": MAX_LINE_BYTES},
                    )
                    writer.write(json.dumps(response, ensure_ascii=False) + "\n")
                    writer.flush()
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError as exc:
                    response = _json_error(None, -32700, f"Parse error: {exc.msg}")
                else:
                    try:
                        response = dispatch_request(request, handler)
                    except BaseException as exc:
                        req_id = request.get("id") if isinstance(request, dict) else None
                        logger.exception("MCP request failed in daemon")
                        response = _json_error(
                            req_id,
                            -32000,
                            "Internal daemon error",
                            {
                                "error_class": type(exc).__name__,
                                "message": str(exc),
                            },
                        )

                if response is None:
                    continue

                writer.write(json.dumps(response, ensure_ascii=False) + "\n")
                writer.flush()


def serve_unix_socket(socket_path: Path, handler: Callable[[Any], Any]) -> None:  # pragma: no cover
    if not hasattr(socket, "AF_UNIX"):
        raise SystemExit("Unix domain sockets are not available on this Python/platform")

    _prepare_socket_path(socket_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        _chmod_private(socket_path, 0o600)
        server.listen(64)
        server.settimeout(0.5)
        logger.info("MemPalace MCP daemon listening on %s", socket_path)

        while not _STOP_EVENT.is_set():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if exc.errno in (errno.EBADF, errno.EINVAL):
                    break
                raise

            thread = threading.Thread(
                target=_handle_client,
                args=(conn, handler),
                name="mempalace-mcp-client",
                daemon=True,
            )
            thread.start()
    finally:
        try:
            server.close()
        finally:
            try:
                socket_path.unlink()
            except OSError:
                pass


def _install_signal_handlers() -> None:  # pragma: no cover
    def _stop(_signum, _frame):
        _STOP_EVENT.set()

    for signum in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signum, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (OSError, ValueError):
                pass


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MemPalace single-writer MCP daemon")
    parser.add_argument("--socket", default=None, help="Unix socket path to listen on")
    parser.add_argument("--palace", default=None, help="Palace path to serve")
    parser.add_argument("--backend", default=None, help="Backend to pass to the MCP server")
    parser.add_argument(
        "--read-only", action="store_true", help="Expose the MCP server in read-only mode"
    )
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    args = _parse_args(argv)

    if args.palace:
        os.environ["MEMPALACE_PALACE_PATH"] = canonical_palace_path(args.palace)
    if args.backend:
        os.environ["MEMPALACE_BACKEND_EXPLICIT"] = args.backend
        os.environ["MEMPALACE_BACKEND"] = args.backend
    if args.read_only:
        os.environ["MEMPALACE_MCP_READ_ONLY"] = "1"

    palace_path = canonical_palace_path(args.palace)
    socket_path = (
        Path(args.socket).expanduser() if args.socket else socket_path_for_palace(palace_path)
    )
    os.environ[SOCKET_ENV] = str(socket_path)

    _install_signal_handlers()
    handler, mcp_server_module = _load_mcp_handler()

    try:
        serve_unix_socket(socket_path, handler)
    finally:
        _close_mcp_client_best_effort(mcp_server_module)


if __name__ == "__main__":
    main()
