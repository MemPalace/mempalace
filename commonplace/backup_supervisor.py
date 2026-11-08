"""Supervisor for ``commonplace backup --online`` and the data-directory fence.

An online backup runs inside the serving container next to a live gateway.
Two locks are involved and never confused:

``data_lock`` (``.commonplace.lock``, :mod:`commonplace.backup`)
    Serve and one-shot CLIs hold it shared; the offline ``backup()`` takes it
    exclusive. ``backup --online`` never touches it while a gateway answers on
    the admin socket. With no gateway answering it takes it exclusive for the
    whole run, so a serve start is refused until the run ends.

Fence (``.commonplace-fence.lock``, :func:`commonplace.backup.fence_lock`)
    One-shot CLIs take it shared for their lifetime; ``backup --online`` takes
    it exclusive, which also serializes backups. ``serve`` never takes it.

The supervisor owns the gateway connection (one connection is one run) and
holds no lock. It forks one child from its main thread; the child sets
``PR_SET_PDEATHSIG``, checks its parent, starts its own session, locks
``<stamp>.lock``, takes the fence exclusive (and, with no gateway answering,
``data_lock`` exclusive) and runs the snapshot
(:func:`commonplace.backup_online.backup_online`). So the fence is taken
exactly once, by the process that writes the snapshot, and a stuck child keeps
every lock until it is gone. Every gateway request of the child goes through
the supervisor (the relay). The supervisor watches the hook's heartbeat file
``<stamp>.hb``, the run deadline and the socket; on any of them it aborts the
run, then stops the child's process group (TERM, wait, KILL, wait). The caller
creates ``<stamp>.hb`` before the run and keeps touching it: a run refuses to
start without it, and a missing heartbeat is "heartbeat gone". The child
also exits on its own when the supervisor's lifeline pipe closes or its
deadline passes. Nothing is published unless the gateway committed the run.
"""

from __future__ import annotations

import errno
import json
import os
import re
import select
import shutil
import signal
import socket
import stat
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from .backup import STAGING_NAME, BackupError, DataDirInUse, FenceBusy, fence_lock

STAMP_RE = re.compile(r"[0-9TZ.]+-[0-9]+")
TERM_GRACE_S = 10.0
KILL_GRACE_S = 10.0
TICK_MAX_S = 5.0
RELAY_OPS = frozenset({"hello", "pause", "renew", "freeze", "thaw", "resume", "commit", "abort"})
COMMITTED = "COMMITTED"
_LOCK_BUSY = (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK)
_PR_SET_PDEATHSIG = 1
_EXIT_FAILED = 1
_EXIT_ORPHAN = 3
_EXIT_INTERNAL = 70


class OnlineBackupError(BackupError):
    """An online backup failed at a named stage; nothing was published."""

    def __init__(self, stage: str, detail: str | None = None):
        self.stage = stage
        self.detail = detail
        message = f"online backup failed (stage: {stage})"
        super().__init__(message + (f": {detail}" if detail else ""))


class _Terminated(BaseException):
    """Raised in the child (or supervisor) by SIGTERM/SIGINT."""

    def __init__(self, stage: str):
        super().__init__(stage)
        self.stage = stage


def _linux() -> bool:
    return sys.platform.startswith("linux")


# ── fence ──
def cli_fence(data_dir):
    """The shared fence a one-shot CLI holds for its lifetime (never ``serve``).

    Waits ``COMMONPLACE_FENCE_WAIT_S`` (default 30 s) for a running backup.
    """
    try:
        wait_s = _pause_module().Knobs.read("cli_fence_wait_s")
    except ValueError as exc:
        raise BackupError(f"COMMONPLACE_FENCE_WAIT_S: {exc}") from None
    return fence_lock(
        data_dir,
        exclusive=False,
        wait_s=wait_s,
        busy_message="a backup is running; try again when it has finished",
    )


# ── staging destination ──
def _real_data_dir(data_dir) -> Path:
    return Path(os.path.realpath(Path(data_dir).expanduser()))


def staging_dest(data_dir, dest) -> tuple[Path, Path]:
    """Return ``(data, <data>/.backup-staging/<stamp>)`` or refuse the DEST."""
    data = _real_data_dir(data_dir)
    if dest is None:
        raise BackupError("online backup needs DEST")
    raw = Path(dest).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    staging = data / STAGING_NAME
    if not STAMP_RE.fullmatch(raw.name) or Path(os.path.realpath(raw.parent)) != staging:
        raise BackupError(
            f"online backup destination must be <data>/{STAGING_NAME}/<stamp> "
            "with a stamp like 20261107T030253.123456789Z-4242"
        )
    return data, staging / raw.name


def is_staging_dest(data_dir, dest) -> bool:
    try:
        staging_dest(data_dir, dest)
    except BackupError:
        return False
    return True


def _sidecar(target: Path, suffix: str) -> Path:
    return target.with_name(target.name + suffix)


def _ensure_staging(staging: Path) -> None:
    try:
        st = os.lstat(staging)
    except FileNotFoundError:
        staging.mkdir(mode=0o700)
        st = os.lstat(staging)
    if not stat.S_ISDIR(st.st_mode):
        raise BackupError(f"{STAGING_NAME} must be a directory, not a symlink or file")


def _check_fresh(target: Path) -> None:
    for path in (target, _sidecar(target, ".partial")):
        if path.exists() or path.is_symlink():
            raise BackupError("backup destination already exists")


# ── gateway discovery ──
def _pause_module():
    from . import backup_pause

    return backup_pause


def _answering(data: Path):
    """The gateway's probe hello when it serves ``data``; otherwise None."""
    backup_pause = _pause_module()
    hello = backup_pause.answering()
    if not hello:
        return None
    served = hello.get("data_dir")
    if not served or os.path.realpath(served) != str(data):
        return None
    return hello


def lock_busy(exc: BaseException) -> bool:
    """True when ``exc`` is the offline backup refusing a data directory in use."""
    if isinstance(exc, DataDirInUse):
        return True
    cause = exc.__cause__
    return isinstance(cause, OSError) and cause.errno in _LOCK_BUSY


# ── public entry points ──
def check_online(data_dir, dest=None) -> dict:
    """Report whether an online backup of ``data_dir`` can start now."""
    if not _linux():
        raise OnlineBackupError("unsupported", "online backup needs Linux")
    data = _real_data_dir(data_dir)
    if dest is not None:
        _, target = staging_dest(data, dest)
        _check_fresh(target)
    hello = _answering(data)
    if hello is None:
        raise OnlineBackupError("gateway not answering")
    return {
        "online": True,
        "proto": hello.get("proto"),
        "gateway_pid": hello.get("gateway_pid"),
        "worker_pids": hello.get("worker_pids", []),
        "successor_active": bool(hello.get("successor_active")),
        "busy": bool(hello.get("busy")),
    }


def cleanup(data_dir, dest) -> dict:
    """Remove one stamp's staging leftovers, refusing while its lock is held.

    Never signals any process: a held ``<stamp>.lock`` means a child is still
    running, so the staging stays as it is.
    """
    _, target = staging_dest(data_dir, dest)
    lock_path = _sidecar(target, ".lock")
    removed, size = [], 0
    with ExitStack() as stack:
        if lock_path.exists() and os.name != "nt":
            import fcntl

            fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            stack.callback(os.close, fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in _LOCK_BUSY:
                    raise BackupError("backup still running; staging locked") from exc
                raise
        for path in (target, _sidecar(target, ".partial"), _sidecar(target, ".hb"), lock_path):
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode):
                size += _tree_bytes(path)
                shutil.rmtree(path)
            else:
                size += st.st_size
                path.unlink()
            removed.append(path.name)
    return {"removed": removed, "bytes": size}


def _tree_bytes(root: Path) -> int:
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    return total


def run_online(data_dir, dest, *, deadline=None) -> Path:
    """Run a supervised online backup into ``<data>/.backup-staging/<stamp>``.

    Returns the published ``manifest.json`` path; raises BackupError otherwise.
    """
    if not _linux():
        raise OnlineBackupError("unsupported", "online backup needs Linux")
    data, target = staging_dest(data_dir, dest)
    if data.is_symlink() or not data.is_dir():
        raise BackupError("data directory must be an existing directory, without a symlink")
    knobs = _pause_module().Knobs.from_env()
    now = time.time()
    deadline = float(deadline) if deadline is not None else now + knobs.deadline_s
    if deadline <= now:
        raise OnlineBackupError("deadline", "the deadline has already passed")
    # No lock here: the child takes the fence (and, with no gateway, data_lock).
    _ensure_staging(target.parent)
    _check_fresh(target)
    # The caller (the backup hook) creates the heartbeat before the run; without
    # it nothing could ever stop a run whose caller is gone.
    if not _sidecar(target, ".hb").is_file():
        raise OnlineBackupError(
            "heartbeat missing",
            f"create {target.name}.hb next to DEST and keep touching it during the run",
        )
    if _answering(data) is not None:
        client = _pause_module().PauseClient()
        try:
            return _Supervisor(data, target, client, deadline, knobs).run()
        finally:
            _quiet_close(client)
    return _Supervisor(data, target, None, deadline, knobs).run()


def backup_auto(data_dir, dest, *, deadline=None, report=None) -> Path:
    """Offline when free, otherwise online; report only the final selected attempt."""
    from . import backup as _backup

    mode = "offline"
    try:
        try:
            _backup.backup(data_dir, dest)
        except BackupError as exc:
            if not lock_busy(exc) or not _linux():
                raise
            data = _real_data_dir(data_dir)
            if _answering(data) is None:
                raise
            if not is_staging_dest(data, dest):
                raise BackupError(
                    f"{exc}; a gateway is serving, and an online backup needs DEST "
                    f"<data>/{STAGING_NAME}/<stamp>"
                ) from exc
            mode = "online"
            result = run_online(data, dest, deadline=deadline)
        else:
            result = Path(dest).expanduser().resolve() / "manifest.json"
    except BaseException:
        if report is not None:
            report(mode, False)
        raise
    if report is not None:
        report(mode, True)
    return result


def _default_snapshot(data_dir, dest, *, client, deadline, knobs):
    from .backup_online import backup_online

    return backup_online(data_dir, dest, client=client, deadline=deadline, knobs=knobs)


def _quiet_close(client) -> None:
    try:
        client.close()
    except Exception:  # noqa: BLE001 - closing is best effort
        pass


# ── relay channel (child <-> supervisor, JSON lines over pipes) ──
class _Channel:
    def __init__(self, write_fd: int, read_fd: int):
        self._w = write_fd
        self._r = read_fd
        self._buf = b""

    def send(self, message: dict) -> None:
        data = json.dumps(message, separators=(",", ":"), default=str).encode("utf-8") + b"\n"
        while data:
            data = data[os.write(self._w, data) :]

    def recv(self):
        while b"\n" not in self._buf:
            chunk = os.read(self._r, 65536)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


class RelayClient:
    """The child's view of the gateway: the ``PauseClient`` API over the relay."""

    def __init__(self, channel: _Channel):
        self._channel = channel

    def call(self, op: str, **fields) -> dict:
        if op not in RELAY_OPS:
            raise ValueError(f"unknown gateway op {op!r}")
        self._channel.send({"op": op, **fields})
        response = self._channel.recv()
        if response is None:
            raise ConnectionError("the backup supervisor closed the relay")
        return response

    def request(self, op: str, **fields) -> dict:
        response = self.call(op, **fields)
        if not response.get("ok"):
            raise _pause_module().PauseError(response)
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

    def progress(self, **counts) -> None:
        """Tell the supervisor the copy is moving (no reply)."""
        self._channel.send({"op": "_progress", **counts})

    def close(self) -> None:
        pass

    def __enter__(self) -> "RelayClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── child ──
def _set_pdeathsig() -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def _lock_stamp(target: Path) -> int:
    import fcntl

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(_sidecar(target, ".lock"), flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in _LOCK_BUSY:
            raise OnlineBackupError(
                "staging locked", "this stamp is already being written"
            ) from exc
        raise
    return fd


def _child_main(data, target, channel, lifeline_fd, has_client, deadline, knobs) -> int:
    why = {"stage": "terminated"}

    def on_signal(signum, _frame):
        raise _Terminated(why["stage"])

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def watch() -> None:
        # Exit when the supervisor goes away (lifeline EOF) or the deadline passes.
        while True:
            left = deadline - time.time()
            if left <= 0:
                why["stage"] = "deadline"
                break
            ready, _, _ = select.select([lifeline_fd], [], [], min(left, 60.0))
            if ready and not os.read(lifeline_fd, 1):
                why["stage"] = "supervisor gone"
                break
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(TERM_GRACE_S)
        os._exit(_EXIT_FAILED)

    threading.Thread(target=watch, name="backup-lifeline", daemon=True).start()
    try:
        _lock_stamp(target)  # never closed: held until this process exits
        with ExitStack() as locks:
            # The only fence holder of the run; a stuck snapshot keeps it.
            try:
                locks.enter_context(
                    fence_lock(data, exclusive=True, wait_s=float(knobs.fence_wait_s))
                )
            except FenceBusy as exc:
                raise OnlineBackupError(
                    "fence busy", "another command or backup is using the data directory"
                ) from exc
            if not has_client:
                from . import backup as _backup

                try:
                    locks.enter_context(_backup.data_lock(data, exclusive=True))
                except BackupError as exc:
                    raise OnlineBackupError(
                        "gateway not answering",
                        "no gateway answers on the admin socket and the data directory is in use",
                    ) from exc
            relay = RelayClient(channel) if has_client else None
            result = _default_snapshot(data, target, client=relay, deadline=deadline, knobs=knobs)
        summary = {"files": len(result.get("files", []))} if isinstance(result, dict) else {}
        _send_quiet(channel, {"op": "_result", "ok": True, **summary})
        return 0
    except _Terminated as exc:
        _send_quiet(channel, {"op": "_result", "ok": False, "stage": exc.stage})
        return _EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - every failure is reported, nothing published
        stage = getattr(exc, "stage", None) or "snapshot"
        detail = getattr(exc, "detail", None) if hasattr(exc, "stage") else None
        _send_quiet(
            channel,
            {"op": "_result", "ok": False, "stage": str(stage), "error": detail or str(exc)},
        )
        return _EXIT_FAILED


def _send_quiet(channel: _Channel, message: dict) -> None:
    try:
        channel.send(message)
    except OSError:
        pass


# ── supervisor ──
class _Supervisor:
    def __init__(self, data: Path, target: Path, client, deadline: float, knobs):
        self.data = data
        self.target = target
        self.client = client
        self.deadline = deadline
        self.knobs = knobs
        self.hb = _sidecar(target, ".hb")
        self.tick = min(TICK_MAX_S, max(0.05, float(knobs.heartbeat_stale_s) / 4))
        self.state = None
        self.result = None
        self.abort_reason = None
        self._reaped = False
        self._peek = None

    def run(self) -> Path:
        req_r, req_w = os.pipe()
        resp_r, resp_w = os.pipe()
        life_r, life_w = os.pipe()
        supervisor_pid = os.getpid()
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        pid = os.fork()
        if pid == 0:  # child: never returns into the CLI
            code = _EXIT_INTERNAL
            try:
                _set_pdeathsig()
                if os.getppid() != supervisor_pid:
                    os._exit(_EXIT_ORPHAN)
                os.setsid()
                for fd in (req_r, resp_w, life_w):
                    os.close(fd)
                if self.client is not None:
                    _quiet_close(self.client)  # the gateway must see EOF when the supervisor dies
                channel = _Channel(req_w, resp_r)
                code = _child_main(
                    self.data,
                    self.target,
                    channel,
                    life_r,
                    self.client is not None,
                    self.deadline,
                    self.knobs,
                )
            except BaseException:  # noqa: BLE001 - the child must never unwind into the CLI
                code = _EXIT_INTERNAL
            finally:
                try:
                    # A late TERM must not unwind the child into the CLI.
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    signal.signal(signal.SIGINT, signal.SIG_IGN)
                finally:
                    os._exit(code)
        for fd in (req_w, resp_r, life_r):
            os.close(fd)
        try:
            return self._supervise(pid, req_r, resp_w)
        finally:
            for fd in (req_r, resp_w, life_w):
                try:
                    os.close(fd)
                except OSError:
                    pass

    # The supervisor itself is stopped by TERM/INT: abort the run, stop the child.
    def _signals(self):
        if threading.current_thread() is not threading.main_thread():
            return []

        def on_signal(signum, _frame):
            raise _Terminated("supervisor terminated")

        return [(sig, signal.signal(sig, on_signal)) for sig in (signal.SIGTERM, signal.SIGINT)]

    def _supervise(self, pid: int, req_r: int, resp_w: int) -> Path:
        previous = self._signals()
        status = None
        try:
            status = self._loop(pid, req_r, resp_w)
        except _Terminated as exc:
            self.abort_reason = exc.stage
        finally:
            for sig, handler in previous:
                signal.signal(sig, handler)
        if self.abort_reason is not None:
            self._abort_gateway()
            # A reaped pid may already belong to another process: never signal it.
            if not self._reaped and self._stop(pid):
                raise OnlineBackupError(
                    "uninterruptible IO",
                    f"the backup child survived KILL after {self.abort_reason}; staging stays locked",
                )
            raise OnlineBackupError(self.abort_reason)
        result = self.result or {}
        if status == 0 and result.get("ok"):
            if self.client is not None and self.state != COMMITTED:
                raise OnlineBackupError("commit", "the gateway never committed the run")
            return self.target / "manifest.json"
        self._abort_gateway()
        stage = result.get("stage") or f"child exited ({_describe(status)})"
        raise OnlineBackupError(str(stage), result.get("error"))

    def _loop(self, pid: int, req_r: int, resp_w: int):
        channel = _Channel(resp_w, -1)
        sock_fd = _client_fileno(self.client) if self.client is not None else None
        if sock_fd is not None:
            self._peek = socket.socket(fileno=os.dup(sock_fd))
        try:
            return self._watch(pid, req_r, channel, sock_fd)
        finally:
            if self._peek is not None:
                self._peek.close()
                self._peek = None

    def _watch(self, pid: int, req_r: int, channel: "_Channel", sock_fd):
        buffer = b""
        req_open = True
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                self._reaped = True
                if req_open:
                    buffer = self._drain(req_r, buffer, channel)
                return status
            watched = ([req_r] if req_open else []) + ([sock_fd] if sock_fd is not None else [])
            ready, _, _ = select.select(watched, [], [], self.tick)
            if req_open and req_r in ready:
                chunk = os.read(req_r, 65536)
                if chunk:
                    buffer = self._dispatch(buffer + chunk, channel)
                else:
                    req_open = False
            if sock_fd is not None and sock_fd in ready and self._socket_closed():
                self.abort_reason = "gateway socket closed"
            if self.abort_reason is None:
                self.abort_reason = self._watch_reason()
            if self.abort_reason is not None:
                return None

    def _drain(self, req_r: int, buffer: bytes, channel: _Channel) -> bytes:
        while True:
            ready, _, _ = select.select([req_r], [], [], 0)
            if not ready:
                return buffer
            chunk = os.read(req_r, 65536)
            if not chunk:
                return buffer
            buffer = self._dispatch(buffer + chunk, channel)

    def _dispatch(self, buffer: bytes, channel: _Channel) -> bytes:
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            try:
                message = json.loads(line.decode("utf-8"))
                op = message.pop("op")
            except (ValueError, KeyError, AttributeError):
                self.abort_reason = "relay protocol error"
                return b""
            if op == "_result":
                self.result = {"ok": bool(message.get("ok")), **message}
            elif op == "_progress":
                pass
            else:
                _send_quiet(channel, self._relay(op, message))
        return buffer

    def _relay(self, op: str, fields: dict) -> dict:
        if op not in RELAY_OPS:
            return {"ok": False, "op": op, "state": self.state, "error": "unknown_op"}
        if self.client is None:
            return {"ok": False, "op": op, "state": "NONE", "error": "bad_request"}
        try:
            response = self.client.call(op, **fields)
        except (ConnectionError, OSError, ValueError):
            self.abort_reason = "gateway socket closed"
            return {
                "ok": False,
                "op": op,
                "state": "INVALID",
                "error": "invalid",
                "reason": "socket closed before commit",
            }
        if isinstance(response, dict) and response.get("state"):
            self.state = response["state"]
        return response

    def _socket_closed(self) -> bool:
        """The idle gateway socket is readable: EOF, an error or unsolicited bytes."""
        try:
            self._peek.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True
        # EOF, or bytes nobody asked for (a protocol violation): the run is lost.
        return True

    def _watch_reason(self):
        if time.time() >= self.deadline:
            return "deadline"
        try:
            age = time.time() - os.stat(self.hb).st_mtime
        except FileNotFoundError:
            # run_online refused a run without it, so it was removed during the run.
            return "heartbeat gone"
        if age > float(self.knobs.heartbeat_stale_s):
            return "heartbeat stale"
        return None

    def _abort_gateway(self) -> None:
        if self.client is None or self.state in (COMMITTED, "INVALID"):
            return
        try:
            self.client.call("abort")
        except Exception:  # noqa: BLE001 - closing the socket invalidates the run anyway
            pass
        self.state = "INVALID"

    @staticmethod
    def _stop(pid: int) -> bool:
        """TERM the child's group, wait, KILL, wait. True if it is still alive."""
        for sig, grace in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, KILL_GRACE_S)):
            for kill in (os.killpg, os.kill):
                try:
                    kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            end = time.monotonic() + grace
            while True:
                try:
                    done, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    return False
                if done:
                    return False
                if time.monotonic() >= end:
                    break
                time.sleep(0.05)
        return True


def _client_socket(client):
    sock = getattr(client, "sock", None) or getattr(client, "_sock", None)
    return sock if isinstance(sock, socket.socket) else None


def _client_fileno(client):
    fileno = getattr(client, "fileno", None)
    if callable(fileno):
        return fileno()
    sock = _client_socket(client)
    return sock.fileno() if sock is not None else None


def _describe(status) -> str:
    if status is None:
        return "unknown"
    if os.WIFSIGNALED(status):
        return f"signal {os.WTERMSIG(status)}"
    return f"status {os.WEXITSTATUS(status)}"
