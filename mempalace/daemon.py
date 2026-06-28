"""Long-lived local daemon for queued MemPalace writes.

Daemon mode is strictly opt-in. The default CLI, hooks, and MCP paths still use
their direct execution behavior unless callers explicitly request daemon-backed
execution.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, urlparse

from .config import MempalaceConfig

HOST = "127.0.0.1"
STATE_ROOT_ENV = "MEMPALACE_DAEMON_STATE_ROOT"
DEFAULT_WAIT_TIMEOUT = 60.0 * 60.0
# Liveness-probe timeout for the hook "is a daemon already running?" precheck.
# Kept well under the ~500ms hook budget so a wedged daemon can't stall the hook
# (it falls back to the direct/spawn path instead). A healthy local daemon
# answers /health in single-digit ms, so this rarely false-negatives.
HOOK_PROBE_TIMEOUT = 0.5
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
MAX_ATTEMPTS = 3
MAX_BODY_BYTES = 1 << 20  # 1 MiB cap on request bodies (auth-gated DoS guard)
SHUTDOWN_DRAIN_SECONDS = 10.0
# Terminal jobs are kept for diagnostics then pruned so the queue DB (which
# holds verbatim payloads) doesn't grow without bound across a long-lived
# daemon. Override via env for operators who want a longer/shorter window.
JOB_RETENTION_DAYS = int(os.environ.get("MEMPALACE_DAEMON_RETENTION_DAYS", "7") or "7")
try:
    import fcntl as _fcntl  # POSIX only; absent on Windows
except ImportError:  # pragma: no cover - Windows fallback
    _fcntl = None


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _chmod_dir_private(path: Path) -> None:
    try:
        os.chmod(str(path), 0o700)
    except OSError:
        pass


class DaemonError(RuntimeError):
    """Raised when daemon client operations fail."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_palace_path(path: str | None = None) -> str:
    value = path or MempalaceConfig().palace_path
    return os.path.abspath(os.path.realpath(os.path.expanduser(value)))


def palace_key(palace_path: str) -> str:
    import hashlib

    normalized = os.path.normcase(canonical_palace_path(palace_path))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def state_root() -> Path:
    raw = os.environ.get(STATE_ROOT_ENV)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".mempalace" / "daemon"


def state_dir(palace_path: str) -> Path:
    return state_root() / palace_key(palace_path)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def ensure_token(palace_path: str) -> str:
    token_path = state_dir(palace_path) / "token"
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    _write_private(token_path, token + "\n")
    return token


def read_token(palace_path: str) -> str:
    token_path = state_dir(palace_path) / "token"
    try:
        return token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DaemonError(f"daemon token not found for {palace_path}") from exc


def endpoint_path(palace_path: str) -> Path:
    return state_dir(palace_path) / "endpoint.json"


def pid_path(palace_path: str) -> Path:
    return state_dir(palace_path) / "pid"


def queue_path(palace_path: str) -> Path:
    return state_dir(palace_path) / "queue.sqlite3"


def _read_endpoint(palace_path: str) -> dict[str, Any]:
    try:
        with open(endpoint_path(palace_path), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise DaemonError("daemon endpoint not found") from exc


def _pid_alive_windows(pid: int) -> bool:
    """Liveness probe for Windows that never sends a console control event.

    ``os.kill(pid, 0)`` is NOT a harmless existence check on Windows: signal 0
    is ``signal.CTRL_C_EVENT``, so Python routes it to
    ``GenerateConsoleCtrlEvent`` and sends a Ctrl-C to the target's process
    group instead of probing the pid. On a process with an attached console
    (e.g. a CI runner) that Ctrl-C is delivered back to *this* interpreter and
    surfaces as a spurious ``KeyboardInterrupt`` — exactly the hang seen when
    ``DaemonClient`` polled a same-process endpoint. Probe via the Win32 process
    handle API instead, which has no signalling side effects.
    """
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    ERROR_ACCESS_DENIED = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        # No handle: access-denied means the process exists but isn't ours to
        # open; any other error (invalid parameter / not found) means it's gone.
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        # A live process is not signalled, so the zero-timeout wait returns
        # WAIT_TIMEOUT; an exited process is signalled and returns WAIT_OBJECT_0.
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            return _pid_alive_windows(pid)
        except OSError:
            # If the Win32 probe itself fails, assume alive rather than risk
            # discarding a healthy endpoint — and never fall back to os.kill.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    state: str
    priority: int
    dedupe_key: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    attempts: int


class QueueStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @contextlib.contextmanager
    def _connect(self):
        """Open a short-lived sqlite3 connection and close it on exit.

        The bare ``with sqlite3.connect(...)`` context manager only manages the
        transaction (commit/rollback) — it does NOT close the connection, so every
        QueueStore call in this long-lived daemon process leaked a connection FD.
        In a daemon that runs thousands of jobs that is an unbounded FD leak. This
        wrapper closes the connection on exit so each call is self-contained.
        """
        conn = sqlite3.connect(str(self.path), timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    dedupe_key TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result_json TEXT,
                    error_json TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, priority)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs(dedupe_key, state)")
            # Unique partial index: at most one queued/running job per dedupe_key.
            # Enforces the dedupe invariant across processes (TOCTOU-safe); finished
            # jobs drop out of the index so a later identical enqueue is allowed.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe_active "
                "ON jobs(dedupe_key) WHERE state IN ('queued', 'running')"
            )
        # The queue DB holds verbatim payloads (diary text, source paths) — lock it
        # down to owner-only regardless of the invoking user's umask. The WAL/SHM
        # sidecars carry the same un-checkpointed payloads, so harden them too when
        # present (the daemon also runs under a 0o077 umask; this covers any
        # QueueStore opened outside that scope, e.g. the CLI `daemon jobs` path).
        _chmod_private(self.path)
        for sidecar_suffix in ("-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + sidecar_suffix)
            if sidecar.exists():
                _chmod_private(sidecar)

    def prune_terminal(self, older_than_days: int = JOB_RETENTION_DAYS) -> int:
        """Delete terminal (succeeded/failed/cancelled) jobs older than the
        retention window.

        Bounded growth for the queue DB, which holds verbatim payloads. Only
        terminal jobs are eligible — queued/running jobs are never touched, so
        a crash mid-prune cannot drop in-flight work (incremental-only). The
        cutoff uses ``finished_at``; a terminal job is never re-examined by
        recover_running, so deleting it is safe.
        """
        if older_than_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM jobs
                WHERE state IN ('succeeded', 'failed', 'cancelled')
                  AND finished_at IS NOT NULL
                  AND finished_at < ?
                """,
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    def recover_running(self) -> int:
        """Re-queue jobs left ``running`` by a crashed/killed daemon.

        Jobs that have already exhausted ``MAX_ATTEMPTS`` claims are dead-lettered
        to ``failed`` instead of being retried — non-idempotent kinds (diary_write
        derives its entry_id from wall-clock time) would otherwise duplicate
        verbatim palace content on every restart, violating the incremental-only
        principle. The last error_json is preserved for diagnostics.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET state = 'failed', finished_at = ?,
                    error_json = COALESCE(error_json, ?)
                WHERE state = 'running' AND attempts >= ?
                """,
                (
                    _now(),
                    json.dumps(
                        {"error_class": "MaxAttemptsExceeded", "message": "max attempts exceeded"},
                        ensure_ascii=False,
                    ),
                    MAX_ATTEMPTS,
                ),
            )
            cur = conn.execute(
                """
                UPDATE jobs
                SET state = 'queued', started_at = NULL
                WHERE state = 'running' AND attempts < ?
                """,
                (MAX_ATTEMPTS,),
            )
            return int(cur.rowcount or 0)

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        priority: int = 0,
    ) -> Job:
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connect() as conn:
            if dedupe_key:
                row = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE dedupe_key = ? AND state IN ('queued', 'running')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (dedupe_key,),
                ).fetchone()
                if row is not None:
                    return self._row_to_job(row)

            job_id = uuid.uuid4().hex
            try:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, kind, payload_json, state, priority, dedupe_key, created_at, attempts
                    ) VALUES (?, ?, ?, 'queued', ?, ?, ?, 0)
                    """,
                    (job_id, kind, payload_json, int(priority), dedupe_key, _now()),
                )
            except sqlite3.IntegrityError:
                # Unique partial index beat us in a cross-process race — return the
                # job that won. SELECT-then-INSERT is not atomic across processes; the
                # index is the source of truth.
                if not dedupe_key:
                    raise
                row = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE dedupe_key = ? AND state IN ('queued', 'running')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (dedupe_key,),
                ).fetchone()
                if row is None:
                    # Index guard fired but the row is already gone — retry the INSERT.
                    conn.execute(
                        """
                        INSERT INTO jobs (
                            id, kind, payload_json, state, priority, dedupe_key, created_at, attempts
                        ) VALUES (?, ?, ?, 'queued', ?, ?, ?, 0)
                        """,
                        (job_id, kind, payload_json, int(priority), dedupe_key, _now()),
                    )
                    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return self._row_to_job(row)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row)

    def claim_next(self) -> Job | None:
        # Atomic across processes: the UPDATE only fires if the row is still
        # 'queued'. If two daemon processes SELECT the same row, the first to
        # UPDATE it flips state to 'running' (rowcount=1); the second's UPDATE
        # matches 0 rows (WHERE state='queued' is now false) and we re-loop
        # instead of double-executing the job. The in-process RLock does not
        # protect against a second OS process — this guard does.
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'queued'
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                """
                UPDATE jobs
                SET state = 'running', started_at = ?, attempts = attempts + 1
                WHERE id = ? AND state = 'queued'
                """,
                (_now(), row["id"]),
            )
            if cur.rowcount != 1:
                # Lost the race to another process — nothing to run this iteration.
                return None
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            return self._row_to_job(claimed)

    def finish(
        self,
        job_id: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        only_if_running: bool = False,
    ) -> Job:
        # ``only_if_running`` guards the worker's finish against a lost race with
        # shutdown's cancel: if the active job was already flipped to 'cancelled'
        # by _drain_and_cleanup, a late worker finish must NOT overwrite it back to
        # 'succeeded'/'failed' (which would un-cancel a job recover_running must
        # not re-run). The conditional UPDATE makes the worker's finish a no-op in
        # that window instead of relying on process-exit timing.
        where = "WHERE id = ?" + (" AND state = 'running'" if only_if_running else "")
        with self._lock, self._connect() as conn:
            conn.execute(
                f"""
                UPDATE jobs
                SET state = ?, finished_at = ?, result_json = ?, error_json = ?
                {where}
                """,
                (
                    state,
                    _now(),
                    json.dumps(result or {}, ensure_ascii=False),
                    json.dumps(error or {}, ensure_ascii=False) if error else None,
                    job_id,
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row)

    def get(self, job_id: str) -> Job:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise DaemonError(f"unknown job id: {job_id}")
            return self._row_to_job(row)

    def list(self, limit: int = 20) -> list[Job]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [self._row_to_job(row) for row in rows]

    def counts(self) -> dict[str, int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").fetchall()
            return {str(row["state"]): int(row["n"]) for row in rows}

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        def _loads(value):
            if not value:
                return None
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return None

        return Job(
            id=str(row["id"]),
            kind=str(row["kind"]),
            payload=_loads(row["payload_json"]) or {},
            state=str(row["state"]),
            priority=int(row["priority"]),
            dedupe_key=row["dedupe_key"],
            created_at=str(row["created_at"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            result=_loads(row["result_json"]),
            error=_loads(row["error_json"]),
            attempts=int(row["attempts"]),
        )


def job_to_dict(job: Job, *, include_payload: bool = True) -> dict[str, Any]:
    out = {
        "id": job.id,
        "kind": job.kind,
        "state": job.state,
        "priority": job.priority,
        "dedupe_key": job.dedupe_key,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "result": job.result,
        "error": job.error,
        "attempts": job.attempts,
    }
    if include_payload:
        out["payload"] = job.payload
    return out


class DaemonRuntime:
    def __init__(self, palace_path: str, backend: str | None = None):
        self.palace_path = canonical_palace_path(palace_path)
        self.backend = backend
        self.store = QueueStore(queue_path(self.palace_path))
        self.shutdown_event = threading.Event()
        self.worker_wake = threading.Event()
        self.active_job_id: str | None = None
        self.worker_thread: threading.Thread | None = None

    def start_worker(self) -> threading.Thread:
        self.store.recover_running()
        # Bounded growth: drop terminal jobs older than the retention window
        # before bringing the worker up. Best-effort — a prune failure must not
        # block startup.
        try:
            self.store.prune_terminal()
        except Exception:  # noqa: BLE001 - retention is best-effort, never fatal
            pass
        thread = threading.Thread(
            target=self._worker_loop, name="mempalace-daemon-worker", daemon=True
        )
        self.worker_thread = thread
        thread.start()
        return thread

    def worker_alive(self) -> bool:
        return self.worker_thread is not None and self.worker_thread.is_alive()

    def _safe_finish(self, job_id: str, *, state: str, result: dict, error: dict | None) -> None:
        try:
            # only_if_running: if shutdown already cancelled this job, don't
            # resurrect it. A finish failure must not kill the worker regardless.
            self.store.finish(job_id, state=state, result=result, error=error, only_if_running=True)
        except Exception:  # noqa: BLE001 - a finish failure must not kill the worker
            pass

    def _worker_loop(self) -> None:
        from .service import execute_job

        while not self.shutdown_event.is_set():
            try:
                job = self.store.claim_next()
            except Exception:  # noqa: BLE001 - sqlite/disk errors must not kill the worker
                self.shutdown_event.wait(1.0)
                continue
            if job is None:
                self.worker_wake.wait(0.5)
                self.worker_wake.clear()
                continue
            self.active_job_id = job.id
            try:
                payload = dict(job.payload)
                # Override, never trust the client: an authenticated request for
                # palace A must not be able to retarget the daemon at palace B.
                payload["palace_path"] = self.palace_path
                if self.backend:
                    payload["backend"] = self.backend
                result = execute_job(job.kind, payload)
                ok = bool(result.get("success", True))
                state = "succeeded" if ok else "failed"
                error = None if ok else {"message": result.get("error", "job failed")}
                self._safe_finish(job.id, state=state, result=result, error=error)
            except (Exception, SystemExit) as exc:
                # SystemExit is BaseException, not Exception — catching it here is
                # deliberate. Without it, a sys.exit() in a dependency would slip
                # past `except Exception`, kill this worker thread, leave the job
                # stuck in 'running' forever, and stall every later job while
                # /health keeps reporting ok. (See mcp_server.py tool_mine for the
                # same BaseException-slip-past semantics, documented in comments.)
                self._safe_finish(
                    job.id,
                    state="failed",
                    result={"success": False, "exit_code": 1},
                    error={"error_class": type(exc).__name__, "message": str(exc)},
                )
            finally:
                self.active_job_id = None


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)
    handler.close_connection = True


def run_server(palace_path: str, *, backend: str | None = None, port: int = 0) -> None:
    palace_path = canonical_palace_path(palace_path)
    previous_env = {
        "MEMPALACE_PALACE_PATH": os.environ.get("MEMPALACE_PALACE_PATH"),
        "MEMPALACE_BACKEND_EXPLICIT": os.environ.get("MEMPALACE_BACKEND_EXPLICIT"),
        "MEMPALACE_BACKEND": os.environ.get("MEMPALACE_BACKEND"),
    }
    os.environ["MEMPALACE_PALACE_PATH"] = palace_path
    if backend:
        os.environ["MEMPALACE_BACKEND_EXPLICIT"] = backend
        os.environ["MEMPALACE_BACKEND"] = backend
    # Privacy by architecture: tighten the umask to owner-only BEFORE the queue
    # DB is created. SQLite's WAL/SHM sidecars hold un-checkpointed verbatim
    # payloads and are (re)created with the process umask on every open/close
    # cycle, so the umask must already be tight when DaemonRuntime builds the
    # QueueStore (its _init_db opens the DB in WAL mode) — not only once the HTTP
    # server starts. Restored in the finally at the end of run_server.
    prev_umask = os.umask(0o077)
    token = ensure_token(palace_path)
    runtime = DaemonRuntime(palace_path, backend=backend)

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 10

        def log_message(self, fmt, *args):  # pragma: no cover - stdlib access logging noise
            return

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization")
            if auth and secrets.compare_digest(auth, f"Bearer {token}"):
                return True
            _json_response(self, 401, {"error": "unauthorized"})
            return False

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            # Reject a negative Content-Length explicitly: self.rfile.read(-1)
            # would read until the client closes the connection, blocking the
            # worker and bypassing the MAX_BODY_BYTES cap (an auth-gated DoS).
            if length < 0:
                raise ValueError("invalid Content-Length")
            if length > MAX_BODY_BYTES:
                raise ValueError("request body too large")
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8")) if raw else {}

        def do_GET(self):
            if not self._authorized():
                return
            try:
                self._handle_get()
            except Exception as exc:  # noqa: BLE001 - malformed query/DB error → 400
                _json_response(self, 400, {"error": str(exc)})

        def _handle_get(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "worker_alive": runtime.worker_alive(),
                        "pid": os.getpid(),
                        "palace_path": runtime.palace_path,
                        "backend": runtime.backend,
                        "active_job_id": runtime.active_job_id,
                        "counts": runtime.store.counts(),
                    },
                )
                return
            if parsed.path == "/jobs":
                qs = parse_qs(parsed.query)
                limit = int((qs.get("limit") or ["20"])[0])
                jobs = [
                    job_to_dict(job, include_payload=False) for job in runtime.store.list(limit)
                ]
                _json_response(self, 200, {"jobs": jobs})
                return
            if parsed.path.startswith("/jobs/"):
                job_id = parsed.path.rsplit("/", 1)[-1]
                try:
                    job = runtime.store.get(job_id)
                except DaemonError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                # Payloads carry verbatim user content (diary text) — do not return
                # them over HTTP unless the caller explicitly opts in.
                qs = parse_qs(parsed.query)
                include_payload = qs.get("include_payload", ["false"])[0].lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
                _json_response(
                    self, 200, {"job": job_to_dict(job, include_payload=include_payload)}
                )
                return
            _json_response(self, 404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/jobs":
                try:
                    body = self._read_json()
                    job = runtime.store.enqueue(
                        str(body.get("kind") or ""),
                        body.get("payload") or {},
                        dedupe_key=body.get("dedupe_key"),
                        priority=int(body.get("priority") or 0),
                    )
                    runtime.worker_wake.set()
                except Exception as exc:  # noqa: BLE001 - client gets structured failure
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 202, {"job": job_to_dict(job)})
                return
            if parsed.path == "/shutdown":
                _json_response(self, 200, {"ok": True})
                runtime.shutdown_event.set()
                threading.Thread(target=httpd.shutdown, daemon=True).start()
                return
            _json_response(self, 404, {"error": "not found"})

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def server_bind(self):
            # http.server's HTTPServer.server_bind() calls socket.getfqdn(host)
            # to set server_name — a reverse-DNS lookup. For our 127.0.0.1 bind
            # that lookup is useless, and on a host with slow or absent reverse
            # DNS it blocks daemon startup for ~30s (until the resolver times
            # out), which looks exactly like the daemon never coming up. Bind via
            # TCPServer directly and set the name from the literal host instead.
            import socketserver

            socketserver.TCPServer.server_bind(self)
            host, port = self.server_address[:2]
            self.server_name = host
            self.server_port = port

    # The owner-only umask set above (before DaemonRuntime built the queue DB)
    # covers every file this process creates — queue.sqlite3, its WAL/SHM
    # sidecars, and any future artifact — and is restored in the finally below.
    try:
        with _Server((HOST, port), _Handler) as httpd:
            actual_port = int(httpd.server_address[1])
            sd = state_dir(palace_path)
            sd.mkdir(parents=True, exist_ok=True)
            _chmod_dir_private(sd)
            endpoint = {
                "host": HOST,
                "port": actual_port,
                "pid": os.getpid(),
                "palace_path": palace_path,
                "started_at": _now(),
            }
            _write_private(endpoint_path(palace_path), json.dumps(endpoint, indent=2) + "\n")
            _write_private(pid_path(palace_path), f"{os.getpid()}\n")
            runtime.start_worker()
            try:
                httpd.serve_forever(poll_interval=0.5)
            finally:
                _drain_and_cleanup(runtime, palace_path, previous_env)
    finally:
        os.umask(prev_umask)


def _drain_and_cleanup(
    runtime: "DaemonRuntime", palace_path: str, previous_env: dict[str, str | None]
) -> None:
    """Drain the active job, then tear down server-side state.

    Killing a daemon thread mid-write (mid mine upsert, mid irreversible sync
    DELETE) violates incremental-only. Give the worker a bounded window to
    finish, then mark whatever is still running as cancelled so recover_running
    won't blindly re-run it on the next start (which would duplicate verbatim
    content). Finally restore the env vars run_server mutated.
    """
    runtime.shutdown_event.set()
    worker = runtime.worker_thread
    if worker is not None:
        worker.join(timeout=SHUTDOWN_DRAIN_SECONDS)
    active = runtime.active_job_id
    if active:
        runtime._safe_finish(
            active,
            state="cancelled",
            result={"success": False, "exit_code": 1},
            error={"message": "cancelled by daemon shutdown"},
        )
    for stale in (endpoint_path(palace_path), pid_path(palace_path)):
        try:
            stale.unlink()
        except OSError:
            pass
    for key, value in previous_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class DaemonClient:
    def __init__(self, palace_path: str):
        self.palace_path = canonical_palace_path(palace_path)
        endpoint = _read_endpoint(self.palace_path)
        port = endpoint.get("port")
        if port is None:
            raise DaemonError("daemon endpoint missing port")
        # Don't read the token until we trust the endpoint points at a live
        # process we started: a stale endpoint whose pid is dead may have its
        # port reused by an unrelated process, and we must not send our bearer
        # token there.
        pid = endpoint.get("pid")
        if pid is not None and not _pid_alive(int(pid)):
            raise DaemonError("daemon endpoint pid is not alive")
        self.token = read_token(self.palace_path)
        self.host = endpoint.get("host") or HOST
        self.port = int(port)
        # The daemon is always on 127.0.0.1, so a request must never go through
        # an HTTP proxy. Building an opener with an empty ProxyHandler bypasses
        # urllib's proxy discovery entirely. On macOS that discovery
        # (urllib.request._scproxy, via the SystemConfiguration framework) runs
        # on the first request to any host and is NOT bounded by the per-request
        # timeout — on a CI runner with no network it can hang for tens of
        # seconds, which looks exactly like the daemon never came up. A no-proxy
        # opener is the correct production choice here and also removes that hang.
        self._opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urlerror.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"error": raw or str(exc)}
            raise DaemonError(str(payload.get("error", exc))) from exc
        except OSError as exc:
            raise DaemonError(str(exc)) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            # A 2xx response with a non-JSON body (empty 200, truncated write,
            # proxy HTML) shouldn't surface as a bare JSONDecodeError to callers
            # that only know how to handle DaemonError.
            raise DaemonError(f"daemon returned non-JSON response: {raw[:200]!r}") from exc

    def health(self, *, timeout: float = 5.0) -> dict[str, Any]:
        return self.request("GET", "/health", timeout=timeout)

    def submit(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/jobs",
            {"kind": kind, "payload": payload, "dedupe_key": dedupe_key, "priority": priority},
        )["job"]

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self.request("GET", f"/jobs/{job_id}")["job"]

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.request("GET", f"/jobs?limit={int(limit)}")["jobs"]

    def wait(self, job_id: str, *, timeout: float = DEFAULT_WAIT_TIMEOUT) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if job["state"] in TERMINAL_STATES:
                return job
            if time.monotonic() >= deadline:
                raise DaemonError(f"timed out waiting for job {job_id}")
            time.sleep(0.2)

    def shutdown(self) -> dict[str, Any]:
        return self.request("POST", "/shutdown", {})


def get_client_if_running(palace_path: str, *, health_timeout: float = 5.0) -> DaemonClient | None:
    # health_timeout bounds the liveness probe. Hook callers (subject to the
    # ~500ms hook budget) pass a short value via HOOK_PROBE_TIMEOUT so a wedged
    # daemon — endpoint present, HTTP server not answering — can't stall the
    # hook for the default 5s before it falls back to the direct path.
    try:
        client = DaemonClient(palace_path)
        client.health(timeout=health_timeout)
        return client
    except DaemonError:
        return None


def _detached_kwargs(log_path: Path) -> dict[str, Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8")
    # The daemon log may capture verbatim content in tracebacks — owner-only.
    _chmod_private(log_path)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh,
        "stderr": log_fh,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_BREAKAWAY_FROM_JOB"):
            flags |= getattr(subprocess, name, 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    return kwargs


def start_daemon(
    palace_path: str,
    *,
    backend: str | None = None,
    foreground: bool = False,
    timeout: float = 15.0,
) -> DaemonClient:
    palace_path = canonical_palace_path(palace_path)
    ensure_token(palace_path)
    existing = get_client_if_running(palace_path)
    if existing is not None:
        return existing
    if foreground:
        # Blocks until the daemon stops. A clean stop is a normal exit, not an
        # error — return None so the caller (cmd_daemon) exits 0.
        run_server(palace_path, backend=backend, port=0)
        return None  # type: ignore[return-value]

    sd = state_dir(palace_path)
    sd.mkdir(parents=True, exist_ok=True)
    _chmod_dir_private(sd)

    # Spawn mutual exclusion: two concurrent `daemon start` callers would both
    # observe no running daemon and both spawn a child, double-claiming jobs.
    # A non-blocking flock serializes the check-then-spawn; the loser waits for
    # the winner to finish coming up, then re-checks and reuses that daemon.
    lock_fh = open(sd / "start.lock", "w") if _fcntl is not None else None
    if _fcntl is not None and lock_fh is not None:
        _chmod_private(sd / "start.lock")
        try:
            _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            # Another start is in flight — wait for it, then reuse its daemon.
            _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_EX)
            existing = get_client_if_running(palace_path)
            if existing is not None:
                return existing
            # The other starter failed without bringing the daemon up; fall
            # through and spawn ourselves (we now hold the lock).

    for stale in (endpoint_path(palace_path), pid_path(palace_path)):
        try:
            stale.unlink()
        except OSError:
            pass
    cmd = [
        sys.executable,
        "-m",
        "mempalace.daemon",
        "serve",
        "--palace",
        palace_path,
    ]
    if backend:
        cmd.extend(["--backend", backend])
    env = os.environ.copy()
    if STATE_ROOT_ENV in os.environ:
        env[STATE_ROOT_ENV] = os.environ[STATE_ROOT_ENV]
    kwargs = _detached_kwargs(sd / "daemon.log")
    proc = None
    try:
        proc = subprocess.Popen(cmd, env=env, **kwargs)
    finally:
        log_fh = kwargs.get("stdout")
        if hasattr(log_fh, "close"):
            log_fh.close()
    try:
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise DaemonError(f"daemon exited during startup with code {proc.returncode}")
            try:
                client = DaemonClient(palace_path)
                client.health()
                return client
            except DaemonError as exc:
                last_error = exc
                time.sleep(0.1)
        raise DaemonError(f"daemon did not become ready: {last_error}")
    except BaseException:
        # Readiness failed — don't leak an orphaned detached child holding the
        # port, token, queue, and log handle. Kill and reap it before raising.
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait()
            except Exception:  # noqa: BLE001 - cleanup best-effort
                pass
        raise
    finally:
        if lock_fh is not None:
            try:
                lock_fh.close()
            except Exception:  # noqa: BLE001 - cleanup best-effort
                pass


def ensure_client(
    palace_path: str, *, backend: str | None = None, auto_start: bool = True
) -> DaemonClient:
    palace_path = canonical_palace_path(palace_path)
    client = get_client_if_running(palace_path)
    if client is not None:
        return client
    if not auto_start:
        raise DaemonError("daemon is not running")
    return start_daemon(palace_path, backend=backend)


def submit_job(
    kind: str,
    payload: dict[str, Any],
    *,
    palace_path: str | None = None,
    backend: str | None = None,
    dedupe_key: str | None = None,
    priority: int = 0,
    wait: bool = True,
    auto_start: bool = False,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
) -> dict[str, Any]:
    # Strictly opt-in: callers that want the daemon auto-started must say so
    # explicitly (the CLI --daemon path passes auto_start=True). The default
    # refuses to spawn a long-lived process on a background code path.
    resolved_palace = canonical_palace_path(palace_path or payload.get("palace_path"))
    payload = dict(payload)
    payload["palace_path"] = resolved_palace  # override, never trust client input
    if backend:
        payload["backend"] = backend
    client = ensure_client(resolved_palace, backend=backend, auto_start=auto_start)
    job = client.submit(kind, payload, dedupe_key=dedupe_key, priority=priority)
    if not wait:
        return job
    return client.wait(job["id"], timeout=timeout)


def stop_daemon(palace_path: str) -> bool:
    client = get_client_if_running(palace_path)
    if client is None:
        return False
    client.shutdown()
    return True


def _cmd_serve(args) -> None:
    run_server(args.palace, backend=args.backend, port=args.port)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MemPalace daemon internals")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--palace", required=True)
    serve.add_argument("--backend", default=None)
    serve.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    if args.command == "serve":
        _cmd_serve(args)


if __name__ == "__main__":
    main()
