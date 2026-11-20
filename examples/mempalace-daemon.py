"""
mempalace-daemon.py — Persistent MemPalace Unix socket MCP server.

Runs ONCE (managed by macOS LaunchAgent). All Claude Code and Codex sessions
connect via mempalace-bridge.py (a stdio<->socket relay). This eliminates the
single-slot problem: every session gets MemPalace access simultaneously.

Architecture:
  LaunchAgent -> mempalace-daemon.py (this file, one process, holds ChromaDB)
  Per session -> mempalace-bridge.py -> Unix socket -> this daemon

Single-writer guarantee: all tools/call requests go through _tool_lock, which
serializes ChromaDB writes. Protocol messages (initialize, tools/list, ping)
are lock-free.
"""
import fcntl
import json
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path

PALACE_DIR = Path(os.environ.get("MEMPALACE_PALACE", Path.home() / ".mempalace"))
SOCK_PATH  = PALACE_DIR / "mcp.sock"
LOCK_PATH  = PALACE_DIR / ".daemon.lock"
LOG_PATH   = PALACE_DIR / "daemon.log"

PALACE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mempalace-daemon")

_lock_fd = open(LOCK_PATH, "w")
try:
    fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except (BlockingIOError, OSError):
    log.error("Daemon already running — exiting.")
    sys.exit(1)

from mempalace.mcp_server import handle_request  # noqa: E402

_tool_lock = threading.Lock()


def _handle_client(conn: socket.socket, client_id: int):
    log.info(f"[client-{client_id}] connected")
    f_in = conn.makefile("rb")
    try:
        for raw in f_in:
            raw = raw.strip()
            if not raw:
                continue
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                continue
            method = request.get("method") or ""
            if method == "tools/call":
                with _tool_lock:
                    response = handle_request(request)
            else:
                response = handle_request(request)
            if response is not None:
                conn.sendall((json.dumps(response) + "\n").encode())
    except (ConnectionResetError, BrokenPipeError):
        pass
    except Exception:
        log.exception(f"[client-{client_id}] handler error")
    finally:
        try:
            f_in.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        log.info(f"[client-{client_id}] disconnected")


def main():
    if SOCK_PATH.exists():
        SOCK_PATH.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(str(SOCK_PATH))
    SOCK_PATH.chmod(0o600)
    server.listen(32)
    log.info(f"MemPalace daemon listening on {SOCK_PATH}")
    print(f"[mempalace-daemon] Listening on {SOCK_PATH}", flush=True)

    def _shutdown(sig, frame):
        log.info("Shutting down.")
        server.close()
        if SOCK_PATH.exists():
            SOCK_PATH.unlink()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    client_id = 0
    try:
        while True:
            conn, _ = server.accept()
            client_id += 1
            t = threading.Thread(target=_handle_client, args=(conn, client_id), daemon=True)
            t.start()
    except OSError:
        pass
    finally:
        if SOCK_PATH.exists():
            SOCK_PATH.unlink()


if __name__ == "__main__":
    main()
