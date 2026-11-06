"""Admission gate for the online backup cut, and the successor registry.

Two process-local pieces the gateway shares with the online backup:

``GATE`` (:class:`AdmissionGate`)
    Every code path of the gateway that creates, deletes or renames a file or
    directory under the data directory, or opens a SQLite database that may not
    exist yet, runs inside ``with GATE.creating(...)``. The backup calls
    :meth:`AdmissionGate.freeze` once it has paused the workers: new creators
    wait, running ones drain (``timeout``, default 5 s), and the gate stays
    frozen while the backup enumerates the data tree and takes its all-database
    barrier. :meth:`AdmissionGate.thaw` reopens it. A freeze carries a lease
    (``COMMONPLACE_BACKUP_FREEZE_LEASE_S``, default 10 s): when it lapses the
    gate thaws by itself and ``on_lapse`` runs, so a stuck backup can never
    hold creators for longer than the lease.

    ``creating()`` is shared and re-entrant per thread: a thread that is
    already inside a ``creating()`` block never waits for a nested one, even
    while a freeze is draining, so nested calls (a store opening its thread
    database) cannot deadlock against a freeze.

``REGISTRY`` (:class:`SuccessorRegistry`)
    Successor agents that the gateway launches as local processes
    (``handoff.launchers`` ``command`` kind, ``start_new_session=True``) are
    recorded in ``successors.json`` (mode 0600) next to the admin socket, in
    ``/tmp/commonplace-<uid>/`` by default and never under the data directory.
    A launch writes a ``starting`` entry and bumps the launch counter *before*
    it spawns, then replaces the entry with ``(pid, starttime, sid,
    sid_leader_starttime)``; a failed spawn removes the entry and keeps the
    bump. The backup refuses while any entry is live and ends INVALID when the
    counter moved during its run.

Liveness: a process is live when ``/proc/<pid>/stat`` exists and its state is
not ``Z`` or ``X``. An entry is live while its ``(pid, starttime)`` is live, or
while any live process belongs to its session; when a live process has
``pid == sid`` but a different starttime than the recorded session leader, it
leads a different session and the old session is empty. A ``starting`` entry
is always live and is only resolved at :meth:`SuccessorRegistry.rehydrate`
(gateway start). Dead entries are pruned whenever the registry is read for a
decision (``hello``, preflight, rehydration).

Threat model: the registry covers successors launched by this gateway and
their sessions. A same-user process that deliberately leaves its session is
out of scope.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock; the thread lock still applies
    fcntl = None

__all__ = [
    "GATE",
    "REGISTRY",
    "AdmissionError",
    "AdmissionGate",
    "FreezeTimeout",
    "GateBusy",
    "RegistryError",
    "SuccessorRegistry",
    "admin_dir",
    "freeze_lease_s",
    "process_info",
    "registry_path",
]

ADMIN_SOCKET_ENV = "COMMONPLACE_ADMIN_SOCKET"
FREEZE_LEASE_ENV = "COMMONPLACE_BACKUP_FREEZE_LEASE_S"
DEFAULT_FREEZE_LEASE_S = 10.0
DEFAULT_DRAIN_S = 5.0
REGISTRY_NAME = "successors.json"
REGISTRY_VERSION = 1


class AdmissionError(RuntimeError):
    """The gate or the registry cannot give a safe answer."""


class FreezeTimeout(AdmissionError):
    """Running creators did not drain before the freeze timeout."""


class GateBusy(AdmissionError, TimeoutError):
    """``creating(timeout=...)`` could not enter before its timeout."""


class RegistryError(AdmissionError):
    """The successor registry is unreadable, unsafe or cannot be written."""


def freeze_lease_s() -> float:
    """The freeze lease in seconds (``COMMONPLACE_BACKUP_FREEZE_LEASE_S``, default 10)."""
    raw = os.environ.get(FREEZE_LEASE_ENV, "")
    try:
        value = float(raw) if raw else DEFAULT_FREEZE_LEASE_S
    except ValueError:
        return DEFAULT_FREEZE_LEASE_S
    return value if 0 < value < float("inf") else DEFAULT_FREEZE_LEASE_S


# ── admission gate ──


class AdmissionGate:
    """Shared creators, one exclusive freeze. See the module docstring."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._local = threading.local()
        self._active = 0
        self._pending = False
        self._frozen = False
        self._epoch = 0
        self._timer: Optional[threading.Timer] = None
        self._lapsed = False
        self._busy = 0

    @property
    def frozen(self) -> bool:
        """True between a successful :meth:`freeze` and its thaw or lapse."""
        with self._cond:
            return self._frozen

    @property
    def active(self) -> int:
        """Creator blocks currently running (nested blocks count once per thread)."""
        with self._cond:
            return self._active

    @property
    def busy(self) -> int:
        """How many ``creating(timeout=...)`` calls gave up (``GateBusy``) so far.

        Monotonic; a backup reads it before and after its cut to report drops.
        """
        with self._cond:
            return self._busy

    @contextmanager
    def creating(self, *paths, timeout: Optional[float] = None) -> Iterator[None]:
        """Run a create/delete/rename block under the shared side of the gate.

        Waits while a freeze is draining or holds the gate (bounded by the
        drain timeout or the freeze lease). Re-entrant per thread: a nested
        call never waits.

        ``paths``: optional files the block may create. While the gate is
        frozen and every one of them already exists, the block creates none of
        the named paths (deletes are gated too), so it runs without waiting and
        uncounted: a freeze does not drain it. This keeps plain opens of
        existing databases from stalling behind a freeze. Such a block may
        still create SQLite ``-wal``/``-shm``/``-journal`` sidecars of those
        databases; the snapshot never copies sidecars, so "nothing is created
        while frozen" holds for every path the snapshot reads.

        ``timeout``: give up waiting after this many seconds and raise
        :class:`GateBusy` (for callers that would rather drop than wait, such as
        the console activity log). ``None`` waits until the gate opens.
        """
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        counted = True
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while self._pending or self._frozen:
                if self._frozen and not self._pending and paths and _all_exist(paths):
                    counted = False
                    break
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._busy += 1
                    raise GateBusy("admission gate is frozen for a backup")
                self._cond.wait(remaining)
            if counted:
                self._active += 1
                self._local.depth = 1
        if not counted:
            # Frozen, and every path exists: nothing to create, so no waiting.
            yield
            return
        try:
            yield
        finally:
            with self._cond:
                self._local.depth = 0
                self._active -= 1
                if self._active == 0:
                    self._cond.notify_all()

    def freeze(
        self,
        timeout: float = DEFAULT_DRAIN_S,
        *,
        lease_s: Optional[float] = None,
        on_lapse: Optional[Callable[[], None]] = None,
    ) -> int:
        """Hold the gate exclusively; returns the freeze epoch.

        New creators wait at once; running ones get ``timeout`` seconds to
        finish, else :class:`FreezeTimeout` is raised and the gate is open again.
        On success the gate stays frozen until :meth:`thaw` or until ``lease_s``
        (default :func:`freeze_lease_s`) lapses; a lapse thaws the gate and calls
        ``on_lapse()`` from a timer thread (the backup marks its run INVALID).
        Raises :class:`AdmissionError` when a freeze is already held or pending,
        or when called from inside a ``creating()`` block.
        """
        if getattr(self._local, "depth", 0):
            raise AdmissionError("freeze called inside a creator block")
        lease = freeze_lease_s() if lease_s is None else float(lease_s)
        if not 0 < lease < float("inf"):
            raise AdmissionError("freeze lease must be positive and finite")
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            if self._pending or self._frozen:
                raise AdmissionError("admission gate is already frozen")
            self._pending = True
            try:
                while self._active:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise FreezeTimeout(
                            f"{self._active} creator(s) still running after {timeout:g} s"
                        )
                    self._cond.wait(remaining)
            except BaseException:
                self._pending = False
                self._cond.notify_all()
                raise
            self._pending = False
            self._frozen = True
            self._lapsed = False
            self._cond.notify_all()  # waiters on existing paths may now pass
            self._epoch += 1
            epoch = self._epoch
            timer = threading.Timer(lease, self._lapse, args=(epoch, on_lapse))
            timer.daemon = True
            self._timer = timer
            timer.start()
            return epoch

    def thaw(self) -> bool:
        """Reopen the gate. True when the freeze was still held (lease intact);
        False when there was nothing to thaw (it lapsed or never froze)."""
        with self._cond:
            timer, self._timer = self._timer, None
            held = self._frozen
            self._frozen = False
            self._cond.notify_all()
        if timer is not None:
            timer.cancel()
        return held

    @property
    def lapsed(self) -> bool:
        """True when the last freeze ended by its lease, not by :meth:`thaw`."""
        with self._cond:
            return self._lapsed

    def _lapse(self, epoch: int, on_lapse: Optional[Callable[[], None]]) -> None:
        with self._cond:
            if not self._frozen or epoch != self._epoch:
                return
            self._frozen = False
            self._lapsed = True
            self._timer = None
            self._cond.notify_all()
        if on_lapse is not None:
            on_lapse()


def _all_exist(paths) -> bool:
    return all(os.path.lexists(os.fspath(path)) for path in paths)


GATE = AdmissionGate()


# ── /proc helpers ──


def process_info(pid: int, *, proc: str = "/proc") -> Optional[dict]:
    """``{"pid", "state", "sid", "starttime"}`` for a live process, else None.

    Live means ``<proc>/<pid>/stat`` exists and the state is not ``Z``/``X``.
    """
    try:
        with open(f"{proc}/{int(pid)}/stat", "rb") as stream:
            raw = stream.read().decode("ascii", "replace")
    except (OSError, ValueError):
        return None
    try:
        rest = raw[raw.rindex(")") + 2 :].split()
        state, sid, starttime = rest[0], int(rest[3]), int(rest[19])
    except (ValueError, IndexError):
        return None
    if state in ("Z", "X", "x"):
        return None
    return {"pid": int(pid), "state": state, "sid": sid, "starttime": starttime}


def _live_processes(proc: str) -> list:
    result = []
    try:
        names = os.listdir(proc)
    except OSError as error:
        raise RegistryError(f"cannot list processes: {error.strerror}") from None
    for name in names:
        if name.isdigit():
            info = process_info(int(name), proc=proc)
            if info is not None:
                result.append(info)
    return result


def _environ_has(pid: int, marker: bytes, proc: str) -> bool:
    try:
        with open(f"{proc}/{pid}/environ", "rb") as stream:
            return marker in stream.read().split(b"\0")
    except OSError:
        return False


# ── successor registry ──


def admin_dir() -> Path:
    """Directory of the admin socket and the registry (never under the data dir).

    ``dirname($COMMONPLACE_ADMIN_SOCKET)`` when set, else ``/tmp/commonplace-<uid>``.
    """
    socket_path = os.environ.get(ADMIN_SOCKET_ENV)
    if socket_path:
        return Path(socket_path).parent
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(tempfile.gettempdir() if os.name == "nt" else "/tmp") / f"commonplace-{uid}"


def registry_path() -> Path:
    """``admin_dir() / "successors.json"``."""
    return admin_dir() / REGISTRY_NAME


def _private_admin_dir(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = os.lstat(directory)
    except OSError as error:
        raise RegistryError(f"admin directory unavailable: {error.strerror}") from None
    if not stat.S_ISDIR(info.st_mode):
        raise RegistryError("admin directory must be a private directory owned by this user")
    if hasattr(os, "geteuid") and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise RegistryError("admin directory must be a private directory owned by this user")


class SuccessorRegistry:
    """Persisted successor entries plus a monotonic launch counter.

    File format (``successors.json``)::

        {"version": 1, "counter": <int>, "entries": [<entry>, ...]}

    Entries: ``{"state": "starting", "token", "handoff_id", "created_at"}`` or
    ``{"state": "running", "token", "handoff_id", "pid", "starttime", "sid",
    "sid_leader_starttime"}``. All methods are thread-safe and take an advisory
    ``flock`` on ``successors.json.lock`` so processes sharing the directory
    serialize. Every failure to read or write raises :class:`RegistryError`
    (callers fail closed).
    """

    def __init__(self, path=None, *, proc: str = "/proc") -> None:
        self._path = Path(path) if path is not None else None
        self._proc = proc
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        """The registry file; resolved from the environment on every call unless fixed."""
        return self._path if self._path is not None else registry_path()

    # launch side

    def begin_launch(self, handoff_id: str) -> str:
        """Record a ``starting`` entry and bump the counter, before spawning.

        Returns the entry token for :meth:`launched` / :meth:`launch_failed`.
        """
        token = secrets.token_hex(8)
        entry = {
            "state": "starting",
            "token": token,
            "handoff_id": str(handoff_id),
            "created_at": time.time(),
        }

        def change(data):
            data["counter"] += 1
            data["entries"].append(entry)

        self._update(change)
        return token

    def launched(self, token: str, pid: int) -> dict:
        """Replace the ``starting`` entry with the spawned process's identity.

        The child was started with ``start_new_session=True``, so it leads its
        own session. When ``/proc`` no longer shows it (it already exited), the
        entry records its pid as sid with unknown starttimes (-1), so only a
        live member of that session keeps it.
        """
        info = process_info(pid, proc=self._proc)
        record = {
            "state": "running",
            "pid": int(pid),
            "starttime": info["starttime"] if info else -1,
            "sid": info["sid"] if info else int(pid),
        }
        leader = process_info(record["sid"], proc=self._proc)
        record["sid_leader_starttime"] = leader["starttime"] if leader else -1
        result = {}

        def change(data):
            for entry in data["entries"]:
                if entry.get("token") == token:
                    handoff_id = entry.get("handoff_id")
                    entry.clear()
                    entry.update(token=token, handoff_id=handoff_id, **record)
                    result.update(entry)
                    return
            data["entries"].append({"token": token, "handoff_id": None, **record})
            result.update(data["entries"][-1])

        self._update(change)
        return dict(result)

    def launch_failed(self, token: str) -> None:
        """Remove the entry for a spawn that failed; the counter bump stays."""

        def change(data):
            data["entries"] = [e for e in data["entries"] if e.get("token") != token]

        self._update(change)

    @contextmanager
    def launching(self, handoff_id: str) -> Iterator[Callable[[int], dict]]:
        """``with REGISTRY.launching(id) as started: proc = Popen(...); started(proc.pid)``.

        Writes the ``starting`` entry first; if the block raises before
        ``started`` was called, the entry is removed (the bump stays).
        """
        token = self.begin_launch(handoff_id)
        done = []

        def started(pid: int) -> dict:
            done.append(pid)
            return self.launched(token, pid)

        try:
            yield started
        except BaseException:
            if not done:
                self.launch_failed(token)
            raise

    # decision side

    def counter(self) -> int:
        """The launch counter (monotonic; 0 when no registry file exists)."""
        return self._read_locked()["counter"]

    def entries(self) -> list:
        """Raw entries as stored, without pruning."""
        return [dict(e) for e in self._read_locked()["entries"]]

    def active(self) -> list:
        """Prune dead entries (persisting the result) and return the live ones.

        Each returned running entry has an extra ``live_pids`` list (sorted).
        ``starting`` entries are always returned and never pruned here.
        """
        live = []

        def change(data):
            processes = _live_processes(self._proc)
            keep = []
            for entry in data["entries"]:
                pids = self._live_pids(entry, processes)
                if pids is None:
                    continue
                keep.append(entry)
                live.append({**entry, "live_pids": pids})
            data["entries"] = keep

        self._update(change, only_if_changed=True)
        return live

    live = active  # alias

    def hello_fields(self) -> dict:
        """Fields for the admin ``hello`` reply (prunes first).

        ``successor_pids``: live pids of every running entry (leaders and
        session members); ``successor_sids``: their session ids;
        ``successors_starting``: handoff ids of ``starting`` entries;
        ``successor_counter``: the launch counter after pruning.
        """
        live = self.active()
        pids, sids, starting = set(), set(), []
        for entry in live:
            if entry["state"] == "starting":
                starting.append(entry.get("handoff_id"))
            else:
                pids.update(entry["live_pids"])
                sids.add(entry["sid"])
        return {
            "successor_pids": sorted(pids),
            "successor_sids": sorted(sids),
            "successors_starting": starting,
            "successor_counter": self.counter(),
        }

    def rehydrate(self) -> list:
        """Gateway start: resolve ``starting`` entries, then prune; returns live entries.

        A ``starting`` entry is adopted when a live process carries
        ``COMMONPLACE_HANDOFF_ID=<handoff_id>`` in its environment (the session
        leader if there is one, else the oldest such process); otherwise it is
        removed.
        """

        def change(data):
            processes = None
            for entry in data["entries"]:
                if entry.get("state") != "starting":
                    continue
                if processes is None:
                    processes = _live_processes(self._proc)
                marker = f"COMMONPLACE_HANDOFF_ID={entry.get('handoff_id')}".encode()
                found = [p for p in processes if _environ_has(p["pid"], marker, self._proc)]
                if not found:
                    entry["state"] = "dropped"
                    continue
                found.sort(key=lambda p: (p["pid"] != p["sid"], p["starttime"], p["pid"]))
                chosen = found[0]
                leader = process_info(chosen["sid"], proc=self._proc)
                handoff_id, token = entry.get("handoff_id"), entry.get("token")
                entry.clear()
                entry.update(
                    state="running",
                    token=token,
                    handoff_id=handoff_id,
                    pid=chosen["pid"],
                    starttime=chosen["starttime"],
                    sid=chosen["sid"],
                    sid_leader_starttime=leader["starttime"] if leader else -1,
                )
            data["entries"] = [e for e in data["entries"] if e.get("state") != "dropped"]

        self._update(change, only_if_changed=True)
        return self.active()

    # internals

    def _live_pids(self, entry: dict, processes: list) -> Optional[list]:
        """Live pids for an entry; None when it is dead. Starting → []."""
        if entry.get("state") == "starting":
            return []
        try:
            pid, starttime = int(entry["pid"]), int(entry["starttime"])
            sid, leader_start = int(entry["sid"]), int(entry["sid_leader_starttime"])
        except (KeyError, TypeError, ValueError):
            raise RegistryError("successor registry entry is malformed") from None
        by_pid = {p["pid"]: p for p in processes}
        pids = set()
        own = by_pid.get(pid)
        if own is not None and own["starttime"] == starttime:
            pids.add(pid)
        leader = by_pid.get(sid)
        if leader is None or leader["starttime"] == leader_start:
            # Leader gone (the session keeps its id) or still the recorded one.
            pids.update(p["pid"] for p in processes if p["sid"] == sid)
        return sorted(pids) if pids else None

    def _read_locked(self) -> dict:
        with self._lock, self._flock():
            return self._load()

    def _update(self, change: Callable[[dict], None], *, only_if_changed: bool = False) -> None:
        with self._lock, self._flock():
            data = self._load()
            before = json.dumps(data, sort_keys=True)
            change(data)
            if only_if_changed and json.dumps(data, sort_keys=True) == before:
                return
            self._store(data)

    @contextmanager
    def _flock(self):
        path = self.path
        if self._path is None:
            _private_admin_dir(path.parent)
        try:
            descriptor = os.open(
                f"{path}.lock",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
        except OSError as error:
            raise RegistryError(f"successor registry lock unavailable: {error.strerror}") from None
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _load(self) -> dict:
        path = self.path
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            if error.errno == errno.ENOENT:
                return {"version": REGISTRY_VERSION, "counter": 0, "entries": []}
            raise RegistryError(f"successor registry unreadable: {error.strerror}") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or (
                hasattr(os, "geteuid") and info.st_uid != os.geteuid()
            ):
                raise RegistryError("successor registry must be a regular file owned by this user")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                data = json.loads(stream.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise RegistryError("successor registry is corrupt") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if (
            not isinstance(data, dict)
            or data.get("version") != REGISTRY_VERSION
            or type(data.get("counter")) is not int
            or not isinstance(data.get("entries"), list)
            or not all(isinstance(e, dict) for e in data["entries"])
        ):
            raise RegistryError("successor registry is corrupt")
        return data

    def _store(self, data: dict) -> None:
        path = self.path
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        except OSError as error:
            raise RegistryError(f"successor registry not writable: {error.strerror}") from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                if hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), 0o600)
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            # rename, not os.replace: same atomic swap on POSIX, and callers' tests
            # that patch os.replace for their own files never see the registry.
            (os.rename if os.name == "posix" else os.replace)(temporary, path)
        except OSError as error:
            raise RegistryError(f"successor registry not writable: {error.strerror}") from None
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)


REGISTRY = SuccessorRegistry()
