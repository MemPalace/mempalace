"""Per-palace discovery registry for the MemPalace HTTP MCP hub.

``mempalace serve`` (and ``mempalace-mcp --transport http``) is meant to be
the single long-lived writer for a shared palace: once it performs its first
mutating tool call it holds the per-palace MCP writer lease (#1818) for its
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

import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Bind-address wildcards: a hub bound to "all interfaces" is dialed via
# loopback by local forwarders.
_WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]"}
_TOKEN_ENV = "MEMPALACE_MCP_HTTP_TOKEN"


def _canonical(palace_path: str) -> str:
    return os.path.abspath(os.path.realpath(os.path.expanduser(palace_path)))


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def server_state_dir(palace_path: str) -> Path:
    """Per-palace state directory shared by the token and the serverinfo.

    Keyed by the canonical palace path so one hub per palace reuses a stable
    directory across restarts. Must stay in sync with the token location used
    by ``mempalace serve`` (see ``cli._server_token_path``, which delegates
    here).
    """
    key = hashlib.sha256(os.path.normcase(_canonical(palace_path)).encode("utf-8")).hexdigest()[:24]
    return Path.home() / ".mempalace" / "server" / key


def server_token_path(palace_path: str) -> Path:
    return server_state_dir(palace_path) / "token"


def serverinfo_path(palace_path: str) -> Path:
    return server_state_dir(palace_path) / "serverinfo.json"


def _read_server_token_file(palace_path: str) -> str:
    try:
        return server_token_path(palace_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
        "palace_path": _canonical(palace_path),
    }
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
        fh.write("\n")
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
    # Write-and-rename: readers in other processes must never observe a
    # half-serialized estate, and this file is rewritten every sync round.
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.write("\n")
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
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
        state = json.loads(mesh_state_path(palace_path).read_text(encoding="utf-8"))
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
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if recorded.get("pid") != os.getpid():
        return
    try:
        path.unlink()
    except OSError:
        logger.debug("serverinfo cleanup failed for %s", path, exc_info=True)


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI only
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_live_serverinfo(palace_path: str):
    """Return the hub record for this palace, or None.

    None when no record exists, the record is unreadable, or the recorded
    pid is no longer alive (crashed hub — stale file, ignore it).
    """
    path = serverinfo_path(palace_path)
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
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
    process_token = os.environ.get(_TOKEN_ENV, "").strip()
    candidates = []
    for token in (palace_token, process_token):
        if token and token not in candidates:
            candidates.append(token)
    return tuple(candidates)


def load_server_token(palace_path: str) -> str:
    """Return the preferred Hub bearer token, or "" when none is configured."""
    candidates = load_server_tokens(palace_path)
    return candidates[0] if candidates else ""


def urlopen_with_server_tokens(
    palace_path: str,
    url: str,
    *,
    data=None,
    headers=None,
    timeout=None,
):
    """Open one Hub request, retrying only a pre-acceptance HTTP 401.

    At most two distinct local credentials exist.  A 401 means the Hub's
    authentication gate rejected the request before dispatch, so trying the
    second credential is safe even for mutating JSON-RPC calls.  Every other
    HTTP or transport failure is surfaced immediately and is never replayed.
    """
    import urllib.error
    import urllib.request

    candidates = load_server_tokens(palace_path) or ("",)
    for index, token in enumerate(candidates):
        attempt_headers = dict(headers or {})
        if token:
            attempt_headers["Authorization"] = f"Bearer {token}"
        else:
            attempt_headers.pop("Authorization", None)
        request = urllib.request.Request(url, data=data, headers=attempt_headers)
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or index + 1 >= len(candidates):
                raise
            exc.close()
