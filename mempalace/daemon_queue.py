"""The daemon's durable job queue and on-disk registration, stdlib-only.

Hooks enqueue into this queue and start the daemon from here, so this module
must stay cheap to import: it may not import ``chromadb``, ``numpy``,
``onnxruntime`` or anything under ``mempalace.palace`` / ``mempalace.backends``
(``tests/test_daemon_autostart.py`` checks it). ``mempalace.daemon``
re-exports every public name for existing callers.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import paths
from ._env import env_int
from .config import MempalaceConfig
from .fsio import atomic_write_text, read_published_bytes
from .timeutil import utc_now, utc_now_iso

STATE_ROOT_ENV = "MEMPALACE_DAEMON_STATE_ROOT"
# Liveness-probe timeout for the hook "is a daemon already running?" precheck.
# 0.15s stays well under the 500ms hook budget: a wedged daemon must not
# consume the whole budget before the hook falls back. A healthy local daemon
# answers /health in single-digit ms, so this rarely false-negatives.
HOOK_PROBE_TIMEOUT = 0.15
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
MAX_ATTEMPTS = 3
# ``error_class`` marking a job refused the per-palace write lock. The lock
# wraps the palace write itself, so a refusal means no drawer was filed and
# re-running cannot duplicate palace content. Distinct from a crash
# mid-execution, whose outcome is unknown -- see ``QueueStore.defer``.
LOCK_REFUSAL_ERROR_CLASS = "LockHeldByOtherProcess"
# Terminal jobs are kept for diagnostics then pruned so the queue DB (which
# holds verbatim payloads) doesn't grow without bound across a long-lived
# daemon. Override via env for operators who want a longer/shorter window.
JOB_RETENTION_DAYS = env_int("MEMPALACE_DAEMON_RETENTION_DAYS", 7)


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


class DaemonWaitTimeout(DaemonError):
    """Raised when a wait runs out before the job it waits on ends.

    The daemon keeps the job and still runs it in turn, so a caller that only
    waited to report the outcome must not count this as a failure. ``state`` is
    the job's last seen state, ``queued`` or ``running``.
    """

    def __init__(self, job_id: str, state: str):
        super().__init__(f"timed out waiting for job {job_id}")
        self.job_id = job_id
        self.state = state


def _now() -> str:
    return utc_now_iso()


def canonical_palace_path(path: str | None = None) -> str:
    return paths.canonical_palace_path(path or MempalaceConfig().palace_path)


def palace_key(palace_path: str) -> str:
    return paths.palace_key(canonical_palace_path(palace_path))


def state_root() -> Path:
    raw = os.environ.get(STATE_ROOT_ENV)
    if raw:
        return Path(raw).expanduser()
    return paths.state_path("daemon")


def state_dir(palace_path: str) -> Path:
    return state_root() / palace_key(palace_path)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)


def endpoint_path(palace_path: str) -> Path:
    return state_dir(palace_path) / "endpoint.json"


def pid_path(palace_path: str) -> Path:
    return state_dir(palace_path) / "pid"


def queue_path(palace_path: str) -> Path:
    return state_dir(palace_path) / "queue.sqlite3"


def _read_endpoint(palace_path: str) -> dict[str, Any]:
    try:
        return json.loads(read_published_bytes(endpoint_path(palace_path)).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise DaemonError("daemon endpoint not found") from exc


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
    # Epoch seconds (``QueueStore.clock``) before which the job is not claimed,
    # and the latest time coalescing may push ``run_after`` to. None on rows
    # written before the debounce columns existed: they run at once.
    run_after: float | None = None
    deadline: float | None = None


# Queue schema version, kept in ``PRAGMA user_version``.
#   0/1: ``idx_jobs_dedupe_active`` unique over state IN ('queued','running')
#   2:   ``idx_jobs_dedupe_queued`` unique over state = 'queued' only, plus the
#        ``run_after`` / ``deadline`` debounce columns.
QUEUE_SCHEMA_VERSION = 2
SUPERSEDED_ERROR_CLASS = "Superseded"


# Busy timeout for the one-time creation or migration of a queue file (see
# ``QueueStore._init_db``); steady-state calls use the caller's timeout.
INIT_BUSY_TIMEOUT_SECONDS = 2.0


class QueueStore:
    def __init__(self, path: Path, *, clock=time.time, timeout: float = 30.0):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Wall-clock epoch seconds. ``run_after`` is compared across processes
        # (the hook enqueues, the daemon claims), so a monotonic clock is out.
        # Injected by tests to drive the debounce deterministically.
        self.clock = clock
        self.timeout = timeout
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
        conn = sqlite3.connect(str(self.path), timeout=self.timeout)
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
        # Fast path for the hook: a queue already at the current schema needs
        # no DDL (WAL mode is persistent in the file), and each DDL statement
        # is a write transaction with its own fsync.
        if self.path.exists():
            try:
                with self._connect() as conn:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version == QUEUE_SCHEMA_VERSION:
                    self._harden_files()
                    return
            except sqlite3.Error:
                pass
        with self._connect() as conn:
            # Setup runs once per queue file, but every hook that fires before it
            # lands queues up behind it. That one-time wait gets more than a
            # hook's per-enqueue busy budget, so the first burst of hooks after
            # an install or upgrade does not fail.
            busy = max(self.timeout, INIT_BUSY_TIMEOUT_SECONDS)
            conn.execute(f"PRAGMA busy_timeout = {int(busy * 1000)}")
            # Two connections upgrading a new database to WAL can deadlock on
            # the journal-mode change itself. SQLite returns BUSY immediately
            # in that case, before its busy handler runs.
            for attempt in range(6):
                try:
                    conn.execute("PRAGMA journal_mode=WAL").fetchone()
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 5:
                        raise
                    time.sleep(0.005 * (attempt + 1))
            conn.execute("BEGIN IMMEDIATE")
            if int(conn.execute("PRAGMA user_version").fetchone()[0]) == QUEUE_SCHEMA_VERSION:
                # Another process finished the setup while this one waited.
                conn.commit()
                self._harden_files()
                return
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
            self._migrate(conn)
        # The queue DB holds verbatim payloads (diary text, source paths) — lock it
        # down to owner-only regardless of the invoking user's umask. The WAL/SHM
        # sidecars carry the same un-checkpointed payloads, so harden them too when
        # present (the daemon also runs under a 0o077 umask; this covers any
        # QueueStore opened outside that scope, e.g. the CLI `daemon jobs` path).
        self._harden_files()

    def _harden_files(self) -> None:
        _chmod_private(self.path)
        for sidecar_suffix in ("-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + sidecar_suffix)
            if sidecar.exists():
                _chmod_private(sidecar)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring an existing queue DB up to ``QUEUE_SCHEMA_VERSION`` in place.

        Additive only: columns are added, never dropped, and no row is
        rewritten, so a queue written by an older daemon keeps every job.

        The dedupe index narrows from ``state IN ('queued','running')`` to
        ``state = 'queued'``. That is what lets one running job and one pending
        rerun of the same source coexist: an event that lands while its source
        is being mined must queue a rerun, not vanish into the running job.
        Every existing row already satisfies the narrower constraint (it is
        implied by the old one), so building the new index cannot fail. The
        new index is built before the old one is dropped, so no instant exists
        without a uniqueness guard.
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name in ("run_after", "deadline"):
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} REAL")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe_queued "
            "ON jobs(dedupe_key) WHERE state = 'queued'"
        )
        conn.execute("DROP INDEX IF EXISTS idx_jobs_dedupe_active")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < QUEUE_SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {QUEUE_SCHEMA_VERSION}")

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
        cutoff = (utc_now() - timedelta(days=older_than_days)).isoformat(timespec="microseconds")
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
            rows = conn.execute(
                "SELECT id FROM jobs WHERE state = 'running' AND attempts < ?",
                (MAX_ATTEMPTS,),
            ).fetchall()
            requeued = 0
            for row in rows:
                if self._requeue_or_supersede(conn, row["id"]):
                    requeued += 1
            return requeued

    def _requeue_or_supersede(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        *,
        extra_where: str = "",
        extra_params: tuple = (),
        error_json: str | None = None,
        set_error: bool = False,
        refund_attempt: bool = False,
        run_after: float | None = None,
    ) -> bool:
        """Move a running job back to ``queued``, unless a rerun already waits.

        A running job and one pending rerun of the same dedupe key may coexist,
        so re-queuing the running row can collide with the queued one on
        ``idx_jobs_dedupe_queued``. The queued row was enqueued later and holds
        the newer payload, so it supersedes the running row: that row is
        closed as ``cancelled`` with ``Superseded``, and the source is still
        processed once, by the rerun. Returns True when the row was re-queued.

        ``run_after`` (epoch seconds) holds the re-queued row back until then;
        None leaves its schedule as it was.
        """
        attempts_sql = "MAX(attempts - 1, 0)" if refund_attempt else "attempts"
        try:
            cur = conn.execute(
                f"""
                UPDATE jobs
                SET state = 'queued', started_at = NULL,
                    attempts = {attempts_sql},
                    error_json = CASE WHEN ? THEN ? ELSE error_json END,
                    run_after = COALESCE(?, run_after)
                WHERE id = ? AND state = 'running' {extra_where}
                """,
                (int(set_error), error_json, run_after, job_id, *extra_params),
            )
            return cur.rowcount == 1
        except sqlite3.IntegrityError:
            conn.execute(
                f"""
                UPDATE jobs
                SET state = 'cancelled', finished_at = ?, error_json = ?
                WHERE id = ? AND state = 'running' {extra_where}
                """,
                (
                    _now(),
                    json.dumps(
                        {
                            "error_class": SUPERSEDED_ERROR_CLASS,
                            "message": "superseded by a newer queued job for the same source",
                        },
                        ensure_ascii=False,
                    ),
                    job_id,
                    *extra_params,
                ),
            )
            return False

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        priority: int = 0,
        settle_seconds: float = 0.0,
        max_wait_seconds: float | None = None,
    ) -> Job:
        """Queue a job, coalescing with a queued job of the same ``dedupe_key``.

        Coalescing is latest-wins: the queued row keeps its id and its place
        in line (``created_at``) but takes this call's payload, so the run that
        eventually happens sees the newest content. A job of the same key that
        is already *running* is left alone; this call queues the one pending
        rerun beside it instead of being swallowed by it.

        Debounce (the Syncthing ``fsWatcherDelayS``/``fsWatcherTimeoutS`` shape):

            deadline  = MIN(deadline, now + max_wait)
            run_after = MIN(deadline, now + settle)

        Each event pushes ``run_after`` out by ``settle`` so a burst collapses
        into one run, but never past ``deadline``, which only ever moves
        earlier -- a steady stream of events still runs within ``max_wait`` of
        the first, and an immediate trigger (``max_wait`` 0) is never delayed
        by a later debounced one. ``max_wait`` defaults to ``settle``.

        UPDATE-first, then INSERT: the UPDATE takes SQLite's write lock, so the
        sequence is atomic against another process enqueuing the same key, and
        it avoids ``INSERT ... ON CONFLICT`` on a partial index, which the
        SQLite bundled with older Pythons does not support.
        """
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        settle = max(0.0, float(settle_seconds or 0.0))
        max_wait = settle if max_wait_seconds is None else max(0.0, float(max_wait_seconds))
        with self._lock, self._connect() as conn:
            now = float(self.clock())
            if dedupe_key:
                row = self._coalesce(
                    conn, dedupe_key, kind, payload_json, priority, now, settle, max_wait
                )
                if row is not None:
                    return self._row_to_job(row)
            job_id = uuid.uuid4().hex
            deadline = now + max_wait
            run_after = min(deadline, now + settle)
            try:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        id, kind, payload_json, state, priority, dedupe_key, created_at,
                        attempts, run_after, deadline
                    ) VALUES (?, ?, ?, 'queued', ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        job_id,
                        kind,
                        payload_json,
                        int(priority),
                        dedupe_key,
                        _now(),
                        run_after,
                        deadline,
                    ),
                )
            except sqlite3.IntegrityError:
                # A queued row for this key appeared between our UPDATE and
                # INSERT (an older client that SELECTs first, or no write lock
                # held yet). The index is the source of truth: coalesce into it.
                if not dedupe_key:
                    raise
                row = self._coalesce(
                    conn, dedupe_key, kind, payload_json, priority, now, settle, max_wait
                )
                if row is None:
                    raise
                return self._row_to_job(row)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row)

    @staticmethod
    def _coalesce(
        conn: sqlite3.Connection,
        dedupe_key: str,
        kind: str,
        payload_json: str,
        priority: int,
        now: float,
        settle: float,
        max_wait: float,
    ):
        """Fold one enqueue into the queued row for ``dedupe_key``; None if absent."""
        cur = conn.execute(
            """
            UPDATE jobs
            SET kind = ?, payload_json = ?, priority = MAX(priority, ?),
                deadline = MIN(COALESCE(deadline, ?), ?),
                run_after = MIN(MIN(COALESCE(deadline, ?), ?), ?)
            WHERE dedupe_key = ? AND state = 'queued'
            """,
            (
                kind,
                payload_json,
                int(priority),
                now,
                now + max_wait,
                now,
                now + max_wait,
                now + settle,
                dedupe_key,
            ),
        )
        if cur.rowcount != 1:
            return None
        return conn.execute(
            "SELECT * FROM jobs WHERE dedupe_key = ? AND state = 'queued'", (dedupe_key,)
        ).fetchone()

    def next_run_after(self) -> float | None:
        """Earliest ``run_after`` among queued jobs, for the worker's sleep."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(COALESCE(run_after, 0)) AS t FROM jobs WHERE state = 'queued'"
            ).fetchone()
            return None if row is None or row["t"] is None else float(row["t"])

    def claim_next(
        self,
        *,
        exclude: set[str] | None = None,
        kinds: tuple[str, ...] | None = None,
        exclude_kinds: tuple[str, ...] | None = None,
    ) -> Job | None:
        # Atomic across processes: the UPDATE only fires if the row is still
        # 'queued'. If two daemon processes SELECT the same row, the first to
        # UPDATE it flips state to 'running' (rowcount=1); the second's UPDATE
        # matches 0 rows (WHERE state='queued' is now false) and we re-loop
        # instead of double-executing the job. The in-process RLock does not
        # protect against a second OS process — this guard does.
        #
        # ``exclude`` skips jobs still cooling off after a lock refusal, so the
        # ordering below cannot hand back the same refused job forever while
        # newer work waits behind it. The cooldown is the worker's, held in
        # memory rather than the schema (which has no migrations): a restart
        # just retries such a job at once, costing one refusal, never the work.
        #
        # The filter runs in Python rather than as ``id NOT IN (?, ?, ...)``:
        # that binds one host parameter per cooling job, and
        # SQLITE_MAX_VARIABLE_NUMBER defaults to 999 below SQLite 3.32 (we
        # support Python 3.9, whose bundled SQLite can predate it). Over the cap
        # the query raises into the worker's catch-all, which retries a second
        # later against a set that only ages out on the cooldown, so the queue
        # would stall in cooldown-long fits rather than drain. Fetching
        # ``len(exclude) + 1`` rows costs one parameter and is enough by
        # pigeonhole: at most ``len(exclude)`` of those rows can be excluded, so
        # the first one that survives the filter is the row a plain LIMIT 1 would
        # have returned.
        #
        # ``error_json`` is cleared because it describes the claim that just
        # ended: ``defer`` parks the refusal reason on a queued job, and a stale
        # one would outlive its cause -- reporting a lock refusal for a job that
        # later crashed, and leaking into recover_running's dead-letter (which
        # COALESCEs it) in place of ``MaxAttemptsExceeded``.
        #
        # A job is claimable once its debounce window (``run_after``) has
        # passed, and only while no job of the same dedupe key is running: the
        # pending rerun waits for the run it follows, so one source is never
        # processed twice at once. ``kinds`` restricts a lane (the diary lane
        # claims only ``diary_write``); ``exclude_kinds`` keeps the main lane off
        # them.
        exclude = exclude or set()
        kind_sql = ""
        kind_params: tuple = ()
        if kinds:
            kind_sql = f"AND kind IN ({', '.join('?' for _ in kinds)})"
            kind_params = tuple(kinds)
        if exclude_kinds:
            kind_sql += f" AND kind NOT IN ({', '.join('?' for _ in exclude_kinds)})"
            kind_params += tuple(exclude_kinds)
        with self._lock, self._connect() as conn:
            now = float(self.clock())
            # Only the ids: the rows being skipped are here to be discarded, and
            # jobs carry verbatim payloads, so selecting * would materialise a
            # cooling job's user content just to read its id past it.
            candidates = conn.execute(
                f"""
                SELECT id FROM jobs AS q
                WHERE state = 'queued'
                  AND COALESCE(run_after, 0) <= ?
                  {kind_sql}
                  AND (
                    dedupe_key IS NULL OR NOT EXISTS (
                      SELECT 1 FROM jobs AS r
                      WHERE r.dedupe_key = q.dedupe_key AND r.state = 'running'
                    )
                  )
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (now, *kind_params, len(exclude) + 1),
            ).fetchall()
            job_id = next((r["id"] for r in candidates if r["id"] not in exclude), None)
            if job_id is None:
                return None
            cur = conn.execute(
                f"""
                UPDATE jobs
                SET state = 'running', started_at = ?, attempts = attempts + 1,
                    error_json = NULL
                WHERE id = ? AND state = 'queued' AND COALESCE(run_after, 0) <= ?
                  {kind_sql}
                """,
                (_now(), job_id, now, *kind_params),
            )
            if cur.rowcount != 1:
                # Lost the race to another process — nothing to run this iteration.
                return None
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
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

    def defer(
        self,
        job_id: str,
        *,
        claimed_started_at: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> Job:
        """Return a job refused the palace write lock to ``queued``, unspent.

        ``mine_palace_lock`` wraps the palace write itself, so a refusal means
        no drawer was filed and re-running cannot duplicate palace content.
        That is what separates it from the failure ``MAX_ATTEMPTS`` guards -- a
        daemon that died mid-execution, whose outcome is unknown and whose blind
        retry would re-file verbatim content. Undoing ``claim_next``'s increment
        keeps a palace that stays locked from spending the retry budget of work
        that never landed.

        ``error`` records why the job went back to the queue, so a job parked
        behind a lock is distinguishable from one merely awaiting its turn.
        ``claim_next`` clears it on the next claim, so the reason never outlives
        the claim it describes.

        The ``state = 'running'`` guard mirrors ``finish(only_if_running=True)``:
        if shutdown already cancelled this job, deferring must not resurrect it.
        ``claimed_started_at`` narrows the update further, to the claim that was
        actually refused: if the row was re-queued and re-claimed in the window
        (recover_running on another daemon start), a late defer must not throw
        away that newer claim by re-queuing -- and refunding -- work it does not
        own. ``None`` skips the check.
        """
        with self._lock, self._connect() as conn:
            self._requeue_or_supersede(
                conn,
                job_id,
                extra_where="AND (? IS NULL OR started_at = ?)",
                extra_params=(claimed_started_at, claimed_started_at),
                error_json=json.dumps(error or {}, ensure_ascii=False) if error else None,
                set_error=True,
                refund_attempt=True,
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_job(row)

    def retry(
        self,
        job_id: str,
        *,
        claimed_started_at: str | None,
        delay_seconds: float = 0.0,
        error: dict[str, Any] | None = None,
    ) -> bool:
        """Put a running job back in the queue to run again after a delay.

        For work that is safe to redo -- a mine is idempotent per file -- whose
        run was cut short: its child was killed or died, or the daemon is
        shutting down. Like ``defer``, a newer queued rerun of the same key
        supersedes it, and the update applies only to the claim
        ``claimed_started_at`` names. The cut-short run keeps its attempt, so
        a job that keeps being cut short is dead-lettered at ``MAX_ATTEMPTS``.
        Returns True when the job was re-queued.
        """
        with self._lock, self._connect() as conn:
            return self._requeue_or_supersede(
                conn,
                job_id,
                extra_where="AND (? IS NULL OR started_at = ?)",
                extra_params=(claimed_started_at, claimed_started_at),
                error_json=json.dumps(error, ensure_ascii=False) if error else None,
                set_error=True,
                run_after=float(self.clock()) + max(0.0, delay_seconds),
            )

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
            run_after=row["run_after"],
            deadline=row["deadline"],
        )


def job_deferred_by_lock(job: dict[str, Any]) -> bool:
    """True when a job's last claim was refused the palace lock.

    Such a job is deferred, not failed: it went back to the queue and
    runs once the lock frees. That makes it non-terminal, so a caller blocking
    until ``TERMINAL_STATES`` would wait out the holder -- which can be a
    long-lived session. Interactive callers use this to report the parked job
    instead of stranding the terminal.

    Keyed on the reason ``defer`` records, which ``claim_next`` clears on the
    next claim: the answer is about the claim that just ended, not a live probe
    of the lock (the holder may already have exited during the backoff).
    """
    if job.get("state") != "queued":
        return False
    return (job.get("error") or {}).get("error_class") == LOCK_REFUSAL_ERROR_CLASS


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
        "run_after": job.run_after,
    }
    if include_payload:
        out["payload"] = job.payload
    return out


def _detached_kwargs(log_path: Path) -> dict[str, Any]:
    """Kwargs that spawn the daemon with a hidden console, detached from the CLI.

    Shares the Windows flag logic with ``hooks_cli._detached_popen_kwargs``
    (which takes no args and opens no log file). On Windows we use
    ``CREATE_NO_WINDOW`` rather than ``DETACHED_PROCESS``: the latter gives the
    child no console, so a console-subsystem grandchild later allocates a fresh
    *visible* window; ``CREATE_NO_WINDOW`` gives a hidden console that
    descendants inherit instead. Surviving the launching terminal is carried by
    ``CREATE_BREAKAWAY_FROM_JOB`` (escapes the parent Job Object's kill-on-close)
    plus the daemon never being attached to that console -- not by the console
    flag -- while ``CREATE_NEW_PROCESS_GROUP`` isolates Ctrl-C/Ctrl-Break routing.
    ``CREATE_NO_WINDOW`` is ignored when OR'd with ``DETACHED_PROCESS``, so this
    replaces that flag rather than adding it. ``stdin=DEVNULL`` and stdout/stderr
    redirected to the log keep the parent from hanging on inherited pipes.
    """
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
        for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP", "CREATE_BREAKAWAY_FROM_JOB"):
            flags |= getattr(subprocess, name, 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _registered_pid(palace_path: str) -> int | None:
    """Pid recorded by the last daemon that registered for ``palace_path``.

    Reads ``endpoint.json`` first (written together with the pid file by
    ``run_server``), then the bare pid file, so a partially written
    registration still yields the pid. Returns None when nothing is registered
    or the value is not an int.
    """
    try:
        pid = _read_endpoint(palace_path).get("pid")
    except DaemonError:
        pid = None
    if pid is None:
        try:
            pid = pid_path(palace_path).read_text(encoding="utf-8").strip()
        except OSError:
            return None
    try:
        return int(pid)
    except (TypeError, ValueError):
        return None


# --- Single-instance guard and detached start ------------------------------
#
# Hooks start the daemon without waiting for it (the Gradle/Bazel
# client-starts-daemon pattern): a detached spawn costs a few milliseconds in
# the hook, and the daemon's own start-up (importing the storage stack) runs
# outside the hook budget. Correctness against two daemons does not rest on
# the hook's check -- the hook exits long before its child is up -- but on
# ``daemon.lock``, which every ``serve`` takes for its whole lifetime before
# touching the palace. A second daemon that loses that race exits quietly.

DAEMON_LOCK_NAME = "daemon.lock"
SPAWN_STAMP_NAME = "spawn.stamp"
# A daemon takes about a second to register. Hooks that fire inside that
# window skip the spawn instead of each paying for a Python start that would
# only lose the lock race.
SPAWN_THROTTLE_SECONDS = 10.0
# A daemon a hook started exits after this many minutes with nothing to do,
# unless daemon.idle_exit_minutes says otherwise; the next hook restarts it.
AUTOSTART_IDLE_EXIT_MINUTES = 30
# ``serve`` exit status when another daemon already owns this palace.
EXIT_ALREADY_RUNNING = 0


class DaemonAlreadyRunning(DaemonError):
    """Raised by ``run_server`` when another daemon holds ``daemon.lock``."""


def _try_lock_nonblocking(fh) -> bool:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        fcntl = None
    if fcntl is not None:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    import msvcrt  # pragma: no cover - Windows

    try:  # pragma: no cover - Windows
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:  # pragma: no cover - Windows
        return False


def _unlock(fh) -> None:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        fcntl = None
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


# How long a starting daemon keeps trying for the lifetime lock. Long enough
# to ride out a hook's momentary probe of it (``daemon_lock_held``), short
# enough that the loser of a real start race exits quickly.
SINGLETON_RETRY_SECONDS = 2.0


@contextlib.contextmanager
def daemon_singleton(palace_path: str, *, retry_seconds: float = SINGLETON_RETRY_SECONDS):
    """Hold this palace's daemon lifetime lock; yield whether it was won.

    The OS drops the lock when the holder exits, however it exits, so a
    crashed daemon never blocks its successor.
    """
    sd = state_dir(palace_path)
    sd.mkdir(parents=True, exist_ok=True)
    _chmod_dir_private(sd)
    fh = open(sd / DAEMON_LOCK_NAME, "a+")
    try:
        _chmod_private(sd / DAEMON_LOCK_NAME)
        won = _try_lock_nonblocking(fh)
        deadline = time.monotonic() + retry_seconds
        while not won and time.monotonic() < deadline:
            time.sleep(0.05)
            won = _try_lock_nonblocking(fh)
        yield won
    finally:
        fh.close()


def daemon_lock_held(palace_path: str) -> bool:
    """Whether some process holds this palace's daemon lifetime lock.

    Probes the lock without waiting and drops it at once if it was free. A
    daemon starting at that instant retries for ``SINGLETON_RETRY_SECONDS``,
    so the probe cannot cost it the start.
    """
    path = state_dir(palace_path) / DAEMON_LOCK_NAME
    try:
        fh = open(path, "r+")
    except OSError:
        return False  # no daemon has ever run for this palace
    try:
        if _try_lock_nonblocking(fh):
            _unlock(fh)
            return False
        return True
    finally:
        fh.close()


def daemon_registered_alive(palace_path: str) -> bool:
    """True when a daemon is registered, its pid is alive, and it holds the lock.

    A file read, a signal-0 probe and a lock probe: no HTTP, so a hook can
    afford it. The lock probe catches a registration a crashed daemon left
    behind whose pid the OS has since given to another process.
    """
    from ._proc import pid_alive

    pid = _registered_pid(palace_path)
    if pid is None or not pid_alive(pid):
        return False
    return daemon_lock_held(palace_path)


def _palace_writer_lock_paths(palace_path: str) -> list[str]:
    """The lock files ``mine_palace_lock`` takes, computed without importing it.

    Keep in step with ``palace_lock._palace_side_lock_path`` and the
    ``mine_palace_<key>.lock`` name there; a test checks they match.
    """
    found = [
        os.path.join(
            paths.lock_dir(), f"mine_palace_{paths.palace_key(palace_path, length=16)}.lock"
        )
    ]
    parent, name = os.path.split(paths.canonical_palace_path(palace_path))
    if name:
        found.append(os.path.join(parent, f".{name}.mempalace.lock"))
    return found


def palace_writer_lock_held(palace_path: str) -> bool:
    """Whether some process holds the palace's writer lock right now.

    Probes each lock file without waiting and drops it at once. A missing
    file, or one that is not a regular file, counts as free.
    """
    import stat

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    for path in _palace_writer_lock_paths(palace_path):
        try:
            fd = os.open(path, flags | getattr(os, "O_BINARY", 0))
        except OSError:
            continue
        with os.fdopen(fd, "rb", buffering=0) as fh:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                continue
            if not _try_lock_nonblocking(fh):
                return True
            _unlock(fh)
    return False


# Written by a daemon that could not take the palace's writer lock (another
# writer owns a single-writer backend). Hooks stop starting daemons that would
# only refuse again, and ``prefer`` falls back to its direct path.
START_REFUSED_NAME = "start.refused"
START_REFUSED_TTL_SECONDS = 60.0


def record_start_refused(palace_path: str, message: str) -> None:
    try:
        sd = state_dir(palace_path)
        sd.mkdir(parents=True, exist_ok=True)
        _write_private(sd / START_REFUSED_NAME, json.dumps({"ts": time.time(), "error": message}))
    except OSError:
        pass


def clear_start_refused(palace_path: str) -> None:
    try:
        (state_dir(palace_path) / START_REFUSED_NAME).unlink()
    except OSError:
        pass


def start_recently_refused(palace_path: str) -> bool:
    try:
        age = time.time() - (state_dir(palace_path) / START_REFUSED_NAME).stat().st_mtime
    except OSError:
        return False
    return age < START_REFUSED_TTL_SECONDS


def spawn_daemon_detached(
    palace_path: str, *, backend: str | None = None, ignore_throttle: bool = False
) -> bool:
    """Start a daemon for ``palace_path`` in the background; never wait for it.

    Returns True when a process was spawned. Skips the spawn when any process
    holds the palace's daemon lock, when another launcher is inside this
    function, or when this function spawned one less than
    ``SPAWN_THROTTLE_SECONDS`` ago (whether or not it is still alive). The child runs in its own session (POSIX) or
    process group without a console window (Windows; see ``_detached_kwargs``
    for why that is ``CREATE_NO_WINDOW`` rather than ``DETACHED_PROCESS``), so
    it survives the hook and the terminal that ran it.
    """
    from ._proc import safe_child_env, safe_module_argv

    palace_path = canonical_palace_path(palace_path)
    # Any holder of the lifetime lock will run what is queued: a registered
    # daemon, one still starting up, one waiting for the writer lock, and one
    # exiting idle (which re-checks the queue after it lets the lock go; see
    # ``daemon._hand_off_late_work``). A second server would only lose the lock.
    if daemon_lock_held(palace_path):
        return False
    sd = state_dir(palace_path)
    sd.mkdir(parents=True, exist_ok=True)
    _chmod_dir_private(sd)
    stamp = sd / SPAWN_STAMP_NAME
    # The lifetime lock admits one server. This separate, short reservation
    # also admits just one launcher while the server is still importing.
    with open(sd / "spawn.lock", "a+b") as launch_lock:
        if not _try_lock_nonblocking(launch_lock):
            return False
        if daemon_lock_held(palace_path):
            return False
        try:
            # On age alone: a daemon that dies at startup (bad config, refused
            # writer lease) must not be respawned by every hook fire.
            if not ignore_throttle and time.time() - stamp.stat().st_mtime < SPAWN_THROTTLE_SECONDS:
                return False
        except OSError:
            pass
        cmd = safe_module_argv(
            "mempalace.daemon",
            "serve",
            "--palace",
            palace_path,
            "--idle-exit-default",
            str(AUTOSTART_IDLE_EXIT_MINUTES),
        )
        if backend:
            cmd.extend(["--backend", backend])
        env = safe_child_env()
        kwargs = _detached_kwargs(sd / "daemon.log")
        try:
            proc = subprocess.Popen(cmd, env=env, **kwargs)
            _write_private(stamp, str(proc.pid))
        finally:
            log_fh = kwargs.get("stdout")
            if hasattr(log_fh, "close"):
                log_fh.close()
    return True


def probe_daemon_health(palace_path: str, *, timeout: float = 0.15) -> bool:
    """True when the registered daemon answers ``GET /health`` within ``timeout``.

    The hook-side counterpart of ``daemon.get_client_if_running``, on
    ``http.client`` instead of ``urllib.request`` (which costs ~100 ms to
    import). A plain connection never consults proxies and never follows a
    redirect, so the bearer token only ever reaches the loopback endpoint.
    The token is not read for a registration whose pid is dead, whose port may
    since belong to an unrelated process.
    """
    from ._proc import pid_alive

    try:
        endpoint = _read_endpoint(palace_path)
        port = int(endpoint["port"])
        pid = endpoint.get("pid")
        if pid is not None and not pid_alive(int(pid)):
            return False
        token = read_published_bytes(state_dir(palace_path) / "token").decode("utf-8").strip()
    except (DaemonError, OSError, KeyError, TypeError, ValueError):
        return False
    # Imported only once there is a live registration to probe: http.client
    # costs ~50 ms, which the common no-daemon hook should not pay.
    import http.client

    conn = http.client.HTTPConnection(endpoint.get("host") or "127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/health", headers={"Authorization": f"Bearer {token}"})
        resp = conn.getresponse()
        resp.read()
        return resp.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()
