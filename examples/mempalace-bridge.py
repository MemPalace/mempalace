"""
mempalace-bridge.py — Lightweight stdio<->socket relay for MemPalace.

Configure each AI agent (Claude Code, Codex, Gemini CLI, etc.) to run this
script as the MCP command instead of mempalace.mcp_server directly.
It auto-starts the daemon on first use.

MCP config example (Claude Code ~/.claude.json):
  "mempalace": {
    "type": "stdio",
    "command": "/path/to/python",
    "args": ["/path/to/mempalace-bridge.py"]
  }
"""
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PALACE_DIR    = Path(os.environ.get("MEMPALACE_PALACE", Path.home() / ".mempalace"))
SOCK_PATH     = PALACE_DIR / "mcp.sock"
DAEMON_PYTHON = sys.executable
DAEMON_SCRIPT = str(Path(__file__).parent / "mempalace-daemon.py")


def _start_daemon():
    PALACE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = PALACE_DIR / "daemon.log"
    subprocess.Popen(
        [DAEMON_PYTHON, DAEMON_SCRIPT],
        stdout=open(str(log_path), "a"),
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )


def _connect(retries: int = 20, delay: float = 0.25) -> socket.socket:
    for i in range(retries):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(SOCK_PATH))
            return s
        except (FileNotFoundError, ConnectionRefusedError):
            if i == 0:
                _start_daemon()
            time.sleep(delay)
    raise RuntimeError(f"MemPalace daemon not reachable at {SOCK_PATH}")


def main():
    try:
        sock = _connect()
    except RuntimeError as e:
        print(f"[mempalace-bridge] {e}", file=sys.stderr)
        sys.exit(1)

    stop = threading.Event()

    def pump_in():
        try:
            while not stop.is_set():
                chunk = sys.stdin.buffer.read1(65536)
                if not chunk:
                    break
                sock.sendall(chunk)
        except Exception:
            pass
        finally:
            stop.set()
            try:
                sock.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    def pump_out():
        try:
            while not stop.is_set():
                chunk = sock.recv(65536)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
        except Exception:
            pass
        finally:
            stop.set()

    t_in  = threading.Thread(target=pump_in,  daemon=True)
    t_out = threading.Thread(target=pump_out, daemon=True)
    t_in.start()
    t_out.start()
    t_out.join()


if __name__ == "__main__":
    main()
