"""Deterministic, Chroma-safe source identity metadata for mined drawers."""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass

SOURCE_KINDS = frozenset({"curated", "code", "documentation", "session", "worktree-artifact"})
MEMORY_TIERS = frozenset({"hot", "cold"})
SOURCE_CANONICALITIES = frozenset({"canonical", "linked-worktree"})


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


def _validate_context(context: SourceContext) -> None:
    if context.source_kind not in SOURCE_KINDS:
        raise ValueError(f"invalid source_kind: {context.source_kind!r}")
    if context.memory_tier not in MEMORY_TIERS:
        raise ValueError(f"invalid memory_tier: {context.memory_tier!r}")
    if context.source_canonicality not in SOURCE_CANONICALITIES:
        raise ValueError(f"invalid source_canonicality: {context.source_canonicality!r}")


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
