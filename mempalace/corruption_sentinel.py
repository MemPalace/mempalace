"""
corruption_sentinel.py — Lightweight SQLite-corruption sentinel helpers.

Writes/reads a ``.sqlite_corrupt`` marker file inside the palace directory so
that hot-path code (hooks, MCP server) can detect known-corrupt palaces with a
single ``os.path.exists`` check — avoiding the expensive ``PRAGMA quick_check``
and, crucially, avoiding the ChromaDB Rust client entirely.

All functions are best-effort: OSError is swallowed so a sentinel failure never
blocks the main operation.  Only stdlib is imported so this module is safe to
import from any code path that must not pull in chromadb at module load time.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

CORRUPTION_SENTINEL_NAME = ".sqlite_corrupt"


def corruption_sentinel_path(palace_path: str) -> str:
    """Return the absolute path of the sentinel file for *palace_path*."""
    return os.path.join(palace_path, CORRUPTION_SENTINEL_NAME)


def write_corruption_sentinel(palace_path: str, errors: list[str]) -> None:
    """Atomically write the corruption sentinel file inside *palace_path*.

    Uses a write-to-temp + ``os.replace`` sequence so no partial file is
    visible to concurrent readers.  The JSON body records the ISO timestamp and
    the first five error strings from ``sqlite_integrity_errors``.

    Best-effort: any ``OSError`` is silently swallowed so sentinel writes never
    interrupt the caller.
    """
    sentinel_path = corruption_sentinel_path(palace_path)
    payload = {
        "detected_at": datetime.now(tz=timezone.utc).isoformat(),
        "errors": errors[:5],
    }
    try:
        os.makedirs(palace_path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=palace_path, prefix=".sqlite_corrupt_tmp_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        os.replace(tmp_path, sentinel_path)
    except OSError:
        pass


def read_corruption_sentinel(palace_path: str) -> Optional[dict]:
    """Return the parsed sentinel dict, or ``None`` if the file is absent.

    Cheap: a single ``os.path.exists`` / ``open`` — safe on the hot hook path.
    Malformed JSON (e.g. from a partial write before atomic rename was added)
    is tolerated by returning ``{"errors": []}``.
    """
    sentinel_path = corruption_sentinel_path(palace_path)
    if not os.path.exists(sentinel_path):
        return None
    try:
        with open(sentinel_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"errors": []}


def clear_corruption_sentinel(palace_path: str) -> None:
    """Remove the corruption sentinel file if it exists.

    Best-effort: ``OSError`` and ``FileNotFoundError`` are silently swallowed.
    """
    sentinel_path = corruption_sentinel_path(palace_path)
    try:
        os.unlink(sentinel_path)
    except OSError:
        pass
