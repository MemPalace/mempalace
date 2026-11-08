"""Bounded, payload-free local allocation observations for storage servers.

Imports do not activate observation. Only serving entrypoints create an Observer.
No record API accepts a request, result, exception, path or caller identity.
"""

import atexit
import builtins
import json
import os
import queue
import re
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MAX_U = (1 << 63) - 1
MAX_LINE = 4096
MAX_FILE = 4 * 1024 * 1024
# Frozen built-in names from mcp_server/schemas.py, never a mutable plugin catalog.
TOOL_NAMES = frozenset(
    """
mempalace_status mempalace_list_wings mempalace_list_rooms mempalace_get_taxonomy
mempalace_get_aaak_spec mempalace_kg_query mempalace_kg_add mempalace_kg_save
mempalace_kg_invalidate mempalace_kg_supersede mempalace_kg_timeline
mempalace_kg_list_wings mempalace_kg_stats mempalace_traverse mempalace_find_tunnels
mempalace_graph_stats mempalace_mesh_peers mempalace_create_tunnel mempalace_list_tunnels
mempalace_delete_tunnel mempalace_list_hallways mempalace_delete_hallway
mempalace_follow_tunnels mempalace_search mempalace_check_duplicate mempalace_add_drawer
mempalace_checkpoint mempalace_delete_drawer mempalace_delete_drawers mempalace_mine
mempalace_delete_by_source mempalace_sync mempalace_get_drawer mempalace_get_drawers
mempalace_list_drawers mempalace_update_drawer mempalace_diary_write mempalace_diary_read
mempalace_hook_settings mempalace_memories_filed_away mempalace_reconnect
mempalace_event_append mempalace_task_create mempalace_event_list mempalace_event_wait
mempalace_event_ack mempalace_artifact_put mempalace_artifact_get mempalace_patch_submit
internal/write_mirror_units internal/read_associations unknown
""".split()
)
ENUMS = {
    "event": {
        "generation_start",
        "call_begin",
        "call_end",
        "phase_begin",
        "phase_end",
        "generation_stop",
        "loss",
    },
    "tool_name": TOOL_NAMES,
    "dispatch_mode": {"http", "stdio", "proxy_local", "light_local", "internal", "background"},
    "startup_phase": {"startup_preflight", "capacity_probe", "eager_warmup"},
    "rss_source": {"linux_proc", "unavailable"},
    "rss_status": {"ok", "partial", "unsupported", "error"},
    "fallback_kind": {"full_metadata", "bounded_pages", "projected_stream", "none", "unknown"},
    "lifecycle_reason": {
        "request_budget",
        "rss_watermark",
        "idle",
        "monitor_lost",
        "shutdown",
        "write_stall",
        "unknown",
    },
    "outcome": {"returned", "raised", "refused", "aborted"},
}
MODELS = {"minilm", "embeddinggemma", "embeddinggemma2", "openai-compat", "other"}
BACKENDS = {
    "chroma",
    "compact",
    "sqlite_exact",
    "rust_exact",
    "pgvector",
    "qdrant",
    "milvus",
    "other",
}
BOOL_KEYS = {"raw_metadata_cache_hit", "taxonomy_cache_hit", "metadata_fallback"}
COUNT_KEYS = set(
    """elapsed_us rss_before_bytes rss_after_bytes rss_anon_before_bytes
rss_anon_after_bytes rss_file_before_bytes rss_file_after_bytes metadata_rows
aggregate_pair_count ef_instance_count chroma_client_count kg_handle_count
logstream_handle_count restricted_pickle_load_count collection_open_count
model_session_create_count active_call_count lost_record_count""".split()
)
KEYS = (
    frozenset(ENUMS)
    | BOOL_KEYS
    | COUNT_KEYS
    | {
        "schema_version",
        "worker_generation",
        "record_seq",
        "call_id",
        "timestamp_utc",
        "build_revision",
        "build_version",
        "model_backend",
    }
)
REQUIRED = {"event", "rss_source", "rss_status", "fallback_kind", "lost_record_count"}
_LOCAL = threading.local()


def encode_record(record):
    """Reject whole candidates, before JSON encoding; no arbitrary coercion."""
    if type(record) is not dict or set(record) != KEYS:
        raise ValueError("invalid observation")
    for key, value in record.items():
        if key in ENUMS:
            valid = (value is None and key not in REQUIRED) or (
                type(value) is str and value in ENUMS[key]
            )
        elif key in BOOL_KEYS:
            valid = value is None or type(value) is bool
        elif key in COUNT_KEYS or key in {"record_seq", "call_id"}:
            valid = (value is None and key not in REQUIRED | {"record_seq"}) or (
                type(value) is int
                and (1 if key in {"record_seq", "call_id"} else 0) <= value <= MAX_U
            )
        elif key == "schema_version":
            valid = type(value) is int and value == 1
        elif key == "worker_generation":
            valid = (
                type(value) is str
                and re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value
                )
                is not None
            )
        elif key == "timestamp_utc":
            valid = (
                type(value) is str
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value) is not None
            )
        elif key == "build_revision":
            valid = value is None or (
                type(value) is str and re.fullmatch(r"[0-9a-f]{40}", value) is not None
            )
        elif key == "build_version":
            valid = value is None or (
                type(value) is str
                and len(value) <= 32
                and re.fullmatch(r"\d+(?:\.\d+)+", value) is not None
            )
        else:
            valid = type(value) is dict and set(value) == {"model", "backend"}
            if valid:
                valid = all(
                    v is None or (type(v) is str and v in choices)
                    for v, choices in ((value["model"], MODELS), (value["backend"], BACKENDS))
                )
        if not valid:
            raise ValueError("invalid observation")
    line = (json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")
    if len(line) > MAX_LINE:
        raise ValueError("invalid observation")
    return line


def read_rss():
    """One current-RSS read; never substitute lifetime maximum RSS."""
    if not sys.platform.startswith("linux"):
        return (None, None, None), "unavailable", "unsupported"
    try:
        values = {}
        with builtins.open("/proc/self/status", encoding="ascii") as stream:
            for line in stream:
                parts = line.split()
                if parts and parts[0] in {"VmRSS:", "RssAnon:", "RssFile:"}:
                    if len(parts) == 3 and parts[2] == "kB" and parts[1].isdigit():
                        value = int(parts[1]) * 1024
                        if value <= MAX_U:
                            values[parts[0]] = value
        sample = tuple(values.get(k) for k in ("VmRSS:", "RssAnon:", "RssFile:"))
        return sample, "linux_proc", "ok" if all(v is not None for v in sample) else "partial"
    except (OSError, ValueError, UnicodeError):
        return (None, None, None), "unavailable", "error"


class RingSink:
    """One private cross-process lock and two bounded files per config root."""

    def __init__(self, directory, max_bytes=MAX_FILE):
        self.directory = Path(directory) / "diagnostics"
        self.max_bytes = max_bytes

    @staticmethod
    def _private_fd(fd, directory=False):
        info = os.fstat(fd)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise OSError("unavailable")
        if os.name != "nt":
            if info.st_uid != os.getuid() or (not directory and info.st_nlink != 1):
                raise OSError("unavailable")
            os.fchmod(fd, 0o700 if directory else 0o600)

    def __call__(self, line):
        if type(line) is not bytes or len(line) > min(MAX_LINE, self.max_bytes):
            raise ValueError("invalid observation")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise OSError("unavailable")
        directory_fd = None
        lock_fd = None
        try:
            if os.name != "nt":
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                self._private_fd(directory_fd, directory=True)

            def open_file(name):
                flags = (
                    os.O_RDWR
                    | os.O_CREAT
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_BINARY", 0)
                )
                if directory_fd is None:
                    path = self.directory / name
                    if path.is_symlink():
                        raise OSError("unavailable")
                    fd = os.open(path, flags, 0o600)
                else:
                    fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
                try:
                    self._private_fd(fd)
                except BaseException:
                    os.close(fd)
                    raise
                return fd

            lock_fd = open_file("worker-memory.lock")
            if os.name == "nt":
                import msvcrt

                if os.fstat(lock_fd).st_size == 0:
                    os.write(lock_fd, b"\0")
                os.lseek(lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Refuse an unsafe backup even if no rotation is needed this time.
            backup = "worker-memory.jsonl.1"
            try:
                info = (
                    os.stat(backup, dir_fd=directory_fd, follow_symlinks=False)
                    if directory_fd is not None
                    else (self.directory / backup).lstat()
                )
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(info.st_mode) or (
                    os.name != "nt" and (info.st_uid != os.getuid() or info.st_nlink != 1)
                ):
                    raise OSError("unavailable")
            name = "worker-memory.jsonl"
            fd = open_file(name)
            try:
                size = os.fstat(fd).st_size
                if size + len(line) > self.max_bytes:
                    os.close(fd)
                    fd = None
                    if directory_fd is None:
                        os.replace(self.directory / name, self.directory / backup)
                    else:
                        os.replace(name, backup, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                    fd = open_file(name)
                    size = 0
                os.lseek(fd, 0, os.SEEK_END)
                try:
                    view = memoryview(line)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("unavailable")
                        view = view[written:]
                except BaseException:
                    os.ftruncate(fd, size)
                    raise
            finally:
                if fd is not None:
                    os.close(fd)
        finally:
            if lock_fd is not None:
                # Closing releases the advisory lock on both supported platforms.
                os.close(lock_fd)
            if directory_fd is not None:
                os.close(directory_fd)


class Token:
    __slots__ = (
        "call_id",
        "tool",
        "mode",
        "phase",
        "before",
        "source",
        "status",
        "started",
        "facts",
        "outcome",
    )

    def __init__(self, call_id, tool, mode, phase, rss, clock):
        self.call_id, self.tool, self.mode, self.phase = call_id, tool, mode, phase
        self.before, self.source, self.status = rss
        self.started = clock()
        self.facts = {}
        self.outcome = "returned"


class Observer:
    """64 live primitive contexts, 128 serialized records, no completed history."""

    def __init__(
        self, directory, *, synchronous=False, sink=None, clock=time.monotonic_ns, rss=read_rss
    ):
        self.generation = str(uuid.uuid4())
        self.clock, self.rss = clock, rss
        self.sink = sink or RingSink(directory)
        self.synchronous = synchronous
        self.queue = queue.Queue(maxsize=128)
        self.lock = threading.Lock()
        self.delivery_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(64)
        self.seq = self.ids = self.active = self.lost = 0
        self.closed = False
        self.stopping = threading.Event()
        self.writer = None
        if not synchronous:
            self.writer = threading.Thread(
                target=self._drain, name="worker-memory-writer", daemon=True
            )
            self.writer.start()
        self._emit("generation_start")
        atexit.register(self.close)

    def loss(self):
        with self.lock:
            self.lost = min(MAX_U, self.lost + 1)

    def _write(self, line):
        try:
            self.sink(line)
        except Exception:
            self.loss()

    def tick(self):
        try:
            line = self.queue.get_nowait()
        except queue.Empty:
            return False
        self._write(line)
        self.queue.task_done()
        return True

    def _drain(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                line = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            self._write(line)
            self.queue.task_done()

    def _emit(self, event, token=None, end=False):
        # Preserve per-generation sequence ordering without waiting on another
        # producer's serialization or sampling work.
        if not self.delivery_lock.acquire(blocking=False):
            self.loss()
            return
        try:
            with self.lock:
                if self.seq == MAX_U:
                    self.lost = min(MAX_U, self.lost + 1)
                    return
                self.seq += 1
                seq, active, lost = self.seq, self.active, self.lost
            row = dict.fromkeys(KEYS)
            row.update(
                schema_version=1,
                event=event,
                worker_generation=self.generation,
                record_seq=seq,
                timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                rss_source="unavailable",
                rss_status="unsupported",
                fallback_kind="unknown",
                model_backend={"model": None, "backend": None},
                active_call_count=active,
                lost_record_count=lost,
            )
            from .version import __version__

            if re.fullmatch(r"\d+(?:\.\d+)+", __version__) and len(__version__) <= 32:
                row["build_version"] = __version__
            if token is None:
                sample, row["rss_source"], row["rss_status"] = self.rss()
                row.update(
                    zip(
                        ("rss_before_bytes", "rss_anon_before_bytes", "rss_file_before_bytes"),
                        sample,
                    )
                )
            else:
                row.update(
                    call_id=token.call_id,
                    tool_name=token.tool,
                    dispatch_mode=token.mode,
                    startup_phase=token.phase,
                    rss_source=token.source,
                    rss_status=token.status,
                )
                row.update(
                    zip(
                        ("rss_before_bytes", "rss_anon_before_bytes", "rss_file_before_bytes"),
                        token.before,
                    )
                )
                row.update(token.facts)
                if end:
                    sample, source, status = self.rss()
                    row.update(
                        zip(
                            ("rss_after_bytes", "rss_anon_after_bytes", "rss_file_after_bytes"),
                            sample,
                        )
                    )
                    row["rss_source"] = source
                    row["rss_status"] = status
                    elapsed = self.clock() - token.started
                    row["elapsed_us"] = min(MAX_U, elapsed // 1000) if elapsed >= 0 else None
                    row["outcome"] = token.outcome
                _snapshots(row)
            line = encode_record(row)
            if self.synchronous:
                self._write(line)
            else:
                self.queue.put_nowait(line)
        except Exception:
            self.loss()
        finally:
            self.delivery_lock.release()

    @contextmanager
    def scope(self, tool=None, mode="stdio", phase=None):
        tool = tool if type(tool) is str and tool in TOOL_NAMES else (None if phase else "unknown")
        current = getattr(_LOCAL, "token", None)
        if current is not None or getattr(_LOCAL, "suppressed", False):
            yield current
            return
        if self.closed or not self.slots.acquire(blocking=False):
            self.loss()
            _LOCAL.suppressed = True
            try:
                yield None
            finally:
                _LOCAL.suppressed = False
            return
        token = None
        active_registered = False
        try:
            with self.lock:
                if self.ids == MAX_U:
                    self.lost = min(MAX_U, self.lost + 1)
                else:
                    self.ids += 1
                    token_id = self.ids
                    active_registered = phase is None
                    self.active += active_registered
            if self.ids == MAX_U and "token_id" not in locals():
                yield None
                return
            try:
                token = Token(
                    token_id,
                    tool
                    if type(tool) is str and tool in TOOL_NAMES
                    else (None if phase else "unknown"),
                    mode,
                    phase,
                    self.rss(),
                    self.clock,
                )
            except Exception:
                self.loss()
            if token is None:
                if active_registered:
                    with self.lock:
                        self.active -= 1
                    active_registered = False
                _LOCAL.suppressed = True
                try:
                    yield None
                finally:
                    _LOCAL.suppressed = False
                return
            _LOCAL.token = token
            self._emit("phase_begin" if phase else "call_begin", token)
            try:
                yield token
            except BaseException:
                token.outcome = "raised"
                raise
            finally:
                self._emit("phase_end" if phase else "call_end", token, end=True)
        finally:
            _LOCAL.token = None
            if active_registered:
                with self.lock:
                    self.active -= 1
            self.slots.release()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._emit("generation_stop")
        # A blocked filesystem never holds process cleanup hostage.
        self.stopping.set()
        if self.writer is not None:
            self.writer.join(timeout=0.05)
        atexit.unregister(self.close)


def _snapshots(row):
    """Only lengths of already resident owners; no cache keys or data reads."""
    server = sys.modules.get("mempalace.mcp_server")
    for attribute, key in (
        ("_metadata_cache", "metadata_rows"),
        ("_kg_by_path", "kg_handle_count"),
        ("_logstream_by_path", "logstream_handle_count"),
    ):
        if server is not None and hasattr(server, attribute):
            owner = getattr(server, attribute)
            if owner is None or type(owner) in (dict, list, tuple):
                row[key] = 0 if owner is None else min(MAX_U, len(owner))
    if (
        row["chroma_client_count"] is None
        and server is not None
        and hasattr(server, "_client_cache")
    ):
        row["chroma_client_count"] = int(server._client_cache is not None)
    embedding = sys.modules.get("mempalace.embedding")
    if embedding is not None and type(getattr(embedding, "_EF_CACHE", None)) is dict:
        row["ef_instance_count"] = min(MAX_U, len(embedding._EF_CACHE))


def mark(**facts):
    """Internal fixed-label facts only; validate before retaining any value."""
    token = getattr(_LOCAL, "token", None)
    if token is None:
        return
    for key, value in facts.items():
        if key in BOOL_KEYS and type(value) is bool:
            token.facts[key] = value
        elif key == "fallback_kind" and type(value) is str and value in ENUMS[key]:
            token.facts[key] = value


def success(counter):
    token = getattr(_LOCAL, "token", None)
    if token is not None and counter in {
        "restricted_pickle_load_count",
        "collection_open_count",
        "model_session_create_count",
    }:
        token.facts[counter] = min(MAX_U, token.facts.get(counter, 0) + 1)


def selected(*, model=None, backend=None):
    token = getattr(_LOCAL, "token", None)
    if token is not None:
        members = token.facts.setdefault("model_backend", {"model": None, "backend": None})
        if model is not None:
            members["model"] = model if type(model) is str and model in MODELS else "other"
        if backend is not None:
            members["backend"] = (
                backend if type(backend) is str and backend in BACKENDS else "other"
            )


@contextmanager
def transport():
    """Hold the primitive token through write/flush and transport ref release."""
    if getattr(_LOCAL, "defer", False):
        yield
        return
    _LOCAL.defer = True
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        pending = getattr(_LOCAL, "pending", None)
        _LOCAL.pending = None
        _LOCAL.defer = False
        if pending is not None:
            if failed:
                token = getattr(_LOCAL, "token", None)
                if token is not None:
                    token.outcome = "raised"
            pending.__exit__(None, None, None)


def control_outcome(value):
    token = getattr(_LOCAL, "token", None)
    if token is not None and type(value) is str and value in ENUMS["outcome"]:
        token.outcome = value


def covered(counter):
    token = getattr(_LOCAL, "token", None)
    if token is not None and counter in {
        "restricted_pickle_load_count",
        "collection_open_count",
        "model_session_create_count",
    }:
        token.facts.setdefault(counter, 0)


def client_count(value):
    token = getattr(_LOCAL, "token", None)
    if token is not None and type(value) is int and 0 <= value <= MAX_U:
        token.facts["chroma_client_count"] = value
