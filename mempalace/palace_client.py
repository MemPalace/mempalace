"""Single construction point for MemPalace's Chroma client (HTTP mode).

Opt-in topology for multi-process palace access (#832, #1096): one
standalone ``chroma run`` server exclusively owns the persist directory;
every MemPalace process (MCP servers, hook CLI, miner) is a thin HTTP
client built here. Nothing outside this module may construct a Chroma
client in HTTP mode — that is what prevents a second embedded writer from
re-appearing and racing on the shared sqlite/HNSW files.

Transport selection:
    MEMPALACE_CHROMA_MODE=embedded (default) | http
The default stays embedded so single-process installs keep working with
zero configuration and no external service. Set ``http`` when more than
one process (multiple MCP servers, stop-hook mines, manual CLI runs)
touches the same palace concurrently — the case embedded
``PersistentClient`` does not support.

Server address (HTTP mode):
    CHROMA_HOST (default 127.0.0.1) / CHROMA_PORT (default 8801)

Guarantees provided to every caller in HTTP mode:

* Health probe — client construction first hits ``/api/v2/heartbeat`` with a
  3-second timeout and raises :class:`PalaceBackendUnreachableError` (naming
  host:port) instead of hanging when the server is down.
* Hard per-operation timeout — every method call on the returned client (and
  on any collection obtained from it) runs under a 15-second ceiling
  (``MEMPALACE_OP_TIMEOUT`` overrides) and raises
  :class:`PalaceOperationTimeoutError` naming the operation. No palace call
  may ever block the MCP server indefinitely.
"""

from __future__ import annotations

import logging
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeoutError

logger = logging.getLogger(__name__)

DEFAULT_CHROMA_HOST = "127.0.0.1"
DEFAULT_CHROMA_PORT = 8801
HEALTH_PROBE_TIMEOUT_S = 3.0
DEFAULT_OP_TIMEOUT_S = 15.0


class PalaceBackendUnreachableError(RuntimeError):
    """The standalone Chroma server did not answer the health probe."""


class PalaceOperationTimeoutError(RuntimeError):
    """A palace operation exceeded the hard per-operation timeout."""


def http_mode() -> bool:
    """True only when ``MEMPALACE_CHROMA_MODE=http`` opts in."""
    return os.environ.get("MEMPALACE_CHROMA_MODE", "embedded").strip().lower() == "http"


def chroma_host() -> str:
    return os.environ.get("CHROMA_HOST", "").strip() or DEFAULT_CHROMA_HOST


def chroma_port() -> int:
    raw = os.environ.get("CHROMA_PORT", "").strip()
    if not raw:
        return DEFAULT_CHROMA_PORT
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid CHROMA_PORT=%r; using default %d", raw, DEFAULT_CHROMA_PORT)
        return DEFAULT_CHROMA_PORT


def backend_address() -> str:
    return f"{chroma_host()}:{chroma_port()}"


def op_timeout_s() -> float:
    raw = os.environ.get("MEMPALACE_OP_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_OP_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid MEMPALACE_OP_TIMEOUT=%r; using default %.0fs", raw, DEFAULT_OP_TIMEOUT_S
        )
        return DEFAULT_OP_TIMEOUT_S


def probe_backend(timeout_s: float = HEALTH_PROBE_TIMEOUT_S) -> None:
    """Hit the server's v2 heartbeat; raise on any failure, never hang.

    chromadb 1.5.x serves ``GET /api/v2/heartbeat`` (verified against the
    `chroma run` server, chromadb 1.5.x; the v1 path is gone in the Rust frontend).
    """
    url = f"http://{backend_address()}/api/v2/heartbeat"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            if resp.status != 200:
                raise PalaceBackendUnreachableError(
                    f"palace backend unreachable at {backend_address()} "
                    f"(heartbeat returned HTTP {resp.status})"
                )
    except PalaceBackendUnreachableError:
        raise
    except Exception as exc:
        raise PalaceBackendUnreachableError(
            f"palace backend unreachable at {backend_address()} ({exc.__class__.__name__}: {exc})"
        ) from exc


# ---------------------------------------------------------------------------
# Hard per-operation timeout
# ---------------------------------------------------------------------------

# chromadb's sync client blocks the calling thread, so the ceiling is
# enforced by running the call in a worker and bounding the wait. A timed-out
# call keeps running in its worker until the socket dies — the executor
# exists to bound *our* wait, not to cancel the remote work.
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="palace-op")
    return _executor


def run_with_timeout(op_name: str, fn, /, *args, timeout_s: float | None = None, **kwargs):
    """Run ``fn(*args, **kwargs)`` under the hard operation timeout.

    Raises :class:`PalaceOperationTimeoutError` naming ``op_name`` when the
    ceiling is hit. Exceptions raised by ``fn`` propagate unchanged so
    callers' existing except-clauses (e.g. Chroma NotFoundError handling)
    keep working.
    """
    limit = op_timeout_s() if timeout_s is None else timeout_s
    future = _get_executor().submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=limit)
    except _FutureTimeoutError:
        future.cancel()
        raise PalaceOperationTimeoutError(
            f"palace operation '{op_name}' timed out after {limit:g}s against {backend_address()}"
        ) from None


# Duck-type check for chromadb Collection objects so the proxy can wrap
# collections returned by client methods without importing chromadb types.
_COLLECTION_MARKERS = ("query", "upsert", "get", "count")


def _looks_like_collection(obj) -> bool:
    return all(callable(getattr(obj, m, None)) for m in _COLLECTION_MARKERS)


def _wrap_result(result, label: str):
    if _looks_like_collection(result):
        name = getattr(result, "name", "?")
        return TimeoutProxy(result, label=f"collection[{name}]")
    if isinstance(result, list):
        return [_wrap_result(item, label) for item in result]
    return result


class TimeoutProxy:
    """Timeout guard for a chromadb client or collection.

    Every method call on the proxied object runs under
    :func:`run_with_timeout`; non-callable attributes pass through raw.
    Collection-shaped return values (single or in lists) are wrapped
    recursively, so ``client.get_collection(...).query(...)`` is guarded end
    to end without any call-site changes.
    """

    __slots__ = ("_tp_inner", "_tp_label")

    def __init__(self, inner, label: str):
        self._tp_inner = inner
        self._tp_label = label

    def __getattr__(self, name):
        attr = getattr(self._tp_inner, name)
        if not callable(attr):
            return attr
        label = self._tp_label

        def _timed(*args, **kwargs):
            result = run_with_timeout(f"{label}.{name}", attr, *args, **kwargs)
            return _wrap_result(result, label)

        _timed.__name__ = name
        return _timed

    def __repr__(self):
        return f"TimeoutProxy({self._tp_inner!r})"


def make_http_client():
    """Probe the backend, then return a timeout-guarded chromadb HttpClient.

    The only place MemPalace constructs a Chroma client in HTTP mode. The
    HttpClient constructor itself performs wire calls (tenant/database
    validation), so it too runs under the operation timeout.
    """
    probe_backend()
    import chromadb  # late import: keeps module import side-effect free

    try:
        client = run_with_timeout(
            "chroma.connect",
            lambda: chromadb.HttpClient(host=chroma_host(), port=chroma_port()),
        )
    except PalaceOperationTimeoutError:
        raise
    except Exception as exc:
        raise PalaceBackendUnreachableError(
            f"palace backend unreachable at {backend_address()} (client construction failed: {exc})"
        ) from exc
    return TimeoutProxy(client, label="chroma")
