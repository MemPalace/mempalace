"""One ``mempalace`` HTTP worker per workspace, started on first use.

Each worker binds loopback on a free port and requires a random bearer token
that only the gateway knows, and trusts the gateway's verified-identity header
(``MEMPALACE_MCP_TRUSTED_IDENTITY=1``). Its palace, home and config live under
the resolved active workspace generation, so no two workspaces share a file.
"""

from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

# The only operator variables a worker sees. Everything else (API keys, LLM
# settings, remote stores, palace paths, Commonplace's own settings) stays
# with the gateway: a worker gets its palace, home, config and token below.
_PASSED_ENV = frozenset(
    {
        "PATH",
        "COMMONPLACE_KEY_STORE",
        "COMMONPLACE_WRAPPING_KEY_FILE",
        "COMMONPLACE_KEY_SLOT",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "VIRTUAL_ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "MEMPALACE_EMBEDDING_DEVICE",
        "MEMPALACE_EMBEDDING_THREADS",
        "MEMPALACE_EMBEDDINGGEMMA_BATCH_SIZE",
    }
)
DEFAULT_IDLE_STOP_S = 15 * 60


@dataclass
class Worker:
    workspace_id: str
    host: str
    port: int
    token: str
    stop: Callable[[], None]
    alive: Callable[[], bool] = lambda: True
    pid: Optional[int] = None
    last_used: float = field(default_factory=time.monotonic)

    @property
    def host_header(self) -> str:
        return f"{self.host}:{self.port}"


class Launcher(Protocol):
    def start(self, workspace_id: str, workspace_dir: Path) -> Worker: ...


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until_ready(worker: Worker, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "ping"})
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        if not worker.alive():
            raise RuntimeError(f"worker for {worker.workspace_id} exited during startup")
        conn = http.client.HTTPConnection(worker.host, worker.port, timeout=2)
        try:
            conn.request(
                "POST",
                "/mcp",
                body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {worker.token}",
                    "Host": worker.host_header,
                },
            )
            # 403: the worker answers but wants an identity, so it is up.
            if conn.getresponse().status in (200, 403):
                return
        except OSError as exc:
            last_error = exc
        finally:
            conn.close()
        time.sleep(0.2)
    raise RuntimeError(f"worker for {worker.workspace_id} did not start: {last_error}")


class SubprocessLauncher:
    """Runs ``python -m mempalace.mcp_server --transport http`` per workspace."""

    def __init__(self, python: str = sys.executable, startup_timeout_s: float = 120.0):
        self.python = python
        self.startup_timeout_s = startup_timeout_s

    def start(self, workspace_id: str, workspace_dir: Path) -> Worker:
        from .workspace_store import resolve_workspace, worker_config

        workspace_dir = resolve_workspace(workspace_dir)
        worker_config(workspace_dir)
        palace = workspace_dir / "palace"
        home = workspace_dir / "home"
        config = workspace_dir / "config"
        for d in (palace, home, config):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        _share_model_cache(home)
        port = _free_loopback_port()
        token = secrets.token_urlsafe(32)
        env = {k: v for k, v in os.environ.items() if k in _PASSED_ENV}
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "MEMPALACE_CONFIG_DIR": str(config),
                "MEMPALACE_PALACE_PATH": str(palace),
                "MEMPALACE_MCP_HTTP_TOKEN": token,
                "MEMPALACE_MCP_TRUSTED_IDENTITY": "1",
            }
        )
        log = open(workspace_dir / "worker.log", "ab")
        try:
            proc = subprocess.Popen(
                [
                    self.python,
                    "-m",
                    "mempalace.mcp_server",
                    "--transport",
                    "http",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--palace",
                    str(palace),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
            )
        finally:
            log.close()  # The worker holds its own copy of the handle.

        def stop() -> None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

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


def _share_model_cache(home: Path) -> None:
    """Point the worker's embedding-model cache at the operator's, if any.

    The cache holds downloaded model files only, never palace data, and
    without it every workspace downloads the model again.
    """
    real = Path.home() / ".cache" / "chroma"
    link = home / ".cache" / "chroma"
    if not real.is_dir() or link.exists() or link.is_symlink():
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pass


class WorkerSupervisor:
    def __init__(
        self,
        data_dir: str,
        launcher: Optional[Launcher] = None,
        idle_stop_s: float = DEFAULT_IDLE_STOP_S,
    ):
        self.root = Path(data_dir) / "workspaces"
        self.launcher = launcher or SubprocessLauncher()
        self.idle_stop_s = idle_stop_s
        self._workers: dict = {}
        self._lock = threading.Lock()
        self._starting: dict = {}
        self._closed = False

    def workspace_dir(self, workspace_id: str) -> Path:
        from .workspace_store import workspace_path

        return workspace_path(self.root.parent, workspace_id)

    def get(self, workspace_id: str) -> Worker:
        while True:
            with self._lock:
                if self._closed:
                    raise RuntimeError("worker supervisor is closed")
                worker = self._workers.get(workspace_id)
                if worker is not None and worker.alive():
                    worker.last_used = time.monotonic()
                    return worker
                self._workers.pop(workspace_id, None)
                gate = self._starting.get(workspace_id)
                if gate is None:
                    gate = self._starting[workspace_id] = threading.Event()
                    break
            gate.wait()
        try:
            worker = self.launcher.start(workspace_id, self.workspace_dir(workspace_id))
            with self._lock:
                closed = self._closed
                if not closed:
                    self._workers[workspace_id] = worker
            if closed:
                worker.stop()
                raise RuntimeError("worker supervisor is closed")
            return worker
        finally:
            with self._lock:
                self._starting.pop(workspace_id, None)
            gate.set()

    def stop_idle(self) -> list:
        now = time.monotonic()
        with self._lock:
            idle = [
                ws
                for ws, w in self._workers.items()
                if now - w.last_used > self.idle_stop_s or not w.alive()
            ]
            stopped = [self._workers.pop(ws) for ws in idle]
        for worker in stopped:
            worker.stop()
        return idle

    def stop_all(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()

    def close(self) -> None:
        """Permanently stop workers, including ones whose launch is in flight."""
        with self._lock:
            self._closed = True
        self.stop_all()
