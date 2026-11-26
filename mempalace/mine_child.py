"""Mine worker child: does a daemon mine's heavy compute at idle priority.

The daemon stays the palace's only writer. This process runs the unchanged
``service.run_mine`` -- parsing, chunking and embedding -- but never opens the
storage backend itself: every collection it asks for is a
:class:`RemoteCollection` whose calls travel over a pipe to the daemon, which
runs them on its own client. Documents are embedded here, through the same
``EmbeddingCollection`` wrapper that backends needing explicit vectors use, so
the daemon receives ready vectors and only writes. A persistent Chroma
database opened by two processes returns inconsistent results
(chroma-core/chroma#3792); this split keeps it at one.

Because the daemon serves one call at a time under its write lock, other work
-- a ``diary_write`` from a Stop hook -- lands between this mine's calls
instead of waiting for the whole mine.

Wire format: each message is a 4-byte big-endian length then a pickled dict.
Requests go out on the original stdout, replies come in on stdin; the mine's
own prints are moved to stderr, which the daemon collects as the job's output.

Priority is lowered first thing, before anything heavy is imported: on Linux
``sched_setscheduler`` applies to the calling thread, and the threads that
onnxruntime starts later inherit it.
"""

from __future__ import annotations

import os
import sys
import time


def lower_priority() -> str:
    """Drop this process to background CPU priority; return what was applied.

    Linux ``SCHED_IDLE`` runs only when nothing else wants the CPU (fallback
    ``nice 19``); macOS ``taskpolicy -b`` also throttles I/O; Windows
    ``PROCESS_MODE_BACKGROUND_BEGIN`` lowers CPU, I/O and memory priority
    (fallback ``IDLE_PRIORITY_CLASS``). Best-effort: a refusal leaves the
    process at normal priority rather than failing the mine.
    """
    if os.name == "nt":  # pragma: no cover - Windows
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetCurrentProcess()
            if kernel32.SetPriorityClass(handle, 0x00100000):  # BACKGROUND_BEGIN
                return "windows-background"
            if kernel32.SetPriorityClass(handle, 0x00000040):  # IDLE_PRIORITY_CLASS
                return "windows-idle"
        except Exception:
            pass
        return "normal"
    applied = "normal"
    sched_idle = getattr(os, "SCHED_IDLE", None)
    if sched_idle is not None:
        try:
            os.sched_setscheduler(0, sched_idle, os.sched_param(0))
            applied = "sched-idle"
        except OSError:
            pass
    if sys.platform == "darwin":  # pragma: no cover - macOS
        try:
            import subprocess

            subprocess.run(
                ["taskpolicy", "-b", "-p", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=True,
            )
            applied = "taskpolicy-background"
        except Exception:
            pass
    if applied == "normal":
        try:
            os.nice(19)
            applied = "nice-19"
        except OSError:
            pass
    return applied


def _read_msg(stream):
    import pickle

    header = stream.read(4)
    if len(header) < 4:
        raise EOFError("daemon closed the mine channel")
    size = int.from_bytes(header, "big")
    data = stream.read(size)
    if len(data) < size:
        raise EOFError("daemon closed the mine channel mid-message")
    return pickle.loads(data)


def _write_msg(stream, msg) -> None:
    import pickle

    data = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(len(data).to_bytes(4, "big") + data)
    stream.flush()


class ChildWriterViolation(RuntimeError):
    """A mine path tried to open the storage backend inside the mine child."""


# How often the child tells the daemon it is alive. The daemon's reaper kills a
# child silent for ``daemon.mine_stall_seconds`` (60 s by default).
HEARTBEAT_SECONDS = 10.0


class _Channel:
    def __init__(self, rx, tx):
        import threading

        self.rx = rx
        self.tx = tx
        # CPU this process has spent since the mine last made progress: a
        # storage reply or an embedded slice (see ``mark_progress``). The
        # heartbeat reports it, so the daemon can tell a mine that computes
        # from one that spins while its heartbeat thread keeps beating.
        self._compute_started = time.process_time()
        # Work units done so far (embedded slices). Sent with each heartbeat.
        self.work_units = 0
        self._request_lock = threading.Lock()
        # The heartbeat thread writes too; frames must not interleave.
        self._tx_lock = threading.Lock()

    def _send(self, msg) -> None:
        with self._tx_lock:
            _write_msg(self.tx, msg)

    def request(self, msg):
        # There is one reply stream. Concurrent callers must keep each
        # request paired with its own reply. Heartbeats remain independent.
        with self._request_lock:
            self._send(msg)
            reply = _read_msg(self.rx)
            self._compute_started = time.process_time()
        if reply.get("ok"):
            return reply.get("value")
        exc = reply.get("exc")
        if isinstance(exc, BaseException):
            raise exc
        raise RuntimeError(reply.get("message") or "daemon refused the call")

    def notify(self, msg) -> None:
        self._send(msg)

    def mark_progress(self) -> None:
        """Record one unit of mine work done between storage calls."""
        self.work_units += 1
        self._compute_started = time.process_time()

    def start_heartbeat(self, interval: float = HEARTBEAT_SECONDS):
        import threading

        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(interval):
                try:
                    self.notify(
                        {
                            "op": "progress",
                            "compute_cpu": time.process_time() - self._compute_started,
                            "units": self.work_units,
                        }
                    )
                except (BrokenPipeError, OSError, ValueError):
                    return

        threading.Thread(target=beat, name="mine-child-heartbeat", daemon=True).start()
        return stop


_CHANNEL: _Channel | None = None

# Documents embedded per slice in the child. A multiple of the ONNX
# embedders' 32-document sub-batch, so slicing does not change how a batch is
# padded, and small enough that a 1000-drawer upsert reports progress many
# times instead of computing for minutes in one silent stretch.
_EMBED_SLICE = 256


def _sliced_embedder(embed):
    """Wrap ``embedding_wrapper._embed_texts`` to embed in progress-marking slices."""

    def embed_texts(texts, *, query=False, metadatas=None, embedder=None):
        vectors = []
        for start in range(0, len(texts), _EMBED_SLICE):
            stop = start + _EMBED_SLICE
            vectors.extend(
                embed(
                    texts[start:stop],
                    query=query,
                    metadatas=None if metadatas is None else metadatas[start:stop],
                    embedder=embedder,
                )
            )
            if _CHANNEL is not None:
                _CHANNEL.mark_progress()
        return vectors

    return embed_texts


# BaseCollection methods that simply run in the daemon. ``close`` is not here:
# the daemon owns the client and its lifetime, so a mine closing "its"
# collection must not close the daemon's.
_FORWARDED_METHODS = (
    "add",
    "upsert",
    "query",
    "get",
    "delete",
    "count",
    "update",
    "estimated_count",
    "health",
    "get_stored_embedder_identity",
    "set_embedder_identity",
    "effective_embedder_identity",
    "get_all_metadata",
    "maintenance_state",
)


def _forward(method):
    def call(self, *args, **kwargs):
        return self._call(method, *args, **kwargs)

    call.__name__ = method
    return call


def _remote_collection_class():
    from .backends.base import BaseCollection

    def __init__(self, handle: int, caps: dict):
        self._handle = handle
        self._caps = caps

    def _call(self, method, *args, **kwargs):
        return _CHANNEL.request(
            {"op": "call", "handle": self._handle, "method": method, "args": args, "kwargs": kwargs}
        )

    def __getattr__(self, name):
        # Backend-specific helpers (anything BaseCollection does not declare)
        # run remotely too. Dunder and private lookups stay local.
        if name.startswith("_"):
            raise AttributeError(name)
        caps = self.__dict__.get("_caps") or {}
        if name not in caps.get("methods", ()):
            raise AttributeError(name)
        return lambda *a, **kw: self._call(name, *a, **kw)

    namespace = {name: _forward(name) for name in _FORWARDED_METHODS}
    namespace.update(
        {
            "__doc__": "A collection whose every call runs in the daemon.",
            "__init__": __init__,
            "_call": _call,
            "__getattr__": __getattr__,
            "close": lambda self: None,
            "upsert_merges_metadata": property(
                lambda self: bool(self._caps.get("upsert_merges_metadata"))
            ),
            "distance_metric": property(lambda self: self._caps.get("distance_metric") or "cosine"),
        }
    )
    remote_cls = type("RemoteCollection", (BaseCollection,), namespace)

    def iter_metadata(self, *args, **kwargs):
        """The one-pass metadata stream, paged over the channel."""
        gen = _CHANNEL.request(
            {"op": "iter", "handle": self._handle, "args": args, "kwargs": kwargs}
        )
        while True:
            page = _CHANNEL.request({"op": "next", "gen": gen})
            yield from page["items"]
            if page["done"]:
                return

    remote_iter_cls = type("RemoteIterCollection", (remote_cls,), {"iter_metadata": iter_metadata})
    return remote_cls, remote_iter_cls


def _refused_in_child(*args, **kwargs):
    # Runs with the poisoned function's globals, so it names nothing from here.
    raise __import__("mempalace.mine_child", fromlist=["_"]).ChildWriterViolation(
        f"{__import__('sys')._getframe().f_code.co_name} called inside the mine child; "
        "every storage write must go through the daemon"
    )


def _refuse_when_called(fn) -> None:
    """Make ``fn`` itself raise ChildWriterViolation, wherever it is referenced."""
    fn.__code__ = _refused_in_child.__code__.replace(co_name=fn.__name__)


def _install_remote_storage() -> None:
    """Route every collection open to the daemon; forbid opening a backend here."""
    import importlib

    from . import (  # noqa: F401 - bind names first
        cli,
        convo_miner,
        format_miner,
        hallways,
        miner,
        palace as palace_mod,
        revisions,
        service,
    )
    from .backends import base as backends_base, embedding_wrapper
    from .backends.embedding_wrapper import EmbeddingCollection

    remote_cls, remote_iter_cls = _remote_collection_class()
    original_get_collection = palace_mod.get_collection
    original_validate = palace_mod._validate_palace_fts5_after_mine
    original_serial = palace_mod.palace_write_serial

    def get_collection(palace_path=None, collection_name=None, create=True, backend=None, **kw):
        info = _CHANNEL.request(
            {
                "op": "open",
                "kwargs": {
                    "collection_name": collection_name,
                    "create": create,
                    "backend": backend,
                    **kw,
                },
            }
        )
        cls = remote_iter_cls if info["caps"].get("iter_metadata") else remote_cls
        coll = cls(info["handle"], info["caps"])
        if info["caps"].get("server_embedder"):
            return coll
        # Embed here, at idle priority; the daemon receives explicit vectors.
        return EmbeddingCollection(coll)

    def validate_after_mine(palace_path, *, writes_since=None):
        # The post-mine integrity check reads the daemon's storage: run it
        # there, against the writes the daemon made for this job.
        return _CHANNEL.request({"op": "validate"})

    def store_hallways(wing, file_keys, pair_counts, pair_rooms, min_count, *, config):
        return _CHANNEL.request(
            {"op": "store_hallways", "args": (wing, file_keys, pair_counts, pair_rooms, min_count)}
        )

    cli_run_pass_zero = cli._run_pass_zero
    hallways_store = hallways._store_wing_hallways
    revisions_append = revisions.append_drawer_revisions
    topic_tunnels = miner._compute_topic_tunnels_for_wing
    entity_tunnels = miner._compute_entity_tunnels_for_wing
    replacements = {
        original_get_collection: get_collection,
        cli_run_pass_zero: lambda project_dir, palace_dir, llm_provider: _CHANNEL.request(
            {"op": "redetect_origin", "source": project_dir}
        ),
        hallways_store: store_hallways,
        revisions_append: lambda palace_path, revisions: _CHANNEL.request(
            {"op": "revisions", "rows": list(revisions)}
        ),
        topic_tunnels: lambda wing, config=None: _CHANNEL.request(
            {"op": "topic_tunnels", "wing": wing}
        ),
        entity_tunnels: lambda wing, config=None: _CHANNEL.request(
            {"op": "entity_tunnels", "wing": wing}
        ),
        embedding_wrapper._embed_texts: _sliced_embedder(embedding_wrapper._embed_texts),
        original_validate: validate_after_mine,
        # Nothing is written to storage from this process, so the serial a
        # miner passes to the validation above never moves here.
        original_serial: lambda: 0,
    }
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "mempalace" or name.startswith("mempalace.")):
            continue
        for attr, value in list(vars(module).items()):
            try:
                replacement = replacements.get(value)
            except TypeError:  # unhashable module attribute
                continue
            if replacement is not None:
                setattr(module, attr, replacement)

    # The sweep only rebinds module attributes. A reference kept anywhere else
    # (a closure, a default argument, a callback table) would still run the
    # original and write the palace from this process, so the originals of
    # the writers above now refuse, as a direct backend open does below.
    for original in (
        original_validate,
        cli_run_pass_zero,
        hallways_store,
        revisions_append,
        topic_tunnels,
        entity_tunnels,
    ):
        _refuse_when_called(original)

    def _forbidden(self, *a, **kw):
        raise ChildWriterViolation(
            f"{type(self).__name__}.get_collection called inside the mine child; "
            "every storage call must go through the daemon"
        )

    # Load the backend this palace uses so its class exists, then refuse every
    # direct open: a mine path that bypassed get_collection fails loudly
    # instead of quietly becoming a second writer.
    try:
        from .backends import get_backend_class
        from .palace import resolve_backend_name

        get_backend_class(resolve_backend_name(os.environ.get("MEMPALACE_PALACE_PATH", "")))
    except Exception:
        pass
    importlib.import_module("mempalace.backends.chroma")
    stack = [backends_base.BaseBackend]
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if "get_collection" in vars(cls) or cls is backends_base.BaseBackend:
            cls.get_collection = _forbidden


def _adopt_palace_lock(palace_path: str) -> None:
    """Treat the palace write lock as held: the daemon holds it for this job."""
    from . import palace as palace_mod, paths

    palace_mod._mark_held(paths.palace_key(palace_path, length=16))


def main() -> int:
    applied = lower_priority()
    rx = sys.stdin.buffer
    tx = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    # The mine's own prints go to stderr from here on; fd 1 is the channel.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr

    global _CHANNEL
    _CHANNEL = _Channel(rx, tx)
    try:
        job = _read_msg(rx)
    except EOFError:
        return 2
    payload = dict(job.get("payload") or {})
    palace_path = payload.get("palace_path") or os.environ.get("MEMPALACE_PALACE_PATH", "")
    _CHANNEL.notify({"op": "started", "pid": os.getpid(), "priority": applied})
    heartbeat = _CHANNEL.start_heartbeat()
    try:
        if job.get("lock_held", True):
            _adopt_palace_lock(palace_path)
        _install_remote_storage()
        from .service import run_mine

        result = run_mine(payload)
    except BaseException as exc:  # noqa: BLE001 - report every failure to the daemon
        result = {
            "success": False,
            "error": f"mine child failed: {type(exc).__name__}: {exc}",
            "error_class": type(exc).__name__,
            "exit_code": 1,
        }
    heartbeat.set()
    result = dict(result or {})
    result["priority"] = applied
    try:
        _CHANNEL.notify({"op": "done", "result": result})
    except (BrokenPipeError, OSError):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
