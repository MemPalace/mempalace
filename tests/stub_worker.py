"""A small MemPalace worker stand-in for online backup tests.

Run as a script, it serves the worker HTTP contract the gateway uses
(``initialize``, ``ping``, ``tools/list``, ``tools/call``) on loopback with the
gateway's bearer token. Its palace holds two files that must stay consistent
with each other in any snapshot:

- ``chroma.sqlite3``: a rollback-journal SQLite database (one row per drawer);
- ``blob.bin``: a checksummed append-only binary log (one record per drawer).

``mempalace_add_drawer`` writes both; content ``HOLD:<path>`` first creates
``<path>.started`` and then waits until ``<path>`` exists, so a test can keep
a call in flight. SIGTERM finishes the current request and exits.

Importing this module (for ``StubLauncher`` and the verifiers) uses only the
standard library and modules that exist on every supported revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_NAME = "chroma.sqlite3"
BLOB_NAME = "blob.bin"
_RECORD = struct.Struct(">I")
_TOOLS = [
    {"name": "mempalace_status", "description": "status", "inputSchema": {"type": "object"}},
    {
        "name": "mempalace_add_drawer",
        "description": "add",
        "inputSchema": {"type": "object"},
    },
]


# ── verifiers (used by the tests on restored copies) ──


def read_blob(path) -> list[bytes]:
    """Return every record of a blob log; raise ValueError on any damage."""
    data = Path(path).read_bytes()
    records, offset = [], 0
    while offset < len(data):
        if offset + _RECORD.size > len(data):
            raise ValueError("truncated record header")
        (length,) = _RECORD.unpack_from(data, offset)
        start = offset + _RECORD.size
        end = start + length + 32
        if end > len(data):
            raise ValueError("truncated record")
        payload, digest = data[start : start + length], data[start + length : end]
        if hashlib.sha256(payload).digest() != digest:
            raise ValueError("record checksum mismatch")
        records.append(payload)
        offset = end
    return records


def drawer_rows(path) -> list[str]:
    """Contents of a stub palace database, opened read-only and immutable."""
    conn = sqlite3.connect(Path(path).as_uri() + "?immutable=1", uri=True)
    try:
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("stub palace database failed quick_check")
        return [row[0] for row in conn.execute("SELECT content FROM drawers ORDER BY id")]
    finally:
        conn.close()


# ── launcher (runs in the gateway process) ──


class StubLauncher:
    """Starts this script per workspace, shaped like ``SubprocessLauncher``.

    The worker gets its palace under the resolved workspace generation and the
    same environment names as a real worker, so process checks see it as one.
    """

    def __init__(self, startup_timeout_s: float = 30.0):
        self.startup_timeout_s = startup_timeout_s
        self.processes: list = []

    def start(self, workspace_id, workspace_dir):
        import secrets

        from commonplace.workers import Worker, _free_loopback_port, wait_until_ready
        from commonplace.workspace_store import resolve_workspace

        workspace_dir = resolve_workspace(Path(workspace_dir))
        palace = workspace_dir / "palace"
        home = workspace_dir / "home"
        config = workspace_dir / "config"
        for directory in (palace, home, config):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        port = _free_loopback_port()
        token = secrets.token_urlsafe(32)
        env = {key: os.environ[key] for key in ("PATH", "LANG", "TMPDIR") if key in os.environ}
        env.update(
            HOME=str(home),
            MEMPALACE_CONFIG_DIR=str(config),
            MEMPALACE_PALACE_PATH=str(palace),
            MEMPALACE_MCP_HTTP_TOKEN=token,
            MEMPALACE_MCP_TRUSTED_IDENTITY="1",
        )
        log = open(workspace_dir / "worker.log", "ab")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--port", str(port)],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
            )
        finally:
            log.close()
        self.processes.append(proc)

        def stop() -> None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)

        worker = Worker(
            workspace_id=workspace_id,
            host="127.0.0.1",
            port=port,
            token=token,
            stop=stop,
            alive=lambda: proc.poll() is None,
            pid=proc.pid,
        )
        try:
            wait_until_ready(worker, self.startup_timeout_s)
        except Exception:
            stop()
            raise
        return worker

    def kill_all(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass


# ── the worker process ──


class _Palace:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.Lock()
        self.blob = directory / BLOB_NAME
        self.conn = sqlite3.connect(
            str(directory / DB_NAME), timeout=30, check_same_thread=False, isolation_level=None
        )
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS drawers (id INTEGER PRIMARY KEY, content TEXT NOT NULL)"
        )

    def count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT count(*) FROM drawers").fetchone()[0]

    def add(self, content: str) -> int:
        payload = content.encode("utf-8")
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self.conn.execute("INSERT INTO drawers (content) VALUES (?)", (content,))
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            with open(self.blob, "ab") as out:
                out.write(_RECORD.pack(len(payload)) + payload + hashlib.sha256(payload).digest())
                out.flush()
                os.fsync(out.fileno())
            return cursor.lastrowid

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def _result(req_id, value) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": json.dumps(value)}]},
    }


def _handle(palace: _Palace, request: dict):
    req_id, method = request.get("id"), request.get("method")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "stub-worker", "version": "1"},
                "capabilities": {"tools": {}},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOLS}}
    if method == "tools/call":
        params = request.get("params") or {}
        name, arguments = params.get("name"), params.get("arguments") or {}
        if name == "mempalace_status":
            return _result(req_id, {"total_drawers": palace.count()})
        if name == "mempalace_add_drawer":
            content = str(arguments.get("content", ""))
            if content.startswith("HOLD:"):
                release = Path(content[5:])
                Path(str(release) + ".started").write_text(str(os.getpid()))
                deadline = time.monotonic() + 120
                while not release.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
            drawer = palace.add(content)
            return _result(req_id, {"success": True, "drawer_id": f"drawer_{drawer}"})
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "unknown"}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    token = os.environ["MEMPALACE_MCP_HTTP_TOKEN"]
    palace = _Palace(Path(os.environ["MEMPALACE_PALACE_PATH"]))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            if self.headers.get("Authorization") != f"Bearer {token}":
                self._send(401, {"error": "unauthorized"})
                return
            try:
                response = _handle(palace, json.loads(body.decode("utf-8")))
            except Exception as exc:  # A stub reports, never hides, its own faults.
                self._send(500, {"error": type(exc).__name__})
                return
            if response is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, response)

        def _send(self, status, value):
            raw = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = False
    server.block_on_close = True

    def terminate(_signum, _frame):
        # Finish in-flight requests (block_on_close), then exit cleanly.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
        palace.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
