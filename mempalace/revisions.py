"""Append-only prior text for drawer updates.

The write-ahead log redacts content. This log lives in the palace directory,
the same privacy domain as the drawers, and keeps the verbatim document
``update_drawer`` is about to replace, and the conversation and diary text a
re-mine is about to replace or remove (``write_file_drawers``). Nothing caps
the file. Deleting a drawer removes its revisions too, except the re-mine
history of its position, which goes when its source is deleted
(``purge_drawer_revisions``).
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from uuid import uuid4

from .fsio import atomic_write_bytes
from .stored_text import dumps_internal
from .timeutil import utc_now_iso

_REVISION_FILENAME = "drawer_revisions.jsonl"


def revisions_path(palace_path: str) -> Path:
    return Path(palace_path) / _REVISION_FILENAME


def append_drawer_revision(
    palace_path: str,
    drawer_id: str,
    content: str,
    metadata: dict | None = None,
) -> str:
    """Append one prior drawer and return its revision id.

    Raises ``OSError`` when the palace directory cannot take the append.
    Callers that are about to overwrite a drawer must not continue after that.
    """
    return append_drawer_revisions(palace_path, [(drawer_id, content, metadata)])[0]


def append_drawer_revisions(palace_path: str, revisions) -> list[str]:
    """Append prior drawers ``(drawer_id, content, metadata)`` in one fsync.

    Every line goes out in one locked append and one ``fsync``, so a re-mine
    that is about to replace many drawers pays for one sync, not one per row.
    Returns the revision ids in order. Raises ``OSError`` when the palace
    directory cannot take the append; a failure can leave the earlier lines
    of the batch written, and callers must not overwrite anything after it.
    """
    if not palace_path:
        raise OSError("no palace path for drawer revision")
    revisions = list(revisions)
    if not revisions:
        return []
    path = revisions_path(palace_path)
    # A remote backend keeps no drawers on disk, so the directory may not exist yet.
    path.parent.mkdir(parents=True, exist_ok=True)
    revision_ids = []
    lines = []
    recorded_at = utc_now_iso()
    for drawer_id, content, metadata in revisions:
        revision_id = uuid4().hex
        revision_ids.append(revision_id)
        record = {
            "revision_id": revision_id,
            "drawer_id": drawer_id,
            "content": content,
            "metadata": metadata or {},
            "recorded_at": recorded_at,
        }
        lines.append(dumps_internal(record, default=str) + "\n")
    payload = "".join(lines)
    with _revisions_lock(path):
        if _ends_mid_line(path):
            payload = "\n" + payload  # a crash tore the last append; start a fresh line
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(str(path), flags, 0o600)
        try:
            remaining = memoryview(payload.encode("utf-8"))
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, "drawer revision write made no progress")
                remaining = remaining[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    return revision_ids


def _revisions_lock(path: Path):
    """The cross-process lock every append and purge of ``path`` holds.

    A purge rewrites the whole file, so an append landing between its read and
    its ``os.replace`` would be lost without it.
    """
    from .palace import mine_lock

    return mine_lock(os.path.abspath(str(path)))


def _ends_mid_line(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except OSError:
        return False


def load_drawer_revisions(palace_path: str, drawer_id: str | None = None) -> list[dict]:
    """Return stored revisions, oldest first. Optionally one drawer's only.

    A line torn by a crash mid-append is skipped, not fatal.
    """
    path = revisions_path(palace_path)
    if not path.is_file():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and (drawer_id is None or row.get("drawer_id") == drawer_id):
                rows.append(row)
    return rows


REMINE_REASONS = frozenset({"remine_overwrite", "remine_prune"})


def is_remine_revision(row: dict) -> bool:
    """Whether a journaled revision is text a re-mine replaced or dropped."""
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return meta.get("revision_reason") in REMINE_REASONS


def count_source_revisions(palace_path: str, source_file: str) -> int:
    """How many journaled revisions record ``source_file`` (what deleting it would purge)."""
    if not palace_path:
        return 0
    return sum(
        1
        for row in load_drawer_revisions(palace_path)
        if isinstance(row.get("metadata"), dict)
        and row["metadata"].get("source_file") == source_file
    )


def purge_drawer_revisions(
    palace_path: str,
    drawer_ids=(),
    source_file: str | None = None,
) -> int:
    """Remove the revisions of deleted drawers and return how many went.

    A revision matches when its ``drawer_id`` is in ``drawer_ids`` or its
    recorded ``source_file`` equals ``source_file``. A re-mine revision
    (:func:`is_remine_revision`) matches by ``source_file`` only: its id is a
    position the current drawer reuses, not that drawer. The file is rewritten
    through ``fsio.atomic_write_bytes``, so a crash leaves either the old
    or the new file. Lines a crash tore are unreadable, so they are dropped
    too. The file is removed once nothing is left.
    """
    if not palace_path:
        return 0
    path = revisions_path(palace_path)
    if not path.exists():
        return 0
    with _revisions_lock(path):
        return _purge_locked(path, set(drawer_ids), source_file)


def _purge_locked(path: Path, ids: set, source_file: str | None) -> int:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return 0
    kept: list[bytes] = []
    removed = 0
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        # A re-mine revision holds text an earlier version of the source kept
        # at a positional id the current drawer reuses; deleting the current
        # drawer is not a decision about that text. Deleting the source is.
        by_id = row.get("drawer_id") in ids and not is_remine_revision(row)
        if by_id or (source_file is not None and meta.get("source_file") == source_file):
            removed += 1
        else:
            kept.append(line)
    if not removed:
        return 0
    if not kept:
        path.unlink()
        return removed
    atomic_write_bytes(path, b"\n".join(kept) + b"\n")
    return removed
