#!/usr/bin/env python3
"""
file_state.py — Content-aware dedup for mining.

Dedup used to be path-only: once a path had drawers in the palace it was never
looked at again, so an edited file kept its stale drawers forever and a growing
session log never got the new half filed.

This module decides per file whether to skip it, file it for the first time, or
re-file it. It compares mtime first (a cheap stat) and the content hash only
when mtime moved, so an untouched palace still costs one stat per file.
"""

import hashlib
import os
from datetime import datetime

# Read the file in 1 MiB blocks so a multi-hundred-MB session log never lands
# in memory whole.
HASH_BLOCK = 1024 * 1024

# mtime is stored as a float in chroma metadata; filesystems and the JSON
# round-trip both wobble in the last bits, so compare with a tolerance.
MTIME_TOLERANCE = 1e-6

# Decisions returned by decide()
SKIP = "skip"  # unchanged since it was filed
MINE = "mine"  # never filed before
REMINE = "remine"  # filed before, content has changed — drop the old drawers first


def hash_file(path) -> str:
    """sha256 of the raw bytes. Returns None if the file can't be read."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(HASH_BLOCK), b""):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def file_mtime(path) -> float:
    """Modification time as a float. Returns None if the file can't be stat'd."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _stored_state(metadatas: list) -> tuple:
    """Pull (content_hash, source_mtime, filed_at) off the first drawer that has them."""
    content_hash = None
    source_mtime = None
    filed_at = None
    for meta in metadatas or []:
        if not meta:
            continue
        if content_hash is None and meta.get("content_hash"):
            content_hash = meta["content_hash"]
        if source_mtime is None and meta.get("source_mtime") is not None:
            try:
                source_mtime = float(meta["source_mtime"])
            except (TypeError, ValueError):
                pass
        if filed_at is None and meta.get("filed_at"):
            filed_at = meta["filed_at"]
        if content_hash is not None and source_mtime is not None:
            break
    return content_hash, source_mtime, filed_at


def _filed_at_timestamp(filed_at: str) -> float:
    """ISO string from the filed_at metadata → epoch seconds, or None."""
    if not filed_at:
        return None
    try:
        return datetime.fromisoformat(filed_at).timestamp()
    except (TypeError, ValueError):
        return None


def existing_drawers(collection, source_file: str) -> tuple:
    """Every drawer id + metadata already filed for this path."""
    try:
        results = collection.get(where={"source_file": source_file}, include=["metadatas"])
    except Exception:
        return [], []
    return results.get("ids") or [], results.get("metadatas") or []


def stamp_state(collection, ids: list, content_hash: str, mtime: float) -> bool:
    """
    Write hash + mtime onto drawers that already hold the right content.

    Metadata-only update: no document is touched, so nothing is re-embedded.
    Used to backfill drawers filed before hashing existed, and to re-stamp a
    file that was touched but not actually edited.
    """
    if not ids:
        return False
    try:
        results = collection.get(ids=ids, include=["metadatas"])
    except Exception:
        return False
    metas = results.get("metadatas") or []
    got_ids = results.get("ids") or []
    if not got_ids:
        return False
    updated = []
    for meta in metas:
        new_meta = dict(meta or {})
        if content_hash is not None:
            new_meta["content_hash"] = content_hash
        if mtime is not None:
            new_meta["source_mtime"] = float(mtime)
        updated.append(new_meta)
    try:
        collection.update(ids=got_ids, metadatas=updated)
        return True
    except Exception:
        return False


def drop_drawers(collection, source_file: str) -> int:
    """Delete every drawer filed from this path. Returns how many went."""
    ids, _ = existing_drawers(collection, source_file)
    if not ids:
        return 0
    try:
        collection.delete(ids=ids)
    except Exception:
        return 0
    return len(ids)


def decide(collection, source_file: str) -> tuple:
    """
    Decide what to do with one source file.

    Returns (action, content_hash, mtime) where action is SKIP / MINE / REMINE.
    content_hash and mtime are what should be stamped on any drawer filed now,
    so the caller never has to hash the file a second time.
    """
    ids, metadatas = existing_drawers(collection, source_file)
    mtime = file_mtime(source_file)

    if not ids:
        return MINE, hash_file(source_file), mtime

    stored_hash, stored_mtime, filed_at = _stored_state(metadatas)

    # Cheap path: the stamp says the file hasn't moved since we filed it.
    if (
        stored_hash is not None
        and stored_mtime is not None
        and mtime is not None
        and abs(stored_mtime - mtime) < MTIME_TOLERANCE
    ):
        return SKIP, stored_hash, mtime

    current_hash = hash_file(source_file)

    if stored_hash is None:
        # Legacy drawers, filed before this module existed — no hash to compare.
        # filed_at is the only evidence we have: a file untouched since it was
        # filed still holds the content in the palace, so stamp it and move on.
        # Anything modified after filing genuinely has to be re-filed.
        filed_ts = _filed_at_timestamp(filed_at)
        if filed_ts is not None and mtime is not None and mtime <= filed_ts:
            stamp_state(collection, ids, current_hash, mtime)
            return SKIP, current_hash, mtime
        return REMINE, current_hash, mtime

    if current_hash is not None and current_hash == stored_hash:
        # Touched but not edited (a checkout, a copy, a formatter that rewrote
        # identical bytes). Re-stamp the mtime so the next run takes the cheap
        # path, and don't re-embed anything.
        stamp_state(collection, ids, current_hash, mtime)
        return SKIP, current_hash, mtime

    return REMINE, current_hash, mtime
