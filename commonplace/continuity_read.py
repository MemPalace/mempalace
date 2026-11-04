"""Filer boundaries and read-only views of shared native sessions."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, replace

from .continuity_schema import archive_segments
from .handoff.models import ThreadMessage
from .handoff.transcripts import parse


def filing_user(db, stored):
    """Resolve a capture's filer, including a revoked filing connection."""
    parts = stored.source_thread_id.split(":", 2)
    if len(parts) != 3 or not parts[2]:
        return None
    namespace, actor, _ = parts
    if namespace in ("capture", "capture-fork") and actor.startswith("usr_"):
        return actor
    if namespace == "capture" and actor.startswith("conn_"):
        with db._lock:
            row = db._conn.execute(
                "SELECT user_id FROM connections WHERE id=?", (actor,)
            ).fetchone()
        return row["user_id"] if row else None
    return None


@dataclass(frozen=True)
class View:
    """An immutable, densely numbered message range, never written to storage."""

    _rows: tuple[ThreadMessage, ...]

    @property
    def count(self):
        return len(self._rows)

    def messages(self, start=0, end=None):
        if start < 0 or end is not None and end < start:
            return []
        return list(self._rows[start : end + 1 if end is not None else None])


def _slice(rows, source, start, end):
    """Keep whole archive segments; reparse only pieces cut at byte boundaries."""
    result = []
    position = 0
    for segment in archive_segments(rows):
        upper = position + len(segment.raw)
        lo, hi = max(start, position), min(end, upper)
        if hi > lo:
            if lo == position and hi == upper:
                result.extend(rows[segment.start_row : segment.end_row + 1])
            else:
                text = segment.raw[lo - position : hi - position].decode("utf-8")
                result.append(
                    ThreadMessage(0, "system", "other", "", text, name="capture.raw", at=segment.at)
                )
                result.extend(parse(text, source, require_recognized=False))
        position = upper
        if position >= end:
            break
    return result


class PrivateReader:
    def __init__(self, db, store_for):
        self._db = db
        self._store_for = store_for
        self._cache = OrderedDict()
        self._lock = threading.Lock()

    def readable(self, principal, stored):
        if stored.wing != "private_chats":
            return True
        filer = filing_user(self._db, stored)
        return filer == principal.user_id if filer is not None else principal.role == "owner"

    def view(self, stored) -> View | None:
        if stored.wing != "private_chats" or not stored.message_count:
            return None
        with self._db._lock:
            row = self._db._conn.execute(
                "SELECT s.*, g.holder_stored_id, g.source FROM sync_streams s "
                "JOIN sync_groups g ON g.group_id=s.group_id AND g.workspace_id=s.workspace_id "
                "WHERE s.workspace_id=? AND (s.own_stored_id=? OR s.fork_stored_id=?)",
                (stored.workspace_id, stored.id, stored.id),
            ).fetchone()
        if row is None or stored.id == row["holder_stored_id"]:
            return None
        store = self._store_for(stored.workspace_id)
        holder = store.get(row["holder_stored_id"])
        if holder is None or holder.workspace_id != stored.workspace_id:
            return None
        own = store.get(row["own_stored_id"])
        fork = store.get(row["fork_stored_id"]) if row["fork_stored_id"] else None
        key = (
            stored.workspace_id,
            stored.id,
            row["offset"],
            row["fork_offset"],
            row["legacy"],
            *((t.id, t.sha256, t.message_count) if t else None for t in (holder, own, fork)),
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        holder_rows = store.messages(holder.id)
        if row["legacy"] and stored.id == row["own_stored_id"]:
            rows = store.messages(stored.id)
            start = sum(len(s.raw) for s in archive_segments(rows))
            if row["offset"] <= start:
                return None
            end = row["fork_offset"] if fork else row["offset"]
            rows += _slice(holder_rows, row["source"], start, end)
            if fork:
                rows += store.messages(fork.id)
        elif fork:
            rows = _slice(holder_rows, row["source"], 0, row["fork_offset"])
            rows += store.messages(fork.id)
        else:
            rows = holder_rows
        view = View(tuple(replace(message, seq=i) for i, message in enumerate(rows)))
        with self._lock:
            self._cache[key] = view
            self._cache.move_to_end(key)
            while len(self._cache) > 16:
                self._cache.popitem(last=False)
        return view


def listed_messages(db, row) -> int | None:
    """Read the import's submission count without opening its holder."""
    with db._lock:
        stream = db._conn.execute(
            "SELECT listed_messages, legacy, stream_id FROM sync_streams "
            "WHERE workspace_id=? AND own_stored_id=? AND kind='import'",
            (row.workspace_id, row.id),
        ).fetchone()
        if stream and stream["legacy"]:
            marked = db._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_import_submissions'"
            ).fetchone()
            if (
                not marked
                or not db._conn.execute(
                    "SELECT 1 FROM sync_import_submissions WHERE workspace_id=? AND stream_id=?",
                    (row.workspace_id, stream["stream_id"]),
                ).fetchone()
            ):
                return None
    return stream["listed_messages"] if stream else None


def sessions(db, store, ws, user_id):
    """List only the caller's groups, with sizes from stored archive bytes."""
    with db._lock:
        groups = db._conn.execute(
            "SELECT g.source, g.native_id, g.holder_stored_id, COUNT(s.stream_id) AS streams, "
            "COUNT(DISTINCT s.fork_stored_id) AS forks, COALESCE(SUM(s.legacy),0) AS legacy "
            "FROM sync_groups g LEFT JOIN sync_streams s "
            "ON s.group_id=g.group_id AND s.workspace_id=g.workspace_id AND s.user_id=g.user_id "
            "WHERE g.workspace_id=? AND g.user_id=? "
            "GROUP BY g.group_id ORDER BY g.source, g.native_id, g.group_id",
            (ws, user_id),
        ).fetchall()
    return [
        dict(
            group,
            bytes=sum(
                len(segment.raw)
                for segment in archive_segments(store.messages(group["holder_stored_id"]))
            ),
        )
        for group in groups
    ]
