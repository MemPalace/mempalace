"""Start a loopback HTTP hub so stdio MCP children can proxy instead of write.

Cursor (and any host that spawns one ``mempalace-mcp`` per window) used to
race for the ChromaDB writer lease. This module is the opt-in fix:
``ensure_hub`` starts one detached ``mempalace.mcp_server --transport http``
on loopback if none is already registered, then stdio children forward to it.

Stdlib plus :mod:`mempalace.server_registry` and :mod:`mempalace.config` only
so ``mempalace-mcp --ensure-hub`` stays a thin proxy and never pulls chromadb
just to decide whether a hub exists.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import server_registry

logger = logging.getLogger(__name__)

ENSURE_HUB_FLAG = "--ensure-hub"
ENSURE_HUB_ENV = "MEMPALACE_MCP_ENSURE_HUB"
_WRITER_WAIT_ENV = "MEMPALACE_MCP_WRITER_WAIT_SECONDS"
_HUB_HOST = "127.0.0.1"
_HUB_PORT = 0
_READY_TIMEOUT_S = 10.0
_READY_POLL_S = 0.1
_MULTI_PROCESS_WRITER_BACKENDS = frozenset({"pgvector", "qdrant"})

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows CI
    _fcntl = None


def detached_popen_kwargs(
    *,
    stdin=subprocess.DEVNULL,
    stdout=None,
    stderr=None,
    close_fds: bool = True,
) -> dict[str, Any]:
    """Popen kwargs for a hidden, session-detached child.

    Shared by the daemon, hook CLI, and hub bootstrap so Windows does not
    flash a console (#1783) or hang the parent on inherited stdio (#1268).
    ``CREATE_NO_WINDOW`` is never OR'd with ``DETACHED_PROCESS``.
    """

    kwargs: dict[str, Any] = {"stdin": stdin, "close_fds": close_fds}
    if stdout is not None:
        kwargs["stdout"] = stdout
    if stderr is not None:
        kwargs["stderr"] = stderr
    if os.name == "nt":
        flags = 0
        for name in (
            "CREATE_NO_WINDOW",
            "CREATE_NEW_PROCESS_GROUP",
            "CREATE_BREAKAWAY_FROM_JOB",
        ):
            flags |= getattr(subprocess, name, 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    return kwargs


def wants_ensure_hub(argv: list[str] | None = None) -> bool:
    """True when the caller asked for a hub via flag or environment."""

    args = argv if argv is not None else sys.argv[1:]
    if ENSURE_HUB_FLAG in args:
        return True
    return os.environ.get(ENSURE_HUB_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _backend_needs_single_writer_hub(backend: str | None) -> bool:
    """Conservative copy of ``backend_requires_single_writer`` without palace.

    Importing :mod:`mempalace.palace` would pull chromadb into every
    ``--ensure-hub`` proxy. Unknown names default to needing a hub.
    """

    name = (backend or os.environ.get("MEMPALACE_BACKEND") or "chroma").strip().lower()
    if name in _MULTI_PROCESS_WRITER_BACKENDS:
        return False
    if name == "milvus":
        try:
            from .backends.milvus import milvus_uri_is_server
            from .config import MempalaceConfig

            return not milvus_uri_is_server(MempalaceConfig().milvus_uri)
        except Exception:
            logger.debug("milvus single-writer probe failed; assuming hub needed", exc_info=True)
            return True
    return True


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("could not chmod %s", path, exc_info=True)


def _hub_ready(palace_path: str) -> bool:
    info = server_registry.read_live_serverinfo(palace_path)
    if not info:
        return False
    base_url = server_registry.client_base_url(info)
    try:
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=1.0) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False


def _spawn_hub(palace_path: str, backend: str | None, log_path: Path) -> subprocess.Popen | None:
    cmd = [
        sys.executable,
        "-m",
        "mempalace.mcp_server",
        "--transport",
        "http",
        "--host",
        _HUB_HOST,
        "--port",
        str(_HUB_PORT),
        "--palace",
        palace_path,
    ]
    if backend:
        cmd.extend(["--backend", backend])
    env = os.environ.copy()
    env[_WRITER_WAIT_ENV] = "0"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8")
    _chmod_private(log_path)
    try:
        return subprocess.Popen(
            cmd,
            env=env,
            **detached_popen_kwargs(stdout=log_fh, stderr=log_fh),
        )
    except OSError:
        logger.warning("failed to spawn palace hub", exc_info=True)
        return None
    finally:
        log_fh.close()


def _wait_for_hub(palace_path: str, proc: subprocess.Popen | None) -> bool:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    child_exit_logged = False
    while time.monotonic() < deadline:
        if _hub_ready(palace_path):
            return True
        if proc is not None and proc.poll() is not None and not child_exit_logged:
            # Our child died (Windows has no flock, so a sibling often wins
            # the lease with WRITER_WAIT=0). Keep polling: the winner's
            # serverinfo is what matters, not this process's pid.
            logger.info(
                "palace hub child exited with code %s; waiting for a sibling hub",
                proc.returncode,
            )
            child_exit_logged = True
        time.sleep(_READY_POLL_S)
    if _hub_ready(palace_path):
        return True
    logger.warning(
        "palace hub did not become ready within %.0fs; falling back to local",
        _READY_TIMEOUT_S,
    )
    return False


def ensure_hub(palace_path: str | None, backend: str | None = None) -> bool:
    """Ensure a live loopback hub for ``palace_path``. True when one is usable.

    Returns False (and logs) when a hub is unnecessary, cannot start, or
    does not answer ``/healthz`` in time. Callers then keep today's local
    writer behaviour — a missed upgrade, not a regression.
    """

    if not palace_path:
        return False
    if not _backend_needs_single_writer_hub(backend):
        return False
    if _hub_ready(palace_path):
        return True

    state_dir = server_registry.server_state_dir(palace_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "hub-start.lock"
    lock_fh = open(lock_path, "w", encoding="utf-8")
    _chmod_private(lock_path)
    try:
        if _fcntl is not None:
            _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_EX)
        if _hub_ready(palace_path):
            return True
        proc = _spawn_hub(palace_path, backend, state_dir / "hub.log")
        return _wait_for_hub(palace_path, proc)
    except OSError:
        logger.warning("palace hub start lock failed; falling back to local", exc_info=True)
        return False
    finally:
        try:
            lock_fh.close()
        except OSError:
            logger.debug("hub-start.lock close failed", exc_info=True)
