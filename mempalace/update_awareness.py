"""Opt-in, cached release awareness without automatic installation."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from .version import __version__


_POLICY_FILE = "updates.json"
_CACHE_FILE = "updates-cache.json"
_STABLE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_CHECK_LOCK = threading.Lock()
_INSTALL_ACTIONS = {
    "uv-tool": ("uv", "tool", "upgrade", "mempalace"),
    "pipx": ("pipx", "upgrade", "mempalace"),
    "pip": None,
}


def _installer_action(installer: str) -> dict:
    argv = (
        [sys.executable, "-m", "pip", "install", "--upgrade", "mempalace"]
        if installer == "pip"
        else list(_INSTALL_ACTIONS[installer] or ())
    )
    return {"kind": "command", "argv": argv}


def _config_dir(value=None) -> Path:
    return Path(value) if value is not None else Path(os.path.expanduser("~/.mempalace"))


def _read_json(name: str, config_dir=None) -> dict:
    try:
        value = json.loads((_config_dir(config_dir) / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(name: str, value: dict, config_dir=None) -> None:
    directory = _config_dir(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".updates-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, directory / name)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def configure_updates(
    *, enabled: bool, interval_days: int = 7, installer: str | None = None, config_dir=None
) -> dict:
    """Persist explicit release-check consent and return the resulting policy."""
    if not isinstance(interval_days, int) or interval_days < 1:
        raise ValueError("interval_days must be a positive integer")
    existing = _read_json(_POLICY_FILE, config_dir)
    installer = installer or existing.get("installer")
    if enabled and installer not in _INSTALL_ACTIONS:
        raise ValueError("enabling checks requires --installer uv-tool, pipx, or pip")
    if installer is not None and installer not in _INSTALL_ACTIONS:
        raise ValueError("installer must be uv-tool, pipx, or pip")
    policy = {"enabled": bool(enabled), "interval_days": interval_days, "channel": "stable"}
    if installer:
        policy["installer"] = installer
    _write_json(_POLICY_FILE, policy, config_dir)
    return policy


def fetch_latest_stable() -> str:
    """Return the latest stable version advertised by the official PyPI project."""
    request = Request(
        "https://pypi.org/pypi/mempalace/json",
        headers={"Accept": "application/json", "User-Agent": "mempalace-update-check"},
    )
    with urlopen(request, timeout=2.0) as response:  # noqa: S310 -- fixed HTTPS origin
        payload = json.load(response)
    version = payload.get("info", {}).get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not _STABLE_VERSION.fullmatch(version):
        raise ValueError("PyPI returned an invalid stable MemPalace version")
    return version


def _version_key(version: str) -> tuple[int, int, int]:
    if not _STABLE_VERSION.fullmatch(version):
        raise ValueError(f"invalid stable version: {version!r}")
    return tuple(int(part) for part in version.split("."))


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _result(state: dict, installed: str, *, checked: bool) -> dict:
    latest = state.get("latest")
    result = {
        "enabled": bool(state.get("enabled", False)),
        "installed": installed,
    }
    if (
        isinstance(latest, str)
        and _STABLE_VERSION.fullmatch(latest)
        and isinstance(state.get("checked_at"), str)
    ):
        result.update(
            {
                "latest": latest,
                "available": _version_key(latest) > _version_key(installed),
                "checked": checked,
                "checked_at": state["checked_at"],
                "restart_required_after_upgrade": True,
            }
        )
    else:
        result["checked"] = checked
    return result


def check_updates(
    *,
    force: bool = False,
    config_dir=None,
    installed_version: str = __version__,
    fetch_latest=fetch_latest_stable,
    now: datetime | None = None,
) -> dict:
    """Check or read cached stable-release state without ever installing it."""
    policy = _read_json(_POLICY_FILE, config_dir)
    cache = _read_json(_CACHE_FILE, config_dir)
    enabled = bool(policy.get("enabled", False))
    if not enabled and not force:
        return {"enabled": False, "installed": installed_version, "checked": False}

    now = now or datetime.now(timezone.utc)
    attempted_at = cache.get("attempted_at") or cache.get("checked_at")
    if not force and attempted_at:
        try:
            last = datetime.fromisoformat(attempted_at.replace("Z", "+00:00"))
            if last.utcoffset() is None:
                raise ValueError("cached update timestamp has no timezone")
            interval = int(policy.get("interval_days", 7))
        except (AttributeError, TypeError, ValueError):
            pass
        else:
            if interval > 0 and now - last < timedelta(days=interval):
                return _result({**policy, **cache}, installed_version, checked=False)

    attempted_at = _utc_text(now)
    cache = {**cache, "attempted_at": attempted_at}
    _write_json(_CACHE_FILE, cache, config_dir)
    cache.update({"latest": fetch_latest(), "checked_at": attempted_at})
    _write_json(_CACHE_FILE, cache, config_dir)
    return _result({**policy, **cache}, installed_version, checked=True)


def cached_update_status(*, config_dir=None, installed_version: str = __version__) -> dict:
    """Return agent-safe cached state without performing network access."""
    policy = _read_json(_POLICY_FILE, config_dir)
    if not policy.get("enabled"):
        return {"enabled": False, "installed": installed_version}
    cache = _read_json(_CACHE_FILE, config_dir)
    return _result({**policy, **cache}, installed_version, checked=False)


def prepare_upgrade(
    *, config_dir=None, installed_version: str = __version__, installer: str | None = None
) -> dict:
    """Describe an upgrade without executing or authorizing it."""
    policy = _read_json(_POLICY_FILE, config_dir)
    cache = _read_json(_CACHE_FILE, config_dir)
    status = _result({**policy, **cache}, installed_version, checked=False)
    if not status.get("available"):
        return {"available": False, "installed": installed_version, "actions": []}
    installer = installer or policy.get("installer")
    if installer not in _INSTALL_ACTIONS:
        return {
            "available": True,
            "installed": installed_version,
            "latest": status["latest"],
            "requires_installer_selection": True,
            "installers": {name: _installer_action(name) for name in _INSTALL_ACTIONS},
            "actions": [],
        }
    return {
        "available": True,
        "installed": installed_version,
        "latest": status["latest"],
        "requires_user_authorization": True,
        "actions": [
            _installer_action(installer),
            {
                "kind": "command",
                "argv": [
                    "npx",
                    "skills",
                    "update",
                    "mempalace",
                    "mempalace-recall",
                    "mempalace-task",
                ],
            },
            {
                "kind": "instruction",
                "text": "restart the MemPalace MCP server or shared hub",
            },
            {"kind": "instruction", "text": "refresh agent MCP tool lists"},
        ],
    }


def schedule_update_check(
    *, config_dir=None, fetch_latest=fetch_latest_stable, now: datetime | None = None
) -> bool:
    """Start one non-blocking due check when the user opted in."""
    policy = _read_json(_POLICY_FILE, config_dir)
    if not policy.get("enabled") or not _CHECK_LOCK.acquire(blocking=False):
        return False

    def run() -> None:
        try:
            check_updates(config_dir=config_dir, fetch_latest=fetch_latest, now=now)
        except (OSError, ValueError):
            return
        finally:
            _CHECK_LOCK.release()

    threading.Thread(target=run, name="mempalace-update-check", daemon=True).start()
    return True
