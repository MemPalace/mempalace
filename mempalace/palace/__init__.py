"""
mempalace.palace — Shared palace operations.

Collection access, closets, mine locks, and mined-set checks used by both
miners and the MCP server. Public import path is ``mempalace.palace``.
"""

# The public import path remains ``mempalace.palace``. Implementation is
# split across this package so PRs can target collection access, closets,
# mine locks, or mined-set checks without colliding. Fragments exec into
# this package's globals (the same namespace as the historical module).

import contextlib
import hashlib
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from ..backends import (
    BackendClosedError,
    BackendMismatchError,
    CollectionNotInitializedError,
    PalaceNotFoundError,
    PalaceRef,
    detect_backend_for_path,
    detect_backends_for_path,
    get_backend,
    get_backend_class,
    resolve_backend_for_palace,
)
from ..backends.embedding_wrapper import EmbeddingCollection
from ..entity_detector import (
    _apply_known_systems_prepass,
    _collapse_long_ascii_runs,
    _get_coca_filter,
)

logger = logging.getLogger("mempalace_mcp")

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

_DEFAULT_BACKEND = get_backend("chroma")
_EXPLICIT_BACKEND_ENV = "MEMPALACE_BACKEND_EXPLICIT"

# Schema version for drawer normalization. Bump when the normalization
# pipeline changes in a way that existing drawers should be rebuilt to pick up
# (e.g., new noise-stripping rules). `file_already_mined` treats drawers with
# a missing or stale `normalize_version` as "not mined", so the next mine pass
# silently rebuilds them — users don't need to manually erase + re-mine.
#
# v2 (2026-04): introduced strip_noise() for Claude Code JSONL; previous
#               drawers stored system tags / hook chrome verbatim.
NORMALIZE_VERSION = 2


# (palace_id, collection_name, model_name) tuples already validated this
# process, so the identity check (one metadata read) runs at most once per
# collection per run — keeps the hot get_collection path cheap.
_VALIDATED_IDENTITY: set = set()

_FRAGMENT_DIR = Path(__file__).resolve().parent
# Load order is a dependency sequence, not a catalog: later files use names
# defined earlier (collection before backend, locks before mined-set).
# Do not reorder this tuple.
_FRAGMENTS = (
    "collection.py",
    "backend.py",
    "closets.py",
    "mine_lock.py",
    "palace_lock.py",
    "mined.py",
)


def _exec_fragment(filename: str) -> None:
    path = _FRAGMENT_DIR / filename
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), globals())


for _fragment in _FRAGMENTS:
    _exec_fragment(_fragment)
del _fragment
