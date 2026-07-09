"""stdio <-> Unix-socket bridge for the single-writer MCP daemon.

The bridge is the process MCP clients attach to. It does not import ChromaDB
or open the palace. It only relays JSON-RPC lines to the daemon process that
owns mempalace.mcp_server and therefore owns the ChromaDB PersistentClient.

By making the console script `mempalace-mcp` point here, existing users get
the safe topology without changing their Claude/Codex/Gemini MCP command.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .mcp_daemon import canonical_palace_path, socket_path_for_palace, state_dir_for_palace

_DISABLE_ENV = "MEMPALACE_MCP_DISABLE_DAEMON"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_STARTUP_TIMEOUT_DEFAULT = 15.0


class BridgeError(RuntimeError):
    """Raised when the bridge cannot reach or relay through the daemon."""


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MemPalace MCP stdio bridge")
    parser.add_argument("--palace", default=None, help="Path to the palace directory")
    parser.add_argument("--backend", default=None, help="Storage backend")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio uses the bridge; non-stdio delegates to raw mcp_server",
    )
    parser.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8765, help=argparse.SUPPRESS)
    parser.add_argument("--tls-cert", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--tls-key", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--read-only", action="store_true", help="Run MCP tools read-only")
    parser.add_argument("--socket", default=None, help="Override daemon Unix socket path")
    parser.add_argument(
        "--no-auto-start",
        action="store_true",
        help="Do not start the daemon automatically when the socket is missing",
    )
    parser.add_argument(
        "--daemon-timeout",
        type=float,
        default=_STARTUP_TIMEOUT_DEFAULT,
        help="Seconds to wait for daemon startup",
    )
    args, unknown = parser.parse_known_args(argv)
    args.unknown = unknown
    return args


def _exec_raw_stdio_server(argv: list[str]) -> None:
    cmd = [sys.executable, "-m", "mempalace.mcp_server", *argv]
    env = os.environ.copy()
    if os.name == "posix":
        os.execvpe(sys.executable, cmd, env)
    raise SystemExit(subprocess.call(cmd, env=env))


def _connect(socket_path: Path, timeout: float = 0.5) -> socket.socket:
    if not hasattr(socket, "AF_UNIX"):
        raise BridgeError("Unix domain sockets are not available on this Python/platform")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(socket_path))
        sock.settimeout(None)
        return sock
    except OSError as exc:
        sock.close()
        raise BridgeError(str(exc)) from exc


def build_daemon_command(args: argparse.Namespace, socket_path: Path) -> list[str]:
    cmd = [sys.executable, "-m", "mempalace.mcp_daemon", "--socket", str(socket_path)]
    if args.palace:
        cmd.extend(["--palace", canonical_palace_path(args.palace)])
    if args.backend:
        cmd.extend(["--backend", str(args.backend)])
    if args.read_only:
        cmd.append("--read-only")
    return cmd


def _detached_kwargs(log_path: Path) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8")
    try:
        os.chmod(str(log_path), 0o600)
    except OSError:
        pass

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh,
        "stderr": log_fh,
        "close_fds": True,
    }

    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        flags = 0
        for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP", "CREATE_BREAKAWAY_FROM_JOB"):
            flags |= getattr(subprocess, name, 0)
        if flags:
            kwargs["creationflags"] = flags

    return kwargs


def start_daemon(args: argparse.Namespace, socket_path: Path, palace_path: str) -> None:
    state_dir = state_dir_for_palace(palace_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(state_dir), 0o700)
    except OSError:
        pass

    kwargs = _detached_kwargs(state_dir / "daemon.log")
    try:
        subprocess.Popen(build_daemon_command(args, socket_path), env=os.environ.copy(), **kwargs)
    finally:
        for stream_name in ("stdout", "stderr"):
            stream = kwargs.get(stream_name)
            if hasattr(stream, "close"):
                try:
                    stream.close()
                except OSError:
                    pass


def connect_or_start(
    args: argparse.Namespace, socket_path: Path, palace_path: str
) -> socket.socket:
    try:
        return _connect(socket_path)
    except BridgeError:
        if args.no_auto_start:
            raise

    start_daemon(args, socket_path, palace_path)

    deadline = time.monotonic() + max(0.1, float(args.daemon_timeout))
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            return _connect(socket_path)
        except BridgeError as exc:
            last_error = exc
            time.sleep(0.05)

    raise BridgeError(f"daemon did not become ready at {socket_path}: {last_error}")


def request_expects_response(request: Any) -> bool:
    """Return False for JSON-RPC notifications."""
    if not isinstance(request, dict):
        return True
    if request.get("method", "").startswith("notifications/"):
        return False
    return request.get("id") is not None


def _bridge_error_response(req_id: Any, message: str) -> str:
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


def run_bridge(args: argparse.Namespace) -> int:
    palace_path = canonical_palace_path(args.palace)
    socket_path = (
        Path(args.socket).expanduser() if args.socket else socket_path_for_palace(palace_path)
    )

    conn = connect_or_start(args, socket_path, palace_path)
    reader = conn.makefile("r", encoding="utf-8", newline="\n")
    writer = conn.makefile("w", encoding="utf-8", newline="\n")

    with conn, reader, writer:
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
                writer.write(raw_line if raw_line.endswith("\n") else raw_line + "\n")
                writer.flush()

                if not expects_response:
                    continue

                response = reader.readline()
                if not response:
                    raise BridgeError("daemon closed the connection")

            except OSError as exc:
                message = str(exc)
                if expects_response:
                    sys.stdout.write(_bridge_error_response(req_id, message))
                    sys.stdout.flush()
                else:
                    print(f"mempalace-mcp bridge: {message}", file=sys.stderr)
                continue

            except BridgeError as exc:
                if expects_response:
                    sys.stdout.write(_bridge_error_response(req_id, str(exc)))
                    sys.stdout.flush()
                else:
                    print(f"mempalace-mcp bridge: {exc}", file=sys.stderr)
                continue

            sys.stdout.write(response if response.endswith("\n") else response + "\n")
            sys.stdout.flush()

    return 0


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.transport != "stdio" or _truthy_env(_DISABLE_ENV):
        _exec_raw_stdio_server(sys.argv[1:] if argv is None else argv)

    try:
        raise SystemExit(run_bridge(args))
    except BridgeError as exc:
        print(f"mempalace-mcp bridge: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
