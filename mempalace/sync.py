"""
sync.py — Gitignore-aware drawer prune (#1252).

Removes drawers whose source files are now gitignored, deleted, or moved
out of the project. Reuses the same GitignoreMatcher infrastructure that
the miner uses on the way in, so the same rules that block ingest also
drive the corresponding cleanup.

Usage:
    from mempalace.sync import sync_palace
    report = sync_palace(palace_path, project_dirs=["/repo"], dry_run=True)
"""

import logging
from collections import defaultdict
from pathlib import Path
from typing import Callable, Optional, TypedDict

from .miner import is_gitignored, load_gitignore_matcher
from .palace import (
    MineAlreadyRunning,
    get_closets_collection,
    get_collection,
    mine_palace_lock,
)


logger = logging.getLogger(__name__)
_BATCH = 1000


class SyncReport(TypedDict):
    scanned: int
    kept: int
    gitignored: int
    missing: int
    no_source: int
    out_of_scope: int
    removed_drawers: int
    removed_closets: int
    dry_run: bool
    by_source: dict[str, int]


def _resolve_project_root(source_file: Path, project_roots: list) -> Optional[Path]:
    """Return the longest project_root that source_file lives under.

    Assumes ``project_roots`` is sorted by path-length descending so the
    first match is the longest (deepest) prefix.
    """
    for root in project_roots:
        try:
            source_file.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def _ancestor_matchers(source_file: Path, root: Path, matcher_cache: dict) -> list:
    """Build the ancestor-chain matcher list, root → file's parent.

    Callers are expected to invoke this only after `_resolve_project_root`
    confirms `source_file` lives under `root`. The defensive try/except
    keeps the function safe if a future caller skips that check.
    """
    matchers: list = []
    try:
        parts = source_file.relative_to(root).parts
    except ValueError:
        return matchers
    cursor = root
    matcher = load_gitignore_matcher(cursor, matcher_cache)
    if matcher is not None:
        matchers.append(matcher)
    for part in parts[:-1]:
        cursor = cursor / part
        matcher = load_gitignore_matcher(cursor, matcher_cache)
        if matcher is not None:
            matchers.append(matcher)
    return matchers


def _is_registry_row(meta: dict, drawer_id: str) -> bool:
    """Convo miner sentinels track 'have I seen this transcript' — preserve them.

    Deleting a `_reg_*` sentinel makes the next mine pass re-chunk and re-embed
    the entire transcript even though its content has not changed.
    """
    if (meta or {}).get("room") == "_registry":
        return True
    if (meta or {}).get("ingest_mode") == "registry":
        return True
    if drawer_id and drawer_id.startswith("_reg_"):
        return True
    return False


def _classify_drawer(
    meta: dict, matcher_cache: dict, project_roots: list, drawer_id: str = ""
) -> str:
    """Classify a drawer by its source_file metadata.

    Returns one of: kept, gitignored, missing, no_source, out_of_scope.
    """
    # Defensive: main loop filters registry rows; this guards direct callers.
    if _is_registry_row(meta, drawer_id):
        return "kept"

    source_file = (meta or {}).get("source_file")
    if not source_file:
        return "no_source"

    src = Path(source_file)
    if not src.is_absolute():
        return "no_source"
    src = src.resolve(strict=False)

    root = _resolve_project_root(src, project_roots)
    if root is None:
        return "out_of_scope"

    if not src.exists():
        return "missing"

    matchers = _ancestor_matchers(src, root, matcher_cache)
    if matchers and is_gitignored(src, matchers, is_dir=False):
        return "gitignored"

    return "kept"


def _iter_drawer_metadata(col, wing: Optional[str]):
    """Yield (id, metadata) tuples from the drawers collection in batches."""
    offset = 0
    where = {"wing": wing} if wing else None
    while True:
        kwargs = {"include": ["metadatas"], "limit": _BATCH, "offset": offset}
        if where:
            kwargs["where"] = where
        batch = col.get(**kwargs)
        ids = batch.get("ids") or []
        metas = batch.get("metadatas") or []
        if not ids:
            return
        for drawer_id, meta in zip(ids, metas):
            yield drawer_id, meta
        if len(ids) < _BATCH:
            return
        offset += len(ids)


def _auto_detect_project_roots(col, wing: Optional[str]) -> list:
    """Walk drawer metadata once collecting candidate project roots.

    A path is a project root if any ancestor up to filesystem root holds
    a `.git` directory or a `.gitignore` file. The deepest such ancestor
    wins, so nested-but-still-tracked subprojects are honoured.
    `Path.parents` iterates deepest-first, so the first hit IS deepest.

    Dedupes on ``source_file`` string so a 200-chunk file costs one disk
    walk, not 200.
    """
    roots: set = set()
    seen_sources: set = set()
    for _, meta in _iter_drawer_metadata(col, wing):
        source_file = (meta or {}).get("source_file")
        if not source_file or source_file in seen_sources:
            continue
        seen_sources.add(source_file)
        src = Path(source_file)
        if not src.is_absolute():
            continue
        for parent in src.parents:
            if (parent / ".git").exists() or (parent / ".gitignore").is_file():
                roots.add(parent.resolve(strict=False))
                break
    return sorted(roots, key=lambda p: (-len(str(p)), str(p)))


def _normalize_project_dirs(project_dirs) -> list:
    """Resolve and sort project dirs so deepest-prefix wins on first match."""
    resolved = [Path(p).resolve(strict=False) for p in project_dirs]
    return sorted(resolved, key=lambda p: (-len(str(p)), str(p)))


def _delete_in_batches(col, ids: list, batch_size: int, wal_log: Optional[Callable]):
    """Delete drawer IDs in batches, optionally logging each batch to WAL."""
    deleted = 0
    for i in range(0, len(ids), batch_size):
        chunk = ids[i : i + batch_size]
        col.delete(ids=chunk)
        deleted += len(chunk)
        if wal_log is not None:
            wal_log(
                "sync_prune",
                {"first_id": chunk[0]},
                {"removed_count": len(chunk)},
            )
    return deleted


def sync_palace(
    palace_path: str,
    project_dirs: Optional[list] = None,
    wing: Optional[str] = None,
    dry_run: bool = True,
    batch_size: int = _BATCH,
    wal_log: Optional[Callable] = None,
) -> SyncReport:
    """Prune drawers whose source files are gitignored, missing, or moved.

    Returns a SyncReport with bucket counts. Dry-run by default; pass
    dry_run=False to actually delete drawers and matching closets.

    Holds ``mine_palace_lock`` for the whole call so the classify pass and
    the apply branch see the same drawer snapshot. Raises
    ``MineAlreadyRunning`` if another mine is in progress on this palace.

    On apply (``dry_run=False``), at least one of ``wing`` or
    ``project_dirs`` must be set so a caller cannot accidentally prune
    every wing in a multi-project palace via auto-detected roots.
    """
    if not dry_run and not wing and not project_dirs:
        raise ValueError(
            "sync apply requires explicit wing= or project_dirs= so it cannot "
            "auto-prune every wing in a multi-project palace; pass --wing or "
            "a project directory"
        )
    if project_dirs is not None and not project_dirs:
        raise ValueError(
            "project_dirs was provided but is empty; pass at least one project "
            "root or pass project_dirs=None to auto-detect from drawer metadata"
        )

    counts = {
        "scanned": 0,
        "kept": 0,
        "gitignored": 0,
        "missing": 0,
        "no_source": 0,
        "out_of_scope": 0,
    }
    by_source: dict = defaultdict(int)
    removable_ids: list = []
    removable_sources: set = set()

    with mine_palace_lock(palace_path):
        col = get_collection(palace_path, create=False)

        if project_dirs is not None:
            roots = _normalize_project_dirs(project_dirs)
        else:
            roots = _auto_detect_project_roots(col, wing)

        matcher_cache: dict = {}
        # Same source_file → same verdict holds because mine_palace_lock
        # blocks concurrent writers and the loop is synchronous.
        classification_cache: dict = {}

        for drawer_id, meta in _iter_drawer_metadata(col, wing):
            counts["scanned"] += 1
            meta = meta or {}
            source_file = meta.get("source_file")

            if _is_registry_row(meta, drawer_id):
                bucket = "kept"
            elif source_file and source_file in classification_cache:
                bucket = classification_cache[source_file]
            else:
                bucket = _classify_drawer(meta, matcher_cache, roots, drawer_id)
                if source_file:
                    classification_cache[source_file] = bucket

            counts[bucket] += 1
            if bucket in ("gitignored", "missing"):
                removable_ids.append(drawer_id)
                if source_file:
                    removable_sources.add(source_file)
                    by_source[source_file] += 1

        report: SyncReport = {
            **counts,
            "removed_drawers": 0,
            "removed_closets": 0,
            "dry_run": dry_run,
            "by_source": dict(by_source),
        }

        if dry_run or not removable_ids:
            return report

        report["removed_drawers"] = _delete_in_batches(col, removable_ids, batch_size, wal_log)

        closets_col = None
        try:
            closets_col = get_closets_collection(palace_path, create=False)
        except Exception as exc:
            logger.warning("Closet purge skipped (collection unavailable): %s", exc)

        closets_removed = 0
        if closets_col is not None and removable_sources:
            closet_ids = (
                closets_col.get(
                    where={"source_file": {"$in": list(removable_sources)}},
                    include=[],
                ).get("ids")
                or []
            )
            if closet_ids:
                closets_col.delete(ids=closet_ids)
                closets_removed = len(closet_ids)
        report["removed_closets"] = closets_removed
    return report


__all__ = [
    "MineAlreadyRunning",
    "SyncReport",
    "sync_palace",
]
