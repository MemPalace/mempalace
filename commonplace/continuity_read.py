"""Filer boundaries and read-only views of shared native sessions."""

from __future__ import annotations

import threading
from collections import OrderedDict
from contextlib import contextmanager
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


@dataclass(frozen=True)
class ImportView(View):
    # Complete stored archive-segment ranges, never caller-provided metadata.
    ranges: tuple[tuple[str, int, int], ...] = ()

    def proves(self, source_id, start, end):
        if type(start) is not int or type(end) is not int or start < 0 or end < start:
            return False
        cursor = start
        for lo, hi in sorted((lo, hi) for source, lo, hi in self.ranges if source == source_id):
            if lo <= cursor <= hi:
                cursor = hi + 1
            if cursor > end:
                return True
        return False


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


@contextmanager
def _reader_locks(*locks):
    from .rules import checks

    acquired = []
    try:
        for lock in locks:
            if not lock.acquire(timeout=checks.remaining(1.0)):
                raise TimeoutError("Import view unavailable")
            acquired.append(lock)
        checks.remaining(0)
        yield
        checks.remaining(0)
    finally:
        for lock in reversed(acquired):
            lock.release()


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

    def import_view(self, principal, stored):
        """Resolve only the immutable filer's import, without adoption or writes.

        A holder may grow through capture. Its physical room is never the view:
        only complete archive ranges inside this import's offset prove a mirror.
        """
        from .continuity_schema import MARKER
        from .handoff.recall import ThreadNotFound, _filer_matches

        def missing():
            raise ThreadNotFound("thread not found")

        if (
            stored.workspace_id != principal.workspace_id
            or stored.wing != "private_chats"
            or not stored.source_thread_id.startswith("import-private:")
            or not _filer_matches(principal, stored, reader=True)
        ):
            missing()
        store = self._store_for(stored.workspace_id)
        with _reader_locks(self._db._lock, store._lock):
            row = self._db._conn.execute(
                "SELECT s.*, g.holder_stored_id, g.source, g.user_id AS group_user "
                "FROM sync_streams s JOIN sync_groups g "
                "ON g.group_id=s.group_id AND g.workspace_id=s.workspace_id "
                "WHERE s.workspace_id=? AND s.stream_id=? AND s.own_stored_id=?",
                (stored.workspace_id, stored.source_thread_id, stored.id),
            ).fetchone()
            own_rows = store.messages(stored.id)
            if not own_rows or len(own_rows) != stored.message_count:
                missing()
            if row is None:
                # Markers, missing continuity rows and partially committed holders
                # cannot be mistaken for standalone imported archives.
                controlled = self._db._conn.execute(
                    "SELECT 1 FROM sync_groups WHERE workspace_id=? AND holder_stored_id=? "
                    "UNION ALL SELECT 1 FROM sync_streams WHERE workspace_id=? "
                    "AND (own_stored_id=? OR fork_stored_id=?) LIMIT 1",
                    (stored.workspace_id, stored.id, stored.workspace_id, stored.id, stored.id),
                ).fetchone()
                if (
                    controlled
                    or any(m.name == MARKER for m in own_rows)
                    or not archive_segments(own_rows)
                ):
                    missing()
                return ImportView(tuple(own_rows), ((stored.id, 0, own_rows[-1].seq),))
            if (
                row["kind"] != "import"
                or row["user_id"] != principal.user_id
                or row["group_user"] != principal.user_id
                or type(row["offset"]) is not int
                or row["offset"] < 0
            ):
                missing()
            holder = store.get(row["holder_stored_id"])
            fork = store.get(row["fork_stored_id"]) if row["fork_stored_id"] else None
            if holder is None or row["fork_stored_id"] and fork is None:
                missing()
            rows, ranges = [], []

            def add(thread, start, end):
                if (
                    thread.workspace_id != stored.workspace_id
                    or thread.wing != "private_chats"
                    or not (
                        thread.source_thread_id.startswith("import-private:")
                        and _filer_matches(principal, thread, reader=True)
                        or filing_user(self._db, thread) == principal.user_id
                    )
                ):
                    missing()
                source_rows = store.messages(thread.id)
                segments = archive_segments(source_rows)
                size = sum(len(seg.raw) for seg in segments)
                if not 0 <= start <= end <= size or len(source_rows) != thread.message_count:
                    missing()
                position = 0
                for seg in segments:
                    upper = position + len(seg.raw)
                    if start <= position and upper <= end:
                        ranges.append((thread.id, seg.start_row, seg.end_row))
                    position = upper
                rows.extend(_slice(source_rows, row["source"], start, end))

            offset = row["offset"]
            boundary = row["fork_offset"] if fork else offset
            if type(boundary) is not int or not 0 <= boundary <= offset:
                missing()
            # Legacy archives remain verbatim and must agree with the group's
            # prefix; a missing/inconsistent mapping never expands authority.
            if row["legacy"]:
                old = b"".join(seg.raw for seg in archive_segments(own_rows))
                base = b"".join(seg.raw for seg in archive_segments(store.messages(holder.id)))
                if not base.startswith(old) or len(old) > boundary:
                    missing()
                add(stored, 0, len(old))
                add(holder, len(old), boundary)
            else:
                add(holder, 0, boundary)
            if fork:
                add(fork, 0, offset - boundary)
            if not rows:
                missing()
            return ImportView(tuple(replace(m, seq=i) for i, m in enumerate(rows)), tuple(ranges))

    def imports_for_source(self, principal, source_id, *, limit=100):
        """Bounded point lookups; never scan the account's unrelated streams."""
        with _reader_locks(self._db._lock):
            rows = self._db._conn.execute(
                "SELECT own_stored_id FROM sync_streams INDEXED BY sync_streams_own "
                "WHERE own_stored_id=? AND workspace_id=? AND user_id=? AND kind='import' "
                "UNION ALL "
                "SELECT own_stored_id FROM sync_streams INDEXED BY sync_streams_fork "
                "WHERE fork_stored_id=? AND workspace_id=? AND user_id=? AND kind='import' "
                "UNION ALL "
                "SELECT s.own_stored_id FROM sync_groups AS g INDEXED BY sync_groups_holder "
                "JOIN sync_streams AS s INDEXED BY sync_streams_reader "
                "ON s.workspace_id=g.workspace_id AND s.user_id=g.user_id "
                "AND s.kind='import' AND s.group_id=g.group_id "
                "WHERE g.workspace_id=? AND g.user_id=? AND g.holder_stored_id=? LIMIT ?",
                (
                    source_id,
                    principal.workspace_id,
                    principal.user_id,
                    source_id,
                    principal.workspace_id,
                    principal.user_id,
                    principal.workspace_id,
                    principal.user_id,
                    source_id,
                    limit,
                ),
            ).fetchall()
        return list(dict.fromkeys(row[0] for row in rows))

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


def content_threads(db, ws, stream_id):
    """Return a stream's content holder and fork within its workspace."""
    with db._lock:
        row = db._conn.execute(
            "SELECT g.holder_stored_id, s.fork_stored_id FROM sync_streams s "
            "JOIN sync_groups g ON g.group_id=s.group_id AND g.workspace_id=s.workspace_id "
            "WHERE s.workspace_id=? AND s.stream_id=?",
            (ws, stream_id),
        ).fetchone()
    return list(dict.fromkeys(value for value in row if value)) if row else []
