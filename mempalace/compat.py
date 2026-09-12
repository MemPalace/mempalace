#!/usr/bin/env python3
"""Compatibility and safety checks for local palace access."""

from __future__ import annotations

import json
from pathlib import Path

import chromadb

from .version import __version__ as mempalace_version

META_FILE = "mempalace_meta.json"


def chromadb_version() -> str:
    return getattr(chromadb, "__version__", "unknown")


def chromadb_major() -> int | None:
    version = chromadb_version().split(".", 1)[0]
    try:
        return int(version)
    except (TypeError, ValueError):
        return None


def meta_path(palace_path: str) -> Path:
    return Path(palace_path).expanduser().resolve() / META_FILE


def write_palace_metadata(palace_path: str) -> None:
    path = meta_path(palace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mempalace_version": mempalace_version,
                "chromadb_version": chromadb_version(),
                "chromadb_major": chromadb_major(),
            },
            indent=2,
        )
        + "\n"
    )


def read_palace_metadata(palace_path: str) -> dict | None:
    path = meta_path(palace_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def ensure_palace_safe(palace_path: str) -> None:
    current_major = chromadb_major()
    meta = read_palace_metadata(palace_path)

    if meta is None:
        if current_major is not None and current_major >= 1:
            raise RuntimeError(
                "Refusing to open palace without compatibility metadata under Chroma 1.x. "
                "Use a tested Chroma <1 environment or rebuild the palace with this version of MemPalace."
            )
        return

    recorded_major = meta.get("chromadb_major")
    if recorded_major is not None and current_major is not None and recorded_major != current_major:
        raise RuntimeError(
            f"Palace was created with Chroma major {recorded_major}, but current environment has {current_major}. "
            "Refusing to proceed to avoid index corruption or segfaults. Rebuild the palace with a compatible version."
        )
