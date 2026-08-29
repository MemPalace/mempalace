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
read_only}`` next to the per-palace bearer token
(``~/.mempalace/server/<key>/``), and callers use
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


def _canonical(palace_path: str) -> str:
    return os.path.abspath(os.path.realpath(os.path.expanduser(palace_path)))


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


def write_serverinfo(palace_path: str, *, host: str, port: int, scheme: str, read_only: bool):
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
        "palace_path": _canonical(palace_path),
    }
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
        fh.write("\n")
    return path


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
        # OpenProcess alone can return a handle for a process that has exited
        # but not fully cleaned up; GetExitCodeProcess == STILL_ACTIVE is the
        # canonical "still running" probe (mirrors hooks_cli._pid_alive, which
        # must never poison its mine child with os.kill -> TerminateProcess).
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
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


def reap_stale_serverinfo(palace_path=None, min_age_seconds: int = 0) -> int:
    """Remove serverinfo records whose recorded hub PID is provably dead.

    ``mempalace serve`` writes a record per palace under
    ``~/.mempalace/server/<key>/serverinfo.json`` and deletes it via
    :func:`clear_serverinfo` on clean exit (atexit). A hub that is killed,
    crashes, or loses its machine never runs that cleanup, so stale records
    with a dead PID accumulate; every reader already ignores them via
    :func:`read_live_serverinfo`, but the files remain.

    Sweeps all palace state directories by default, or just the one for
    ``palace_path`` when given. Only records whose ``pid`` is a positive
    int that :func:`_pid_alive` reports as dead are removed — a live hub's
    record is never touched, no matter how old, and records with a
    missing/non-int/<=0 pid or unparseable JSON are conservatively left in
    place (mirroring the reader's own tolerance). Removal is a plain unlink
    of a dead record: the reaper never holds a lock, never writes, and
    cannot race a live hub the way a lock reaper must. ``min_age_seconds``
    is an optional courtesy throttle for callers that want one; the
    liveness check, not age, is the safety gate, so the default of 0 is
    always safe.

    Returns the number of records removed.
    """
    server_root = Path.home() / ".mempalace" / "server"
    if palace_path is not None:
        candidates = [server_state_dir(palace_path)]
    else:
        try:
            candidates = [server_root / entry for entry in os.listdir(str(server_root))]
        except OSError:
            return 0
    removed = 0
    for state_dir in candidates:
        path = state_dir / "serverinfo.json"
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict):
            continue
        pid = info.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        if min_age_seconds:
            try:
                if time.time() - path.stat().st_mtime < min_age_seconds:
                    continue
            except OSError:
                continue
        if _pid_alive(pid):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            logger.debug("serverinfo reap failed for %s", path, exc_info=True)
    return removed


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


def load_server_token(palace_path: str) -> str:
    """The palace's hub bearer token, or "" when none was ever generated."""
    try:
        return server_token_path(palace_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
