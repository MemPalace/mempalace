"""Daemon side of a mine run in a supervised child (see ``mine_child``).

The daemon owns the palace: it holds the writer lock and the only storage
client. :func:`run_mine_in_child` starts ``mempalace.mine_child`` at idle
priority, hands it the job, and serves its storage calls one at a time under
``write_lock`` -- the lock every other daemon write (a diary entry) also takes,
so those land between the mine's calls rather than after the whole mine.
Writes to the sidecar stores, which hold their own file locks, skip it.

Every message from the child is a heartbeat (``MineChild.last_progress``); the
child can be killed from another thread (``MineChild.kill``) without taking
the daemon down, which an in-process mine cannot offer.
"""

from __future__ import annotations

import contextlib
import os
import pickle
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ._proc import safe_child_env, safe_module_argv
from .mine_child import _read_msg, _write_msg

# Cap on the child's captured stdout/stderr kept for the job result.
_OUTPUT_TAIL_BYTES = 64 * 1024
# Rows per page when the child streams a metadata scan.
_ITER_PAGE = 2000
# A child silent this long (no request, no heartbeat) is wedged and killed.
DEFAULT_STALL_SECONDS = 60.0
# A child that reported done but has not exited after this long is killed.
DEFAULT_EXIT_GRACE_SECONDS = 30.0
# A child that keeps heartbeating but makes no progress (no storage call, no
# embedded slice) is killed once it has burned this many multiples of the
# stall limit in CPU-seconds: a pure-Python loop that never returns.
CPU_STALL_FACTOR = 5.0
# A background-priority child the reaper cannot sample (macOS taskpolicy,
# Windows background mode, Linux nice 19, or SCHED_IDLE without /proc) may be
# starved rather than wedged, and nothing tells the two apart. Its silence
# limit is this multiple of the stall limit: 10 minutes by default.
UNOBSERVED_STALL_FACTOR = 10.0
# Priorities ``mine_child.lower_priority`` reports for a lowered child.
_BACKGROUND_PRIORITIES = frozenset(
    {"sched-idle", "nice-19", "taskpolicy-background", "windows-background", "windows-idle"}
)


def _child_env(palace_path: str, threads: int) -> dict:
    env = safe_child_env()
    env["MEMPALACE_PALACE_PATH"] = palace_path
    # Background mining: a couple of intra-op threads cost far less CPU per
    # row than the default pool (0.108 CPU-s/row at 1 thread, 0.41 at 8 in the
    # measurements behind this default), and spinning threads burn idle cores for nothing.
    if threads > 0 and "MEMPALACE_EMBEDDING_THREADS" not in os.environ:
        env["MEMPALACE_EMBEDDING_THREADS"] = str(threads)
    env.setdefault("MEMPALACE_ORT_ALLOW_SPINNING", "0")
    return env


def _popen_kwargs() -> dict:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "close_fds": True,
    }
    if os.name == "nt":  # pragma: no cover - Windows
        flags = 0
        for name in ("CREATE_NO_WINDOW", "IDLE_PRIORITY_CLASS"):
            flags |= getattr(subprocess, name, 0)
        kwargs["creationflags"] = flags
    return kwargs


def _send_reply(stream, reply: dict) -> None:
    """Send ``reply``, or an error in its place when it cannot be pickled."""
    try:
        data = pickle.dumps(reply, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:  # noqa: BLE001 - any pickling failure becomes the child's error
        error = RuntimeError(f"daemon could not send the result: {type(exc).__name__}: {exc}")
        data = pickle.dumps({"ok": False, "exc": error}, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(len(data).to_bytes(4, "big") + data)
    stream.flush()


def _picklable_exc(exc: BaseException) -> BaseException:
    try:
        pickle.loads(pickle.dumps(exc))
        return exc
    except Exception:
        return RuntimeError(f"{type(exc).__name__}: {exc}")


class MineChild:
    """One running mine child and the storage handles it opened."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.pid = proc.pid
        self.started = time.monotonic()
        self.last_progress = self.started
        # Last time the mine did work: a storage call or an embedded slice.
        # A heartbeat alone moves ``last_progress`` but not this.
        self.last_work = self.started
        self.work_units = 0
        self.calls = 0
        self.priority: Optional[str] = None
        self.done_at: Optional[float] = None
        self.killed_reason: Optional[str] = None
        # True while the daemon runs one of the child's calls: the child is
        # waiting on us then, so its silence is not a stall.
        self.serving = False
        self._output = bytearray()
        self._output_lock = threading.Lock()
        self._reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._reader.start()

    def _drain_stderr(self) -> None:
        stream = self.proc.stderr
        while True:
            chunk = stream.read1(8192) if hasattr(stream, "read1") else stream.read(8192)
            if not chunk:
                return
            with self._output_lock:
                self._output.extend(chunk)
                if len(self._output) > _OUTPUT_TAIL_BYTES:
                    del self._output[: len(self._output) - _OUTPUT_TAIL_BYTES]

    def output(self) -> str:
        self._reader.join(timeout=5)
        with self._output_lock:
            return bytes(self._output).decode("utf-8", errors="replace")

    def kill(self, reason: str) -> None:
        """Stop the child. The daemon keeps running; the job is failed."""
        self.killed_reason = reason
        try:
            self.proc.kill()
        except OSError:
            pass

    def stalled_for(self, now: float) -> float:
        if self.serving or self.done_at is not None:
            return 0.0
        return now - self.last_progress

    def progress(self) -> dict:
        now = time.monotonic()
        return {
            "pid": self.pid,
            "calls": self.calls,
            "priority": self.priority,
            "state": "done" if self.done_at else ("writing" if self.serving else "computing"),
            "running_seconds": round(now - self.started, 1),
            "seconds_since_progress": round(now - self.last_progress, 1),
        }


def run_mine_in_child(
    payload: dict,
    *,
    palace_path: str,
    write_lock: threading.Lock,
    threads: int = 2,
    on_start: Optional[Callable[[MineChild], None]] = None,
    argv: Optional[list] = None,
    stall_seconds: float = DEFAULT_STALL_SECONDS,
    exit_grace_seconds: float = DEFAULT_EXIT_GRACE_SECONDS,
    lock_held: bool = True,
    cpu_stall_seconds: Optional[float] = None,
    proc_sampler: Optional[Callable[[int], Optional[tuple]]] = None,
) -> dict:
    """Run one mine job in a child process; return the job result.

    A reaper thread kills the child when it goes ``stall_seconds`` without a
    message (requests and its heartbeat both count; time the daemon spends
    serving a call does not), and the caller kills one that reported done but
    is still running ``exit_grace_seconds`` later. Either way the job
    fails and the daemon carries on; writes already made stay, and the next
    mine picks up where this one stopped.

    ``lock_held`` says whether this process holds the palace write lock for
    the job, so the child may treat it as held; otherwise the child's mine
    takes the lock itself. ``argv`` overrides the child command (tests use a
    fake child).

    ``cpu_stall_seconds`` (default ``CPU_STALL_FACTOR`` x ``stall_seconds``)
    bounds a child that heartbeats but does no work: once it has spent that
    long, in wall time and in CPU time, since its last storage call or
    embedded slice, it is killed. ``proc_sampler`` reads a child's CPU ticks
    and whether any of its threads is runnable (see ``_sample_proc``).
    """
    if cpu_stall_seconds is None:
        cpu_stall_seconds = CPU_STALL_FACTOR * stall_seconds if stall_seconds else 0.0
    from .palace import _validate_palace_fts5_after_mine, get_collection, palace_write_serial

    cmd = argv or safe_module_argv("mempalace.mine_child")
    proc = subprocess.Popen(cmd, env=_child_env(palace_path, threads), **_popen_kwargs())
    child = MineChild(proc)
    if on_start is not None:
        on_start(child)
    reaper_stop = threading.Event()
    if stall_seconds and stall_seconds > 0:
        threading.Thread(
            target=_reap_stalled,
            args=(child, stall_seconds, reaper_stop, proc_sampler or _sample_proc),
            name="mempalace-mine-reaper",
            daemon=True,
        ).start()
    handles: dict[int, Any] = {}
    gens: dict[int, Any] = {}
    writes_at_start = palace_write_serial()
    result: Optional[dict] = None
    try:
        try:
            _write_msg(proc.stdin, {"payload": payload, "lock_held": lock_held})
        except (BrokenPipeError, OSError):
            pass  # died before reading its job; reported below from the exit code
        while True:
            try:
                msg = _read_msg(proc.stdout)
            except (EOFError, OSError, pickle.UnpicklingError):
                break
            child.last_progress = time.monotonic()
            op = msg.get("op")
            if op == "started":
                child.priority = msg.get("priority")
                continue
            if op == "progress":
                # A heartbeat proves the child is scheduled, not that the mine
                # moves: its own thread beats on while the mine spins. Work is
                # a storage call or an embedded slice (``units``).
                units = msg.get("units", 0)
                if units != child.work_units:
                    child.work_units = units
                    child.last_work = child.last_progress
                elif (
                    cpu_stall_seconds > 0
                    and child.last_progress - child.last_work > cpu_stall_seconds
                    and msg.get("compute_cpu", 0) > cpu_stall_seconds
                ):
                    child.kill(
                        f"mine child spent {msg.get('compute_cpu', 0):.0f} CPU-s without "
                        f"progress (limit {cpu_stall_seconds:g} s)"
                    )
                    break
                continue
            if op == "done":
                result = dict(msg.get("result") or {})
                child.done_at = time.monotonic()
                break
            child.calls += 1
            child.serving = True
            try:
                # Sidecar stores (origin, revisions, hallways, tunnels) each
                # hold their own cross-process file lock and never touch the
                # collection, so they don't hold up the diary lane.
                with contextlib.nullcontext() if op in _SIDECAR_OPS else write_lock:
                    value = _serve(
                        op,
                        msg,
                        palace_path,
                        handles,
                        gens,
                        writes_at_start,
                        get_collection,
                        _validate_palace_fts5_after_mine,
                    )
                reply = {"ok": True, "value": value}
            except BaseException as exc:  # noqa: BLE001 - every failure goes back to the child
                reply = {"ok": False, "exc": _picklable_exc(exc)}
            finally:
                child.last_progress = time.monotonic()
                child.last_work = child.last_progress
                child.serving = False
            try:
                _send_reply(proc.stdin, reply)
            except (BrokenPipeError, OSError):
                break
    finally:
        reaper_stop.set()
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=exit_grace_seconds)
        except subprocess.TimeoutExpired:
            # Reported done (or lost its channel) but did not exit.
            child.kill(f"did not exit {exit_grace_seconds:g} s after its channel closed")
            proc.wait()
    output = child.output()
    if result is None:
        if child.killed_reason:
            result = {
                "success": False,
                "error": child.killed_reason,
                "error_class": "MineChildKilled",
            }
        else:
            reason = f"mine child exited with code {proc.returncode} before finishing"
            result = {"success": False, "error": reason, "error_class": "MineChildDied"}
    elif child.killed_reason:
        # Its mine finished and every write is in; only the exit was reaped.
        result["reaped"] = child.killed_reason
    result.setdefault("success", True)
    result.setdefault("exit_code", 0 if result.get("success") else 1)
    result["child"] = child.progress()
    if output:
        result["stdout"] = output
    return result


def _sample_proc(pid: int) -> Optional[tuple]:
    """``(cpu_ticks, any_thread_runnable)`` for ``pid``; None where unknown.

    Linux only (``/proc``). ``cpu_ticks`` is the process's utime + stime, so
    it includes threads that have exited; the run state is read per thread.
    """
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])
        runnable = False
        for task in Path(f"/proc/{pid}/task").iterdir():
            try:
                state = (task / "stat").read_bytes().rsplit(b")", 1)[1].split()[0]
            except (OSError, IndexError):
                continue  # the thread exited while we listed it
            runnable = runnable or state == b"R"
        return ticks, runnable
    except (OSError, ValueError, IndexError):
        return None


def _reap_stalled(
    child: MineChild,
    stall_seconds: float,
    stop: threading.Event,
    sampler: Callable[[int], Optional[tuple]] = _sample_proc,
) -> None:
    """Kill the child once it has been silent for ``stall_seconds``.

    A ``SCHED_IDLE`` child runs only when no other process wants the CPU, so
    on a saturated machine it can go silent without being wedged. Silence is
    spared when the child is *starved*: its CPU time has not moved since it
    last spoke and one of its threads is runnable, waiting for a CPU. Silence
    with flat CPU and no runnable thread is a child blocked for good (a native
    hang holding the GIL), and silence with CPU advancing is one spinning:
    both are killed.

    Any other background child, and a ``SCHED_IDLE`` one the sampler cannot
    read, gives no such signal; it is allowed ``UNOBSERVED_STALL_FACTOR``
    times the limit instead.
    """
    tick = min(1.0, stall_seconds / 4)
    baseline = None
    last_seen = None
    while not stop.wait(tick):
        if child.proc.poll() is not None:
            return
        sample = sampler(child.pid) if child.priority == "sched-idle" else None
        if last_seen != child.last_progress:
            last_seen = child.last_progress
            baseline = sample[0] if sample else None
        silent = child.stalled_for(time.monotonic())
        if sample is not None and baseline is not None and sample[1] and sample[0] == baseline:
            continue  # starved by busier processes, not stuck
        limit = stall_seconds
        if sample is None and child.priority in _BACKGROUND_PRIORITIES:
            limit = stall_seconds * UNOBSERVED_STALL_FACTOR
        if silent > limit:
            child.kill(f"mine child sent nothing for {silent:.0f} s (stall limit {limit:g} s)")
            return


_SIDECAR_OPS = frozenset(
    {"redetect_origin", "revisions", "store_hallways", "topic_tunnels", "entity_tunnels"}
)


def _serve(op, msg, palace_path, handles, gens, writes_at_start, get_collection, validate):
    if op == "open":
        kwargs = dict(msg.get("kwargs") or {})
        coll = get_collection(palace_path, **kwargs)
        handle = len(handles) + 1
        handles[handle] = coll
        caps = _caps(coll, palace_path, kwargs.get("backend"))
        return {"handle": handle, "caps": caps}
    if op == "call":
        coll = handles[msg["handle"]]
        method = msg["method"]
        if method.startswith("_"):
            raise AttributeError(method)
        return getattr(coll, method)(*msg.get("args", ()), **msg.get("kwargs", {}))
    if op == "iter":
        coll = handles[msg["handle"]]
        gen_id = len(gens) + 1
        gens[gen_id] = iter(coll.iter_metadata(*msg.get("args", ()), **msg.get("kwargs", {})))
        return gen_id
    if op == "next":
        gen = gens[msg["gen"]]
        items = []
        for item in gen:
            items.append(item)
            if len(items) >= _ITER_PAGE:
                return {"items": items, "done": False}
        del gens[msg["gen"]]
        return {"items": items, "done": True}
    if op == "redetect_origin":
        from .cli import _run_pass_zero

        return _run_pass_zero(project_dir=msg["source"], palace_dir=palace_path, llm_provider=None)
    if op == "revisions":
        from .revisions import append_drawer_revisions

        return append_drawer_revisions(palace_path, msg["rows"])
    if op in {"store_hallways", "topic_tunnels", "entity_tunnels"}:
        from .config import MempalaceConfig
        from .hallways import _store_wing_hallways
        from .miner import _compute_entity_tunnels_for_wing, _compute_topic_tunnels_for_wing

        config = MempalaceConfig(palace_path=palace_path)
        if op == "store_hallways":
            return _store_wing_hallways(*msg["args"], config=config)
        if op == "topic_tunnels":
            return _compute_topic_tunnels_for_wing(msg["wing"], config=config)
        return _compute_entity_tunnels_for_wing(msg["wing"], config=config)
    if op == "validate":
        return validate(palace_path, writes_since=writes_at_start)
    raise ValueError(f"unknown mine channel op: {op!r}")


def _caps(coll, palace_path, backend) -> dict:
    from .backends.embedding_wrapper import EmbeddingCollection

    inner = coll._inner if isinstance(coll, EmbeddingCollection) else coll
    from .palace import _backend_has_server_embedder

    methods = sorted(
        name
        for name in dir(inner)
        if not name.startswith("_") and callable(getattr(type(inner), name, None))
    )
    return {
        "iter_metadata": hasattr(inner, "iter_metadata"),
        "upsert_merges_metadata": bool(getattr(coll, "upsert_merges_metadata", False)),
        "distance_metric": getattr(coll, "distance_metric", "cosine"),
        "server_embedder": _backend_has_server_embedder(palace_path, backend),
        "methods": methods,
    }
