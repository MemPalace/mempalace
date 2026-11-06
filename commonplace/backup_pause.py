"""Online backup control: the gateway's pause socket and run fence.

An online backup copies the data directory while ``serve`` keeps running. It
talks to the running gateway over a local Unix socket (never under the data
directory) to pause the memory workers, freeze file creation for a moment,
resume, and finally ask whether the whole run stayed valid.

Socket
    ``$COMMONPLACE_ADMIN_SOCKET``, default ``/tmp/commonplace-<uid>/gateway.sock``.
    The directory is ``0700`` and must be owned by the gateway's uid; the
    socket is ``0600``; every peer's uid is checked with ``SO_PEERCRED``. A
    path inside the data directory is refused. The gateway binds it in
    ``Gateway.serve`` (stale socket unlinked first, a live one is left alone,
    and serve refuses to start while another gateway answers on it) and
    removes it when serve ends. Linux only.

Protocol (version 1)
    JSON lines, UTF-8, one request object per line, one response per request,
    at most ``MAX_LINE_BYTES`` per line. A request is ``{"op": <op>, ...}``;
    a response always carries ``ok`` (bool), ``op`` and ``state``. A refused
    request carries ``error`` (one of the ``E_*`` codes) and, for an invalid
    run, ``reason`` (one of the ``R_*`` reasons).

    One connection is one backup run. ``hello`` opens the run (``ACTIVE``);
    ``hello`` with ``"probe": true`` only reports and opens nothing, and is
    answered even while another run is open. A second non-probe ``hello``
    while a run is open is refused with ``busy``.

    ===========  ===========================  ==========================================
    op           allowed in                    effect / extra response fields
    ===========  ===========================  ==========================================
    hello        no run on this connection     ACTIVE; see ``HELLO_FIELDS``
    pause        ACTIVE                        gate memory calls, drain in-flight calls
                                               (``drain_s``), stop workers (TERM, wait,
                                               KILL, wait, ``/proc`` gone) -> PAUSED;
                                               ``stopped_worker_pids``, ``lease_s``,
                                               ``pause_max_s``. The request may carry
                                               ``lease_s`` / ``pause_max_s``; the run
                                               uses ``min(server, request)`` for each.
                                               ``no_drain``: workers resumed, still
                                               ACTIVE, may be retried.
    renew        PAUSED                        lease = now + the run's ``lease_s``; ``paused_s``
    freeze       PAUSED, not frozen            admission gate frozen (creators drained
                                               within ``creator_drain_s``), freeze lease
                                               ``freeze_lease_s`` starts; ``frozen``
    thaw         PAUSED, frozen                admission gate thawed
    resume       PAUSED                        (thaws if frozen) workers resume ->
                                               RESUMED_OK; the lease ends here
    commit       RESUMED_OK                    -> COMMITTED; ``console_drops``
    abort        any open run                  -> INVALID (``abort``), everything undone
    ===========  ===========================  ==========================================

Run fence
    ``ACTIVE -> PAUSED -> RESUMED_OK -> COMMITTED``. ``INVALID`` is permanent
    for the run; it is set on lease lapse, freeze-lease lapse, ``PAUSE_MAX``,
    a successor launch (the registry counter differs from the one at
    ``hello``), socket EOF before ``COMMITTED``, ``abort``, a failed freeze,
    a worker that will not stop, and gateway shutdown. Whenever a run becomes
    INVALID the gateway thaws the admission gate and resumes the workers at
    once; a backup never leaves serve paused. Every later request on that run
    gets ``{"ok": false, "error": "invalid", "state": "INVALID", "reason": ...}``.

Env knobs (``COMMONPLACE_BACKUP_*``) are read by :meth:`Knobs.from_env`; the
client-side ones (heartbeat, deadline, bandwidth, reserve, seam dir) live here
too so every part of the backup reads one definition.

The admission gate and the successor registry belong to
``commonplace.admission``; :func:`bind_admission` is the only place that
names them.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import stat
import struct
import sys
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Optional, Protocol

logger = logging.getLogger("commonplace.backup_pause")

PROTOCOL_VERSION = 1
SOCKET_ENV = "COMMONPLACE_ADMIN_SOCKET"
SOCKET_NAME = "gateway.sock"
MAX_LINE_BYTES = 64 * 1024
# AF_UNIX sun_path on Linux, including the terminating NUL.
_MAX_SOCKET_PATH = 107
COUNTER_CHECK_S = 1.0

# Run states.
ACTIVE = "ACTIVE"
PAUSED = "PAUSED"
RESUMED_OK = "RESUMED_OK"
COMMITTED = "COMMITTED"
INVALID = "INVALID"
NO_RUN = "NONE"  # reported by a probe hello / refused hello: no run on this connection
RUN_STATES = (ACTIVE, PAUSED, RESUMED_OK, COMMITTED, INVALID)

# Error codes (``error`` in a refused response).
E_BAD_REQUEST = "bad_request"
E_UNKNOWN_OP = "unknown_op"
E_BUSY = "busy"
E_BAD_STATE = "bad_state"
E_NO_DRAIN = "no_drain"
E_INVALID = "invalid"

# INVALID reasons (``reason``).
R_LEASE = "lease lapsed"
R_FREEZE_LEASE = "freeze lease lapsed"
R_PAUSE_MAX = "pause max reached"
R_SUCCESSOR = "successor launched"
R_REGISTRY = "successor registry unavailable"
R_EOF = "socket closed before commit"
R_ABORT = "aborted"
R_CREATOR_DRAIN = "creator drain timeout"
R_WORKER_STOP = "worker did not stop"
R_SHUTDOWN = "gateway shutdown"
R_INTERNAL = "internal error"

OPS = ("hello", "pause", "renew", "freeze", "thaw", "resume", "commit", "abort")

#: Fields of every ``hello`` response (probe or not), besides ok/op/state.
HELLO_FIELDS = (
    "proto",  # PROTOCOL_VERSION
    "gateway_pid",  # os.getpid() of the gateway
    "data_dir",  # realpath of the gateway's data directory; a client must match it
    "worker_pids",  # pids of live workers, sorted
    "successors",  # live registry entries after prune (dicts, see Admission.successors)
    "successor_pids",  # sorted live pids of running entries (leaders and session members)
    "successor_sids",  # sorted sids of those entries
    "successors_starting",  # handoff ids of entries still in their pre-spawn ``starting`` state
    "successor_active",  # bool: any live entry, or the registry could not be read
    "successor_error",  # None, or why the registry could not be read (fail closed)
    "successor_counter",  # registry launch counter; commit needs it unchanged
    "busy",  # bool: another run is open
    "knobs",  # lease_s, freeze_lease_s, pause_max_s, drain_s, creator_drain_s
)


# ── knobs ──
@dataclass(frozen=True)
class Knobs:
    """``COMMONPLACE_BACKUP_<NAME>`` environment knobs (seconds unless noted)."""

    lease_s: float = 60.0  # LEASE_S: pause lease, renewed by ``renew``
    heartbeat_stale_s: float = 60.0  # HEARTBEAT_STALE_S: supervisor/hook side
    freeze_lease_s: float = 10.0  # FREEZE_LEASE_S: auto-thaw + INVALID after this
    deadline_s: float = 4500.0  # DEADLINE: whole-run budget (hook/supervisor)
    pause_max_s: float = 300.0  # PAUSE_MAX: a pause never lasts longer
    max_mbps: float = 200.0  # MAX_MBPS: copy bandwidth cap, MB/s
    reserve_bytes: Optional[int] = None  # RESERVE_BYTES: None -> max(10 GB, 5 %)
    seam_dir: Optional[str] = None  # SEAM_DIR: tests only
    drain_s: float = 60.0  # DRAIN_S: in-flight memory calls to drain at pause
    creator_drain_s: float = 5.0  # CREATOR_DRAIN_S: GATE.freeze timeout
    memory_wait_s: float = 20.0  # MEMORY_WAIT_S: a memory call waits this long in a pause
    fence_wait_s: float = 60.0  # FENCE_WAIT_S: the backup waits this long for one-shot CLIs
    cli_fence_wait_s: float = 30.0  # COMMONPLACE_FENCE_WAIT_S: a one-shot CLI waits for a backup

    _ENV = {
        "lease_s": "LEASE_S",
        "heartbeat_stale_s": "HEARTBEAT_STALE_S",
        "freeze_lease_s": "FREEZE_LEASE_S",
        "deadline_s": "DEADLINE",
        "pause_max_s": "PAUSE_MAX",
        "max_mbps": "MAX_MBPS",
        "reserve_bytes": "RESERVE_BYTES",
        "seam_dir": "SEAM_DIR",
        "drain_s": "DRAIN_S",
        "creator_drain_s": "CREATOR_DRAIN_S",
        "memory_wait_s": "MEMORY_WAIT_S",
        "fence_wait_s": "FENCE_WAIT_S",
        "cli_fence_wait_s": None,  # COMMONPLACE_FENCE_WAIT_S (read by every one-shot CLI)
    }
    #: Fence waits may be 0 (fail at once); every other knob must be positive.
    _ZERO_OK = frozenset({"fence_wait_s", "cli_fence_wait_s"})

    @staticmethod
    def env_name(name: str) -> str:
        suffix = Knobs._ENV[name]
        return "COMMONPLACE_FENCE_WAIT_S" if suffix is None else "COMMONPLACE_BACKUP_" + suffix

    @classmethod
    def read(cls, name: str, environ=None):
        """One knob from the environment (its default when unset); ValueError when bad."""
        env = os.environ if environ is None else environ
        default = {f.name: f.default for f in fields(cls)}[name]
        key = cls.env_name(name)
        raw = env.get(key)
        if raw is None or raw == "":
            return default
        if name == "seam_dir":
            return raw
        number = int(raw) if name == "reserve_bytes" else float(raw)
        low_ok = number >= 0 if name in cls._ZERO_OK else number > 0
        if not (low_ok and number < float("inf")):
            raise ValueError(
                f"{key} must be {'non-negative' if name in cls._ZERO_OK else 'positive'}"
            )
        return number

    @classmethod
    def from_env(cls, environ=None) -> "Knobs":
        """Read the knobs; an unparsable or non-positive value raises ValueError.

        The two fence waits may be 0 (no wait).
        """
        return cls(**{f.name: cls.read(f.name, environ) for f in fields(cls)})

    def server_view(self) -> dict:
        return {
            "lease_s": self.lease_s,
            "freeze_lease_s": self.freeze_lease_s,
            "pause_max_s": self.pause_max_s,
            "drain_s": self.drain_s,
            "creator_drain_s": self.creator_drain_s,
        }


def reserve_bytes(knobs: Knobs, fs_total_bytes: int) -> int:
    """Free space the backup must leave: RESERVE_BYTES, else max(10 GB, 5 %)."""
    if knobs.reserve_bytes is not None:
        return int(knobs.reserve_bytes)
    return max(10 * 1000**3, int(fs_total_bytes) // 20)


# ── socket path ──
class UnsafeSocketPath(ValueError):
    """The admin socket path or its directory is not safe to use."""


def socket_path(environ=None) -> Path:
    """``$COMMONPLACE_ADMIN_SOCKET``, else ``/tmp/commonplace-<uid>/gateway.sock``."""
    env = os.environ if environ is None else environ
    raw = env.get(SOCKET_ENV)
    if raw:
        return Path(raw)
    # A literal /tmp: the gateway and a ``compose exec``'d CLI may see
    # different TMPDIRs but always share the container's /tmp.
    return Path("/tmp") / f"commonplace-{os.getuid()}" / SOCKET_NAME


def check_socket_path(path, data_dir) -> Path:
    """Refuse a socket path inside ``data_dir`` (resolved) or too long to bind."""
    path = Path(path)
    if not path.is_absolute():
        raise UnsafeSocketPath("the admin socket path must be absolute")
    if len(os.fsencode(str(path))) > _MAX_SOCKET_PATH:
        raise UnsafeSocketPath("the admin socket path is too long")
    real = Path(os.path.realpath(path.parent)) / path.name
    data = Path(os.path.realpath(data_dir))
    if real == data or data in real.parents:
        raise UnsafeSocketPath("the admin socket must not be inside the data directory")
    return path


def prepare_socket_dir(directory, uid: Optional[int] = None) -> Path:
    """Create the socket's directory 0700, or check an existing one is ours."""
    directory = Path(directory)
    uid = os.getuid() if uid is None else uid
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(directory)
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeSocketPath("the admin socket directory is not a directory")
    if st.st_uid != uid:
        raise UnsafeSocketPath("the admin socket directory belongs to another user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(directory, 0o700)
    return directory


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def supported() -> bool:
    return sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED")


# ── admission (commonplace.admission) ──
class Admission(Protocol):
    """What the pause server needs from the admission gate and successor registry."""

    def freeze(self, timeout: float, lease_s: float, on_lapse: Callable[[], None]) -> object:
        """Block new creators, drain running ones within ``timeout`` (raise if not).

        The freeze holds until :meth:`thaw` or until ``lease_s`` lapses; a lapse
        thaws by itself and calls ``on_lapse``.
        """

    def thaw(self) -> Optional[bool]:
        """Reopen the gate. ``False`` when the freeze had already lapsed."""

    def successors(self) -> list:
        """Live registry entries after prune-on-empty, in this pid namespace."""

    def hello_fields(self) -> dict:
        """``successor_pids``, ``successor_sids``, ``successors_starting``, ``successor_counter``."""

    def successor_counter(self) -> int:
        """Launch counter, bumped before every registered successor spawn."""

    def rehydrate(self) -> object:
        """Load the persisted registry at gateway start and prune dead entries."""


class _ModuleAdmission:
    """Binds :class:`Admission` to ``commonplace.admission`` (the one place names live)."""

    def __init__(self, module):
        self._m = module

    def freeze(self, timeout: float, lease_s: float, on_lapse: Callable[[], None]) -> object:
        return self._m.GATE.freeze(timeout=timeout, lease_s=lease_s, on_lapse=on_lapse)

    def thaw(self) -> Optional[bool]:
        return self._m.GATE.thaw()

    def successors(self) -> list:
        return [dict(e) for e in self._m.REGISTRY.active()]

    def hello_fields(self) -> dict:
        return dict(self._m.REGISTRY.hello_fields())

    def successor_counter(self) -> int:
        return int(self._m.REGISTRY.counter())

    def rehydrate(self) -> object:
        return self._m.REGISTRY.rehydrate()


def bind_admission() -> Admission:
    from . import admission

    return _ModuleAdmission(admission)


# ── console drops (informational, reported by commit) ──
_drops_lock = threading.Lock()
_console_drops = 0


def record_console_drop(n: int = 1) -> None:
    """Count a console write dropped because file creation was frozen."""
    global _console_drops
    with _drops_lock:
        _console_drops += n


def console_drops() -> int:
    with _drops_lock:
        return _console_drops


# ── server ──
@dataclass
class _Run:
    counter0: int
    console0: int
    state: str = ACTIVE
    reason: Optional[str] = None
    paused: bool = False  # the supervisor is paused for this run
    frozen: bool = False  # the admission gate is frozen for this run
    pause_started: float = 0.0
    lease_deadline: float = 0.0
    freeze_deadline: float = 0.0
    pause_deadline: float = 0.0
    lease_s: Optional[float] = None  # min(server, request) at pause
    pause_max_s: Optional[float] = None  # min(server, request) at pause


def _requested(value, server: float) -> float:
    """``min(server, request)``; a missing or unusable request keeps the server's value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return server
    if not 0 < value < float("inf"):
        return server
    return min(server, float(value))


_ACTIVE_SERVER: Optional["PauseServer"] = None


def invalidate_active_run(reason: str) -> bool:
    """Mark the open run INVALID (e.g. the admission gate's own lease lapsed)."""
    server = _ACTIVE_SERVER
    return server.invalidate(reason) if server is not None else False


class PauseServer:
    """The gateway side: binds the admin socket and runs the fence.

    ``supervisor`` is the gateway's :class:`~commonplace.workers.WorkerSupervisor`
    (``pause``/``resume``/``worker_pids``). ``start`` never raises: it returns
    False and sets ``start_error`` when the socket cannot be bound (for example
    another gateway answers on it). ``Gateway.serve`` then refuses to run on a
    supported platform: one gateway per data directory.
    """

    def __init__(
        self,
        supervisor,
        data_dir,
        *,
        path=None,
        admission: Optional[Admission] = None,
        knobs: Optional[Knobs] = None,
        clock: Callable[[], float] = time.monotonic,
        tick_s: float = 0.05,
    ):
        self.supervisor = supervisor
        self.data_dir = os.path.realpath(data_dir)
        self.path = Path(path) if path is not None else None
        self.admission = admission
        self.knobs = knobs
        self.clock = clock
        self.tick_s = tick_s
        self._uid = os.getuid() if hasattr(os, "getuid") else -1
        self._lock = threading.RLock()
        self._run: Optional[_Run] = None
        self._sock: Optional[socket.socket] = None
        self._ident: Optional[tuple] = None
        self._stop = threading.Event()
        self._threads: list = []
        self._conns: set = set()
        self._registry_error: Optional[str] = None
        self._next_counter_check = 0.0
        self.start_error: Optional[str] = None

    @property
    def serving(self) -> bool:
        return self._sock is not None

    # lifecycle
    def start(self) -> bool:
        global _ACTIVE_SERVER
        if not supported():
            self.start_error = "not supported on this platform"
            logger.warning("online backup socket is not supported on this platform")
            return False
        try:
            if self.knobs is None:
                self.knobs = Knobs.from_env()
            if self.path is None:
                self.path = socket_path()
            check_socket_path(self.path, self.data_dir)
            if self.admission is None:
                self.admission = bind_admission()
            try:
                self.admission.rehydrate()
            except Exception as exc:  # fail closed: hello reports successors active
                self._registry_error = type(exc).__name__
                logger.error("successor registry could not be loaded: %s", type(exc).__name__)
            prepare_socket_dir(self.path.parent, self._uid)
            self._sock = self._bind(self.path)
        except Exception as exc:
            self.start_error = str(exc) or type(exc).__name__
            logger.error("online backup socket not started: %s", exc)
            return False
        _ACTIVE_SERVER = self
        for target in (self._accept_loop, self._watchdog):
            t = threading.Thread(target=target, name=f"commonplace-{target.__name__}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.info("online backup socket ready")
        return True

    def _bind(self, path: Path) -> socket.socket:
        # Probe, unlink and bind under one exclusive lock in the socket's own
        # (private) directory: two gateways starting at once over a stale socket
        # must not both pass the probe and then unlink each other's socket.
        # The lock file is never removed, so every starter locks the same inode.
        lock_fd = os.open(
            path.parent / f"{path.name}.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            import fcntl  # Linux only, like the socket itself (see supported())

            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return self._bind_locked(path)
        finally:
            os.close(lock_fd)  # also releases the lock

    def _bind_locked(self, path: Path) -> socket.socket:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            st = None
        if st is not None:
            if not stat.S_ISSOCK(st.st_mode):
                raise UnsafeSocketPath("the admin socket path exists and is not a socket")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1.0)
            try:
                probe.connect(str(path))
            except OSError:
                os.unlink(path)  # stale: nobody listens
            else:
                raise UnsafeSocketPath("another gateway already answers on the admin socket")
            finally:
                probe.close()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
            os.chmod(path, 0o600)
            sock.listen(4)
            st = os.lstat(path)
        except BaseException:
            sock.close()
            raise
        self._ident = (st.st_dev, st.st_ino)
        sock.settimeout(0.2)
        return sock

    def stop(self) -> None:
        """Invalidate any open run (undoing its pause), close and remove the socket."""
        global _ACTIVE_SERVER
        self._stop.set()
        self.invalidate(R_SHUTDOWN)
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for t in self._threads:
            t.join(timeout=5)
        if self.path is not None and self._ident is not None:
            try:
                st = os.lstat(self.path)
                if (st.st_dev, st.st_ino) == self._ident:
                    os.unlink(self.path)
            except OSError:
                pass
        if _ACTIVE_SERVER is self:
            _ACTIVE_SERVER = None

    # threads
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(None)
            t = threading.Thread(target=self._serve_conn, args=(conn,), daemon=True)
            t.start()

    def _watchdog(self) -> None:
        while not self._stop.wait(self.tick_s):
            self.check_deadlines()

    def check_deadlines(self) -> None:
        with self._lock:
            run = self._run
            if run is None or run.state in (COMMITTED, INVALID):
                return
            now = self.clock()
            # Every op checks the launch counter; between ops, once a second
            # (it reads the persisted registry).
            if now >= self._next_counter_check:
                self._next_counter_check = now + COUNTER_CHECK_S
                if not self._counter_ok(run):
                    return
            if run.state != PAUSED:
                return
            if run.frozen and now >= run.freeze_deadline:
                self._invalidate(run, R_FREEZE_LEASE)
            elif now >= run.lease_deadline:
                self._invalidate(run, R_LEASE)
            elif now >= run.pause_deadline:
                self._invalidate(run, R_PAUSE_MAX)

    def _serve_conn(self, conn: socket.socket) -> None:
        with self._lock:
            self._conns.add(conn)
        run: Optional[_Run] = None
        try:
            if _peer_uid(conn) != self._uid:
                return
            reader = conn.makefile("rb")
            while not self._stop.is_set():
                line = reader.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    self._send(conn, self._refuse("", run, E_BAD_REQUEST))
                    break
                request = self._parse(line)
                if request is None:
                    self._send(conn, self._refuse("", run, E_BAD_REQUEST))
                    continue
                op = request["op"]
                if op == "hello":
                    response, opened = self._hello(run, bool(request.get("probe")))
                    run = opened or run
                elif op not in OPS:
                    response = self._refuse(op, run, E_UNKNOWN_OP)
                elif run is None:
                    response = self._refuse(op, run, E_BAD_STATE)
                else:
                    response = self._dispatch(op, run, request)
                self._send(conn, response)
        except OSError:
            pass
        finally:
            if run is not None:
                with self._lock:
                    if run.state != COMMITTED:
                        self._invalidate(run, R_EOF)
                    if self._run is run:
                        self._run = None
            with self._lock:
                self._conns.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, op: str, run: _Run, request: Optional[dict] = None) -> dict:
        try:
            if op == "pause":
                return self._op_pause(run, request or {})
            return getattr(self, "_op_" + op)(run)
        except Exception as exc:
            logger.error("online backup %s failed: %s", op, type(exc).__name__)
            with self._lock:
                self._invalidate(run, R_INTERNAL)
                return self._refuse(op, run, E_INVALID)

    @staticmethod
    def _parse(line: bytes):
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            return None
        if not isinstance(request, dict) or not isinstance(request.get("op"), str):
            return None
        return request

    @staticmethod
    def _send(conn: socket.socket, response: dict) -> None:
        conn.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")

    # responses
    @staticmethod
    def _state(run: Optional[_Run]) -> str:
        return run.state if run is not None else NO_RUN

    def _refuse(self, op: str, run: Optional[_Run], error: str, **extra) -> dict:
        out = {"ok": False, "op": op, "state": self._state(run), "error": error}
        if run is not None and run.state == INVALID:
            out["error"] = E_INVALID
            out["reason"] = run.reason
        out.update(extra)
        return out

    def _ok(self, op: str, run: Optional[_Run], **extra) -> dict:
        out = {"ok": True, "op": op, "state": self._state(run)}
        out.update(extra)
        return out

    # fence helpers (call with self._lock held)
    def _counter_ok(self, run: _Run) -> bool:
        try:
            same = self.admission.successor_counter() == run.counter0
        except Exception:
            self._invalidate(run, R_REGISTRY)
            return False
        if not same:
            self._invalidate(run, R_SUCCESSOR)
        return same

    def _invalidate(self, run: _Run, reason: str) -> bool:
        if run.state in (COMMITTED, INVALID):
            return False
        run.state = INVALID
        run.reason = reason
        logger.warning("online backup run invalid: %s", reason)
        self._undo(run)
        return True

    def _undo(self, run: _Run) -> None:
        if run.frozen:
            run.frozen = False
            try:
                self.admission.thaw()
            except Exception as exc:
                logger.error("admission thaw failed: %s", type(exc).__name__)
        if run.paused:
            run.paused = False
            self.supervisor.resume()

    def invalidate(self, reason: str) -> bool:
        with self._lock:
            run = self._run
            return self._invalidate(run, reason) if run is not None else False

    # ops
    def _info(self) -> dict:
        try:
            worker_pids = sorted(self.supervisor.worker_pids())
        except Exception:
            worker_pids = []
        error = self._registry_error
        successors: list = []
        report = {}
        if error is None:
            try:
                successors = list(self.admission.successors())
                report = self.admission.hello_fields()
                report["successor_counter"] = int(report["successor_counter"])
            except Exception as exc:
                error = type(exc).__name__
                report = {}
        pids = report.get("successor_pids", [])
        sids = report.get("successor_sids", [])
        starting = report.get("successors_starting", [])
        return {
            "proto": PROTOCOL_VERSION,
            "gateway_pid": os.getpid(),
            "data_dir": self.data_dir,
            "worker_pids": worker_pids,
            "successors": successors,
            "successor_pids": pids,
            "successor_sids": sids,
            "successors_starting": starting,
            "successor_active": bool(successors or pids or sids or starting) or error is not None,
            "successor_error": error,
            "successor_counter": report.get("successor_counter"),
            "busy": self._run is not None,
            "knobs": self.knobs.server_view(),
        }

    def _hello(self, run: Optional[_Run], probe: bool):
        with self._lock:
            info = self._info()
            if probe:
                return self._ok("hello", run, **info), None
            if run is not None:
                return self._refuse("hello", run, E_BAD_STATE, **info), None
            if self._run is not None:
                return self._refuse("hello", None, E_BUSY, **info), None
            if info["successor_counter"] is None:
                # No counter, no fence: refuse rather than open an unguarded run.
                return self._refuse("hello", None, E_BAD_STATE, **info), None
            opened = _Run(counter0=info["successor_counter"], console0=console_drops())
            self._run = opened
            info["busy"] = True
            return self._ok("hello", opened, **info), opened

    def _gate(self, run: _Run, op: str, *allowed: str) -> Optional[dict]:
        """Refusal for ``op`` unless the run is open, in ``allowed`` and the counter held."""
        if run.state == INVALID:
            return self._refuse(op, run, E_INVALID)
        if run.state not in allowed:
            return self._refuse(op, run, E_BAD_STATE)
        if not self._counter_ok(run):
            return self._refuse(op, run, E_INVALID)
        return None

    def _op_pause(self, run: _Run, request: Optional[dict] = None) -> dict:
        from .workers import DrainTimeout, WorkerStopFailed

        request = request or {}
        with self._lock:
            refused = self._gate(run, "pause", ACTIVE)
            if refused:
                return refused
            run.pause_started = self.clock()
            # The client may only shorten the lease and the pause budget.
            run.lease_s = _requested(request.get("lease_s"), self.knobs.lease_s)
            run.pause_max_s = _requested(request.get("pause_max_s"), self.knobs.pause_max_s)
        started = self.clock()
        try:
            stopped = self.supervisor.pause(drain_s=self.knobs.drain_s)
        except DrainTimeout:
            return self._refuse("pause", run, E_NO_DRAIN, drain_s=self.clock() - started)
        except WorkerStopFailed:
            with self._lock:
                self._invalidate(run, R_WORKER_STOP)
                return self._refuse("pause", run, E_INVALID)
        with self._lock:
            run.paused = True
            if run.state == ACTIVE:
                self._counter_ok(run)
            if run.state != ACTIVE:
                # Invalidated while draining (shutdown, successor launch):
                # the undo ran before the pause took hold, so resume now.
                self._undo(run)
                return self._refuse("pause", run, E_INVALID)
            now = self.clock()
            run.state = PAUSED
            run.lease_deadline = now + run.lease_s
            run.pause_deadline = run.pause_started + run.pause_max_s
            return self._ok(
                "pause",
                run,
                stopped_worker_pids=sorted(p for p in stopped if p is not None),
                drain_s=now - started,
                lease_s=run.lease_s,
                pause_max_s=run.pause_max_s,
            )

    def _op_renew(self, run: _Run) -> dict:
        with self._lock:
            self.check_deadlines()
            refused = self._gate(run, "renew", PAUSED)
            if refused:
                return refused
            now = self.clock()
            lease_s = run.lease_s if run.lease_s is not None else self.knobs.lease_s
            run.lease_deadline = now + lease_s
            return self._ok("renew", run, lease_s=lease_s, paused_s=now - run.pause_started)

    def _op_freeze(self, run: _Run) -> dict:
        with self._lock:
            self.check_deadlines()
            refused = self._gate(run, "freeze", PAUSED)
            if refused:
                return refused
            if run.frozen:
                return self._refuse("freeze", run, E_BAD_STATE)
        try:
            self.admission.freeze(
                timeout=self.knobs.creator_drain_s,
                lease_s=self.knobs.freeze_lease_s,
                on_lapse=lambda: self.invalidate(R_FREEZE_LEASE),
            )
        except Exception:
            with self._lock:
                self._invalidate(run, R_CREATOR_DRAIN)
                return self._refuse("freeze", run, E_INVALID)
        with self._lock:
            run.frozen = True
            if run.state != PAUSED:
                # Invalidated while creators drained: the undo ran before the
                # freeze took hold, so thaw now.
                self._undo(run)
                return self._refuse("freeze", run, E_INVALID)
            run.freeze_deadline = self.clock() + self.knobs.freeze_lease_s
            return self._ok("freeze", run, frozen=True, freeze_lease_s=self.knobs.freeze_lease_s)

    def _op_thaw(self, run: _Run) -> dict:
        with self._lock:
            self.check_deadlines()
            refused = self._gate(run, "thaw", PAUSED)
            if refused:
                return refused
            if not run.frozen:
                return self._refuse("thaw", run, E_BAD_STATE)
            run.frozen = False
            if self.admission.thaw() is False:  # the gate's own lease lapsed first
                self._invalidate(run, R_FREEZE_LEASE)
                return self._refuse("thaw", run, E_INVALID)
            return self._ok("thaw", run, frozen=False)

    def _op_resume(self, run: _Run) -> dict:
        with self._lock:
            self.check_deadlines()
            refused = self._gate(run, "resume", PAUSED)
            if refused:
                return refused
            paused_s = self.clock() - run.pause_started
            if run.frozen:
                run.frozen = False
                if self.admission.thaw() is False:  # the gate's own lease lapsed first
                    self._invalidate(run, R_FREEZE_LEASE)
                    return self._refuse("resume", run, E_INVALID)
            self._undo(run)
            run.state = RESUMED_OK
            return self._ok("resume", run, paused_s=paused_s)

    def _op_commit(self, run: _Run) -> dict:
        with self._lock:
            refused = self._gate(run, "commit", RESUMED_OK)
            if refused:
                return refused
            run.state = COMMITTED
            return self._ok("commit", run, console_drops=console_drops() - run.console0)

    def _op_abort(self, run: _Run) -> dict:
        with self._lock:
            if run.state == COMMITTED:
                return self._refuse("abort", run, E_BAD_STATE)
            self._invalidate(run, R_ABORT)
            return self._ok("abort", run, reason=run.reason)


# ── client ──
class PauseError(RuntimeError):
    """A refused request; ``response`` is the gateway's answer."""

    def __init__(self, response: dict):
        self.response = response
        self.error = response.get("error")
        self.reason = response.get("reason")
        self.state = response.get("state")
        super().__init__(
            f"{response.get('op')}: {self.error}" + (f" ({self.reason})" if self.reason else "")
        )


class PauseClient:
    """One connection = one run. ``request`` raises :class:`PauseError` on a refusal."""

    def __init__(self, path=None, timeout: float = 180.0):
        self.path = Path(path) if path is not None else socket_path()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect(str(self.path))
            if hasattr(socket, "SO_PEERCRED") and _peer_uid(self._sock) != os.getuid():
                raise PermissionError(errno.EPERM, "the admin socket belongs to another user")
        except BaseException:
            self._sock.close()
            raise
        self._reader = self._sock.makefile("rb")

    def call(self, op: str, **fields) -> dict:
        """Send one request and return the response, refused or not."""
        payload = json.dumps({"op": op, **fields}, separators=(",", ":")).encode("utf-8")
        self._sock.sendall(payload + b"\n")
        line = self._reader.readline(MAX_LINE_BYTES + 1)
        if not line:
            raise ConnectionError("the gateway closed the admin socket")
        return json.loads(line.decode("utf-8"))

    def request(self, op: str, **fields) -> dict:
        response = self.call(op, **fields)
        if not response.get("ok"):
            raise PauseError(response)
        return response

    def hello(self, probe: bool = False) -> dict:
        return self.request("hello", probe=True) if probe else self.request("hello")

    def pause(self) -> dict:
        return self.request("pause")

    def renew(self) -> dict:
        return self.request("renew")

    def freeze(self) -> dict:
        return self.request("freeze")

    def thaw(self) -> dict:
        return self.request("thaw")

    def resume(self) -> dict:
        return self.request("resume")

    def commit(self) -> dict:
        return self.request("commit")

    def abort(self) -> dict:
        return self.call("abort")

    def fileno(self) -> int:
        """The socket's descriptor (a supervisor watches it for EOF while idle)."""
        return self._sock.fileno()

    def close(self) -> None:
        try:
            self._reader.close()
        finally:
            self._sock.close()

    def __enter__(self) -> "PauseClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def answering(path=None, timeout: float = 2.0) -> Optional[dict]:
    """The gateway's probe ``hello``, or ``None`` when no gateway answers."""
    if not supported():
        return None
    try:
        with PauseClient(path, timeout=timeout) as client:
            return client.hello(probe=True)
    except (OSError, ValueError, PauseError):
        return None
