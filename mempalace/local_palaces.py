"""Named, workspace-relative palaces that bind storage and source selection."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import fsio, paths
from ._proc import safe_child_env, safe_module_argv

_MODES = frozenset({"projects", "convos", "extract", "media"})
_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}
_RUN_COMMANDS = frozenset(
    {
        "audit",
        "export",
        "hallways",
        "kg",
        "mine",
        "repair-status",
        "rooms",
        "search",
        "status",
        "sync",
        "tunnels",
        "wake-up",
        "wings",
    }
)


def _validate_name(name: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name) or name in _RESERVED_NAMES:
        raise ValueError(
            "palace name must be 1–64 lowercase letters, digits, hyphens or underscores"
        )


def _contained(path: Path, owner: Path) -> Path:
    """Reject symlink and junction redirections for managed state."""
    if path.resolve() != path or not path.is_relative_to(owner):
        raise ValueError(f"local palace state must not be redirected: {path}")
    return path


def _source_relative(raw: str) -> Path:
    candidate = Path(raw)
    if not raw.strip() or candidate.anchor or ".." in candidate.parts or ":" in raw:
        raise ValueError(f"source must be a workspace-relative path without '..': {raw!r}")
    return candidate


@dataclass(frozen=True)
class LocalPalace:
    workspace: Path
    name: str
    sources: dict[str, list[str]]

    @property
    def root(self) -> Path:
        return self.workspace / paths.DIRNAME / "palaces" / self.name

    @property
    def palace(self) -> Path:
        return self.root / "palace"

    @classmethod
    def load(cls, workspace: Path, name: str) -> LocalPalace:
        _validate_name(name)
        base = workspace / paths.DIRNAME / "palaces"
        root = _contained(base / name, workspace)
        scope = _contained(root / "scope.json", root)
        try:
            data = json.loads(scope.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot load local palace {name!r}: {scope}: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError(f"invalid or unsupported local palace scope: {scope}")
        profile = cls(workspace, name, cls._validate_sources(base, data.get("sources")))
        for directory in ("config", "palace", "state"):
            if not _contained(root / directory, root).is_dir():
                raise ValueError(f"missing local palace directory: {root / directory}")
        return profile

    @classmethod
    def create(cls, workspace: Path, name: str, selectors: list[str]) -> LocalPalace:
        _validate_name(name)
        base = _contained(workspace / paths.DIRNAME / "palaces", workspace)
        sources: dict[str, list[str]] = {}
        for selector in selectors:
            mode, _, raw = selector.partition("=")
            path = _source_relative(raw)
            # Canonical paths are recorded relative to the workspace, so moving
            # the project does not invalidate the scope declaration itself.
            actual = (workspace / path).resolve(strict=True)
            if not actual.is_relative_to(workspace):
                raise ValueError(f"source escapes the workspace: {raw!r}")
            if not actual.is_dir() and not (mode in {"convos", "media"} and actual.is_file()):
                raise ValueError(f"source is not a directory or supported single file: {raw!r}")
            sources.setdefault(mode, []).append(actual.relative_to(workspace).as_posix())
        profile = cls(workspace, name, cls._validate_sources(base, sources))
        base.mkdir(parents=True, exist_ok=True)
        # Protect future stores without editing the project's existing ignore rules.
        try:
            with (base / ".gitignore").open("x", encoding="utf-8") as handle:
                handle.write("*\n!.gitignore\n")
        except FileExistsError:
            pass
        try:
            profile.root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ValueError(f"local palace already exists: {profile.root}") from exc
        for directory in ("config", "palace", "state"):
            (profile.root / directory).mkdir()
        # A reader sees the scope only after all three roots exist.
        fsio.atomic_write_json(
            profile.root / "scope.json", {"version": 1, "sources": profile.sources}
        )
        return profile

    @staticmethod
    def _validate_sources(base: Path, data: object) -> dict[str, list[str]]:
        if not isinstance(data, dict) or not data:
            raise ValueError("a local palace needs at least one MODE=PATH source")
        sources = {}
        for mode, values in data.items():
            if mode not in _MODES:
                raise ValueError(
                    f"unknown local source mode {mode!r}; use {', '.join(sorted(_MODES))}"
                )
            if not isinstance(values, list) or not values:
                raise ValueError(f"local palace mode {mode!r} needs a list of source paths")
            roots = []
            for raw in values:
                if not isinstance(raw, str):
                    raise ValueError(f"local palace mode {mode!r} has a non-string source")
                relative = _source_relative(raw)
                if (base.parent.parent / relative).is_relative_to(base):
                    raise ValueError(f"local palace state cannot be indexed: {raw!r}")
                roots.append(relative.as_posix())
            sources[mode] = list(dict.fromkeys(roots))
        return sources

    def environment(self) -> dict[str, str]:
        # Do not inherit a global database, remote embedding provider, hub,
        # daemon registry, or other MemPalace overrides from a shell or harness.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("MEMPALACE_", "MEMPAL_")) and key.upper() != "PYTHONPATH"
        }
        env[paths.LOCAL_ROOT_ENV] = str(self.root)
        env["MEMPALACE_BACKEND_EXPLICIT"] = "chroma"
        env["MEMPALACE_HUB_FORWARD"] = "0"
        return safe_child_env(env)


def enforce_source(source: str, palace_path: str, *modes: str) -> str:
    """Resolve a selected local source before any file scanning or DB writes."""
    root = paths.local_root()
    if root is None:
        return source
    profile = LocalPalace.load(root.parents[2], root.name)
    if not paths.same_path(palace_path, str(profile.palace)):
        raise ValueError("cannot redirect the database of a named local palace")
    try:
        candidate = Path(source).expanduser().resolve(strict=True)
        if not candidate.is_relative_to(profile.root.parent):
            for mode in modes:
                for relative in profile.sources.get(mode, []):
                    allowed = profile.workspace / relative
                    # Do not follow an allowed root that was replaced by a link.
                    if candidate.is_relative_to(allowed) and allowed.resolve() == allowed:
                        return str(candidate)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError(f"cannot resolve local palace source {source!r}: {exc}") from exc
    mode_names = "/".join(modes)
    raise ValueError(
        f"{source!r} is outside the local palace scope for {profile.name!r} ({mode_names})"
    )


def add_local_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("local", help="Create and use named workspace palaces")
    local = parser.add_subparsers(dest="local_action", required=True)
    create = local.add_parser("create", help="Register a named palace and its allowed sources")
    listing = local.add_parser("list", help="List the workspace's named palaces")
    run = local.add_parser("run", help="Run a MemPalace command inside a named palace")
    mcp = local.add_parser("mcp", help="Serve a named palace over stdio MCP")
    for command in (create, listing, run, mcp):
        command.add_argument("--workspace", type=Path, default=Path.cwd(), help="Workspace root")
    for command in (create, run, mcp):
        command.add_argument("name", help="Name of the local palace")
    create.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="MODE=PATH",
        help="Workspace-relative source (projects, convos, extract or media); repeatable",
    )
    listing.add_argument("--json", action="store_true", help="Output the scopes as JSON")
    mcp.add_argument("--read-only", action="store_true", help="Disable write tools")
    mcp.add_argument("--light", action="store_true", help="Serve the lightweight tool surface")
    run.epilog = "Example: mempalace local run handbook -- search 'design notes'"
    return parser


def parse_local_args(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    """Keep the launcher separator distinct from options to the child command."""
    command = []
    if argv[:2] == ["local", "run"] and "--" in argv:
        separator = argv.index("--")
        command = argv[separator + 1 :]
        argv = argv[:separator]
    args = parser.parse_args(argv)
    local = paths.local_root()
    override = args.palace is not None or getattr(args, "backend", None) or args.global_backend
    if local is not None and override:
        parser.error("--palace and --backend cannot override a named local palace")
    if args.command == "local":
        if override or local is not None:
            parser.error("select only one palace")
        try:
            code = run_local(args, command)
        except (ValueError, OSError, RuntimeError) as exc:
            parser.error(str(exc))
        parser.exit(code)
    return args


def run_local(args, command: list[str]) -> int:
    workspace = args.workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")
    action = args.local_action
    if action == "create":
        profile = LocalPalace.create(workspace, args.name, args.source)
        print(f"Created {profile.name}: {profile.palace}")
        return 0
    if action == "list":
        base = _contained(workspace / paths.DIRNAME / "palaces", workspace)
        available = sorted(p for p in base.iterdir() if p.is_dir()) if base.exists() else []
        profiles = [LocalPalace.load(workspace, entry.name) for entry in available]
        if args.json:
            print(json.dumps([{"name": p.name, "sources": p.sources} for p in profiles]))
        else:
            for profile in profiles:
                print(f"{profile.name}\t{json.dumps(profile.sources)}")
        return 0

    profile = LocalPalace.load(workspace, args.name)
    if action == "run":
        if not command or command[0] not in _RUN_COMMANDS:
            raise ValueError(
                f"use '-- COMMAND'; supported commands: {', '.join(sorted(_RUN_COMMANDS))}"
            )
        module = "mempalace"
    else:
        module = "mempalace.mcp_light_server" if args.light else "mempalace.mcp_server"
        command = ["--read-only"] if args.read_only else []
    code = subprocess.call(
        safe_module_argv(module, *command), env=profile.environment(), cwd=workspace
    )
    return code if code >= 0 else 128 - code
