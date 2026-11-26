"""Long-lived local daemon for queued MemPalace writes.

Daemon mode is strictly opt-in. The default CLI, hooks, and MCP paths still use
their direct execution behavior unless callers explicitly request daemon-backed
execution.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urlerror, request as urlrequest
from urllib.parse import parse_qs, urlparse

from ._env import env_float
from ._net import NoRedirectHandler as _NoRedirectHandler, bind_without_reverse_dns
from ._proc import (
    kernel32 as _kernel32,
    pid_alive as _pid_alive,
    safe_child_env as _safe_child_env,
    safe_module_argv as _safe_module_argv,
)
from .daemon_queue import (  # noqa: F401 - re-exported for existing callers
    EXIT_ALREADY_RUNNING,
    HOOK_PROBE_TIMEOUT,
    JOB_RETENTION_DAYS,
    LOCK_REFUSAL_ERROR_CLASS,
    MAX_ATTEMPTS,
    QUEUE_SCHEMA_VERSION,
    SPAWN_STAMP_NAME,
    STATE_ROOT_ENV,
    SUPERSEDED_ERROR_CLASS,
    TERMINAL_STATES,
    DaemonAlreadyRunning,
    DaemonError,
    DaemonWaitTimeout,
    Job,
    QueueStore,
    _chmod_dir_private,
    _chmod_private,
    _detached_kwargs,
    _now,
    _read_endpoint,
    _registered_pid,
    _write_private,
    canonical_palace_path,
    clear_start_refused,
    daemon_lock_held,
    daemon_registered_alive,
    daemon_singleton,
    endpoint_path,
    job_deferred_by_lock,
    job_to_dict,
    palace_key,
    pid_path,
    queue_path,
    record_start_refused,
    spawn_daemon_detached,
    state_dir,
    state_root,
)
from .fsio import read_published_bytes
from .palace import (
    MineAlreadyRunning,
    backend_requires_single_writer,
    mine_palace_lock,
    resolve_backend_name,
)
from .stored_text import dumps_internal

HOST = "127.0.0.1"
DEFAULT_WAIT_TIMEOUT = 60.0 * 60.0
_DEFAULT_LOCK_BACKOFF_SECONDS = 60.0


def _lock_defer_backoff_seconds() -> float:
    """Seconds a job refused the lock cools off before the worker re-claims it.

    The holder keeps the lock for as long as its write runs, which can be a long
    mine, so re-claiming at once would spin without progress. Anything that is
    not a positive, finite number yields the default: a typo must not crash the
    daemon at import; ``0`` or a negative value would make the cooldown expire
    instantly and spin the worker against a lock it cannot take; and ``inf``
    would park the job until the daemon restarts, silently dropping its work.
    """
    value = env_float("MEMPALACE_DAEMON_LOCK_BACKOFF_SECONDS", _DEFAULT_LOCK_BACKOFF_SECONDS)
    return value if value > 0 else _DEFAULT_LOCK_BACKOFF_SECONDS


# Override via env for operators; tests patch the module attribute directly.
LOCK_DEFER_BACKOFF_SECONDS = _lock_defer_backoff_seconds()
MAX_BODY_BYTES = 1 << 20  # 1 MiB cap on request bodies (auth-gated DoS guard)
SHUTDOWN_DRAIN_SECONDS = 10.0
# A mine whose child was killed or died is retried (it is idempotent per
# file), this many seconds times the attempt number later, until the job's
# MAX_ATTEMPTS claims are spent.
MINE_RETRY_BACKOFF_SECONDS = 30.0
# How long a hook-started daemon keeps retrying a writer lease another process
# holds, while jobs are queued, before it gives up and exits.
WRITER_LEASE_WAIT_SECONDS = 600.0
# Results of a mine cut short in its child rather than failed by the mine.
MINE_CHILD_CUT_SHORT = ("MineChildKilled", "MineChildDied")
try:
    import fcntl as _fcntl  # POSIX only; absent on Windows
except ImportError:  # pragma: no cover - Windows fallback
    _fcntl = None


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
        return read_published_bytes(token_path).decode("utf-8").strip()
    except OSError as exc:
        raise DaemonError(f"daemon token not found for {palace_path}") from exc


# Diary entries take their own lane so a long mine never holds them back.
DIARY_LANE_KINDS = ("diary_write",)
WORKER_LANES = ("main", "diary")


class _LaneWake:
    """One wake-up event per lane; ``set`` wakes them all."""

    def __init__(self, lanes):
        self.events = {lane: threading.Event() for lane in lanes}

    def set(self) -> None:
        for event in self.events.values():
            event.set()


class DaemonRuntime:
    """Queue workers for one palace.

    Two lanes drain the queue: ``main`` runs every kind except ``diary_write``,
    and ``diary`` runs only ``diary_write``. Both write through this one
    process, and every in-process write takes ``write_lock``. A mine job runs
    its compute in a child process (:mod:`mempalace.mine_supervisor`) whose
    storage calls the daemon serves under the same lock, one call at a time,
    so a diary entry lands between a mine's batches.
    """

    def __init__(self, palace_path: str, backend: str | None = None):
        self.palace_path = canonical_palace_path(palace_path)
        self.backend = backend
        self.store = QueueStore(queue_path(self.palace_path))
        self.shutdown_event = threading.Event()
        self.worker_wake = _LaneWake(WORKER_LANES)
        self.write_lock = threading.RLock()
        # lane -> id of the job that lane is running.
        self.active_jobs: dict[str, str] = {}
        # The mine child the main lane is supervising, if any.
        self.active_child = None
        # Command for the mine child; None runs ``mempalace.mine_child``.
        self.mine_child_argv: list[str] | None = None
        # Whether this process holds the palace write lock for its lifetime
        # (single-writer backends). A mine child counts it as held only then;
        # otherwise the child takes the lock itself, as any other miner would.
        self.holds_palace_lock = True
        # Set when the idle watch stopped the server (see run_server).
        self.idle_exited = False
        self.worker_threads: dict[str, threading.Thread] = {}
        # job_id -> monotonic deadline before which a lock-refused job is not
        # re-claimed. Each lane only touches its own jobs' entries.
        self._deferred_until: dict[str, float] = {}
        self._deferred_lock = threading.Lock()
        self._last_busy = time.monotonic()

    @property
    def active_job_id(self) -> str | None:
        return self.active_jobs.get("main") or self.active_jobs.get("diary")

    @property
    def worker_thread(self) -> threading.Thread | None:
        return self.worker_threads.get("main")

    def start_worker(self) -> threading.Thread:
        self.store.recover_running()
        # Bounded growth: drop terminal jobs older than the retention window
        # before bringing the worker up. Best-effort — a prune failure must not
        # block startup.
        try:
            self.store.prune_terminal()
        except Exception:  # noqa: BLE001 - retention is best-effort, never fatal
            pass
        lanes = {
            "main": {"exclude_kinds": DIARY_LANE_KINDS},
            "diary": {"kinds": DIARY_LANE_KINDS},
        }
        for lane, claim in lanes.items():
            thread = threading.Thread(
                target=self._worker_loop,
                args=(lane, claim),
                name=f"mempalace-daemon-worker-{lane}",
                daemon=True,
            )
            self.worker_threads[lane] = thread
            thread.start()
        return self.worker_threads["main"]

    def worker_alive(self) -> bool:
        return bool(self.worker_threads) and all(
            thread.is_alive() for thread in self.worker_threads.values()
        )

    def alive_workers(self) -> list[threading.Thread]:
        return [thread for thread in self.worker_threads.values() if thread.is_alive()]

    def idle_seconds(self) -> float:
        """How long nothing has been running or waiting in the queue."""
        now = time.monotonic()
        if self.active_jobs:
            self._last_busy = now
            return 0.0
        try:
            queued = self.store.counts().get("queued", 0)
        except Exception:  # noqa: BLE001 - an unreadable queue is not idleness
            queued = 1
        if queued:
            # A debounced job not yet due still keeps the daemon up to run it.
            self._last_busy = now
            return 0.0
        return now - self._last_busy

    def progress(self) -> dict | None:
        child = self.active_child
        return child.progress() if child is not None else None

    def _safe_finish(self, job_id: str, *, state: str, result: dict, error: dict | None) -> None:
        try:
            # only_if_running: if shutdown already cancelled this job, don't
            # resurrect it. A finish failure must not kill the worker regardless.
            self.store.finish(job_id, state=state, result=result, error=error, only_if_running=True)
        except Exception:  # noqa: BLE001 - a finish failure must not kill the worker
            pass

    def _safe_defer(
        self, job_id: str, *, claimed_started_at: str | None = None, error: dict | None = None
    ) -> None:
        try:
            self.store.defer(job_id, claimed_started_at=claimed_started_at, error=error)
        except Exception:  # noqa: BLE001 - a defer failure must not kill the worker
            # The job stays 'running', so recover_running normally re-queues it
            # on the next start and the work is still held -- mirroring
            # _safe_finish's swallow. (recover_running does not refund the
            # attempt, so repeated defer failures across MAX_ATTEMPTS restarts
            # would eventually dead-letter it.)
            pass

    def _safe_retry(
        self,
        job: Job,
        *,
        delay_seconds: float,
        error: dict,
    ) -> bool:
        try:
            return self.store.retry(
                job.id,
                claimed_started_at=job.started_at,
                delay_seconds=delay_seconds,
                error=error,
            )
        except Exception:  # noqa: BLE001 - a retry failure must not kill the worker
            return False

    def _cooling_job_ids(self) -> set[str]:
        """Ids of jobs still inside their post-refusal cooldown.

        Pruned on every read, so the map tracks only jobs the worker is actually
        holding back and cannot grow without bound.
        """
        now = time.monotonic()
        with self._deferred_lock:
            self._deferred_until = {
                job_id: until for job_id, until in self._deferred_until.items() if until > now
            }
            return set(self._deferred_until)

    def _run_job(self, job: Job, payload: dict) -> dict:
        from .config import MempalaceConfig
        from .service import execute_job

        config = MempalaceConfig()
        # Third-party adapters receive a writable KnowledgeGraph and can store
        # media outside the collection API. They need the in-process writer.
        if job.kind == "mine" and config.daemon_mine_child and not payload.get("source_adapter"):
            from .mine_supervisor import run_mine_in_child

            def _attach(child) -> None:
                self.active_child = child

            try:
                return run_mine_in_child(
                    payload,
                    palace_path=self.palace_path,
                    write_lock=self.write_lock,
                    threads=config.daemon_mine_threads,
                    on_start=_attach,
                    argv=self.mine_child_argv,
                    stall_seconds=config.daemon_mine_stall_seconds,
                    lock_held=self.holds_palace_lock,
                )
            finally:
                self.active_child = None
        with self.write_lock:
            return execute_job(job.kind, payload)

    def _worker_loop(self, lane: str = "main", claim: dict | None = None) -> None:
        claim = claim or {}
        while not self.shutdown_event.is_set():
            try:
                job = self.store.claim_next(exclude=self._cooling_job_ids(), **claim)
            except Exception:  # noqa: BLE001 - sqlite/disk errors must not kill the worker
                self.shutdown_event.wait(1.0)
                continue
            if job is None:
                wake = self.worker_wake.events[lane]
                wake.wait(0.5)
                wake.clear()
                continue
            self.active_jobs[lane] = job.id
            try:
                payload = dict(job.payload)
                # Override, never trust the client: an authenticated request for
                # palace A must not be able to retarget the daemon at palace B.
                payload["palace_path"] = self.palace_path
                if self.backend:
                    payload["backend"] = self.backend
                result = self._run_job(job, payload)
                ok = bool(result.get("success", True))
                if not ok and result.get("error_class") == LOCK_REFUSAL_ERROR_CLASS:
                    # Refused the palace write lock: no drawer was filed, so this
                    # is a deferral, not a failure. Dead-lettering it
                    # here would silently drop the work -- only hook-submitted
                    # kinds get re-emitted, and the hook is not a retry
                    # mechanism.
                    self._safe_defer(
                        job.id,
                        claimed_started_at=job.started_at,
                        error={
                            "error_class": LOCK_REFUSAL_ERROR_CLASS,
                            "message": result.get(
                                "error", "palace write lock held by another process"
                            ),
                        },
                    )
                    # Cool the job off in the map instead of sleeping here. The
                    # holder may outlive any wait we could pick -- it keeps the
                    # lock until its write finishes, and that write can be a
                    # long mine. Blocking would stall every unrelated job behind
                    # a lock that has nothing to do with them, and a job merely
                    # queued behind this one would never be claimed, so it would
                    # never get the refusal marker its caller waits on, and that
                    # caller would hang.
                    with self._deferred_lock:
                        self._deferred_until[job.id] = time.monotonic() + LOCK_DEFER_BACKOFF_SECONDS
                    continue
                if (
                    not ok
                    and job.kind == "mine"
                    and result.get("error_class") in MINE_CHILD_CUT_SHORT
                    and job.attempts < MAX_ATTEMPTS
                    and self._safe_retry(
                        job,
                        delay_seconds=MINE_RETRY_BACKOFF_SECONDS * job.attempts,
                        error={
                            "error_class": result["error_class"],
                            "message": result.get("error", "mine child cut short"),
                        },
                    )
                ):
                    # Writes the child asked for are whole and stay; the rerun
                    # redoes the files it did not finish. A newer queued run
                    # of the same source supersedes this one instead.
                    continue
                state = "succeeded" if ok else "failed"
                error = None
                if not ok:
                    error = {"message": result.get("error", "job failed")}
                    if result.get("error_class"):
                        error["error_class"] = result["error_class"]
                self._safe_finish(job.id, state=state, result=result, error=error)
            except (Exception, SystemExit) as exc:
                # SystemExit is BaseException, not Exception — catching it here is
                # deliberate. Without it, a sys.exit() in a dependency would slip
                # past `except Exception`, kill this worker thread, leave the job
                # stuck in 'running' forever, and stall every later job while
                # /health keeps reporting ok.
                self._safe_finish(
                    job.id,
                    state="failed",
                    result={"success": False, "exit_code": 1},
                    error={"error_class": type(exc).__name__, "message": str(exc)},
                )
            finally:
                self.active_jobs.pop(lane, None)
                self._last_busy = time.monotonic()


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = dumps_internal(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)
    handler.close_connection = True


def _restore_server_process_state(previous_env: dict[str, str | None], previous_umask: int) -> None:
    for key, value in previous_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    os.umask(previous_umask)


def _close_writer_lease_after_worker(
    writer_lease: contextlib.ExitStack,
    worker: "threading.Thread | list[threading.Thread]",
) -> None:
    """Retain writer ownership until a timed-out daemon worker really exits.

    The normal daemon process may terminate first, in which case the operating
    system releases the file lock. When ``run_server`` is embedded in a larger
    process, this reaper prevents the server thread from exposing the palace to
    another writer while the old worker is still finishing a mutation.
    """
    workers = worker if isinstance(worker, list) else [worker]

    def _wait_and_close() -> None:
        for thread in workers:
            thread.join()
        writer_lease.close()

    threading.Thread(
        target=_wait_and_close,
        name="mempalace-daemon-writer-lease-reaper",
        daemon=True,
    ).start()


def _close_or_defer_writer_lease(
    writer_lease: contextlib.ExitStack,
    runtime: "DaemonRuntime | None",
) -> None:
    workers = runtime.alive_workers() if runtime is not None else []
    if workers:
        _close_writer_lease_after_worker(writer_lease, workers)
    else:
        writer_lease.close()


# --- Foreground/lifecycle logging -------------------------------------------
#
# run_server (both the foreground in-process path and the detached ``serve``
# child) emitted no structured output of its own: the HTTP handler's
# ``log_message`` was a deliberate no-op, so a supervisor capturing the daemon
# via stdout/stderr (launchd StandardOutPath/StandardErrorPath) saw an empty
# file for the life of a healthy run.
#
# Stream contract for the daemon's own records:
#   INFO  -> stdout   lifecycle lines (startup/shutdown) + HTTP access lines
#   ERROR -> stderr   HTTP error lines (http.server.log_error)
# The background path is unaffected: its stdout/stderr already go to the
# per-daemon ``daemon.log`` via ``_detached_kwargs`` — this only adds the lines
# that were previously missing, not a change of destination.
_LIFECYCLE_LOGGER = logging.getLogger("mempalace.daemon")
_LIFECYCLE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_lifecycle_logging_ready = False


class _BelowLevel(logging.Filter):
    """Reject records whose level is at or above ``max_level``.

    ``Handler.setLevel`` is a *floor*, not a range, so it cannot express
    "INFO+WARNING only" on the stdout handler: an ERROR record (levelno 40)
    would pass the INFO (20) floor and be emitted by both handlers.
    ``StreamHandler.emit`` consults ``self.filters`` after the level check,
    so a handler-level filter is the minimal way to bound the stream from
    above and keep ERROR/CRITICAL exclusively on stderr.
    """

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.max_level


def _ensure_lifecycle_logging() -> None:
    """Attach stdout/stderr handlers so daemon lifecycle + HTTP lines are emitted.

    run_server has no logging configuration of its own, so without this the
    daemon's INFO/ERROR records would fall through to Python's default
    ``lastResort`` handler (WARNING+ to stderr only) and the INFO access and
    lifecycle lines a ``StandardOutPath`` capture needs would be dropped.

    ``propagate`` is disabled so the records are not re-emitted by an ancestor
    logger the embedding application may also configure. Idempotent and
    thread-safe enough for the single-threaded call sites (foreground
    start_daemon and the detached child), and reset-able from tests via
    clearing ``handlers`` and flipping ``_lifecycle_logging_ready``.
    """
    global _lifecycle_logging_ready
    if _lifecycle_logging_ready:
        return

    formatter = logging.Formatter(_LIFECYCLE_FORMAT)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    # ``StreamHandler.emit`` consults ``self.filters`` after the level check,
    # so a handler-level filter (not a logger filter) bounds the stream from
    # above: ERROR/CRITICAL pass the INFO *floor* setLevel above but are
    # rejected here — the stated contract is ERROR+ → stderr ONLY (the
    # background path merges both fds into one daemon.log, where a
    # level-floor alone would write every ERROR twice).
    stdout_handler.addFilter(_BelowLevel(logging.ERROR))
    stdout_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(formatter)

    # Replace (rather than append) so a re-init after a test reset doesn't
    # stack duplicate handlers.
    _LIFECYCLE_LOGGER.handlers = [stdout_handler, stderr_handler]
    _LIFECYCLE_LOGGER.setLevel(logging.INFO)
    _LIFECYCLE_LOGGER.propagate = False
    _lifecycle_logging_ready = True


def _resolve_idle_exit_minutes(default: float | None) -> float:
    try:
        from .config import MempalaceConfig

        configured = MempalaceConfig().daemon_idle_exit_minutes
    except Exception:  # noqa: BLE001 - a bad config must not stop the daemon
        configured = None
    if configured is not None:
        return configured
    return default or 0.0


def _start_idle_exit_watch(runtime: "DaemonRuntime", httpd, idle_minutes: float) -> None:
    """Stop serving once the queue has been empty for ``idle_minutes``."""
    limit = idle_minutes * 60.0
    tick = max(0.05, min(10.0, limit / 4))

    def watch() -> None:
        while not runtime.shutdown_event.wait(tick):
            if runtime.idle_seconds() >= limit:
                _LIFECYCLE_LOGGER.info("idle for %g min with an empty queue; exiting", idle_minutes)
                runtime.idle_exited = True
                runtime.shutdown_event.set()
                httpd.shutdown()
                return

    threading.Thread(target=watch, name="mempalace-daemon-idle-exit", daemon=True).start()


def run_server(  # noqa: C901
    palace_path: str,
    *,
    backend: str | None = None,
    port: int = 0,
    idle_exit_minutes: float | None = None,
) -> None:
    """Serve the palace's queue until shutdown.

    ``idle_exit_minutes`` is the default idle exit when config sets none
    (``daemon.idle_exit_minutes``); ``None`` or ``0`` keeps serving.
    """
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
    runtime = None
    writer_lease = contextlib.ExitStack()
    try:
        # One daemon per palace, whoever started it (a hook's detached spawn,
        # ``daemon start``, a service manager). Taken first, before the palace
        # is touched, and held for the life of the process.
        if not writer_lease.enter_context(daemon_singleton(palace_path)):
            raise DaemonAlreadyRunning(f"a daemon is already running for {palace_path!r}")
        resolved_backend = resolve_backend_name(palace_path, explicit=backend)
        if backend_requires_single_writer(resolved_backend):
            delay = 1.0
            lease_deadline = time.monotonic() + WRITER_LEASE_WAIT_SECONDS
            while True:
                try:
                    writer_lease.enter_context(mine_palace_lock(palace_path))
                    break
                except MineAlreadyRunning as exc:
                    message = (
                        "writable daemon startup refused: another writer owns "
                        f"local backend {resolved_backend!r} for {palace_path!r}; "
                        "stop the existing writable MCP/direct/daemon owner, or "
                        "route all writes through that owner"
                    )
                    # Hooks read this and stop starting daemons that would only
                    # refuse again; ``prefer`` then takes its direct path.
                    record_start_refused(palace_path, message)
                    # A hook has already committed its job. Retain the daemon
                    # singleton and retry the writer lease without another hook,
                    # for a bounded time: a writer that never lets go must not
                    # leave an unregistered daemon holding the singleton forever.
                    # The job stays queued for the next daemon.
                    if (
                        idle_exit_minutes is None
                        or time.monotonic() >= lease_deadline
                        or not QueueStore(queue_path(palace_path)).counts().get("queued")
                    ):
                        raise DaemonError(message) from exc
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
        holds_palace_lock = backend_requires_single_writer(resolved_backend)
        clear_start_refused(palace_path)

        token = ensure_token(palace_path)
        # Backend resolution above is only the ownership decision. Preserve
        # the caller's explicit/implicit distinction in queued payloads:
        # DaemonRuntime historically injects a backend only when one was
        # explicitly selected.
        runtime = DaemonRuntime(palace_path, backend=backend)
        runtime.holds_palace_lock = holds_palace_lock
    except BaseException:
        writer_lease.close()
        _restore_server_process_state(previous_env, prev_umask)
        raise

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 10

        def log_message(self, fmt, *args):
            # Route access lines to the daemon's stdout handler (INFO); the
            # previous no-op made a foreground/serve daemon completely silent.
            _LIFECYCLE_LOGGER.info(fmt, *args)

        def log_error(self, fmt, *args):
            # Same routing but at ERROR level, so these are the "error lines"
            # that land in launchd's StandardErrorPath.
            _LIFECYCLE_LOGGER.error(fmt, *args)

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
                        "active_jobs": dict(runtime.active_jobs),
                        "mine_progress": runtime.progress(),
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
                        settle_seconds=float(body.get("settle_seconds") or 0.0),
                        max_wait_seconds=(
                            None
                            if body.get("max_wait_seconds") is None
                            else float(body["max_wait_seconds"])
                        ),
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
            # Not HTTPServer's bind: that one resolves 127.0.0.1 back to a name,
            # and on a host with slow or absent reverse DNS it blocked daemon
            # startup for ~30s, which looked exactly like the daemon never
            # coming up.
            bind_without_reverse_dns(self)

    # The owner-only umask set above (before DaemonRuntime built the queue DB)
    # covers every file this process creates — queue.sqlite3, its WAL/SHM
    # sidecars, and any future artifact — and is restored in the finally below.
    # Ensure the daemon's own lifecycle lines actually reach stdout/stderr:
    # without this a foreground or ``serve`` daemon was silent to any
    # supervisor that captures those streams.
    _ensure_lifecycle_logging()
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
            _LIFECYCLE_LOGGER.info(
                "daemon listening on %s:%s (pid=%s palace=%s)",
                HOST,
                actual_port,
                os.getpid(),
                palace_path,
            )
            runtime.start_worker()
            idle_minutes = _resolve_idle_exit_minutes(idle_exit_minutes)
            if idle_minutes > 0:
                _start_idle_exit_watch(runtime, httpd, idle_minutes)
            try:
                httpd.serve_forever(poll_interval=0.5)
            finally:
                _LIFECYCLE_LOGGER.info("daemon shutting down")
                _drain_and_cleanup(runtime, palace_path, previous_env)
    finally:
        _close_or_defer_writer_lease(writer_lease, runtime)
        _restore_server_process_state(previous_env, prev_umask)
        if runtime is not None and runtime.idle_exited:
            _hand_off_late_work(runtime, palace_path, backend)


def _hand_off_late_work(runtime: "DaemonRuntime", palace_path: str, backend: str | None) -> None:
    """Start a successor if a job was queued while this idle daemon exited.

    A hook that enqueued after the idle check but before the registration was
    removed saw this daemon alive and started none. By now the registration
    and the lifetime lock are gone, so a hook enqueuing from here on starts
    its own; this check covers the jobs that arrived before.
    """
    try:
        queued = runtime.store.counts().get("queued", 0)
    except Exception:  # noqa: BLE001 - best-effort; the next hook starts one anyway
        return
    if queued:
        _LIFECYCLE_LOGGER.info("%d job(s) arrived while exiting; starting a successor", queued)
        try:
            spawn_daemon_detached(palace_path, backend=backend, ignore_throttle=True)
        except Exception:  # noqa: BLE001 - the jobs stay queued for the next hook
            pass


def _drain_and_cleanup(
    runtime: "DaemonRuntime", palace_path: str, previous_env: dict[str, str | None]
) -> None:
    """Drain the active job, then tear down server-side state.

    Killing a daemon thread mid-write (mid mine upsert, mid irreversible sync
    DELETE) violates incremental-only. Give the worker a bounded window to
    finish, then mark whatever is still running as cancelled so recover_running
    won't blindly re-run it on the next start (which would duplicate verbatim
    content). The one exception is a mine run in a child, which is re-queued
    (see below). Finally restore the env vars run_server mutated.
    """
    runtime.shutdown_event.set()
    child = runtime.active_child
    if child is not None:
        # A mine child only computes; every write it asked for already ran
        # here, whole. Stopping it first loses no stored data -- the next mine
        # resumes where it stopped -- and frees the main lane at once instead
        # of serving the child's writes through the whole drain window.
        # The mine goes back to the queue for the next daemon: a
        # file-idempotent mine is safe to redo. The interrupted run still
        # counts as an attempt, so a mine that ``daemon stop`` keeps cutting
        # short is dead-lettered after MAX_ATTEMPTS rather than restarted by
        # every later daemon; the next hook for its source queues a fresh one.
        # A newer queued run of the source supersedes it.
        main_job = runtime.active_jobs.get("main")
        if main_job:
            try:
                if runtime.store.get(main_job).attempts < MAX_ATTEMPTS:
                    runtime.store.retry(
                        main_job,
                        claimed_started_at=None,
                        error={"message": "interrupted by daemon shutdown; re-queued"},
                    )
            except Exception:  # noqa: BLE001 - recover_running re-queues it at next start
                pass
        child.kill("daemon shutdown")
    deadline = time.monotonic() + SHUTDOWN_DRAIN_SECONDS
    for worker in list(runtime.worker_threads.values()):
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
    for active in list(runtime.active_jobs.values()):
        runtime._safe_finish(
            active,
            state="cancelled",
            result={"success": False, "exit_code": 1},
            error={"message": "cancelled by daemon shutdown"},
        )
    for stale in (
        state_dir(palace_path) / SPAWN_STAMP_NAME,
        endpoint_path(palace_path),
        pid_path(palace_path),
    ):
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
        # It follows no redirect either: the daemon token travels only to
        # the daemon's own loopback endpoint.
        self._opener = urlrequest.build_opener(urlrequest.ProxyHandler({}), _NoRedirectHandler)

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
        data = None if body is None else dumps_internal(body).encode("utf-8")
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
        settle_seconds: float = 0.0,
        max_wait_seconds: float | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": kind,
            "payload": payload,
            "dedupe_key": dedupe_key,
            "priority": priority,
        }
        # Only sent when set, so an older daemon sees the request it always did.
        if settle_seconds:
            body["settle_seconds"] = settle_seconds
        if max_wait_seconds is not None:
            body["max_wait_seconds"] = max_wait_seconds
        return self.request("POST", "/jobs", body)["job"]

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self.request("GET", f"/jobs/{job_id}")["job"]

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.request("GET", f"/jobs?limit={int(limit)}")["jobs"]

    def wait(
        self,
        job_id: str,
        *,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        stop_on_lock_deferral: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if job["state"] in TERMINAL_STATES:
                return job
            # A job parked behind the palace lock never becomes terminal on its
            # own, so an interactive caller must be able to stop here instead of
            # waiting out the holder. Background callers keep the old
            # behaviour and simply wait.
            if stop_on_lock_deferral and job_deferred_by_lock(job):
                return job
            if time.monotonic() >= deadline:
                raise DaemonWaitTimeout(job_id, job["state"])
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


def _process_start_time_windows(pid: int) -> float | None:
    """Creation time of ``pid`` as a Unix timestamp via ``GetProcessTimes``."""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = _kernel32()
    filetime_p = ctypes.POINTER(wintypes.FILETIME)
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        filetime_p,
        filetime_p,
        filetime_p,
        filetime_p,
    )

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        # FILETIME counts 100 ns intervals since 1601-01-01.
        return ticks / 10_000_000 - 11_644_473_600
    finally:
        kernel32.CloseHandle(handle)


def _process_start_time_linux(pid: int) -> float | None:
    """Start time of ``pid`` from ``/proc`` (boot time plus clock ticks)."""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        # The command name can contain spaces and parentheses; fields resume
        # after the last ")". starttime is field 22, index 19 from "state".
        fields = stat[stat.rindex(")") + 2 :].split()
        start_ticks = int(fields[19])
        boot = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines()
            if line.startswith("btime ")
        )
        return boot + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _process_start_time_ps(pid: int) -> float | None:
    """Start time of ``pid`` from ``ps -o lstart=`` (macOS and other POSIX)."""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C"},
            check=False,
        ).stdout.strip()
        if not out:
            return None
        return time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _process_start_time(pid: int) -> float | None:
    """When ``pid`` started, as a Unix timestamp, or None when it cannot be read.

    Used to tell a live registered daemon from an unrelated process that has
    since been given the same pid. ``psutil`` is only a development
    dependency, so it is used when present and the platform source otherwise.
    """
    if pid <= 0:
        return None
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return float(psutil.Process(int(pid)).create_time())
        except Exception:
            return None
    if os.name == "nt":
        try:
            return _process_start_time_windows(pid)
        except OSError:
            return None
    if Path("/proc/self/stat").exists():
        return _process_start_time_linux(pid)
    return _process_start_time_ps(pid)


# A process that started after the registration was written cannot be the
# daemon that wrote it. The slack absorbs timestamp granularity between the
# process start clock and file modification times.
_REGISTRATION_START_SLACK_SECONDS = 2.0


def _registration_state(palace_path: str) -> tuple[str, int | None]:
    """Classify the daemon registration for ``palace_path``.

    Returns ``("none", None)`` when nothing is registered, ``("stale", pid)``
    when the registered pid is dead or now belongs to a process that started
    after the registration was written (pid reuse after a crash), and
    ``("live", pid)`` when the pid is alive and is, or may be, the daemon that
    registered. An unreadable start time counts as live: refusing is
    recoverable, discarding a live owner's registration is not.
    """
    pid = _registered_pid(palace_path)
    if pid is None:
        return "none", None
    if not _pid_alive(pid):
        return "stale", pid
    started = _process_start_time(pid)
    if started is None:
        return "live", pid
    written = []
    for path in (endpoint_path(palace_path), pid_path(palace_path)):
        try:
            written.append(path.stat().st_mtime)
        except OSError:
            pass
    if written and started > max(written) + _REGISTRATION_START_SLACK_SECONDS:
        return "stale", pid
    return "live", pid


def _refuse_live_registration(palace_path: str) -> None:
    """Raise when a registered daemon is alive but did not answer the probe.

    Its process may still own the palace (the lock, or for backends without a
    single-writer lease, the endpoint), so a second server must not start and
    the registration must stay for when it answers again.
    """
    state, registered_pid = _registration_state(palace_path)
    if state == "live":
        raise DaemonError(
            f"daemon pid {registered_pid} is running but did not answer the "
            "health probe (busy or wedged); not starting a second one. "
            f"Retry later, or stop that process ({_stop_hint(registered_pid)}) "
            "and start again."
        )


@contextlib.contextmanager
def _no_start_in_flight(palace_path: str):
    """Yield True unless a ``daemon start`` holds the spawn lock right now.

    ``start_daemon`` holds ``start.lock`` from its check until the new daemon
    answers, and the new daemon writes its registration inside that window.
    Taking the lock without waiting keeps ``stop_daemon`` from removing a
    registration a concurrent start has just published. Windows has no spawn
    lock; there ``_remove_registration(expected_pid=...)`` narrows the race.
    """
    if _fcntl is None:
        yield True
        return
    try:
        lock_fh = open(state_dir(palace_path) / "start.lock", "a")
    except OSError:
        # No state directory: nothing can be registered or starting.
        yield True
        return
    with lock_fh:
        try:
            _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_UN)


def _remove_registration(palace_path: str, *, expected_pid: int | None = None) -> None:
    """Remove the endpoint and pid files.

    With ``expected_pid``, only when they still name that pid, so a
    registration published after the caller classified the old one survives.
    """
    if expected_pid is not None and _registered_pid(palace_path) != expected_pid:
        return
    for stale in (
        state_dir(palace_path) / SPAWN_STAMP_NAME,
        endpoint_path(palace_path),
        pid_path(palace_path),
    ):
        try:
            stale.unlink()
        except OSError:
            pass


def _stop_hint(pid: int) -> str:
    return f"taskkill /PID {pid} /F" if os.name == "nt" else f"kill {pid}"


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
        # The same guard as the spawn path below: a foreground server must not
        # start beside a registered daemon that is alive but not answering.
        _refuse_live_registration(palace_path)
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

    # The health probe above can fail while the registered daemon is alive but
    # busy (a long mine) or wedged. Its process still owns the palace lock, so
    # a replacement would die at startup -- and unlinking the registration
    # first would leave the live owner unreachable. Refuse instead of
    # spawning; ``daemon stop`` is the explicit way to replace it.
    # A pid that is dead, or that now belongs to a process started after the
    # registration was written (pid reuse after a crash), is stale and is
    # replaced below.
    _refuse_live_registration(palace_path)

    # A daemon a hook started may have taken the lifetime lock since the
    # probe above and be about to register, or have just registered. Its
    # registration is not stale; the server spawned below loses the lock and
    # exits 0, and the loop waits for that daemon to answer instead.
    if not daemon_lock_held(palace_path):
        _remove_registration(palace_path)
    # Started from the caller's cwd, which may be an untrusted checkout: run
    # the installed module, never one planted in the working directory.
    cmd = _safe_module_argv("mempalace.daemon", "serve", "--palace", palace_path)
    if backend:
        cmd.extend(["--backend", backend])
    env = _safe_child_env()
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
            if proc.poll() is not None and proc.returncode != EXIT_ALREADY_RUNNING:
                raise DaemonError(f"daemon exited during startup with code {proc.returncode}")
            # A clean exit means another daemon (say, one a hook spawned) won
            # the lifetime lock: keep polling until that one answers.
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
    settle_seconds: float = 0.0,
    max_wait_seconds: float | None = None,
    wait: bool = True,
    auto_start: bool = False,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    stop_on_lock_deferral: bool = False,
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
    job = client.submit(
        kind,
        payload,
        dedupe_key=dedupe_key,
        priority=priority,
        settle_seconds=settle_seconds,
        max_wait_seconds=max_wait_seconds,
    )
    if not wait:
        return job
    return client.wait(job["id"], timeout=timeout, stop_on_lock_deferral=stop_on_lock_deferral)


def stop_daemon(palace_path: str) -> bool:
    """Ask the registered daemon to stop. True when a running daemon was asked.

    When the health probe fails, the registration decides what happens: a dead
    pid, or a pid reused by an unrelated process, is a stale registration and
    is removed so the next ``daemon start`` works. A live daemon that does not
    answer cannot be asked to stop over HTTP, so its registration is left in
    place and the error names the process to stop instead.
    """
    client = get_client_if_running(palace_path)
    if client is not None:
        client.shutdown()
        return True
    state, pid = _registration_state(palace_path)
    if state == "live":
        raise DaemonError(
            f"daemon pid {pid} is registered and running but did not answer the "
            "health probe (busy or wedged), so it cannot be asked to stop. Wait "
            f"for it, or stop that process ({_stop_hint(pid)}); its registration "
            "is kept until it exits."
        )
    if state == "stale":
        # A start in flight replaces the stale files itself; removing them now
        # could delete the registration it is about to publish.
        with _no_start_in_flight(palace_path) as free:
            if free:
                _remove_registration(palace_path, expected_pid=pid)
    return False


def _cmd_serve(args) -> None:
    try:
        run_server(
            args.palace,
            backend=args.backend,
            port=args.port,
            idle_exit_minutes=args.idle_exit_default,
        )
    except DaemonAlreadyRunning as exc:
        # Losing the start race is the expected outcome of two hooks spawning
        # at once, not an error: the winner serves the queue.
        _ensure_lifecycle_logging()
        _LIFECYCLE_LOGGER.info("not starting: %s", exc)
        sys.exit(EXIT_ALREADY_RUNNING)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MemPalace daemon internals")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--palace", required=True)
    serve.add_argument("--backend", default=None)
    serve.add_argument("--port", type=int, default=0)
    # Idle exit when config sets none; hooks pass 30 for the daemon they start.
    serve.add_argument("--idle-exit-default", type=float, default=None)
    args = parser.parse_args(argv)
    if args.command == "serve":
        _cmd_serve(args)


if __name__ == "__main__":
    main()
