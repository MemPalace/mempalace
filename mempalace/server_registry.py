"""Per-palace discovery registry for the MemPalace HTTP MCP hub.

``mempalace serve`` (and ``mempalace-mcp --transport http``) is meant to be
the single long-lived writer for a shared palace: once it performs its first
mutating tool call it holds the per-palace MCP writer lease for its
whole lifetime, and every other process is refused palace writes. That is
correct for agents — they talk to the hub — but the background save hooks
and the plain CLI still spawn short-lived ``mempalace mine`` processes,
which would be refused while a hub is up and silently stop transcript
capture on the hub machine.

This module gives those local processes a way to find the hub instead of
fighting it: the HTTP transport records ``{pid, host, port, scheme,
read_only, capabilities, search_config_fingerprint}`` next to the per-palace
bearer token (``~/.mempalace/server/<key>/``), and callers use
:func:`read_live_serverinfo` to decide "forward this write over HTTP" vs
"no hub — do the write directly".

The registry is local-machine only by design: it lives under the user's
home, is keyed by the canonical palace path, and a record is trusted only
while the recorded pid is still alive — a crashed hub leaves a stale file
that every reader ignores and the next hub overwrites.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mempalace import paths
from mempalace._env import env_str
from mempalace._proc import pid_alive as _pid_alive
from mempalace.fsio import atomic_write_text, read_published_bytes

from .timeutil import utc_now_z

logger = logging.getLogger(__name__)

# Bind-address wildcards: a hub bound to "all interfaces" is dialed via
# loopback by local forwarders.
_WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]"}
_TOKEN_ENV = "MEMPALACE_MCP_HTTP_TOKEN"


def _utc_now() -> str:
    return utc_now_z()


def server_state_dir(palace_path: str) -> Path:
    """Per-palace state directory shared by the token and the serverinfo.

    Keyed by the canonical palace path so one hub per palace reuses a stable
    directory across restarts.
    """
    return paths.state_path("server", paths.palace_key(palace_path))


def server_token_path(palace_path: str) -> Path:
    return server_state_dir(palace_path) / "token"


def serverinfo_path(palace_path: str) -> Path:
    return server_state_dir(palace_path) / "serverinfo.json"


def _read_server_token_file(palace_path: str) -> str:
    """The stored token, or "" when it is missing, unreadable or not text."""
    try:
        return read_published_bytes(server_token_path(palace_path)).decode("utf-8").strip()
    except (OSError, ValueError):
        return ""


def load_or_create_server_token(palace_path: str) -> tuple[str, bool]:
    """Return ``(token, created)`` for the palace's HTTP bearer token.

    Reuses the stored token so it is stable across restarts, or mints one.
    Creation runs under a cross-process lock, so hubs starting together settle
    on one token, and an empty or garbled file left by a crash is replaced.
    The file is published 0600 by rename, so it is never readable half-written;
    a symlink at the path is removed first, never written through.
    """
    import secrets

    from .palace import mine_lock

    path = server_token_path(palace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path.parent), 0o700)
    except OSError:
        pass
    with mine_lock(str(path)):
        existing = _read_server_token_file(palace_path)
        if existing:
            return existing, False
        token = secrets.token_urlsafe(32)
        if path.is_symlink():
            path.unlink()
        atomic_write_text(path, token + "\n")
    return token, True


def write_serverinfo(
    palace_path: str,
    *,
    host: str,
    port: int,
    scheme: str,
    read_only: bool,
    capabilities=None,
    search_config_fingerprint=None,
):
    """Record this process as the palace's HTTP hub. Returns the file path.

    0600 like the token: the record itself is not secret, but the directory
    convention is "private to the user" and there is no reason to relax it.
    """
    path = serverinfo_path(palace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path.parent), 0o700)
    except OSError:
        pass
    payload = {
        "pid": os.getpid(),
        "host": host,
        "port": int(port),
        "scheme": scheme,
        "read_only": bool(read_only),
        "capabilities": sorted(set(capabilities or [])),
        "search_config_fingerprint": search_config_fingerprint,
        "palace_path": paths.canonical_palace_path(palace_path),
    }
    atomic_write_text(path, json.dumps(payload) + "\n")
    return path


def mesh_state_path(palace_path: str) -> Path:
    return server_state_dir(palace_path) / "mesh_state.json"


def write_mesh_state(palace_path: str, *, peers: dict, profiles: dict) -> Path:
    """Publish the hub's mesh estate so other local processes can read it.

    The estate — which peers answered last round, their version vectors and
    advertised profiles — is built by the peer sync loop, and that loop only
    runs in the HTTP transport. Every other process for this palace (the
    stdio MCP servers agents actually connect through, the CLI) has the same
    ``mempalace_mesh_peers`` tool and an empty in-memory estate behind it, so
    without this file they answer "two peers, no status at all" while the hub
    next door knows the whole picture.

    0600 like the token and the serverinfo. The contents are not secret —
    peers.json tokens never reach the estate — but the directory convention
    is "private to the user".
    """
    path = mesh_state_path(palace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(path.parent), 0o700)
    except OSError:
        pass
    payload = {
        "pid": os.getpid(),
        "written_at": _utc_now(),
        "peers": peers,
        "profiles": profiles,
    }
    # Readers in other processes must never observe a half-serialized
    # estate, and this file is rewritten every sync round.
    atomic_write_text(path, json.dumps(payload) + "\n", follow_symlinks=False)
    return path


def read_mesh_state(palace_path: str) -> dict:
    """Return the published estate for this palace.

    Always a dict with ``peers``/``profiles`` mappings so callers can merge
    without None-checks; empty when no hub has published yet or the file is
    unreadable. ``writer_alive`` reports whether the publishing process is
    still running — a crashed hub leaves a last-known-good estate that is
    worth showing but must not be read as live.
    """
    empty = {"peers": {}, "profiles": {}, "written_at": None, "writer_alive": False}
    try:
        state = json.loads(read_published_bytes(mesh_state_path(palace_path)).decode("utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(state, dict):
        return empty
    peers = state.get("peers")
    profiles = state.get("profiles")
    return {
        "peers": peers if isinstance(peers, dict) else {},
        "profiles": profiles if isinstance(profiles, dict) else {},
        "written_at": state.get("written_at"),
        "writer_alive": _pid_alive(state.get("pid")),
    }


def clear_serverinfo(palace_path: str) -> None:
    """Remove this process's serverinfo record, if it is still ours.

    Guarded on the recorded pid so a slow atexit from an old hub cannot
    delete the record a newer hub just wrote for the same palace.
    """
    path = serverinfo_path(palace_path)
    try:
        recorded = json.loads(read_published_bytes(path).decode("utf-8"))
    except (OSError, ValueError):
        return
    if recorded.get("pid") != os.getpid():
        return
    try:
        path.unlink()
    except OSError:
        logger.debug("serverinfo cleanup failed for %s", path, exc_info=True)


def read_live_serverinfo(palace_path: str):
    """Return the hub record for this palace, or None.

    None when no record exists, the record is unreadable, or the recorded
    pid is no longer alive (crashed hub — stale file, ignore it).
    """
    path = serverinfo_path(palace_path)
    try:
        info = json.loads(read_published_bytes(path).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    if not _pid_alive(info.get("pid")):
        return None
    if not isinstance(info.get("port"), int) or not info.get("host"):
        return None
    return info


def client_base_url(info: dict) -> str:
    """Dial address for a local client, from a serverinfo record.

    A wildcard bind ("all interfaces") is dialed via loopback — the record
    is only ever read on the hub's own machine.
    """
    host = str(info.get("host", "")).strip()
    if host.lower() in _WILDCARD_HOSTS:
        host = "127.0.0.1"
    scheme = info.get("scheme") or "http"
    return f"{scheme}://{host}:{info['port']}"


def load_server_tokens(palace_path: str) -> tuple[str, ...]:
    """Return distinct local token candidates in safe retry order.

    The target palace's credential always goes first so a process token for a
    different palace is never sent unnecessarily.  A distinct process token
    is retained as a second candidate for a Hub restarted with ``--token``
    while an older generated palace credential remains on disk.
    """
    palace_token = _read_server_token_file(palace_path)
    process_token = env_str(_TOKEN_ENV, "")
    candidates = []
    for token in (palace_token, process_token):
        if token and token not in candidates:
            candidates.append(token)
    return tuple(candidates)
