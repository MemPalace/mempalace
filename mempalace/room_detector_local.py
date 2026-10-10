#!/usr/bin/env python3
"""
room_detector_local.py — Local setup, no API required.

Two ways to define rooms without calling any AI:
  1. Auto-detect from folder structure (zero config)
  2. Define manually in mempalace.yaml

No internet. No API key. Your files stay on your machine.
"""

import logging
import os
import sys
import yaml
from pathlib import Path
from collections import defaultdict

from .project_config import find_project_config

logger = logging.getLogger(__name__)

# Common room patterns — detected from folder names and filenames
# Format: {folder_keyword: room_name}
FOLDER_ROOM_MAP = {
    "frontend": "frontend",
    "front-end": "frontend",
    "front_end": "frontend",
    "client": "frontend",
    "ui": "frontend",
    "views": "frontend",
    "components": "frontend",
    "pages": "frontend",
    "backend": "backend",
    "back-end": "backend",
    "back_end": "backend",
    "server": "backend",
    "api": "backend",
    "routes": "backend",
    "services": "backend",
    "controllers": "backend",
    "models": "backend",
    "database": "backend",
    "db": "backend",
    "docs": "documentation",
    "doc": "documentation",
    "documentation": "documentation",
    "wiki": "documentation",
    "readme": "documentation",
    "notes": "documentation",
    "design": "design",
    "designs": "design",
    "mockups": "design",
    "wireframes": "design",
    "assets": "design",
    "storyboard": "design",
    "costs": "costs",
    "cost": "costs",
    "budget": "costs",
    "finance": "costs",
    "financial": "costs",
    "pricing": "costs",
    "invoices": "costs",
    "accounting": "costs",
    "meetings": "meetings",
    "meeting": "meetings",
    "calls": "meetings",
    "meeting_notes": "meetings",
    "standup": "meetings",
    "minutes": "meetings",
    "team": "team",
    "staff": "team",
    "hr": "team",
    "hiring": "team",
    "employees": "team",
    "people": "team",
    "research": "research",
    "references": "research",
    "reading": "research",
    "papers": "research",
    "planning": "planning",
    "roadmap": "planning",
    "strategy": "planning",
    "specs": "planning",
    "requirements": "planning",
    "tests": "testing",
    "test": "testing",
    "testing": "testing",
    "qa": "testing",
    "scripts": "scripts",
    "tools": "scripts",
    "utils": "scripts",
    "config": "configuration",
    "configs": "configuration",
    "settings": "configuration",
    "infrastructure": "configuration",
    "infra": "configuration",
    "deploy": "configuration",
}


def detect_rooms_from_folders(project_dir: str) -> list:
    """
    Walk the project folder structure.
    Find top-level subdirectories that match known room patterns.
    Returns list of room dicts.
    """
    project_path = Path(project_dir).expanduser().resolve()
    found_rooms = {}

    SKIP_DIRS = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        ".next",
        "coverage",
    }

    # Check top-level directories first (most reliable signal)
    for item in project_path.iterdir():
        try:
            is_dir = item.is_dir()  # WinError 448 — reparse point / untrusted mount point
        except OSError as exc:
            logger.debug("Skipping %s: %s", item, exc)
            continue
        if is_dir and item.name not in SKIP_DIRS:
            name_lower = item.name.lower().replace("-", "_")
            if name_lower in FOLDER_ROOM_MAP:
                room_name = FOLDER_ROOM_MAP[name_lower]
                if room_name not in found_rooms:
                    found_rooms[room_name] = item.name
            # Also check if folder name IS a good room name directly
            elif len(item.name) > 2 and item.name[0].isalpha():
                clean = item.name.lower().replace("-", "_").replace(" ", "_")
                if clean not in found_rooms:
                    found_rooms[clean] = item.name

    # Walk one level deeper for nested patterns
    for item in project_path.iterdir():
        try:
            item_is_dir = item.is_dir()  # WinError 448 — reparse point / untrusted mount point
        except OSError as exc:
            logger.debug("Skipping %s: %s", item, exc)
            continue
        if item_is_dir and item.name not in SKIP_DIRS:
            try:
                subitems = list(
                    item.iterdir()
                )  # WinError 448 — iterdir can also fail on some reparse points
            except OSError as exc:
                logger.debug("Skipping contents of %s: %s", item, exc)
                continue
            for subitem in subitems:
                try:
                    subitem_is_dir = (
                        subitem.is_dir()
                    )  # WinError 448 — reparse point / untrusted mount point
                except OSError as exc:
                    logger.debug("Skipping %s: %s", subitem, exc)
                    continue
                if subitem_is_dir and subitem.name not in SKIP_DIRS:
                    name_lower = subitem.name.lower().replace("-", "_")
                    if name_lower in FOLDER_ROOM_MAP:
                        room_name = FOLDER_ROOM_MAP[name_lower]
                        if room_name not in found_rooms:
                            found_rooms[room_name] = subitem.name

    # Build room list
    rooms = []
    for room_name, original in found_rooms.items():
        rooms.append(
            {
                "name": room_name,
                "description": f"Files from {original}/",
                "keywords": [room_name, original.lower()],
            }
        )

    # Always add "general" as fallback
    if not any(r["name"] == "general" for r in rooms):
        rooms.append(
            {
                "name": "general",
                "description": "Files that don't fit other rooms",
                "keywords": [],
            }
        )

    return rooms


def detect_rooms_from_files(project_dir: str) -> list:
    """
    Fallback: if folder structure gives no signal,
    detect rooms from recurring filename patterns.
    """
    project_path = Path(project_dir).expanduser().resolve()
    keyword_counts = defaultdict(int)

    SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

    for _root, dirs, filenames in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in filenames:
            name_lower = filename.lower().replace("-", "_").replace(" ", "_")
            for keyword, room in FOLDER_ROOM_MAP.items():
                if keyword in name_lower:
                    keyword_counts[room] += 1

    # Return rooms that appear more than twice
    rooms = []
    for room, count in sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True):
        if count >= 2:
            rooms.append(
                {
                    "name": room,
                    "description": f"Files related to {room}",
                    "keywords": [room],
                }
            )
        if len(rooms) >= 6:
            break

    if not rooms:
        rooms = [{"name": "general", "description": "All project files", "keywords": []}]

    return rooms


def print_proposed_structure(project_name: str, rooms: list, total_files: int, source: str):
    print(f"\n{'=' * 55}")
    print("  MemPalace Init -- Local setup")
    print(f"{'=' * 55}")
    print(f"\n  WING: {project_name}")
    print(f"  ({total_files} files found, rooms detected from {source})\n")
    for room in rooms:
        print(f"    ROOM: {room['name']}")
        print(f"          {room['description']}")
    print(f"\n{'-' * 55}")


def get_user_approval(rooms: list) -> list:
    """Same approval flow as AI version."""
    print("  Review the proposed rooms above.")
    print("  Options:")
    print("    [enter]  Accept all rooms")
    print("    [edit]   Remove or rename rooms")
    print("    [add]    Add a room manually")
    print()

    choice = input("  Your choice [enter/edit/add]: ").strip().lower()

    if choice in ("", "y", "yes"):
        return rooms

    if choice == "edit":
        print("\n  Current rooms:")
        for i, room in enumerate(rooms):
            print(f"    {i + 1}. {room['name']} -- {room['description']}")
        remove = input("\n  Room numbers to REMOVE (comma-separated, or enter to skip): ").strip()
        if remove:
            to_remove = {int(x.strip()) - 1 for x in remove.split(",") if x.strip().isdigit()}
            rooms = [r for i, r in enumerate(rooms) if i not in to_remove]

    if choice == "add" or input("\n  Add any missing rooms? [y/N]: ").strip().lower() == "y":
        while True:
            new_name = (
                input("  New room name (or enter to stop): ").strip().lower().replace(" ", "_")
            )
            if not new_name:
                break
            new_desc = input(f"  Description for '{new_name}': ").strip()
            rooms.append({"name": new_name, "description": new_desc, "keywords": [new_name]})
            print(f"  Added: {new_name}")

    return rooms


def _write_backup(path: Path, data: bytes) -> Path:
    """Write ``data`` to a new backup next to ``path`` and return its path.

    Tries ``<name>.bak``, then ``<name>.bak.<timestamp>``, then numbered
    variants of that. Each candidate is created with ``O_EXCL``, so an
    existing backup is never overwritten, even by a concurrent init.
    """
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidates = [path.with_name(f"{path.name}.bak"), path.with_name(f"{path.name}.bak.{stamp}")]
    candidates += [path.with_name(f"{path.name}.bak.{stamp}-{n}") for n in range(1, 100)]
    for candidate in candidates:
        try:
            fd = os.open(
                candidate,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                os.unlink(candidate)
            except OSError:
                pass
            raise
        return candidate
    raise OSError(f"no free backup name next to {path}")


def save_config(project_dir: str, project_name: str, rooms: list):
    config = {
        "wing": project_name,
        "rooms": [
            {
                "name": r["name"],
                "description": r["description"],
                "keywords": r.get("keywords", [r["name"]]),
            }
            for r in rooms
        ],
    }
    project_path = Path(project_dir).expanduser().resolve()
    # Update the config the miner will actually read. A project that already
    # has ``mempalace.yml`` (or a legacy ``mempal.*``) keeps it: writing a new
    # ``mempalace.yaml`` beside it would shadow it, and every setting in it
    # would stop applying without a word (#2676).
    existing_path = find_project_config(project_path)
    config_path = existing_path or project_path / "mempalace.yaml"
    if existing_path is not None:
        # Keep what init does not generate (``exclude_patterns`` and any
        # other hand-written key); init only owns ``wing`` and ``rooms``.
        try:
            raw = existing_path.read_bytes()
        except OSError as exc:
            raise OSError(
                f"Refusing to write config: {existing_path} exists and could not be "
                f"read ({exc}), so it is not overwritten"
            ) from exc
        problem = None
        try:
            previous = yaml.safe_load(raw.decode("utf-8-sig"))
        except (ValueError, yaml.YAMLError):
            previous, problem = None, "does not parse"
        if isinstance(previous, dict):
            config = {**config, **{k: v for k, v in previous.items() if k not in config}}
        elif problem is None and previous is not None:
            problem = "is not a mapping"
        if problem is None and not previous:
            # Comments only (or ``{}`` / ``~``): parses to nothing, but the
            # bytes are still the user's and the rewrite drops them.
            problem = "parses as empty"
        if problem is not None and raw.strip():
            # Init has to write a config the miner can use, but the file it
            # replaces is the user's and could not be read: keep a byte-exact
            # copy first, and refuse to regenerate if that copy cannot be made.
            try:
                backup = _write_backup(existing_path, raw)
            except OSError as exc:
                raise OSError(
                    f"Refusing to write config: {existing_path} {problem} and could not "
                    f"be backed up ({exc}), so it is not overwritten"
                ) from exc
            print(
                f"  ! {existing_path} {problem}; kept a copy at {backup} and "
                "regenerating it. Settings in it (exclude_patterns, ...) are not "
                "carried over; copy them back from the backup.",
                file=sys.stderr,
            )
    # Opening a pre-existing FIFO for writing blocks in the kernel until a
    # reader appears. Only a regular file is a valid config target; refuse
    # loudly rather than park ``init`` with no output.
    if config_path.exists() and not config_path.is_file():
        raise OSError(f"Refusing to write config: {config_path} is not a regular file")
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\n  Config saved: {config_path}")
    print("\n  Next step:")
    print(f"    mempalace mine {project_dir}")
    print(f"\n{'=' * 55}\n")


def detect_rooms_local(project_dir: str, yes: bool = False):
    """Main entry point for local setup."""
    from .config import normalize_wing_name

    project_path = Path(project_dir).expanduser().resolve()
    project_name = normalize_wing_name(project_path.name)

    if not project_path.exists():
        print(f"ERROR: Directory not found: {project_dir}")
        sys.exit(1)

    # Count files
    from .miner import scan_project

    files = scan_project(project_dir)

    # Try folder structure first
    rooms = detect_rooms_from_folders(project_dir)
    source = "folder structure"

    # If only "general" found, try filename patterns
    if len(rooms) <= 1:
        rooms = detect_rooms_from_files(project_dir)
        source = "filename patterns"

    # If still nothing, just use general
    if not rooms:
        rooms = [{"name": "general", "description": "All project files", "keywords": []}]
        source = "fallback (flat project)"

    print_proposed_structure(project_name, rooms, len(files), source)
    if yes:
        approved_rooms = rooms
    else:
        approved_rooms = get_user_approval(rooms)
    save_config(project_dir, project_name, approved_rooms)
