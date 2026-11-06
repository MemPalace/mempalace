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
from contextlib import contextmanager
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
# A memory call that meets a backup pause waits this long, then is refused
# with a retry hint (COMMONPLACE_BACKUP_MEMORY_WAIT_S overrides the wait).
DEFAULT_PAUSE_WAIT_S = 20.0
PAUSE_RETRY_AFTER_S = 30
STOP_WAIT_S = 10.0


class WorkersPaused(RuntimeError):
    """A memory call met a backup pause that did not end in time."""

    def __init__(self, retry_after_s: int = PAUSE_RETRY_AFTER_S):
        super().__init__("memory is paused for a backup")
        self.retry_after_s = retry_after_s


class DrainTimeout(RuntimeError):
    """In-flight memory calls did not finish in time; nothing was stopped."""


class SupervisorClosed(RuntimeError):
    """The supervisor closed while a memory call waited."""


class WorkerStopFailed(RuntimeError):
    """A worker process survived SIGTERM and SIGKILL."""


def _proc_starttime(pid: Optional[int]) -> Optional[int]:
    """Field 22 of ``/proc/<pid>/stat``; ``None`` if gone, a zombie or not Linux."""
    if pid is None:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    rest = raw[raw.rfind(b")") + 2 :].split()
    if not rest or rest[0] in (b"Z", b"X"):
        return None
    try:
        return int(rest[19])
    except (IndexError, ValueError):
        return None


def process_gone(pid: Optional[int], starttime: Optional[int]) -> bool:
    """True once ``pid`` (started at ``starttime``) no longer runs."""
    if pid is None or not sys.platform.startswith("linux"):
        return True
    now = _proc_starttime(pid)
    return now is None or (starttime is not None and now != starttime)


def stop_process(proc, term_wait_s: float = STOP_WAIT_S, kill_wait_s: float = STOP_WAIT_S) -> bool:
    """SIGTERM and wait, then SIGKILL and wait. True when the process was reaped."""
    if proc.poll() is not None:
        return True
    proc.terminate()
    try:
        proc.wait(timeout=term_wait_s)
        return True
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.wait(timeout=kill_wait_s)
        return True
    except subprocess.TimeoutExpired:
        return False


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
    starttime: Optional[int] = None

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

        starttime = _proc_starttime(proc.pid)

        def stop() -> None:
            stop_process(proc)

        worker = Worker(
            workspace_id=workspace_id,
            host="127.0.0.1",
            port=port,
            token=token,
            stop=stop,
            alive=lambda: proc.poll() is None,
            pid=proc.pid,
            starttime=starttime,
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
        self._cond = threading.Condition(self._lock)
        self._starting: dict = {}
        self._closed = False
        # Backup pause: memory calls hold a slot; a pause closes the gate,
        # drains the slots and stops every worker until resume.
        self._paused = False
        self._in_flight = 0
        self._local = threading.local()
        self.pause_wait_s = float(
            os.environ.get("COMMONPLACE_BACKUP_MEMORY_WAIT_S") or DEFAULT_PAUSE_WAIT_S
        )

    def workspace_dir(self, workspace_id: str) -> Path:
        from .workspace_store import workspace_path

        return workspace_path(self.root.parent, workspace_id)

    @contextmanager
    def call(self, wait_s: Optional[float] = None):
        """Hold one in-flight memory slot. Re-entrant per thread.

        While a backup pause is on, a new call waits up to ``wait_s``
        (default ``pause_wait_s``) for resume, then raises ``WorkersPaused``.
        """
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            wait_s = self.pause_wait_s if wait_s is None else wait_s
            with self._cond:
                if not self._cond.wait_for(lambda: not self._paused or self._closed, wait_s):
                    raise WorkersPaused()
                if self._closed:
                    raise SupervisorClosed("worker supervisor is closed")
                self._in_flight += 1
        self._local.depth = depth + 1
        try:
            yield
        finally:
            self._local.depth = depth
            if depth == 0:
                with self._cond:
                    self._in_flight -= 1
                    self._cond.notify_all()

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def worker_pids(self) -> list:
        with self._lock:
            return [w.pid for w in self._workers.values() if w.pid is not None and w.alive()]

    def pause(self, drain_s: float = 60.0) -> list:
        """Close the gate, drain in-flight calls, stop every worker.

        Returns the stopped workers' pids. ``DrainTimeout`` (gate reopened,
        nothing stopped) if calls are still running after ``drain_s``: a call
        is never cut. ``WorkerStopFailed`` (gate reopened) if a worker
        survives SIGKILL or its ``/proc`` entry stays.
        """
        with self._cond:
            if self._closed:
                raise RuntimeError("worker supervisor is closed")
            if self._paused:
                raise RuntimeError("workers are already paused")
            self._paused = True
            drained = self._cond.wait_for(
                lambda: self._in_flight == 0 and not self._starting, drain_s
            )
            if not drained:
                self._paused = False
                self._cond.notify_all()
                raise DrainTimeout(f"{self._in_flight} memory call(s) still running")
            workers = list(self._workers.values())
            self._workers.clear()
        try:
            for worker in workers:
                worker.stop()
                deadline = time.monotonic() + 1.0
                # A worker without a pid runs in-process; its stop() is final.
                while worker.pid is not None and (
                    worker.alive() or not process_gone(worker.pid, worker.starttime)
                ):
                    if time.monotonic() >= deadline:
                        raise WorkerStopFailed(f"worker for {worker.workspace_id} did not stop")
                    time.sleep(0.02)
        except BaseException:
            self.resume()
            raise
        return [w.pid for w in workers]

    def resume(self) -> None:
        """Reopen the gate; workers start again on their next call."""
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    def get(self, workspace_id: str) -> Worker:
        with self.call():
            return self._get(workspace_id)

    def _get(self, workspace_id: str) -> Worker:
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
            with self._cond:
                self._starting.pop(workspace_id, None)
                self._cond.notify_all()
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
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self.stop_all()
