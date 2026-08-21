"""Changed-set project ingest without a full filesystem walk.

The producer may be Git, an IDE, or a file watcher. Core accepts only project-
relative paths and keeps all mutation semantics inside MemPalace.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from .miner import (
    is_gitignored,
    load_config,
    load_gitignore_matcher,
    process_file,
)
from .palace import (
    get_closets_collection,
    get_collection,
    mine_palace_lock,
)


class ChangedSetReport(TypedDict):
    changed: int
    deleted: int
    ignored: int
    reindexed: int
    drawers_added: int
    dry_run: bool


def _resolve_source(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("changed-set paths must be non-empty strings")
    candidate = (root / value).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"changed-set path escapes project root: {value}") from exc
    return candidate


def normalize_changed_set(
    project_root: str | Path, changed: list[str], deleted: list[str]
) -> tuple[list[Path], list[Path]]:
    """Validate, resolve, sort, and deduplicate an external changed manifest."""
    if not isinstance(changed, list) or not all(isinstance(value, str) for value in changed):
        raise ValueError("changed must be an array of strings")
    if not isinstance(deleted, list) or not all(isinstance(value, str) for value in deleted):
        raise ValueError("deleted must be an array of strings")
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project root does not exist: {root}")
    changed_paths = sorted({_resolve_source(root, value) for value in changed}, key=str)
    deleted_paths = sorted({_resolve_source(root, value) for value in deleted}, key=str)
    overlap = set(changed_paths) & set(deleted_paths)
    if overlap:
        raise ValueError(f"paths cannot be both changed and deleted: {sorted(map(str, overlap))}")
    missing_changed = [str(path) for path in changed_paths if not path.is_file()]
    if missing_changed:
        raise ValueError(f"changed paths must exist as files: {missing_changed}")
    return changed_paths, deleted_paths


def _is_gitignored_source(root: Path, path: Path) -> bool:
    """Apply root and nested gitignore rules to one explicit changed path."""
    matchers = []
    cache: dict[Path, object] = {}
    current = root
    directories = [root]
    for part in path.relative_to(root).parts[:-1]:
        current /= part
        directories.append(current)
    for directory in directories:
        matcher = load_gitignore_matcher(directory, cache)
        if matcher is not None:
            matchers.append(matcher)
    return bool(matchers and is_gitignored(path, matchers, is_dir=False))


def sync_changed_sources(
    *,
    palace_path: str,
    project_root: str | Path,
    changed: list[str],
    deleted: list[str],
    wing: str | None = None,
    agent: str = "mempalace",
    dry_run: bool = True,
) -> ChangedSetReport:
    """Serialize replacement of changed sources and removal of deleted sources."""
    root = Path(project_root).expanduser().resolve()
    changed_paths, deleted_paths = normalize_changed_set(root, changed, deleted)
    ignored_paths = [path for path in changed_paths if _is_gitignored_source(root, path)]
    ignored_set = set(ignored_paths)
    indexable_paths = [path for path in changed_paths if path not in ignored_set]
    report: ChangedSetReport = {
        "changed": len(changed_paths),
        "deleted": len(deleted_paths),
        "ignored": len(ignored_paths),
        "reindexed": 0,
        "drawers_added": 0,
        "dry_run": dry_run,
    }
    if dry_run:
        return report

    project_config = load_config(str(root))
    resolved_wing = wing or project_config["wing"]
    rooms = project_config.get("rooms", [{"name": "general", "description": "All files"}])
    affected = [*ignored_paths, *deleted_paths]
    with mine_palace_lock(palace_path):
        drawers = get_collection(palace_path, create=False)
        closets = get_closets_collection(palace_path, create=True)
        for path in affected:
            source = str(path)
            drawers.delete(where={"$and": [{"source_file": source}, {"wing": resolved_wing}]})
            closets.delete(where={"$and": [{"source_file": source}, {"wing": resolved_wing}]})
        for path in indexable_paths:
            added, _room, skip_reason = process_file(
                path,
                root,
                drawers,
                resolved_wing,
                rooms,
                agent,
                False,
                closets_col=closets,
                force_reindex=True,
            )
            if skip_reason is None and added > 0:
                report["reindexed"] += 1
            report["drawers_added"] += added
    return report


__all__ = ["ChangedSetReport", "normalize_changed_set", "sync_changed_sources"]
