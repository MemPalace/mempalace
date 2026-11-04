"""Explicit opt-in CLI for local synchronization; never opens a server database."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .capture_client import CaptureError, endpoint
from .sync_protocol import BACKFILL_WRITES, CURSOR_MESSAGE, TOKEN_ENV


def register(sub):
    parser = sub.add_parser("sync", help="opt in to local transcript and memory sync")
    parser.add_argument("--home", type=Path)
    commands = parser.add_subparsers(dest="sync_command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--source", action="append", default=[])
    install.add_argument("--memory", action="append", default=[])
    install.add_argument("--hub")
    install.add_argument("--env-file", type=Path)
    install.add_argument("--enable", action="store_true")
    install.add_argument("--no-service", action="store_true")
    install.add_argument("--no-activate", action="store_true")
    install.add_argument("--service", choices=("auto", "systemd", "launchd"), default="auto")
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--hub")
    commands.add_parser("status")
    probe = commands.add_parser("probe")
    probe.add_argument("file", type=Path)
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--enable", action="store_true")
    uninstall.add_argument("--no-activate", action="store_true")
    for command in commands.choices.values():
        command.add_argument("--home", type=Path, default=argparse.SUPPRESS)
        command.set_defaults(func=main)


def _check(path, mode, *, directory=False):
    info = path.lstat()
    if (
        path.is_symlink()
        or stat.S_IMODE(info.st_mode) != mode
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
    ):
        raise ValueError("Sync paths need private ownership and permissions")


def _read(path):
    _check(path, 0o600)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        return stream.read()


def _environment(path):
    values = {
        key: os.environ[key]
        for key in (*TOKEN_ENV.values(), "COMMONPLACE_URL")
        if key in os.environ
    }
    if path is not None:
        for line in _read(path).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.removeprefix("export ").partition("=")
            if not sep or key.strip() not in (*TOKEN_ENV.values(), "COMMONPLACE_URL"):
                continue
            fields = shlex.split(value, comments=True)
            if len(fields) != 1:
                raise ValueError("Invalid sync environment file")
            values[key.strip()] = fields[0]
    return values


def _selections(args, home):
    sources, memories = [], []
    for value in args.source or ([] if args.memory else ["claude-code", "codex"]):
        source, _, root = value.partition(":")
        if source == "cursor":
            return None, None
        if source not in ("claude-code", "codex", "t3") or (source == "t3" and not root):
            raise ValueError("Select a native transcript source")
        default = home / (".claude/projects" if source == "claude-code" else ".codex/sessions")
        path = Path(root).expanduser().absolute() if root else default
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Symlinked transcript directory refused")
        regular = path.is_dir()
        if root and not regular:
            raise ValueError("Select a regular transcript directory")
        if not args.source and not regular:
            continue
        sources.append({"source": source, "root": str(path)})
    for value in args.memory:
        label, sep, selection = value.partition("=")
        if not sep or not selection:
            raise ValueError("Memory selection needs NAME=ROOT[:GLOB]")
        root, sep, glob = selection.partition(":")
        from .sync_protocol import memory_thread_id

        memory_thread_id(label, "file.md", "0" * 64)
        path = Path(root).expanduser().absolute()
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("Symlinked memory directory refused")
        memories.append(
            {
                "label": label,
                "root": str(path),
                "glob": glob if sep else "**/*.md",
            }
        )
    if not sources and not memories:
        raise ValueError("Select at least one source")
    return sources, memories


def _save(directory, config):
    parent = directory.parent
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("Symlinked sync directory refused")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    _check(directory, 0o700, directory=True)
    path = directory / "config.json"
    if path.exists() or path.is_symlink():
        _check(path, 0o600)
    descriptor, temp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _install(args, home, directory):
    sources, memories = _selections(args, home)
    if sources is None:
        print(CURSOR_MESSAGE)
        return 1
    values = _environment(args.env_file)
    hub = args.hub or values.get("COMMONPLACE_URL") or "https://app.cmmn.dev"
    endpoint(hub)
    if not args.enable:
        print("Sync is disabled. Selected sources:")
        print(json.dumps({"sources": sources, "memories": memories}))
        command = [sys.executable, "-m", "commonplace", "sync", "install", "--enable"]
        for source in sources:
            command += ["--source", source["source"] + ":" + source["root"]]
        for memory in memories:
            command += ["--memory", memory["label"] + "=" + memory["root"] + ":" + memory["glob"]]
        command += ["--hub", hub, "--home", str(home)]
        if args.env_file:
            command += ["--env-file", str(args.env_file.absolute())]
        if args.no_service:
            command += ["--no-service"]
        if args.no_activate:
            command += ["--no-activate"]
        command += ["--service", args.service]
        print("Enable explicitly: " + shlex.join(command))
        return 0
    if not args.no_service and args.env_file is None:
        raise ValueError("Service installation needs a private environment file")
    config = {
        "version": 1,
        "home": str(home),
        "hub": hub,
        "env_file": str(args.env_file.absolute()) if args.env_file else None,
        "sources": sources,
        "memories": memories,
    }
    _save(directory, config)
    if not args.no_service:
        from .sync_service import install

        install(
            args.service,
            home=home,
            env_file=Path(config["env_file"]),
            activate=not args.no_activate,
        )
    print("Sync enabled. Run commonplace sync run --once to synchronize.")
    return 0


def _load(directory, home):
    _check(directory, 0o700, directory=True)
    config = json.loads(_read(directory / "config.json"))
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("Unsupported sync configuration")
    config["home"] = str(home)
    if not isinstance(config.get("sources"), list) or not isinstance(config.get("memories"), list):
        raise ValueError("Invalid sync sources")
    endpoint(config["hub"])
    return config


def _run(args, home, directory):
    from . import plans, sync_daemon

    config = _load(directory, home)

    from .sync_state import open_state

    if args.hub:
        endpoint(args.hub)
        config["hub"] = args.hub
    values = _environment(Path(config["env_file"]) if config.get("env_file") else None)
    config["tokens"] = {key: values[key] for key in TOKEN_ENV.values() if values.get(key)}
    with open_state(directory / "state.db") as state:
        return sync_daemon.run(
            config,
            state,
            clock=plans._now,
            sleep=time.sleep,
            transport=sync_daemon.request,
            once=args.once,
        )


def _status(directory, home):
    from .sync_daemon import _feed_key
    from .sync_state import open_state

    config = _load(directory, home)
    if not (directory / "state.db").exists():
        print("Sync configured; no local cursor yet.")
        return 0
    with open_state(directory / "state.db") as state:
        for name in TOKEN_ENV.values():
            row = state.feed_cursor(_feed_key(config["hub"], name))
            if row:
                print(f"Sync cursor: {row['seq']}")
        bkey = _feed_key(config["hub"], "backfill")
        used = state.next_due(bkey)
        if used > 0:
            mode = "done" if state.next_due(bkey + "-done") else "active"
            print(f"Backfill: {mode}, {int(used)} of {BACKFILL_WRITES} attempts")
        print(f"Pending operations: {len(state.outbox_pending())}")
        for row in state.statuses():
            print(f"Source status: {row['status']}")
    return 0


def main(args) -> int:
    """Dispatch without ever opening server state or displaying private errors."""
    home = Path(args.home or Path.home()).expanduser().absolute()
    directory = home / ".commonplace/sync"
    try:
        if args.sync_command == "install":
            return _install(args, home, directory)
        if args.sync_command == "uninstall":
            if not args.enable:
                print("Sync uninstall is inert; pass --enable to remove user services.")
                return 0
            from .sync_service import uninstall

            for kind in ("systemd", "launchd"):
                uninstall(kind, home=home, activate=not args.no_activate)
            return 0
        if args.sync_command == "probe":
            from .sync_sources import probe

            print(json.dumps(probe(args.file), sort_keys=True))
            return 0
        if args.sync_command == "status":
            return _status(directory, home)
        return _run(args, home, directory)
    except KeyboardInterrupt:
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        CaptureError,
        subprocess.CalledProcessError,
    ):
        print(
            "Sync failed; check configuration, credentials and private file permissions.",
            file=sys.stderr,
        )
        return 2
