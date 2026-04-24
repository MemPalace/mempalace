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

from .palace import (
    NORMALIZE_VERSION,
    SKIP_DIRS,
    build_closet_lines,
    file_already_mined,
    get_closets_collection,
    get_collection,
    mine_lock,
    purge_file_closets,
    upsert_closet_lines,
)

READABLE_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".json",
    ".jsonl",
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
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB — skip files larger than this.
# Long Claude Code sessions and large transcript exports routinely exceed
# 10 MB. The cap exists as a defensive rail against pathological binary
# files, not as a limit on legitimate text. Per-drawer size is bounded
# by CHUNK_SIZE, but larger sources still produce proportionally more
# drawers and therefore more storage, embedding, and processing work —
# and file reads are not streamed (the whole content is loaded into
# memory before chunking), so memory use scales with source size too.


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

    resolved_project_dir = Path(project_dir).expanduser().resolve()
    config_path = resolved_project_dir / "mempalace.yaml"
    if not config_path.exists():
        # Fallback to legacy name
        legacy_path = resolved_project_dir / "mempal.yaml"
        if legacy_path.exists():
            config_path = legacy_path
        else:
            wing_name = resolved_project_dir.name
            print(
                f"  No mempalace.yaml found in {resolved_project_dir} "
                f"— using auto-detected defaults (wing='{wing_name}'). "
                "Directories with the same basename will share a wing; "
                "add mempalace.yaml to disambiguate.",
                file=sys.stderr,
            )
            return {
                "wing": wing_name,
                "rooms": [
                    {
                        "name": "general",
                        "description": "All project files",
                        "keywords": ["general"],
                    }
                ],
            }
    with open(config_path) as f:
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


_ENTITY_REGISTRY_PATH = os.path.join(os.path.expanduser("~"), ".mempalace", "known_entities.json")
_ENTITY_REGISTRY_CACHE: dict = {"mtime": None, "names": frozenset(), "raw": {}}
_ENTITY_EXTRACT_WINDOW = 5000  # chars of content scanned for capitalized words
_ENTITY_METADATA_LIMIT = 25  # max entities packed into the metadata field


def _refresh_known_entities_cache() -> None:
    """Reload ``~/.mempalace/known_entities.json`` into the module cache if
    its mtime changed since the last read. Shared by ``_load_known_entities``
    (flat set) and ``_load_known_entities_raw`` (category dict), so callers
    can pick whichever shape they need without duplicating the mtime-gated
    disk read.
    """
    try:
        mtime = os.path.getmtime(_ENTITY_REGISTRY_PATH)
    except OSError:
        if _ENTITY_REGISTRY_CACHE["mtime"] is not None:
            _ENTITY_REGISTRY_CACHE["mtime"] = None
            _ENTITY_REGISTRY_CACHE["names"] = frozenset()
            _ENTITY_REGISTRY_CACHE["raw"] = {}
        return

    if _ENTITY_REGISTRY_CACHE["mtime"] == mtime:
        return

    names: set = set()
    raw: dict = {}
    try:
        import json

        with open(_ENTITY_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            raw = data
            for cat in data.values():
                if isinstance(cat, list):
                    names.update(str(n) for n in cat if n)
                elif isinstance(cat, dict):
                    names.update(str(k) for k in cat.keys() if k)
    except Exception:
        names = set()
        raw = {}

    _ENTITY_REGISTRY_CACHE["mtime"] = mtime
    _ENTITY_REGISTRY_CACHE["names"] = frozenset(names)
    _ENTITY_REGISTRY_CACHE["raw"] = raw


def _load_known_entities() -> frozenset:
    """Flat set of every known entity name (across all categories).

    Cached by mtime; invalidated when the registry file changes.
    """
    _refresh_known_entities_cache()
    return _ENTITY_REGISTRY_CACHE["names"]


def _load_known_entities_raw() -> dict:
    """Full category-dict view of the registry, shape
    ``{"category": ["Name1", ...], ...}``. Cached by mtime.

    Consumed by modules (e.g., fact_checker) that need to reason about
    categories rather than a flat name set. Never returns a mutable
    reference to the cache — callers get a shallow copy.
    """
    _refresh_known_entities_cache()
    return dict(_ENTITY_REGISTRY_CACHE["raw"])


_HALL_KEYWORDS_CACHE = None


def detect_hall(content: str) -> str:
    """Route content to a hall based on keyword scoring.

    Halls connect rooms within a wing — they categorize the TYPE of content
    (emotional, technical, family, etc.) while rooms categorize the TOPIC.
    """
    global _HALL_KEYWORDS_CACHE
    if _HALL_KEYWORDS_CACHE is None:
        from .config import MempalaceConfig

        _HALL_KEYWORDS_CACHE = MempalaceConfig().hall_keywords
    content_lower = content[:3000].lower()

    scores = {}
    for hall, keywords in _HALL_KEYWORDS_CACHE.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[hall] = score

    if scores:
        return max(scores, key=scores.get)
    return "general"


def _extract_entities_for_metadata(content: str) -> str:
    """Extract entity names from content for metadata tagging.

    Combines the user's known-entity registry (cached across calls) with
    capitalized words appearing ≥2 times in the first ``_ENTITY_EXTRACT_WINDOW``
    chars. Filters out the closet stoplist (``When``, ``After``, ``The``, …)
    so sentence-starters don't masquerade as proper nouns.

    Returns semicolon-separated string suitable for ChromaDB metadata
    filtering. The list is truncated to ``_ENTITY_METADATA_LIMIT`` entries
    *before* joining so a name is never cut in half.
    """
    import re

    from .palace import _ENTITY_STOPLIST

    matched: set = set()

    known = _load_known_entities()
    for name in known:
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", content):
            matched.add(name)

    from .palace import _candidate_entity_words

    window = content[:_ENTITY_EXTRACT_WINDOW]
    words = _candidate_entity_words(window)
    freq: dict = {}
    for w in words:
        if w in _ENTITY_STOPLIST:
            continue
        freq[w] = freq.get(w, 0) + 1
    for w, c in freq.items():
        if c >= 2 and len(w) > 2:
            matched.add(w)

    if not matched:
        return ""
    # Truncate the *list*, not the joined string — never split a name.
    capped = sorted(matched)[:_ENTITY_METADATA_LIMIT]
    return ";".join(capped)


def add_drawer(
    collection, wing: str, room: str, content: str, source_file: str, chunk_index: int, agent: str
):
    """Add one drawer to the palace."""
    drawer_id = f"drawer_{wing}_{room}_{hashlib.sha256((source_file + str(chunk_index)).encode()).hexdigest()[:24]}"
    try:
        metadata = {
            "wing": wing,
            "room": room,
            "source_file": source_file,
            "chunk_index": chunk_index,
            "added_by": agent,
            "filed_at": datetime.now().isoformat(),
            "normalize_version": NORMALIZE_VERSION,
        }
        # Store file mtime so we can detect modifications later.
        try:
            metadata["source_mtime"] = os.path.getmtime(source_file)
        except OSError:
            pass
        # Tag with hall for graph connectivity within wings
        metadata["hall"] = detect_hall(content)
        # Tag with entity names for filterable search
        entities = _extract_entities_for_metadata(content)
        if entities:
            metadata["entities"] = entities
        collection.upsert(
            documents=[content],
            ids=[drawer_id],
            metadatas=[metadata],
        )
        return True
    except Exception:
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
    closets_col=None,
) -> tuple:
    """Read, chunk, route, and file one file. Returns (drawer_count, room_name)."""

    # Skip if already filed
    source_file = str(filepath)
    if not dry_run and file_already_mined(collection, source_file, check_mtime=True):
        return 0, "general"

    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, "general"

    content = content.strip()
    if len(content) < MIN_CHUNK_SIZE:
        return 0, "general"

    room = detect_room(filepath, content, rooms, project_path)
    chunks = chunk_text(content, source_file)

    if dry_run:
        print(f"    [DRY RUN] {filepath.name} -> room:{room} ({len(chunks)} drawers)")
        return len(chunks), room

    # Lock this file so concurrent agents don't interleave delete+insert.
    # Without the lock, two agents can both pass file_already_mined(),
    # both delete, and both insert — creating duplicates or losing data.
    with mine_lock(source_file):
        # Re-check after acquiring lock — another agent may have just finished
        if file_already_mined(collection, source_file, check_mtime=True):
            return 0, room

        # Purge stale drawers for this file before re-inserting the fresh chunks.
        # Converts modified-file re-mines from upsert-over-existing-IDs (which hits
        # hnswlib's thread-unsafe updatePoint path and can segfault on macOS ARM
        # with chromadb 0.6.3) into a clean delete+insert, bypassing the update
        # path entirely.
        try:
            collection.delete(where={"source_file": source_file})
        except Exception:
            pass

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
            )
            if added:
                drawers_added += 1

        # Build closet — the searchable index pointing to these drawers.
        # Purge first: a re-mine (mtime change or normalize_version bump) must
        # fully replace the prior closets, not append to them.
        if closets_col and drawers_added > 0:
            drawer_ids = [
                f"drawer_{wing}_{room}_{hashlib.sha256((source_file + str(c['chunk_index'])).encode()).hexdigest()[:24]}"
                for c in chunks
            ]
            closet_lines = build_closet_lines(source_file, drawer_ids, content, wing, room)
            closet_id_base = (
                f"closet_{wing}_{room}_{hashlib.sha256(source_file.encode()).hexdigest()[:24]}"
            )
            entities = _extract_entities_for_metadata(content)
            closet_meta = {
                "wing": wing,
                "room": room,
                "source_file": source_file,
                "drawer_count": drawers_added,
                "filed_at": datetime.now().isoformat(),
                "normalize_version": NORMALIZE_VERSION,
            }
            if entities:
                closet_meta["entities"] = entities
            purge_file_closets(closets_col, source_file)
            upsert_closet_lines(closets_col, closet_id_base, closet_lines, closet_meta)

    return drawers_added, room


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
            # Skip symlinks — prevents following links to /dev/urandom, etc.
            if filepath.is_symlink():
                continue
            # Skip files exceeding size limit
            try:
                if filepath.stat().st_size > MAX_FILE_SIZE:
                    continue
            except OSError:
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
    print(f"{'-' * 55}\n")

    if not dry_run:
        collection = get_collection(palace_path)
        closets_col = get_closets_collection(palace_path)
    else:
        collection = None
        closets_col = None

    total_drawers = 0
    files_skipped = 0
    room_counts = defaultdict(int)

    for i, filepath in enumerate(files, 1):
        drawers, room = process_file(
            filepath=filepath,
            project_path=project_path,
            collection=collection,
            wing=wing,
            rooms=rooms,
            agent=agent,
            dry_run=dry_run,
            closets_col=closets_col,
        )
        if drawers == 0 and not dry_run:
            files_skipped += 1
        else:
            total_drawers += drawers
            room_counts[room] += 1
            if not dry_run:
                print(f"  + [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers}")

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {len(files) - files_skipped}")
    print(f"  Files skipped (already filed): {files_skipped}")
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
        col = get_collection(palace_path, create=False)
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        return

    # Count by wing and room — paginate to avoid SQLite "too many SQL
    # variables" error on large palaces (see #802, #850).
    total = col.count()
    wing_rooms: dict = defaultdict(lambda: defaultdict(int))
    batch_size = 5000
    offset = 0
    while offset < total:
        r = col.get(limit=batch_size, offset=offset, include=["metadatas"])
        batch = r["metadatas"]
        if not batch:
            break
        for m in batch:
            m = m or {}
            wing_rooms[m.get("wing", "?")][m.get("room", "?")] += 1
        offset += len(batch)

    print(f"\n{'=' * 55}")
    print(f"  MemPalace Status — {total} drawers")
    print(f"{'=' * 55}\n")
    for wing, rooms in sorted(wing_rooms.items()):
        print(f"  WING: {wing}")
        for room, count in sorted(rooms.items(), key=lambda x: x[1], reverse=True):
            print(f"    ROOM: {room:20} {count:5} drawers")
        print()
    print(f"{'=' * 55}\n")
