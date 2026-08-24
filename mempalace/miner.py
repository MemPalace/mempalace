#!/usr/bin/env python3
"""
miner.py — Files everything into the palace.

Reads mempalace.yaml from the project directory to know the wing + rooms.
Routes each file to the right room based on content.
Stores verbatim chunks as drawers. No summaries. Ever.
"""

import os
import sys
import hashlib
import fnmatch
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import chromadb

from . import file_state

READABLE_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".html",
    ".css",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".sh",
    ".csv",
    ".sql",
    ".toml",
}

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
    ".mempalace",
    ".ruff_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".cache",
    ".tox",
    ".nox",
    ".idea",
    ".vscode",
    ".ipynb_checkpoints",
    ".eggs",
    "htmlcov",
    "target",
}

SKIP_FILENAMES = {
    "mempalace.yaml",
    "mempalace.yml",
    "mempal.yaml",
    "mempal.yml",
    ".gitignore",
    "package-lock.json",
}

CHUNK_SIZE = 800  # chars per drawer
CHUNK_OVERLAP = 100  # overlap between chunks
MIN_CHUNK_SIZE = 50  # skip tiny chunks


# =============================================================================
# IGNORE MATCHING
# =============================================================================


class GitignoreMatcher:
    """Lightweight matcher for one directory's .gitignore patterns."""

    def __init__(self, base_dir: Path, rules: list):
        self.base_dir = base_dir
        self.rules = rules

    @classmethod
    def from_dir(cls, dir_path: Path):
        gitignore_path = dir_path / ".gitignore"
        if not gitignore_path.is_file():
            return None

        try:
            lines = gitignore_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return None

        rules = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("\\#") or line.startswith("\\!"):
                line = line[1:]
            elif line.startswith("#"):
                continue

            negated = line.startswith("!")
            if negated:
                line = line[1:]

            anchored = line.startswith("/")
            if anchored:
                line = line.lstrip("/")

            dir_only = line.endswith("/")
            if dir_only:
                line = line.rstrip("/")

            if not line:
                continue

            rules.append(
                {
                    "pattern": line,
                    "anchored": anchored,
                    "dir_only": dir_only,
                    "negated": negated,
                }
            )

        if not rules:
            return None

        return cls(dir_path, rules)

    def matches(self, path: Path, is_dir: bool = None):
        try:
            relative = path.relative_to(self.base_dir).as_posix().strip("/")
        except ValueError:
            return None

        if not relative:
            return None

        if is_dir is None:
            is_dir = path.is_dir()

        ignored = None
        for rule in self.rules:
            if self._rule_matches(rule, relative, is_dir):
                ignored = not rule["negated"]
        return ignored

    def _rule_matches(self, rule: dict, relative: str, is_dir: bool) -> bool:
        pattern = rule["pattern"]
        parts = relative.split("/")
        pattern_parts = pattern.split("/")

        if rule["dir_only"]:
            target_parts = parts if is_dir else parts[:-1]
            if not target_parts:
                return False
            if rule["anchored"] or len(pattern_parts) > 1:
                return self._match_from_root(target_parts, pattern_parts)
            return any(fnmatch.fnmatch(part, pattern) for part in target_parts)

        if rule["anchored"] or len(pattern_parts) > 1:
            return self._match_from_root(parts, pattern_parts)

        return any(fnmatch.fnmatch(part, pattern) for part in parts)

    def _match_from_root(self, target_parts: list, pattern_parts: list) -> bool:
        def matches(path_index: int, pattern_index: int) -> bool:
            if pattern_index == len(pattern_parts):
                return True

            if path_index == len(target_parts):
                return all(part == "**" for part in pattern_parts[pattern_index:])

            pattern_part = pattern_parts[pattern_index]
            if pattern_part == "**":
                return matches(path_index, pattern_index + 1) or matches(
                    path_index + 1, pattern_index
                )

            if not fnmatch.fnmatch(target_parts[path_index], pattern_part):
                return False

            return matches(path_index + 1, pattern_index + 1)

        return matches(0, 0)


def load_gitignore_matcher(dir_path: Path, cache: dict):
    """Load and cache one directory's .gitignore matcher."""
    if dir_path not in cache:
        cache[dir_path] = GitignoreMatcher.from_dir(dir_path)
    return cache[dir_path]


def is_gitignored(path: Path, matchers: list, is_dir: bool = False) -> bool:
    """Apply active .gitignore matchers in ancestor order; last match wins."""
    ignored = False
    for matcher in matchers:
        decision = matcher.matches(path, is_dir=is_dir)
        if decision is not None:
            ignored = decision
    return ignored


def should_skip_dir(dirname: str) -> bool:
    """Skip known generated/cache directories before gitignore matching."""
    return dirname in SKIP_DIRS or dirname.endswith(".egg-info")


def normalize_include_paths(include_ignored: list) -> set:
    """Normalize comma-parsed include paths into project-relative POSIX strings."""
    normalized = set()
    for raw_path in include_ignored or []:
        candidate = str(raw_path).strip().strip("/")
        if candidate:
            normalized.add(Path(candidate).as_posix())
    return normalized


def is_exact_force_include(path: Path, project_path: Path, include_paths: set) -> bool:
    """Return True when a path exactly matches an explicit include override."""
    if not include_paths:
        return False

    try:
        relative = path.relative_to(project_path).as_posix().strip("/")
    except ValueError:
        return False

    return relative in include_paths


def is_force_included(path: Path, project_path: Path, include_paths: set) -> bool:
    """Return True when a path or one of its ancestors/descendants was explicitly included."""
    if not include_paths:
        return False

    try:
        relative = path.relative_to(project_path).as_posix().strip("/")
    except ValueError:
        return False

    if not relative:
        return False

    for include_path in include_paths:
        if relative == include_path:
            return True
        if relative.startswith(f"{include_path}/"):
            return True
        if include_path.startswith(f"{relative}/"):
            return True

    return False


# =============================================================================
# CONFIG
# =============================================================================


def load_config(project_dir: str) -> dict:
    """Load mempalace.yaml from project directory (falls back to mempal.yaml)."""
    import yaml

    config_path = Path(project_dir).expanduser().resolve() / "mempalace.yaml"
    if not config_path.exists():
        # Fallback to legacy name
        legacy_path = Path(project_dir).expanduser().resolve() / "mempal.yaml"
        if legacy_path.exists():
            config_path = legacy_path
        else:
            print(f"ERROR: No mempalace.yaml found in {project_dir}")
            print(f"Run: mempalace init {project_dir}")
            sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# =============================================================================
# FILE ROUTING — which room does this file belong to?
# =============================================================================


def detect_room(filepath: Path, content: str, rooms: list, project_path: Path) -> str:
    """
    Route a file to the right room.
    Priority:
    1. Folder path matches a room name
    2. Filename matches a room name or keyword
    3. Content keyword scoring
    4. Fallback: "general"
    """
    relative = str(filepath.relative_to(project_path)).lower()
    filename = filepath.stem.lower()
    content_lower = content[:2000].lower()

    # Priority 1: folder path matches room name or keywords
    path_parts = relative.replace("\\", "/").split("/")
    for part in path_parts[:-1]:  # skip filename itself
        for room in rooms:
            candidates = [room["name"].lower()] + [k.lower() for k in room.get("keywords", [])]
            if any(part == c or c in part or part in c for c in candidates):
                return room["name"]

    # Priority 2: filename matches room name
    for room in rooms:
        if room["name"].lower() in filename or filename in room["name"].lower():
            return room["name"]

    # Priority 3: keyword scoring from room keywords + name
    scores = defaultdict(int)
    for room in rooms:
        keywords = room.get("keywords", []) + [room["name"]]
        for kw in keywords:
            count = content_lower.count(kw.lower())
            scores[room["name"]] += count

    if scores:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best

    return "general"


# =============================================================================
# CHUNKING
# =============================================================================


def chunk_text(content: str, source_file: str) -> list:
    """
    Split content into drawer-sized chunks.
    Tries to split on paragraph/line boundaries.
    Returns list of {"content": str, "chunk_index": int}
    """
    # Clean up
    content = content.strip()
    if not content:
        return []

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(content):
        end = min(start + CHUNK_SIZE, len(content))

        # Try to break at paragraph boundary
        if end < len(content):
            newline_pos = content.rfind("\n\n", start, end)
            if newline_pos > start + CHUNK_SIZE // 2:
                end = newline_pos
            else:
                newline_pos = content.rfind("\n", start, end)
                if newline_pos > start + CHUNK_SIZE // 2:
                    end = newline_pos

        chunk = content[start:end].strip()
        if len(chunk) >= MIN_CHUNK_SIZE:
            chunks.append(
                {
                    "content": chunk,
                    "chunk_index": chunk_index,
                }
            )
            chunk_index += 1

        start = end - CHUNK_OVERLAP if end < len(content) else end

    return chunks


# =============================================================================
# PALACE — ChromaDB operations
# =============================================================================


def get_collection(palace_path: str):
    os.makedirs(palace_path, exist_ok=True)
    client = chromadb.PersistentClient(path=palace_path)
    try:
        return client.get_collection("mempalace_drawers")
    except Exception:
        return client.create_collection("mempalace_drawers")


def file_already_mined(collection, source_file: str) -> bool:
    """Path-only check: does this path have any drawer at all?

    Kept for callers that only need "have we ever seen this path". Mining
    itself uses file_state.decide(), which also compares mtime and content
    hash so an edited file gets re-filed instead of skipped forever.
    """
    try:
        results = collection.get(where={"source_file": source_file}, limit=1)
        return len(results.get("ids", [])) > 0
    except Exception:
        return False


def add_drawer(
    collection,
    wing: str,
    room: str,
    content: str,
    source_file: str,
    chunk_index: int,
    agent: str,
    content_hash: str = None,
    source_mtime: float = None,
):
    """Add one drawer to the palace."""
    drawer_id = f"drawer_{wing}_{room}_{hashlib.md5((source_file + str(chunk_index)).encode(), usedforsecurity=False).hexdigest()[:16]}"
    metadata = {
        "wing": wing,
        "room": room,
        "source_file": source_file,
        "chunk_index": chunk_index,
        "added_by": agent,
        "filed_at": datetime.now().isoformat(),
    }
    # The stamp the next run reads to tell "unchanged" from "edited".
    if content_hash is not None:
        metadata["content_hash"] = content_hash
    if source_mtime is not None:
        metadata["source_mtime"] = float(source_mtime)
    try:
        collection.add(
            documents=[content],
            ids=[drawer_id],
            metadatas=[metadata],
        )
        return True
    except Exception as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            return False
        raise


# =============================================================================
# PROCESS ONE FILE
# =============================================================================


def process_file(
    filepath: Path,
    project_path: Path,
    collection,
    wing: str,
    rooms: list,
    agent: str,
    dry_run: bool,
) -> tuple:
    """Read, chunk, route, and file one file.

    Returns (action, drawer_count). The action is file_state.SKIP / MINE /
    REMINE — the caller needs it to report "skipped, unchanged" separately
    from "read it, and it held nothing worth filing".
    """

    source_file = str(filepath)

    if dry_run:
        action, content_hash, source_mtime = file_state.MINE, None, None
    else:
        action, content_hash, source_mtime = file_state.decide(collection, source_file)
        if action == file_state.SKIP:
            return file_state.SKIP, 0

    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Couldn't read it — leave whatever is already filed alone.
        return action, 0

    if action == file_state.REMINE:
        # The old drawers hold content that no longer exists. Drop them before
        # filing the new ones: chunk boundaries move when a file is edited, so
        # overwriting by id would strand the tail of the previous version.
        # Done after the read succeeds so a transient read error can't wipe
        # good drawers.
        file_state.drop_drawers(collection, source_file)

    content = content.strip()
    if len(content) < MIN_CHUNK_SIZE:
        return action, 0

    room = detect_room(filepath, content, rooms, project_path)
    chunks = chunk_text(content, source_file)

    if dry_run:
        print(f"    [DRY RUN] {filepath.name} → room:{room} ({len(chunks)} drawers)")
        return action, len(chunks)

    drawers_added = 0
    for chunk in chunks:
        added = add_drawer(
            collection=collection,
            wing=wing,
            room=room,
            content=chunk["content"],
            source_file=source_file,
            chunk_index=chunk["chunk_index"],
            agent=agent,
            content_hash=content_hash,
            source_mtime=source_mtime,
        )
        if added:
            drawers_added += 1

    return action, drawers_added


# =============================================================================
# SCAN PROJECT
# =============================================================================


def scan_project(
    project_dir: str,
    respect_gitignore: bool = True,
    include_ignored: list = None,
) -> list:
    """Return list of all readable file paths."""
    project_path = Path(project_dir).expanduser().resolve()
    files = []
    active_matchers = []
    matcher_cache = {}
    include_paths = normalize_include_paths(include_ignored)

    for root, dirs, filenames in os.walk(project_path):
        root_path = Path(root)

        if respect_gitignore:
            active_matchers = [
                matcher
                for matcher in active_matchers
                if root_path == matcher.base_dir or matcher.base_dir in root_path.parents
            ]
            current_matcher = load_gitignore_matcher(root_path, matcher_cache)
            if current_matcher is not None:
                active_matchers.append(current_matcher)

        dirs[:] = [
            d
            for d in dirs
            if is_force_included(root_path / d, project_path, include_paths)
            or not should_skip_dir(d)
        ]
        if respect_gitignore and active_matchers:
            dirs[:] = [
                d
                for d in dirs
                if is_force_included(root_path / d, project_path, include_paths)
                or not is_gitignored(root_path / d, active_matchers, is_dir=True)
            ]

        for filename in filenames:
            filepath = root_path / filename
            force_include = is_force_included(filepath, project_path, include_paths)
            exact_force_include = is_exact_force_include(filepath, project_path, include_paths)

            if not force_include and filename in SKIP_FILENAMES:
                continue
            if filepath.suffix.lower() not in READABLE_EXTENSIONS and not exact_force_include:
                continue
            if respect_gitignore and active_matchers and not force_include:
                if is_gitignored(filepath, active_matchers, is_dir=False):
                    continue
            files.append(filepath)
    return files


# =============================================================================
# MAIN: MINE
# =============================================================================


def mine(
    project_dir: str,
    palace_path: str,
    wing_override: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    respect_gitignore: bool = True,
    include_ignored: list = None,
):
    """Mine a project directory into the palace."""

    project_path = Path(project_dir).expanduser().resolve()
    config = load_config(project_dir)

    wing = wing_override or config["wing"]
    rooms = config.get("rooms", [{"name": "general", "description": "All project files"}])

    files = scan_project(
        project_dir,
        respect_gitignore=respect_gitignore,
        include_ignored=include_ignored,
    )
    if limit > 0:
        files = files[:limit]

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Rooms:   {', '.join(r['name'] for r in rooms)}")
    print(f"  Files:   {len(files)}")
    print(f"  Palace:  {palace_path}")
    if dry_run:
        print("  DRY RUN — nothing will be filed")
    if not respect_gitignore:
        print("  .gitignore: DISABLED")
    if include_ignored:
        print(f"  Include: {', '.join(sorted(normalize_include_paths(include_ignored)))}")
    print(f"{'─' * 55}\n")

    if not dry_run:
        collection = get_collection(palace_path)
    else:
        collection = None

    total_drawers = 0
    files_skipped = 0
    files_remined = 0
    room_counts = defaultdict(int)

    for i, filepath in enumerate(files, 1):
        action, drawers = process_file(
            filepath=filepath,
            project_path=project_path,
            collection=collection,
            wing=wing,
            rooms=rooms,
            agent=agent,
            dry_run=dry_run,
        )
        if action == file_state.SKIP:
            files_skipped += 1
            continue
        if action == file_state.REMINE:
            files_remined += 1
        total_drawers += drawers
        if drawers:
            room = detect_room(filepath, "", rooms, project_path)
            room_counts[room] += 1
            if not dry_run:
                mark = "↻" if action == file_state.REMINE else "✓"
                print(f"  {mark} [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers}")

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {len(files) - files_skipped}")
    print(f"  Files skipped (unchanged): {files_skipped}")
    print(f"  Files re-filed (content changed): {files_remined}")
    print(f"  Drawers filed: {total_drawers}")
    print("\n  By room:")
    for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"    {room:20} {count} files")
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")


# =============================================================================
# STATUS
# =============================================================================


def status(palace_path: str):
    """Show what's been filed in the palace."""
    try:
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        return

    # Count by wing and room
    r = col.get(limit=10000, include=["metadatas"])
    metas = r["metadatas"]

    wing_rooms = defaultdict(lambda: defaultdict(int))
    for m in metas:
        wing_rooms[m.get("wing", "?")][m.get("room", "?")] += 1

    print(f"\n{'=' * 55}")
    print(f"  MemPalace Status — {len(metas)} drawers")
    print(f"{'=' * 55}\n")
    for wing, rooms in sorted(wing_rooms.items()):
        print(f"  WING: {wing}")
        for room, count in sorted(rooms.items(), key=lambda x: x[1], reverse=True):
            print(f"    ROOM: {room:20} {count:5} drawers")
        print()
    print(f"{'=' * 55}\n")
