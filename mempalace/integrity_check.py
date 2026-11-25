"""Post-mine integrity check: a scoped per-mine probe and a maintenance full check.

Every mine that wrote anything used to end with a whole-database
``PRAGMA quick_check``: 7-12 s on a 1.2 GB palace, ~40 s at 1M drawers. It
exists to catch FTS5 corruption caused by mine writes, whose root cause, a
raw NUL reaching Chroma, is now escaped at the write chokepoint
(``stored_text``, which every write goes through). So the per-mine check is
scoped to what a mine can break, and the full check moves to a maintenance
lane.

**Scoped check** (every mine that wrote, default). In a child process:

* every row this mine wrote, found by drawer id from the write journal
  (:func:`mempalace.backends.chroma.chroma_written_ids_since`), is scanned in
  ``embedding_metadata``, ``embedding_metadata_array`` and
  ``embedding_fulltext_search_content`` for a raw NUL or text that is not
  valid UTF-8 (an encoded lone surrogate). Either one means a write got past
  the chokepoint, which is how that FTS5 corruption started;
* ``PRAGMA quick_check(embeddings)`` and ``PRAGMA quick_check(embedding_metadata)``
  check the b-trees and indexes of the two tables every write touches.

What it does **not** catch: corruption of the FTS5 inverted index itself
(``embedding_fulltext_search_data`` / ``_idx``; SQLite >= 3.44 re-tokenizes
FTS5 under quick_check, so checking it costs as much as the full check),
damage in any other table, and damage to rows this mine did not write. A
killed-mid-write mine is the usual source of the first; the unclean-exit
trigger below sends the next mine to the full check for exactly that case.

**Full check** (``PRAGMA quick_check`` of the whole database) runs:

* on the first mine after a writer died mid-write (an in-flight marker it
  left behind; see :func:`note_write_started`);
* when the last clean full check is older than
  ``integrity_full_check_interval_hours`` (default 24) or there is none;
* when the write journal cannot vouch for every row the mine wrote;
* when the scoped check fails, so the FTS5 auto-heal has a verdict to act on;
* on every mine with ``post_mine_integrity_check = "full"``;
* on demand with ``mempalace integrity-check``, and in ``mempalace repair``.

A clean full check is recorded in ``<palace>/.mempalace/integrity.json``
keyed on the database's size and mtime (and its journal's), and reused
until the palace changes.

**Child process.** Chroma's Rust core links its own SQLite; Python's
``sqlite3`` is another copy. Two copies in one process can clear each
other's POSIX locks (sqlite.org, "How To Corrupt An SQLite Database File",
"Multiple copies of SQLite linked into the same application"). Both checks run in
a short-lived ``python -I`` child, so the mining process never opens
``chroma.sqlite3`` through Python's SQLite. A child that fails to start,
crashes, or answers garbage is reported as a check failure, never as clean.

This module keeps its imports light: it is the child's entry point too.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Iterable, Optional

from . import paths

SCOPED_TABLES = ("embeddings", "embedding_metadata")
# PRAGMA quick_check(TABLE) arrived in SQLite 3.33.0; older builds read the
# argument as a row limit, so they get the whole-database check instead.
_TABLE_QUICK_CHECK_MIN = (3, 33, 0)
_BUSY_TIMEOUT_SECONDS = 15.0
_ID_BATCH = 400
_MAX_REPORTED = 50
# A full check of a cold 10 GB palace can take minutes; a scoped one, seconds.
_SCOPED_TIMEOUT_SECONDS = 600.0
_FULL_TIMEOUT_SECONDS = 6 * 3600.0


# ── Probes: run in the child ──────────────────────────────────────────


def _connect(db_path: str):
    from .config import connect_sqlite_read

    return connect_sqlite_read(db_path, timeout=_BUSY_TIMEOUT_SECONDS)


def _quick_check(conn, table: Optional[str] = None) -> list[str]:
    if table is None:
        rows = conn.execute("PRAGMA quick_check").fetchall()
    else:
        rows = conn.execute(f'PRAGMA quick_check("{table}")').fetchall()
    return [str(row[0]) for row in rows if row and str(row[0]).lower() != "ok"]


def _batches(items: list, size: int = _ID_BATCH) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _text_fault(blob) -> Optional[str]:
    if blob is None:
        return None
    data = bytes(blob)
    if b"\x00" in data:
        return "raw NUL"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "invalid UTF-8"
    return None


def _label(blob) -> str:
    return bytes(blob).decode("utf-8", errors="replace").replace("\x00", "\\0")


def _scan_written_rows(conn, ids: list[str]) -> list[str]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if "embeddings" not in tables or "segments" not in tables:
        return []
    segments = [row[0] for row in conn.execute("SELECT id FROM segments WHERE scope = 'METADATA'")]
    if not segments:
        return []
    seg_marks = ",".join("?" * len(segments))
    rowids: dict[int, str] = {}
    for batch in _batches(ids):
        marks = ",".join("?" * len(batch))
        for rowid, drawer_id in conn.execute(
            f"SELECT id, embedding_id FROM embeddings "
            f"WHERE segment_id IN ({seg_marks}) AND embedding_id IN ({marks})",
            (*segments, *batch),
        ):
            rowids[rowid] = drawer_id

    sources = []
    for table in ("embedding_metadata", "embedding_metadata_array"):
        if table in tables:
            sources.append(
                (
                    table,
                    f"SELECT id, CAST(key AS BLOB), CAST(string_value AS BLOB) FROM {table} "
                    "WHERE id IN ({marks}) AND string_value IS NOT NULL",
                )
            )
    if "embedding_fulltext_search_content" in tables:
        sources.append(
            (
                "embedding_fulltext_search_content",
                "SELECT id, NULL, CAST(c0 AS BLOB) FROM embedding_fulltext_search_content "
                "WHERE id IN ({marks})",
            )
        )

    errors: list[str] = []
    for batch in _batches(sorted(rowids)):
        marks = ",".join("?" * len(batch))
        for table, sql in sources:
            for rowid, key, value in conn.execute(sql.format(marks=marks), batch):
                for part, blob in (("key", key), ("value", value)):
                    fault = _text_fault(blob)
                    if fault:
                        where = f"key {_label(key)!r}" if key is not None else "document"
                        errors.append(
                            f"{fault} in {table} {part} ({where}) of drawer "
                            f"{rowids[rowid]!r}: a write bypassed the stored-text escape"
                        )
    return errors


def _capped(errors: list[str]) -> list[str]:
    if len(errors) <= _MAX_REPORTED:
        return errors
    return [*errors[:_MAX_REPORTED], f"... and {len(errors) - _MAX_REPORTED} more issue(s)"]


def scoped_check_errors(db_path: str, ids: Iterable[str]) -> list[str]:
    """Scan the rows written for ``ids`` and quick_check the tables writes touch.

    See the module docstring for what this catches and what it leaves to the
    full check.
    """
    try:
        with closing(_connect(db_path)) as conn:
            if sqlite3.sqlite_version_info < _TABLE_QUICK_CHECK_MIN:
                errors = _quick_check(conn)
            else:
                errors = [e for table in SCOPED_TABLES for e in _quick_check(conn, table)]
            errors += _scan_written_rows(conn, sorted(set(ids)))
    except (sqlite3.Error, ValueError) as exc:
        return [f"PRAGMA quick_check failed: {exc}"]
    return _capped(errors)


def full_check_errors(db_path: str) -> list[str]:
    """``PRAGMA quick_check`` of the whole database (includes FTS5 integrity)."""
    try:
        with closing(_connect(db_path)) as conn:
            return _capped(_quick_check(conn))
    except (sqlite3.Error, ValueError) as exc:
        return [f"PRAGMA quick_check failed: {exc}"]


def _child_main() -> None:  # pragma: no cover - runs in the child; see run_probe
    request = json.loads(sys.stdin.read())
    db_path = request["db_path"]
    answer = {"sqlite_version": sqlite3.sqlite_version, "scoped": None, "full": None}
    if request.get("ids") is not None:
        answer["scoped"] = scoped_check_errors(db_path, request["ids"])
    if request.get("full"):
        answer["full"] = full_check_errors(db_path)
    sys.stdout.write(json.dumps(answer, ensure_ascii=True))
    sys.stdout.flush()


# ── Maintenance state: <palace>/.mempalace/ ───────────────────────────

_STATE_DIR = paths.DIRNAME
_RECORD = "integrity.json"
_IN_FLIGHT = "write-in-flight"
_UNCLEAN = "integrity-unclean"

_in_flight_lock = threading.Lock()
_in_flight: dict[str, int] = {}


def _reset_in_flight_after_fork() -> None:  # pragma: no cover - runs in a forked child
    global _in_flight_lock, _in_flight
    _in_flight_lock = threading.Lock()
    _in_flight = {}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_in_flight_after_fork)


def _key(palace_path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(palace_path)))


def _state_path(palace_path: str, name: str) -> str:
    return os.path.join(palace_path, _STATE_DIR, name)


def _write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def note_write_started(palace_path: str) -> None:
    """Mark a Chroma write in flight. Called inside the palace writer lock.

    The marker outlives a writer killed mid-write (SIGKILL, power loss, a
    crash in Chroma's core). Finding one that no write in this process owns
    means the last writer died with a write open, so the next mine runs the
    full check. A finished write, even a failed one, removes it. Best-effort:
    it never fails a write.
    """
    if not palace_path:
        return
    key = _key(palace_path)
    with _in_flight_lock:
        count = _in_flight.get(key, 0)
        _in_flight[key] = count + 1
        if count:
            return
        try:
            marker = _state_path(palace_path, _IN_FLIGHT)
            if os.path.exists(marker):
                # Latch the evidence before this write replaces the marker.
                _write_atomic(_state_path(palace_path, _UNCLEAN), "")
            elif not os.path.isdir(os.path.dirname(marker)):
                if not os.path.isdir(palace_path):
                    return
                os.mkdir(os.path.dirname(marker))
            # One create, no rename: a torn marker still proves a write was open.
            with open(marker, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
        except OSError:
            pass


def note_write_finished(palace_path: str) -> None:
    """Undo :func:`note_write_started` once the last in-flight write ends."""
    if not palace_path:
        return
    key = _key(palace_path)
    with _in_flight_lock:
        count = _in_flight.get(key, 0) - 1
        if count > 0:
            _in_flight[key] = count
            return
        _in_flight.pop(key, None)
        _unlink(_state_path(palace_path, _IN_FLIGHT))


def _unclean_exit_seen(palace_path: str) -> bool:
    if os.path.exists(_state_path(palace_path, _UNCLEAN)):
        return True
    with _in_flight_lock:
        ours = _in_flight.get(_key(palace_path), 0) > 0
    return not ours and os.path.exists(_state_path(palace_path, _IN_FLIGHT))


def db_fingerprint(palace_path: str) -> Optional[dict]:
    """Size and mtime of ``chroma.sqlite3`` and any journal beside it."""
    db = os.path.join(palace_path, "chroma.sqlite3")
    fingerprint = {}
    for suffix in ("", "-wal", "-journal"):
        try:
            st = os.stat(db + suffix)
        except FileNotFoundError:
            if not suffix:
                return None
            continue
        except OSError:
            return None
        fingerprint[suffix or "db"] = [st.st_size, st.st_mtime_ns]
    return fingerprint


def _load_record(palace_path: str) -> Optional[dict]:
    try:
        with open(_state_path(palace_path, _RECORD), encoding="utf-8") as handle:
            record = json.load(handle)
        float(record["checked_at"])
        return record
    except (OSError, ValueError, KeyError, TypeError):
        return None


def record_clean_full_check(
    palace_path: str,
    *,
    now: Optional[float] = None,
    sqlite_version: str = "",
    fingerprint: Optional[dict] = None,
    holds_writer_lock: bool = True,
) -> None:
    """Remember a clean full check.

    ``now`` and ``fingerprint`` should be taken before the probe started: a
    write that lands while the child reads then leaves the record stale
    instead of certifying rows nobody checked. Only a caller holding the
    palace writer lock (the post-mine check) clears the unclean-exit evidence;
    without it a live writer in another process may own the marker.
    """
    record = {
        "checked_at": time.time() if now is None else now,
        "fingerprint": db_fingerprint(palace_path) if fingerprint is None else fingerprint,
        "sqlite_version": sqlite_version or sqlite3.sqlite_version,
    }
    try:
        _write_atomic(_state_path(palace_path, _RECORD), json.dumps(record))
    except OSError:
        return
    if not holds_writer_lock:
        return
    _unlink(_state_path(palace_path, _UNCLEAN))
    with _in_flight_lock:
        if not _in_flight.get(_key(palace_path)):
            _unlink(_state_path(palace_path, _IN_FLIGHT))


def _cached_clean(record: Optional[dict], palace_path: str) -> bool:
    """True when ``record`` is a clean full check of the database as it is now."""
    if record is None:
        return False
    current = db_fingerprint(palace_path)
    return current is not None and record.get("fingerprint") == current


def full_check_reason(
    palace_path: str,
    *,
    mode: str,
    interval_hours: float,
    now: Optional[float] = None,
    ids_complete: bool = True,
    force: bool = False,
) -> Optional[str]:
    """Why the full check must run now, or None when the scoped check suffices.

    A clean record for the database exactly as it is now (same size and
    mtime) answers every trigger except ``force``: nothing changed since a
    full check found it clean.
    """
    now = time.time() if now is None else now
    record = _load_record(palace_path)
    if not force and _cached_clean(record, palace_path):
        return None
    if force:
        return "requested"
    if mode == "full":
        return "post_mine_integrity_check = full"
    if _unclean_exit_seen(palace_path):
        return "a writer exited mid-write"
    if not ids_complete:
        return "the write journal cannot name every row this mine wrote"
    if record is None:
        return "no full check on record"
    if interval_hours > 0 and now - float(record["checked_at"]) >= interval_hours * 3600:
        return f"last full check is older than {interval_hours:g} h"
    return None


# ── Parent side: launch the child ─────────────────────────────────────


@dataclass(frozen=True)
class ProbeResult:
    """What the child reported. ``None`` for a check that was not requested."""

    scoped: Optional[tuple[str, ...]]
    full: Optional[tuple[str, ...]]
    sqlite_version: str = ""


_CHILD_CODE = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from mempalace.integrity_check import _child_main; _child_main()"
)


def run_probe(db_path: str, *, ids: Optional[Iterable[str]], full: bool) -> ProbeResult:
    """Run the scoped check (``ids`` not None) and/or the full check in a child.

    The one door between a mining process and Python's SQLite on the palace:
    tests patch this to script verdicts. A child that cannot answer is a
    failed check for every part requested.
    """
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    request = {"db_path": db_path, "ids": None if ids is None else sorted(set(ids)), "full": full}
    timeout = _FULL_TIMEOUT_SECONDS if full else _SCOPED_TIMEOUT_SECONDS
    failure = None
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _CHILD_CODE, package_parent],
            input=json.dumps(request, ensure_ascii=True),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        failure = f"integrity check child could not run: {exc}"
    else:
        try:
            if proc.returncode != 0:
                raise ValueError(f"exit status {proc.returncode}")
            answer = json.loads(proc.stdout)
            scoped, full_errors = answer["scoped"], answer["full"]
            return ProbeResult(
                scoped=None if scoped is None else tuple(scoped),
                full=None if full_errors is None else tuple(full_errors),
                sqlite_version=str(answer.get("sqlite_version", "")),
            )
        except (ValueError, KeyError, TypeError) as exc:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            failure = f"integrity check child failed ({exc})" + (
                f": {' | '.join(tail)}" if tail else ""
            )
    errors = (failure,)
    return ProbeResult(scoped=None if ids is None else errors, full=errors if full else None)


# ── The post-mine check ───────────────────────────────────────────────


def _db_absent(db_path: str) -> bool:
    try:
        os.lstat(db_path)
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return False


def _is_named_pipe(db_path: str) -> bool:
    try:
        return stat.S_ISFIFO(os.stat(db_path).st_mode)
    except (OSError, ValueError):
        return False


def _settings() -> tuple[str, float]:
    from .config import MempalaceConfig

    config = MempalaceConfig()
    return config.post_mine_integrity_check, config.integrity_full_check_interval_hours


def _probe_without_inproc_writes(db_path: str, *, ids, full: bool) -> ProbeResult:
    # Hold this process's per-database lock while the child reads: Chroma
    # writes from other threads (the MCP HTTP transport) wait here instead of
    # hitting the child's SHARED lock with SQLITE_BUSY. It is a Python RLock;
    # it opens nothing.
    from .backends._inproc_sqlite import palace_db_lock

    with palace_db_lock(db_path):
        return run_probe(db_path, ids=ids, full=full)


def check_after_mine(palace_path: str, ids: Optional[Iterable[str]], *, heal) -> list[str]:
    """Run the post-mine check and return the errors that remain.

    ``ids`` are the drawer ids the mine wrote, or None when the journal cannot
    name them all (that forces the full check). ``heal(errors)`` gets a full
    check's errors and returns what is left; it runs only when a check failed.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if _db_absent(db_path):
        return []
    if _is_named_pipe(db_path):
        return [
            "PRAGMA quick_check failed: chroma.sqlite3 resolves to a named pipe, not a database"
        ]
    mode, interval_hours = _settings()
    reason = full_check_reason(
        palace_path, mode=mode, interval_hours=interval_hours, ids_complete=ids is not None
    )
    started, fingerprint = time.time(), db_fingerprint(palace_path)
    result = _probe_without_inproc_writes(db_path, ids=ids, full=reason is not None)
    scoped = list(result.scoped or ())
    full = result.full
    if scoped and full is None:
        # Escalate: the heal acts on a full check's verdict, and the operator
        # gets the whole picture.
        result = _probe_without_inproc_writes(db_path, ids=None, full=True)
        full = result.full
    full_errors = list(full or ())
    if full_errors:
        full_errors = list(heal(full_errors))
        # A heal wrote the database: record what it left, not the pre-probe state.
        started, fingerprint = time.time(), None
    if full is not None and not full_errors and not scoped:
        # Not when the scoped scan failed: quick_check cannot see a stored NUL,
        # so a clean full verdict would certify the very rows that failed.
        record_clean_full_check(
            palace_path,
            now=started,
            fingerprint=fingerprint,
            sqlite_version=result.sqlite_version,
        )
    return scoped + full_errors


@dataclass(frozen=True)
class FullCheckOutcome:
    """Result of :func:`run_full_check`. ``cached`` means no check ran."""

    errors: tuple[str, ...]
    cached: bool
    checked_at: Optional[float] = None


def run_full_check(palace_path: str, *, force: bool = False) -> FullCheckOutcome:
    """The maintenance full check: ``mempalace integrity-check``.

    Reuses a clean result recorded for the database as it is now unless
    ``force``. Runs in a child like the post-mine check, reports only (``mempalace
    repair`` heals), and records a clean result for the next mine to reuse.
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if _db_absent(db_path):
        return FullCheckOutcome(errors=(f"no database at {db_path}",), cached=False)
    if _is_named_pipe(db_path):
        return FullCheckOutcome(
            errors=("chroma.sqlite3 resolves to a named pipe, not a database",), cached=False
        )
    record = _load_record(palace_path)
    if not force and _cached_clean(record, palace_path):
        return FullCheckOutcome(errors=(), cached=True, checked_at=record.get("checked_at"))
    started, fingerprint = time.time(), db_fingerprint(palace_path)
    result = _probe_without_inproc_writes(db_path, ids=None, full=True)
    errors = tuple(result.full or ())
    if not errors:
        # Takes no writer lock (a long check must not refuse mines), so it
        # records only the verdict and leaves the unclean-exit evidence alone.
        record_clean_full_check(
            palace_path,
            now=started,
            fingerprint=fingerprint,
            sqlite_version=result.sqlite_version,
            holds_writer_lock=False,
        )
    return FullCheckOutcome(errors=errors, cached=False, checked_at=None if errors else started)
