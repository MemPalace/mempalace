"""Deterministic, Chroma-safe source identity metadata for mined drawers."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

SOURCE_KINDS = frozenset({"curated", "code", "documentation", "session", "worktree-artifact"})
MEMORY_TIERS = frozenset({"hot", "cold"})
SOURCE_CANONICALITIES = frozenset({"canonical", "linked-worktree"})


class LinkedWorktreeRejected(ValueError):
    """Raised when project mining targets a non-canonical Git worktree."""

    def __init__(self, source: str, canonical_root: str | None):
        self.source = source
        self.canonical_root = canonical_root
        message = "linked Git worktree mining is disabled; mine the canonical checkout"
        if canonical_root:
            message += f" at {canonical_root}"
        super().__init__(message)


@dataclass(frozen=True)
class SourceContext:
    root: str
    source_file: str
    source_kind: str
    memory_tier: str = "hot"
    source_revision: str | None = None
    source_canonicality: str = "canonical"
    source_sha256: str | None = None


def sha256_file(path: str) -> str | None:
    """Hash source bytes without loading a potentially large file into memory."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def git_revision(root: str) -> str | None:
    """Return the repository HEAD for provenance, or None outside Git."""
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = result.stdout.strip()
    return revision if result.returncode == 0 and revision else None


def _git_output(source: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", source, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _primary_worktree(source: str) -> str | None:
    listing = _git_output(source, "worktree", "list", "--porcelain")
    if not listing:
        return None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            return os.path.realpath(line.removeprefix("worktree ").strip())
    return None


def detect_linked_worktree(source: str) -> tuple[bool, str | None]:
    """Return whether ``source`` is a linked checkout and its primary root."""
    source = os.path.realpath(os.path.expanduser(source))
    roots = _git_output(
        source,
        "rev-parse",
        "--absolute-git-dir",
        "--path-format=absolute",
        "--git-common-dir",
    )
    lines = roots.splitlines() if roots else []
    if len(lines) != 2:
        return False, None

    git_dir = Path(lines[0]).resolve()
    common_dir = Path(lines[1]).resolve()
    try:
        linked = git_dir.is_relative_to(common_dir / "worktrees")
    except ValueError:
        linked = False
    return linked, _primary_worktree(source)


def enforce_worktree_policy(
    source: str,
    config: dict,
    requested_canonicality: str,
) -> str:
    """Apply project config and return canonical provenance for this source."""
    linked, canonical_root = detect_linked_worktree(source)
    explicitly_allowed = requested_canonicality == "linked-worktree"
    if linked and config.get("reject_linked_worktrees", True) and not explicitly_allowed:
        raise LinkedWorktreeRejected(source, canonical_root)
    return "linked-worktree" if linked else "canonical"


def _validate_context(context: SourceContext) -> None:
    if context.source_kind not in SOURCE_KINDS:
        raise ValueError(f"invalid source_kind: {context.source_kind!r}")
    if context.memory_tier not in MEMORY_TIERS:
        raise ValueError(f"invalid memory_tier: {context.memory_tier!r}")
    if context.source_canonicality not in SOURCE_CANONICALITIES:
        raise ValueError(f"invalid source_canonicality: {context.source_canonicality!r}")


def source_kind_for_room(config: dict, room_name: str) -> str:
    """Resolve a validated per-room source kind with a project default."""
    default = config.get("source_kind", "code")
    room = next(
        (
            item
            for item in config.get("rooms", [])
            if isinstance(item, dict) and item.get("name") == room_name
        ),
        {},
    )
    value = room.get("source_kind", default)
    if value not in SOURCE_KINDS:
        raise ValueError(f"invalid source_kind: {value!r}")
    return value


def build_source_metadata(
    context: SourceContext,
    content: str,
    chunk_index: int,
) -> dict[str, str]:
    """Build stable scalar metadata while leaving drawer content untouched."""
    _validate_context(context)
    root = os.path.realpath(os.path.expanduser(context.root))
    source_file = os.path.realpath(os.path.expanduser(context.source_file))
    relative = os.path.relpath(source_file, root).replace(os.sep, "/")
    source_sha = context.source_sha256 or sha256_file(source_file)
    content_bytes = content.encode("utf-8", errors="surrogatepass")

    metadata = {
        "source_kind": context.source_kind,
        "memory_tier": context.memory_tier,
        "source_root": root,
        "source_identity": f"{context.source_kind}:{relative}",
        "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
        "source_canonicality": context.source_canonicality,
    }
    if context.source_revision:
        metadata["source_revision"] = context.source_revision
    if source_sha:
        metadata["source_sha256"] = source_sha

    # Kept in the public signature because callers build metadata per chunk;
    # the existing scalar chunk_index remains the chunk coordinate.
    _ = chunk_index
    return metadata
