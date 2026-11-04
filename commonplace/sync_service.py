"""Render and manage opt-in user services without reading credentials.

Rendered paths are relative to the supplied home. Service-manager commands run
only when activation is requested, through the caller's injectable runner.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

_SYSTEMD = Path(".config/systemd/user/commonplace-sync.service")
_LAUNCHD = Path("Library/LaunchAgents/dev.commonplace.sync.plist")
_LABEL = "dev.commonplace.sync"


def _kind(kind):
    if kind == "auto":
        kind = {"linux": "systemd", "darwin": "launchd"}.get(sys.platform)
    if kind not in {"systemd", "launchd"}:
        raise ValueError("Choose systemd or launchd for the user service")
    return kind


def _absolute(value):
    text = os.fspath(value)
    if not Path(text).is_absolute() or any(c in text for c in "\r\n\x00"):
        raise ValueError("Service paths must be absolute and contain no line breaks")
    return text


def _systemd_argument(text, *, executable=False):
    text = text.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    if executable:
        text = text.replace("$", "$$")
    quote = executable or any(c.isspace() or c in '\\"' for c in text)
    return '"' + text + '"' if quote else text


def render(kind, *, python, env_file):
    """Return a home-relative service path and its UTF-8 text; write nothing."""
    kind = _kind(kind)
    python = _absolute(python)
    env_file = _absolute(env_file)
    if kind == "systemd":
        return _SYSTEMD, (
            "[Unit]\n"
            "Description=Commonplace live sync\n\n"
            "[Service]\n"
            f"EnvironmentFile={_systemd_argument(env_file)}\n"
            f"ExecStart={_systemd_argument(python, executable=True)} -m commonplace sync run\n"
            "Restart=on-failure\n"
            "UMask=0077\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
    # Paths are positional arguments: neither the env-file nor Python path is
    # interpolated into shell code. The file uses trusted shell KEY=value lines.
    document = {
        "Label": _LABEL,
        "ProgramArguments": [
            "/bin/sh",
            "-c",
            'set -a; . "$1" || exit; set +a; shift; exec "$@"',
            "commonplace-sync",
            env_file,
            python,
            "-m",
            "commonplace",
            "sync",
            "run",
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "Umask": 0o077,
    }
    return _LAUNCHD, plistlib.dumps(document, sort_keys=False).decode("utf-8")


def install(kind, *, home, env_file, activate, runner=subprocess.run):
    """Write a service file; optionally activate it using the injected runner."""
    kind = _kind(kind)
    relative, text = render(kind, python=sys.executable, env_file=env_file)
    path = Path(home) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    if activate:
        if kind == "systemd":
            runner(["systemctl", "--user", "daemon-reload"], check=True)
            runner(["systemctl", "--user", "enable", "--now", path.name], check=True)
        else:
            runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=True)
    return path


def uninstall(kind=None, *, home, activate, runner=subprocess.run):
    """Remove present services (both kinds by default), optionally stopping them."""
    kinds = ("systemd", "launchd") if kind is None else (_kind(kind),)
    for service_kind in kinds:
        relative = _SYSTEMD if service_kind == "systemd" else _LAUNCHD
        path = Path(home) / relative
        if not path.exists():
            continue
        if activate:
            if service_kind == "systemd":
                runner(["systemctl", "--user", "disable", "--now", path.name], check=True)
            else:
                runner(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=True)
        path.unlink()
        if activate and service_kind == "systemd":
            runner(["systemctl", "--user", "daemon-reload"], check=True)
