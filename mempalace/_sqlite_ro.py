"""Centralized read-only SQLite connection helper for MemPalace.

SQLite has no ``SET statement_timeout`` (that is Postgres-only). The correct
analogs are:

- ``PRAGMA busy_timeout`` -- a *lock-wait* ceiling (how long to wait for a
  writer's lock before giving up). Bounds contention with the miner/repair.
- ``set_progress_handler`` + an abort return -- a *runaway-statement* ceiling
  (wall-clock deadline for a single statement). Bounds a pathological
  BM25/IN/metadata scan that would otherwise run unbounded against the
  multi-GB ``chroma.sqlite3`` and block the MCP event loop.

This helper centralizes both ceilings, guarantees the connection is closed on
every exit path (success or exception), enforces ``query_only`` as defense in
depth, and applies read-tuning PRAGMAs (``mmap_size`` / ``cache_size`` /
``temp_store``) sized for the large store.

Behaviour-preserving: callers run exactly the same SQL and get exactly the same
rows in the same order. The only new behaviour is that a statement exceeding its
wall-clock deadline now raises :class:`StatementTimeout` instead of running
unbounded. Normal queries (well under the deadline) see no behavioural change.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# --- Defaults -----------------------------------------------------------------

# Lock-wait ceiling. Matches SQLite's historical 5 s default; explicit so the
# value is visible and tunable rather than implicit.
DEFAULT_BUSY_TIMEOUT_MS = 5_000

# Runaway-statement wall-clock ceiling for interactive read surfaces
# (search, status, vector-segment lookup). Batch/repair callers pass a
# larger value explicitly.
DEFAULT_DEADLINE_S = 15.0

# Progress handler fires every N virtual-machine opcodes and calls
# time.monotonic(). Lower = finer deadline granularity but more per-opcode
# overhead -- and FTS5/union scans burn opcodes fast, so an aggressive value
# measurably slowed the union path (p50 +8%, p95 +63%) in the first AFTER run.
# For a 15-120 s wall-clock deadline, checking the clock every ~1M opcodes is
# more than frequent enough; it cuts the callback rate ~20x and erases the
# union regression while still aborting a true runaway well inside the ceiling.
_PROGRESS_OPS = 1_000_000

# Read-tuning PRAGMAs (mmap_size / cache_size / temp_store) are DISABLED by
# default (0 = "don't set"). Two measured reasons, from the paired benchmark in
# projects/active/mempalace-ab/bench/harden-sqlite/:
#   1. No latency benefit. The warm read path is served from the OS page cache
#      and is Python/chromadb-bound, not SQLite-I/O-bound; 5 alternating rounds
#      showed the tuning delta inside the run-to-run noise band (+/-~40 ms p50).
#   2. They perturb results. The upstream FTS5 candidate query has no
#      deterministic ORDER BY before its LIMIT; mmap/cache/temp_store change the
#      b-tree traversal order, which changes which candidates survive the LIMIT
#      -> different (and run-to-run unstable) recall on high-fanout queries.
#      A read-safety change must be recall-neutral, so we do not pay an ordering
#      risk for a benefit that does not exist.
# Callers that benchmark a STATIC snapshot may still opt in by passing explicit
# mmap_size / cache_size_kib. The live read path uses 0/0.
DEFAULT_MMAP_SIZE = 0  # bytes; 0 = leave SQLite default (no PRAGMA mmap_size)
DEFAULT_CACHE_SIZE_KIB = 0  # 0 = leave SQLite default (no PRAGMA cache_size)
DEFAULT_TEMP_STORE_MEMORY = False  # leave PRAGMA temp_store at its default


class StatementTimeout(sqlite3.OperationalError):
    """A read statement exceeded its wall-clock deadline and was aborted.

    When the progress handler trips, the sqlite3 driver surfaces the abort as a
    generic :class:`sqlite3.OperationalError` ("interrupted"). The connection
    returned by :func:`open_ro` / :func:`connect_ro` translates that specific
    abort -- recognised by the connection's wall-clock deadline having already
    passed -- into this subclass, so a runaway-statement timeout is catchable
    distinctly. Every other ``OperationalError`` (locked, malformed, read-only
    violation, …) propagates unchanged. Because it subclasses
    ``OperationalError``, existing broad ``except sqlite3.OperationalError``
    handlers keep working.
    """


def _timeout_or_original(
    exc: sqlite3.OperationalError, deadline: Optional[float]
) -> sqlite3.OperationalError:
    """Map a statement abort to StatementTimeout iff the deadline has passed.

    The progress-handler abort is the only OperationalError whose timing
    coincides with the deadline; any other OperationalError raised before the
    deadline (locked, read-only violation, malformed schema) is returned as-is.
    """
    if deadline is not None and time.monotonic() > deadline:
        return StatementTimeout(str(exc) or "statement exceeded its wall-clock deadline")
    return exc


class _RODeadlineCursor(sqlite3.Cursor):
    """Cursor that translates a deadline-abort into :class:`StatementTimeout`."""

    def execute(self, *args, **kwargs):  # type: ignore[override]
        try:
            return super().execute(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            deadline = getattr(self.connection, "_ro_deadline", None)
            raise _timeout_or_original(exc, deadline) from exc

    def executemany(self, *args, **kwargs):  # type: ignore[override]
        try:
            return super().executemany(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            deadline = getattr(self.connection, "_ro_deadline", None)
            raise _timeout_or_original(exc, deadline) from exc


class _RODeadlineConnection(sqlite3.Connection):
    """Connection that maps a deadline-abort into :class:`StatementTimeout`.

    Covers both the direct ``connection.execute(...)`` shortcut and the
    ``connection.cursor().execute(...)`` surface (callers use both).
    """

    # Armed by _install_deadline; None when no deadline is set. Declared at
    # class level so getattr is safe before the connection is configured.
    _ro_deadline: Optional[float] = None

    def execute(self, *args, **kwargs):  # type: ignore[override]
        try:
            return super().execute(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            raise _timeout_or_original(exc, self._ro_deadline) from exc

    def executemany(self, *args, **kwargs):  # type: ignore[override]
        try:
            return super().executemany(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            raise _timeout_or_original(exc, self._ro_deadline) from exc

    def cursor(self, factory=_RODeadlineCursor):  # type: ignore[override]
        return super().cursor(factory)


def _install_deadline(conn: "_RODeadlineConnection", deadline_s: Optional[float]) -> None:
    """Arm a wall-clock deadline via the progress handler.

    The handler returns non-zero once the deadline passes, which makes SQLite
    abort the running statement; :class:`_RODeadlineConnection` then translates
    that abort into :class:`StatementTimeout`. The deadline is also stored on the
    connection so the translation can tell a timeout abort from any other
    OperationalError.
    """
    if not deadline_s or deadline_s <= 0:
        conn._ro_deadline = None
        return
    deadline = time.monotonic() + deadline_s
    conn._ro_deadline = deadline

    def _handler() -> int:  # pragma: no cover - exercised via integration
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(_handler, _PROGRESS_OPS)


def _apply_read_pragmas(
    conn: sqlite3.Connection,
    *,
    busy_timeout_ms: int,
    mmap_size: int,
    cache_size_kib: int,
    temp_store_memory: bool,
) -> None:
    # Safety floor -- always applied, never changes query RESULTS or ordering:
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA query_only = ON")
    # Read-tuning -- opt-in only (default off); see the DEFAULT_* notes above on
    # why these are not applied to the live read path (no measured speedup +
    # they perturb FTS5 candidate traversal order).
    if temp_store_memory:
        conn.execute("PRAGMA temp_store = MEMORY")
    if cache_size_kib:
        conn.execute(f"PRAGMA cache_size = {int(cache_size_kib)}")
    if mmap_size:
        # mmap_size is best-effort; ignored if the build caps it lower.
        conn.execute(f"PRAGMA mmap_size = {int(mmap_size)}")


def _build_uri(db_path: str, *, immutable: bool) -> str:
    # Build the file: URI via Path.as_uri() rather than f-string interpolation:
    # it produces an absolute, properly percent-encoded URI and handles Windows
    # drive letters / backslashes and POSIX paths containing '?' or '#' (both
    # valid in filenames) that would otherwise confuse SQLite's URI parser.
    uri = f"{Path(db_path).absolute().as_uri()}?mode=ro"
    if immutable:
        # immutable=1 promises SQLite the file will not change while open, so
        # it can skip locking and shared-memory work for a read speedup. Valid
        # ONLY for a static snapshot -- NEVER for the live, actively-mined
        # palace (the miner/repair write it concurrently).
        uri += "&immutable=1"
    return uri


def open_ro(
    db_path: str,
    *,
    deadline_s: Optional[float] = DEFAULT_DEADLINE_S,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    immutable: bool = False,
    mmap_size: int = DEFAULT_MMAP_SIZE,
    cache_size_kib: int = DEFAULT_CACHE_SIZE_KIB,
    temp_store_memory: bool = DEFAULT_TEMP_STORE_MEMORY,
) -> sqlite3.Connection:
    """Open and configure a read-only connection (caller owns ``.close()``).

    Use this at call sites that already manage the connection with their own
    ``try/finally`` -- it is a drop-in replacement for
    ``sqlite3.connect(f"file:{path}?mode=ro", uri=True)`` that additionally
    applies the read PRAGMAs and arms the statement deadline. For new code,
    prefer the :func:`connect_ro` context manager, which also guarantees the
    close.

    The read-tuning args (``mmap_size`` / ``cache_size_kib`` /
    ``temp_store_memory``) default to off; see the module-level notes for why.

    Raises:
        sqlite3.OperationalError: on open failure or PRAGMA failure (the
            half-open connection is closed before the exception propagates).
        StatementTimeout: (subclass of OperationalError) when a later statement
            on the returned connection exceeds ``deadline_s``.
    """
    conn = sqlite3.connect(
        _build_uri(db_path, immutable=immutable),
        uri=True,
        factory=_RODeadlineConnection,
    )
    try:
        _apply_read_pragmas(
            conn,
            busy_timeout_ms=busy_timeout_ms,
            mmap_size=mmap_size,
            cache_size_kib=cache_size_kib,
            temp_store_memory=temp_store_memory,
        )
        _install_deadline(conn, deadline_s)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def connect_ro(
    db_path: str,
    *,
    deadline_s: Optional[float] = DEFAULT_DEADLINE_S,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    immutable: bool = False,
    mmap_size: int = DEFAULT_MMAP_SIZE,
    cache_size_kib: int = DEFAULT_CACHE_SIZE_KIB,
    temp_store_memory: bool = DEFAULT_TEMP_STORE_MEMORY,
) -> Iterator[sqlite3.Connection]:
    """Open a read-only SQLite connection with timeout + lifecycle guarantees.

    Args:
        db_path: filesystem path to the SQLite database.
        deadline_s: wall-clock ceiling for any single statement; ``None``/``0``
            disables. Exceeding it aborts the statement (OperationalError).
        busy_timeout_ms: lock-wait ceiling in milliseconds.
        immutable: snapshot-only fast path; must stay ``False`` for the live
            palace.
        mmap_size: memory-map ceiling in bytes (best-effort).
        cache_size_kib: page cache size; negative value is KiB per SQLite.

    Yields:
        An open ``sqlite3.Connection`` in read-only mode. Always closed on exit.

    Raises:
        sqlite3.OperationalError: on open failure.
        StatementTimeout: (subclass of OperationalError) on deadline overrun.
    """
    conn = open_ro(
        db_path,
        deadline_s=deadline_s,
        busy_timeout_ms=busy_timeout_ms,
        immutable=immutable,
        mmap_size=mmap_size,
        cache_size_kib=cache_size_kib,
        temp_store_memory=temp_store_memory,
    )
    try:
        yield conn
    finally:
        try:
            conn.set_progress_handler(None, 0)
        except Exception:  # pragma: no cover - best effort teardown
            pass
        conn.close()
