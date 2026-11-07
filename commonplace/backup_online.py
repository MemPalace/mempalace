"""Online snapshot of a serving data directory: the cut.

``commonplace backup --online DEST`` runs :func:`backup_online` in the
snapshot child (:mod:`commonplace.backup_supervisor`) while ``serve`` keeps
running. The child talks to the gateway over the admin socket
(:mod:`commonplace.backup_pause`, relayed by the supervisor): pause the memory workers,
freeze file creation, take one instant L across every database and file,
thaw, copy the paused worker files, resume, copy everything else as of L,
verify, ask the gateway to ``commit`` (the run stayed valid), then publish.
The snapshot format, :func:`~commonplace.backup.restore` and
:func:`~commonplace.backup.verify_backup` are the offline ones (manifest
version 1).

Locks
    This module takes no lock. Its caller, the supervised snapshot child, holds
    ``<stamp>.lock`` and the fence (:func:`~commonplace.backup.fence_lock`)
    exclusive for the whole run; one-shot CLI commands hold the fence shared.
    With a gateway (``client`` given), ``data_lock`` is never touched. With no
    gateway (``client=None``), the caller holds ``data_lock`` exclusive for the
    whole snapshot, so a serve start is refused mid-run. :func:`backup_online`
    refuses to start unless those locks are held.

Staging
    DEST is ``<data>/.backup-staging/<stamp>`` (stamp ``[0-9TZ.]+-[0-9]+``) and
    must not exist. The run builds ``<stamp>.partial/`` and renames it to
    ``<stamp>`` only after ``commit`` returned OK; nothing else ever creates
    ``<stamp>``. A failed run removes its partial; ``backup --online-cleanup``
    removes what a killed run left, once its lock is free.

The cut
    1. Preflight: ``hello`` (protocol, same data directory, no live or
       starting successor), ``/proc`` scan (gateway, its ancestors and workers exempt),
       encrypted databases refused, rollback-journal databases at most
       ``ROLLBACK_MAX_BYTES``, disk reserve, copy probe and pause estimate.
    2. ``pause`` (up to ``PAUSE_TRIES`` on ``no_drain``), ``/proc`` scan with
       only the gateway and its ancestors exempt, ``freeze``.
    3. Enumerate the tree. Worker files are those in the resolved active
       generation's ``palace``, ``home`` and ``config``. Seam ``enumerated``.
    4. Barrier: ``BEGIN IMMEDIATE`` with no busy wait on every other database,
       in sorted order; any busy database rolls the whole set back and it is
       retried for at most ``BARRIER_S``. With every lock held, one pinned
       read transaction per database and an fd plus size per other file. Then
       roll back and ``thaw``: that instant is L.
    6. Rollback-journal databases are copied from their pins first and the
       pins released (their writers wait while a pin holds SHARED). Seam
       ``pinned``.
    7. Worker databases are recovered and checkpointed (``TRUNCATE``), their
       journals must be empty; seam ``worker-copy``; raw copy of the worker
       files; ``resume``.
    8. WAL databases are copied from their pins (inside the pinned read
       transaction, so the copy never restarts); other files copy
       ``[0, size at L)`` from their fds.
    9. Every stored, launched, done or launch_failed handoff must find its
       stored thread in the snapshot; manifest; ``verify_backup``; fsync;
       ``commit`` must be OK; rename; fsync the staging directory.

    Never copied: the data and fence locks, a previous ``manifest.json``, the
    shared model-cache link, SQLite sidecars (``-wal``, ``-shm``,
    ``-journal``), ``.backup-staging/`` and ELF core dumps (``core*``).

Leases
    While paused the run renews the pause lease every
    ``min(RENEW_EVERY_S, lease_s / 3)`` seconds, and only while it is making
    progress (a stage, a copied chunk, a database step within the last
    ``PROGRESS_WINDOW_S``). A stalled run lets the lease lapse; the gateway
    then resumes the workers and the run ends INVALID. The run also counts
    its own lease and stops when it lapsed. The pause request carries the
    run's ``lease_s`` and ``pause_max_s``.

Seams (tests only)
    With ``COMMONPLACE_BACKUP_SEAM_DIR`` set, each seam writes
    ``<name>.reached`` there and waits for ``<name>.go``. Nothing is renewed
    while a seam waits.

Logs and errors
    Log lines carry a stage name, counts and seconds only. Errors are
    :class:`OnlineBackupError` (``stage``, ``detail``); a run the gateway
    invalidated reads ``<stage>: run INVALID (<reason>)``.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .backup import (
    _SQLITE_HEADER,
    FENCE_NAME,
    LOCK_NAME,
    STAGING_NAME,
    BackupError,
    _ancestors,
    _encrypted_database,
    _file_entry,
    _inside,
    _inventory,
    _is_sqlite,
    _verify_offline,
    verify_backup,
)

STAMP_RE = re.compile(r"[0-9TZ.]+-[0-9]+")
CHUNK_BYTES = 64 * 1024 * 1024
PROBE_BYTES = 256 * 1024 * 1024
PROBE_MIN_BYTES = 4 * 1024 * 1024
ROLLBACK_MAX_BYTES = 64 * 1024 * 1024
WAL_MAX_BYTES = 2 * 1024**3
BARRIER_S = 5.0
PAUSE_TRIES = 3
RENEW_EVERY_S = 10.0
PROGRESS_WINDOW_S = 30.0
STALL_S = 30.0
PAUSE_ESTIMATE_SHARE = 0.8
MONITOR_EVERY_S = 1.0
SIDECARS = ("-wal", "-shm", "-journal")
WORKER_DIRS = ("palace", "home", "config")
HANDOFF_STATES = ("stored", "launched", "done", "launch_failed")
_BUSY_ERRNOS = (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
_NO_COPY_RANGE = (errno.EXDEV, errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EPERM)


class OnlineBackupError(BackupError):
    """An online backup stopped at ``stage``; nothing was published."""

    def __init__(self, stage: str, detail: str = ""):
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail}" if detail else stage)


# ── staging layout ──


def _resolve_dest(root: Path, dest) -> tuple[Path, str]:
    """``<root>/.backup-staging/<stamp>``, which must not exist yet."""
    dest = Path(dest).expanduser().absolute()
    parent = Path(os.path.realpath(dest.parent))
    if parent != root / STAGING_NAME or not STAMP_RE.fullmatch(dest.name):
        raise OnlineBackupError(
            "destination", f"an online backup writes to <data>/{STAGING_NAME}/<stamp>"
        )
    dest = parent / dest.name
    for path in (dest, parent / f"{dest.name}.partial"):
        if os.path.lexists(path):
            raise OnlineBackupError("destination", "the destination stamp already exists")
    return dest, dest.name


def _ensure_staging(staging: Path) -> None:
    try:
        os.mkdir(staging, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(staging)
    if not stat.S_ISDIR(st.st_mode):
        raise OnlineBackupError("destination", f"{STAGING_NAME} is not a directory")
    if st.st_uid != os.getuid():
        raise OnlineBackupError("destination", f"{STAGING_NAME} belongs to another user")


def _lock_held(path: Path, *, only_exclusive: bool = False) -> bool:
    """True when another open file holds a flock on ``path``.

    By default any holder counts (the probe asks for an exclusive lock). With
    ``only_exclusive`` only an exclusive holder counts: the probe asks for a
    shared lock, which shared holders do not block.
    """
    import fcntl

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    try:
        mode = fcntl.LOCK_SH if only_exclusive else fcntl.LOCK_EX
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _BUSY_ERRNOS:
            return True
        raise
    finally:
        os.close(fd)
    return False


# ── gateway side ──


def _successor_refusal(info: dict) -> OnlineBackupError:
    parts = []
    if info.get("successor_pids"):
        parts.append(f"live pids {info['successor_pids']}")
    if info.get("successor_sids"):
        parts.append(f"sessions {info['successor_sids']}")
    if info.get("successors_starting"):
        parts.append(f"{len(info['successors_starting'])} starting")
    if info.get("successor_error"):
        parts.append(f"registry unreadable ({info['successor_error']})")
    return OnlineBackupError("successor active", "; ".join(parts) or "a successor is registered")


def _refused(stage: str, response: dict) -> OnlineBackupError:
    if response.get("error") == "invalid" or response.get("state") == "INVALID":
        return OnlineBackupError(stage, f"run INVALID ({response.get('reason') or 'unknown'})")
    return OnlineBackupError(stage, f"the gateway refused ({response.get('error') or 'unknown'})")


class _Gateway:
    """One run on the gateway's admin socket (``backup_pause.PauseClient``)."""

    online = True

    def __init__(self, client, root: Path, knobs):
        self.client = client
        self.root = root
        self.lease_s = knobs.lease_s
        self.pause_max_s = knobs.pause_max_s
        self.opened = False
        self.info: dict = {}

    def _call(self, stage: str, op: str, **fields) -> dict:
        try:
            response = self.client.call(op, **fields)
        except (OSError, ValueError) as exc:  # ConnectionError, timeout, bad JSON
            raise OnlineBackupError(
                stage, f"run INVALID (admin socket lost: {type(exc).__name__})"
            ) from exc
        if not response.get("ok"):
            raise _refused(stage, response)
        return response

    def hello(self) -> dict:
        try:
            info = self.client.call("hello")
        except (OSError, ValueError) as exc:
            raise OnlineBackupError("gateway", f"no hello ({type(exc).__name__})") from exc
        if info.get("successor_active"):
            raise _successor_refusal(info)
        if not info.get("ok"):
            if info.get("error") == "busy":
                raise OnlineBackupError("previous run", "previous backup still running")
            raise _refused("gateway", info)
        self.opened = True
        if info.get("proto") != 1:
            raise OnlineBackupError("gateway", "unsupported admin protocol")
        if os.path.realpath(str(info.get("data_dir") or "")) != str(self.root):
            raise OnlineBackupError("gateway", "the gateway serves another data directory")
        server = info.get("knobs") or {}
        self.lease_s = min(self.lease_s, float(server.get("lease_s") or self.lease_s))
        self.pause_max_s = min(
            self.pause_max_s, float(server.get("pause_max_s") or self.pause_max_s)
        )
        self.info = info
        return info

    def exempt(self, *, workers: bool) -> set:
        gateway_pid = self.info.get("gateway_pid")
        # An exec-created backup need not descend from the gateway's init or
        # launcher. Those supervisors inherit its argv/environment too.
        pids = _ancestors(pid=int(gateway_pid)) if gateway_pid is not None else set()
        if workers:
            pids.update(self.info.get("worker_pids") or ())
        return {int(pid) for pid in pids if pid is not None}

    def pause(self, log) -> dict:
        for attempt in range(1, PAUSE_TRIES + 1):
            try:
                response = self.client.call(
                    "pause", lease_s=self.lease_s, pause_max_s=self.pause_max_s
                )
            except (OSError, ValueError) as exc:
                raise OnlineBackupError(
                    "pause", f"run INVALID (admin socket lost: {type(exc).__name__})"
                ) from exc
            if response.get("ok"):
                return response
            if response.get("error") != "no_drain":
                raise _refused("pause", response)
            log("pause", attempt=attempt, drained=0)
        raise OnlineBackupError(
            "pause", f"in-flight memory calls did not drain ({PAUSE_TRIES} tries)"
        )

    def renew(self, stage: str) -> dict:
        return self._call(stage, "renew")

    def freeze(self) -> dict:
        return self._call("freeze", "freeze")

    def thaw(self) -> dict:
        return self._call("barrier", "thaw")

    def resume(self) -> dict:
        return self._call("resume", "resume")

    def commit(self) -> dict:
        return self._call("commit", "commit")

    def abort(self) -> None:
        if not self.opened:
            return
        try:
            self.client.call("abort")
        except (OSError, ValueError):
            pass


class _NoGateway:
    """No gateway answers; the caller holds ``data_lock`` exclusive for the whole run."""

    online = False
    lease_s = float("inf")
    pause_max_s = float("inf")

    def hello(self) -> dict:
        return {}

    def exempt(self, *, workers: bool) -> set:
        return set()

    def pause(self, log) -> dict:
        return {"ok": True}

    def renew(self, stage: str) -> dict:
        return {"ok": True}

    def freeze(self) -> dict:
        return {"ok": True}

    def thaw(self) -> dict:
        return {"ok": True}

    def resume(self) -> dict:
        return {"ok": True}

    def commit(self) -> dict:
        return {"ok": True, "console_drops": 0}

    def abort(self) -> None:
        pass


# ── the cut ──


def _header(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(100)


def _is_core(path: Path) -> bool:
    if path.name.split(".")[0] != "core":
        return False
    try:
        return _header(path)[:4] == b"\x7fELF"
    except OSError:
        return False


def _open_db(path: Path, *, timeout: float) -> sqlite3.Connection:
    """Open an existing database read-write; never creates one."""
    return sqlite3.connect(
        path.as_uri() + "?mode=rw",
        uri=True,
        timeout=timeout,
        isolation_level=None,
        check_same_thread=False,
    )


def _busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class _Tree:
    dirs: list = field(default_factory=list)
    wal: list = field(default_factory=list)  # gateway WAL databases
    rollback: list = field(default_factory=list)  # gateway rollback-journal databases
    other: list = field(default_factory=list)  # gateway files
    worker: list = field(default_factory=list)  # files in worker dirs (paused copy)
    sizes: dict = field(default_factory=dict)
    cores: int = 0

    @property
    def databases(self) -> list:
        return sorted(self.wal + self.rollback)

    def total(self, paths=None) -> int:
        return sum(self.sizes[p] for p in (self.sizes if paths is None else paths))


class _Cut:
    def __init__(self, root, partial, control, knobs, deadline, log, progress):
        self.root = root
        self.partial = partial
        self.control = control
        self.knobs = knobs
        self.deadline = deadline
        self.log = log
        self.progress = progress
        self.stage = "preflight"
        self.pins: dict = {}
        self.fds: dict = {}
        self.copied: list = []
        self.paused = False
        self.lease_s = control.lease_s
        self.renew_every = min(RENEW_EVERY_S, control.lease_s / 3)
        now = time.monotonic()
        self.last_renew = self.last_progress = self.last_monitor = self.last_beat = now
        self.rate = knobs.max_mbps * 1_000_000
        self.copy_started = now
        self.copy_bytes = 0
        self.reserve = None

    # bookkeeping
    def _enter(self, stage: str) -> None:
        self.stage = stage
        self._tick(0, progressed=True)

    def _tick(self, nbytes: int = 0, *, progressed: bool = True, throttle: bool = False) -> None:
        now = time.monotonic()
        if progressed:
            self.last_progress = now
        if self.deadline is not None and time.time() > self.deadline:
            raise OnlineBackupError(self.stage, "deadline reached")
        if self.paused:
            if now - self.last_renew > self.lease_s:
                raise OnlineBackupError(self.stage, "run INVALID (lease lapsed)")
            if (
                now - self.last_renew >= self.renew_every
                and now - self.last_progress <= PROGRESS_WINDOW_S
            ):
                self.control.renew(self.stage)
                self.last_renew = time.monotonic()
        if now - self.last_monitor >= MONITOR_EVERY_S:
            self.last_monitor = now
            self._monitor()
        if self.progress is not None and now - self.last_beat >= 1.0:
            self.last_beat = now
            self.progress()
        if throttle and nbytes:
            self.copy_bytes += nbytes
            ahead = self.copy_bytes / self.rate - (time.monotonic() - self.copy_started)
            if ahead > 0:
                time.sleep(ahead)

    def _monitor(self) -> None:
        for path in self.pins:
            wal = Path(str(path) + "-wal")
            try:
                if wal.stat().st_size > WAL_MAX_BYTES:
                    raise OnlineBackupError(self.stage, "a pinned database's WAL outgrew its limit")
            except FileNotFoundError:
                pass
        if self.reserve is not None and self._free()[0] < self.reserve:
            raise OnlineBackupError(self.stage, "free space fell below the reserve")

    def _start_copy(self) -> None:
        self.copy_started = time.monotonic()
        self.copy_bytes = 0

    def _free(self) -> tuple[int, int]:
        st = os.statvfs(self.root)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize

    def _seam(self, name: str) -> None:
        directory = self.knobs.seam_dir
        if not directory:
            return
        directory = Path(directory)
        (directory / f"{name}.reached").write_text(str(os.getpid()))
        go = directory / f"{name}.go"
        while not go.exists():
            if self.deadline is not None and time.time() > self.deadline:
                raise OnlineBackupError(self.stage, "deadline reached")
            time.sleep(0.01)

    def _scan(self, workers: bool) -> None:
        try:
            _verify_offline(self.root, self.control.exempt(workers=workers))
        except BackupError as exc:
            pid = re.search(r"process (\d+)", str(exc))
            detail = f"process {pid.group(1)} " if pid else "a process "
            raise OnlineBackupError(
                "writer process", detail + "outside the gateway uses the data directory"
            ) from exc

    # enumeration
    def _worker_roots(self) -> list:
        from .workspace_store import resolve_workspace

        base = self.root / "workspaces"
        if base.is_symlink() or not base.is_dir():
            return []
        roots = []
        for entry in sorted(base.iterdir()):
            if ".generation-" in entry.name or entry.is_symlink() or not entry.is_dir():
                continue
            try:
                active = resolve_workspace(entry)
            except ValueError as exc:
                raise OnlineBackupError("enumerate", "a workspace pointer is invalid") from exc
            roots.extend(active / name for name in WORKER_DIRS)
        return roots

    def _walk(self) -> _Tree:
        files, dirs = _inventory(
            self.root, exclude=(FENCE_NAME, STAGING_NAME), volatile_suffixes=SIDECARS
        )
        workers = self._worker_roots()
        tree = _Tree(dirs=dirs)
        for path in files:
            if path.name.endswith(SIDECARS):
                continue
            if _is_core(path):
                tree.cores += 1
                continue
            tree.sizes[path] = path.lstat().st_size
            if any(_inside(path, root) for root in workers):
                tree.worker.append(path)
                continue
            header = _header(path)
            if len(header) == 100 and header.startswith(_SQLITE_HEADER):
                (tree.wal if header[18] == 2 else tree.rollback).append(path)
            else:
                tree.other.append(path)
        return tree

    def _refuse_encrypted(self, tree: _Tree) -> None:
        for path in tree.sizes:
            if _encrypted_database(path):
                raise OnlineBackupError(
                    "encrypted", "online backup does not copy encrypted databases"
                )

    def _check_rollback(self, tree: _Tree) -> None:
        large = [p for p in tree.rollback if tree.sizes[p] > ROLLBACK_MAX_BYTES]
        if large:
            raise OnlineBackupError(
                "rollback journal",
                f"{len(large)} rollback-journal databases exceed {ROLLBACK_MAX_BYTES} bytes",
            )

    def _check_disk(self, tree: _Tree) -> None:
        from .backup_pause import reserve_bytes

        free, total = self._free()
        self.reserve = reserve_bytes(self.knobs, total)
        if free - tree.total() < self.reserve:
            raise OnlineBackupError("disk", "not enough free space for the snapshot and reserve")

    def _probe(self, tree: _Tree) -> Optional[float]:
        if not tree.worker:
            return None
        source = max(tree.worker, key=lambda p: tree.sizes[p])
        size = min(PROBE_BYTES, tree.sizes[source])
        if size < PROBE_MIN_BYTES:
            return None
        target = self.partial / ".probe"
        started = time.monotonic()
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self._copy_fd(fd, size, target, throttle=False)
        finally:
            os.close(fd)
            target.unlink(missing_ok=True)
        return size / max(time.monotonic() - started, 1e-6)

    def _check_pause(self, tree: _Tree, measured: Optional[float]) -> float:
        rate = min(measured or float("inf"), self.rate)
        estimate = tree.total(tree.worker) / rate
        if estimate > PAUSE_ESTIMATE_SHARE * self.control.pause_max_s:
            raise OnlineBackupError(
                "pause estimate", f"the paused copy needs about {estimate:.0f} s"
            )
        return estimate

    # copying
    def _target(self, path: Path) -> Path:
        return self.partial / path.relative_to(self.root)

    def _copy_fd(self, fd: int, size: int, target: Path, *, throttle: bool = True) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        out = os.open(target, flags, 0o600)
        try:
            ranged = hasattr(os, "copy_file_range")
            offset = 0
            while offset < size:
                count = min(CHUNK_BYTES, size - offset)
                done = None
                if ranged:
                    try:
                        done = os.copy_file_range(fd, out, count, offset, offset)
                    except OSError as exc:
                        if exc.errno not in _NO_COPY_RANGE:
                            raise
                        ranged = False
                if done is None:
                    data = os.pread(fd, count, offset)
                    view, position = memoryview(data), offset
                    while view:
                        written = os.pwrite(out, view, position)
                        view, position = view[written:], position + written
                    done = len(data)
                if done == 0:
                    raise OnlineBackupError(self.stage, "a file shrank during the copy")
                try:
                    os.posix_fadvise(fd, offset, done, os.POSIX_FADV_DONTNEED)
                except (AttributeError, OSError):
                    pass
                offset += done
                self._tick(done, throttle=throttle)
            os.fsync(out)
        finally:
            os.close(out)

    def _copy_pinned(self, path: Path) -> None:
        pin = self.pins[path]
        target = self._target(path)
        page = pin.execute("PRAGMA page_size").fetchone()[0]
        low, stalled_at = None, time.monotonic() + STALL_S
        last = 0

        def step(_status, remaining, total):
            nonlocal low, stalled_at, last
            now = time.monotonic()
            if low is None or remaining < low:
                low, stalled_at = remaining, now + STALL_S
            if now > stalled_at:
                raise OnlineBackupError(self.stage, "a database copy stalled")
            done = (total - remaining) * page
            self._tick(max(done - last, 0), throttle=True)
            last = done

        dst = sqlite3.connect(target)
        try:
            pin.backup(dst, pages=4096, progress=step, sleep=0)
            dst.execute("PRAGMA journal_mode = DELETE")
            if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise OnlineBackupError(self.stage, "database integrity check failed")
        finally:
            dst.close()
        target.chmod(0o600)
        _fsync_path(target)
        self._release_pin(path)
        self.copied.append(target)

    def _release_pin(self, path: Path) -> None:
        pin = self.pins.pop(path, None)
        if pin is not None:
            try:
                if pin.in_transaction:
                    pin.execute("ROLLBACK")
            finally:
                pin.close()

    def _barrier(self, tree: _Tree) -> int:
        databases = tree.databases
        give_up = time.monotonic() + BARRIER_S
        attempts = 0
        while True:
            attempts += 1
            writers = []
            busy = False
            try:
                try:
                    for path in databases:
                        conn = _open_db(path, timeout=0)
                        writers.append(conn)
                        conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if not _busy(exc):
                        raise OnlineBackupError(
                            "barrier", "a database could not be locked"
                        ) from exc
                    if time.monotonic() >= give_up:
                        raise OnlineBackupError(
                            "barrier", f"the database lock set stayed busy for {BARRIER_S:.0f} s"
                        ) from exc
                    busy = True
                if not busy:
                    # Every lock held: this is the instant L.
                    for path in databases:
                        pin = _open_db(path, timeout=1)
                        self.pins[path] = pin
                        pin.execute("BEGIN")
                        pin.execute("SELECT count(*) FROM sqlite_master").fetchone()
                    for path in tree.other:
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                        self.fds[path] = (fd, os.fstat(fd).st_size)
                    return attempts
            finally:
                for conn in writers:
                    try:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                    finally:
                        conn.close()
            time.sleep(0.02)
            self._tick(0, progressed=False)

    def _prepare_worker_db(self, path: Path) -> None:
        conn = _open_db(path, timeout=5)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # recovers a hot journal
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                busy, _log, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if busy:
                    raise OnlineBackupError("worker copy", "a worker database checkpoint is busy")
        finally:
            conn.close()
        for suffix in ("-wal", "-journal"):
            try:
                if os.stat(str(path) + suffix).st_size:
                    raise OnlineBackupError("worker copy", "a worker database journal is not empty")
            except FileNotFoundError:
                pass

    def _finish_worker_db(self, target: Path) -> None:
        """A raw copy of a WAL database becomes a self-contained rollback-journal file."""
        if _header(target)[18:19] != b"\x02":
            return
        conn = sqlite3.connect(target)
        try:
            conn.execute("PRAGMA journal_mode = DELETE")
        finally:
            conn.close()
        target.chmod(0o600)
        _fsync_path(target)

    def _check_handoffs(self) -> int:
        from .workspace_store import thread_path, workspace_path

        control = self.partial / "control.sqlite3"
        if not control.is_file() or not _is_sqlite(control):
            raise OnlineBackupError("cut check", "the snapshot has no valid control.sqlite3")
        conn = sqlite3.connect(control.as_uri() + "?immutable=1", uri=True)
        try:
            marks = ",".join("?" * len(HANDOFF_STATES))
            rows = conn.execute(
                f"SELECT workspace_id, stored_thread_id FROM handoffs WHERE state IN ({marks})",
                HANDOFF_STATES,
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            rows = []
        finally:
            conn.close()
        wanted: dict = {}
        for workspace_id, stored in rows:
            wanted.setdefault(workspace_id, set()).add(stored)
        missing = 0
        for workspace_id, stored_ids in wanted.items():
            try:
                path = thread_path(workspace_path(self.partial, workspace_id))
            except (ValueError, OSError):
                missing += len(stored_ids)
                continue
            if not path.is_file():
                missing += len(stored_ids)
                continue
            conn = sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)
            try:
                for stored in stored_ids:
                    found = (
                        stored
                        and conn.execute("SELECT 1 FROM threads WHERE id=?", (stored,)).fetchone()
                    )
                    missing += 0 if found else 1
            except sqlite3.OperationalError:
                missing += len(stored_ids)
            finally:
                conn.close()
        if missing:
            raise OnlineBackupError(
                "cut check", f"{missing} handoffs have no stored thread in the snapshot"
            )
        return len(rows)

    def _fsync_dirs(self) -> None:
        for directory, _subdirs, _files in os.walk(self.partial):
            _fsync_path(Path(directory))

    def close(self) -> None:
        for path in list(self.pins):
            try:
                self._release_pin(path)
            except sqlite3.Error:
                pass
        for fd, _size in self.fds.values():
            os.close(fd)
        self.fds.clear()

    # the run
    def run(self) -> dict:
        control = self.control
        started = time.monotonic()

        # 1. preflight
        self._enter("preflight")
        control.hello()
        self.lease_s = control.lease_s
        self.renew_every = min(RENEW_EVERY_S, self.lease_s / 3)
        self._scan(workers=True)
        tree = self._walk()
        self._refuse_encrypted(tree)
        self._check_rollback(tree)
        self._check_disk(tree)
        estimate = self._check_pause(tree, self._probe(tree))
        self.log(
            "preflight",
            files=len(tree.sizes),
            databases=len(tree.databases),
            worker_files=len(tree.worker),
            bytes=tree.total(),
            pause_estimate_s=round(estimate, 3),
        )

        # 2. pause, then freeze
        self._enter("pause")
        control.pause(self.log)
        paused_at = time.monotonic()
        self.paused = control.online
        self.last_renew = paused_at
        self._scan(workers=False)
        self._enter("freeze")
        control.freeze()
        frozen_at = time.monotonic()

        # 3. enumerate
        self._enter("enumerate")
        tree = self._walk()
        self._check_rollback(tree)
        self.log(
            "enumerate",
            files=len(tree.sizes),
            databases=len(tree.databases),
            rollback=len(tree.rollback),
            worker_files=len(tree.worker),
            cores=tree.cores,
            seconds=round(time.monotonic() - frozen_at, 3),
        )
        self._seam("enumerated")

        # 4. barrier, pins and fds; thaw = L
        self._enter("barrier")
        attempts = self._barrier(tree)
        control.thaw()
        self.log(
            "barrier",
            attempts=attempts,
            frozen_s=round(time.monotonic() - frozen_at, 3),
        )
        for directory in tree.dirs:
            self._target(directory).mkdir(mode=0o700)

        # 6. rollback-journal databases first; their pins hold SHARED
        self._enter("copy first")
        self._start_copy()
        first = time.monotonic()
        for path in sorted(tree.rollback):
            self._copy_pinned(path)
        self.log(
            "copy first", databases=len(tree.rollback), seconds=round(time.monotonic() - first, 3)
        )
        self._seam("pinned")

        # 7. worker files, paused
        self._enter("worker copy")
        worker_dbs = [p for p in tree.worker if _is_sqlite(p)]
        for path in worker_dbs:
            self._prepare_worker_db(path)
        self._seam("worker-copy")
        self._tick(0)
        self._start_copy()
        for path in tree.worker:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                self._copy_fd(fd, os.fstat(fd).st_size, self._target(path))
            finally:
                os.close(fd)
            self.copied.append(self._target(path))
        self._enter("resume")
        resumed = control.resume()
        self.paused = False
        self.log(
            "resume",
            worker_files=len(tree.worker),
            worker_bytes=tree.total(tree.worker),
            paused_s=round(float(resumed.get("paused_s", time.monotonic() - paused_at)), 3),
        )

        # 8. as of L
        self._enter("copy")
        self._start_copy()
        copy_started = time.monotonic()
        for path in sorted(tree.wal):
            self._copy_pinned(path)
        # Every other file is copied from the descriptor opened at L, up to its
        # size at L. That is a consistent cut only because no gateway or worker
        # path rewrites an existing non-SQLite file in place: they append (logs),
        # create exclusively, or write a temporary file and rename it over the
        # old one (the descriptor keeps the old inode). Worker files are copied
        # above while their workers are paused. A new in-place writer must be
        # paused or gated for the run like the workers.
        for path in sorted(self.fds):
            fd, size = self.fds.pop(path)
            try:
                self._copy_fd(fd, size, self._target(path))
            finally:
                os.close(fd)
            self.copied.append(self._target(path))
        for path in worker_dbs:
            self._finish_worker_db(self._target(path))
        self.log(
            "copy",
            databases=len(tree.wal),
            files=len(tree.other),
            seconds=round(time.monotonic() - copy_started, 3),
        )

        # 9. verify, commit, publish (the rename is the caller's)
        self._enter("cut check")
        handoffs = self._check_handoffs()
        self._enter("verify")
        manifest = {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "files": [_file_entry(target, self.partial) for target in sorted(self.copied)],
        }
        manifest_path = self.partial / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
        _fsync_path(manifest_path)
        try:
            verify_backup(self.partial)
        except OnlineBackupError:
            raise
        except BackupError as exc:
            raise OnlineBackupError("verify", str(exc)) from exc
        self._fsync_dirs()
        self._enter("commit")
        committed = control.commit()
        self.log(
            "commit",
            files=len(manifest["files"]),
            handoffs=handoffs,
            console_drops=committed.get("console_drops", 0),
            seconds=round(time.monotonic() - started, 3),
        )
        return manifest


def _default_log(stage: str, **fields) -> None:
    detail = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"commonplace backup: {stage} {detail}".rstrip(), file=sys.stderr, flush=True)


def _raise_nofile() -> None:
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft != hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    except (ImportError, ValueError, OSError):
        pass


def backup_online(
    data_dir,
    dest,
    *,
    client,
    deadline: Optional[float] = None,
    knobs=None,
    log: Optional[Callable[..., None]] = None,
    progress: Optional[Callable[[], None]] = None,
) -> dict:
    """Take an online snapshot of ``data_dir`` into DEST and return its manifest.

    The supervised snapshot child calls this once, holding ``<stamp>.lock``
    and the fence exclusive and, when ``client`` is None (no gateway answers),
    ``data_lock`` exclusive; it refuses to run otherwise. ``client`` is the
    gateway run (``call(op, **fields)``, as ``backup_pause.PauseClient`` or the
    supervisor's relay) or None. ``deadline`` is an epoch second
    (``--deadline``); default now + ``COMMONPLACE_BACKUP_DEADLINE``.
    ``log(stage, **counts)`` receives the stage lines; ``progress()`` is called
    at most once a second while the run makes progress. Raises
    :class:`OnlineBackupError` (or :class:`~commonplace.backup.BackupError`);
    then nothing is published and the gateway run is aborted.
    """
    from .backup_pause import Knobs

    knobs = knobs if knobs is not None else Knobs.from_env()
    log = log or _default_log
    root = Path(data_dir).expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise BackupError("data directory must be an existing directory, without a symlink")
    root = root.resolve()
    if deadline is None:
        deadline = time.time() + knobs.deadline_s
    dest, stamp = _resolve_dest(root, dest)
    staging = dest.parent
    _ensure_staging(staging)
    held = {
        "run lock": _lock_held(staging / f"{stamp}.lock"),
        "fence": _lock_held(root / FENCE_NAME, only_exclusive=True),
    }
    if client is None:
        held["data lock"] = _lock_held(root / LOCK_NAME, only_exclusive=True)
    for stage, ok in held.items():
        if not ok:
            raise OnlineBackupError(stage, "the snapshot caller does not hold this lock")
    for name in os.listdir(staging):
        if name.endswith(".lock") and name != f"{stamp}.lock" and _lock_held(staging / name):
            raise OnlineBackupError("previous run", "previous backup still running")
    _raise_nofile()
    partial = staging / f"{stamp}.partial"
    os.mkdir(partial, 0o700)
    control = _Gateway(client, root, knobs) if client is not None else _NoGateway()
    try:
        cut = _Cut(root, partial, control, knobs, deadline, log, progress)
        try:
            manifest = cut.run()
        finally:
            cut.close()
        if os.path.lexists(dest):
            raise OnlineBackupError("publish", "the destination appeared during the run")
        os.rename(partial, dest)
        partial = None
        _fsync_path(staging)
        log("publish", files=len(manifest["files"]))
        return manifest
    except BaseException as exc:
        control.abort()
        if isinstance(exc, OnlineBackupError):
            log("failed", at=exc.stage)
        raise
    finally:
        if partial is not None:
            shutil.rmtree(partial, ignore_errors=True)
