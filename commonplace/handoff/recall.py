"""Read stored threads: recall, ranges and the chain. Owner: part T09.

Access: a principal reads a stored thread only if it has ``threads:read``,
the thread is in its workspace, and its policy can read the thread's wing
(``THREADS_WING`` unless the store says otherwise). Anything else looks
exactly like a missing thread (``ThreadNotFound``), never "forbidden".

``recall`` merges FTS (``ThreadStore.search``) with the palace mirror's
semantic hits when a ``call_tool`` is given, dedupes by (thread, chunk), and
with ``follow_chain`` also searches the older threads of the chain.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Optional

from .models import RecallHit, StoredThread, ThreadMessage

MAX_RANGE = 200  # messages per thread_get
_GRANT_PREFIX = "import-private:"
_THREAD_ID = re.compile(r"thr_[0-9a-fA-F]+")
_logger = logging.getLogger(__name__)


class ThreadNotFound(LookupError):
    pass


def thread_provenance(stored: StoredThread) -> dict:
    """A filing actor does not verify any transcript's deciding speakers."""
    from mempalace.fact_provenance import source_link

    return {
        "origin_kind": "thread",
        "origin_id": stored.source_thread_id,
        "origin_link": source_link(stored.id),
        "filed_by": stored.created_by or "unknown",
        "approval_status": "proposal" if stored.created_by else "unknown",
    }


def _filer_matches(principal, stored: StoredThread) -> bool:
    if not stored.source_thread_id.startswith(_GRANT_PREFIX):
        return True
    parts = stored.source_thread_id.split(":", 2)
    return len(parts) == 3 and parts[1] != "" and parts[1] == principal.user_id


def _readable(principal, stored: StoredThread) -> bool:
    return (
        principal.policy.has_scope("threads:read")
        and stored.workspace_id == principal.workspace_id
        and principal.policy.can_read_wing(stored.wing)
        and _filer_matches(principal, stored)
    )


def readable(principal, stored: StoredThread) -> bool:
    """Apply the read policy shared by recall and message ranges."""
    return _readable(principal, stored)


def _normalise(hits: list[RecallHit], ids: set[str]) -> list[RecallHit]:
    """Min-max scale each source independently; higher input scores are better.

    A source with one distinct score gives every hit 1.0. Ignore out-of-scope
    hits and non-finite scores before scaling. Text and range fields stay verbatim.
    """
    hits = [hit for hit in hits if hit.stored_thread_id in ids and math.isfinite(hit.score)]
    if not hits:
        return []
    low = min(hit.score for hit in hits)
    high = max(hit.score for hit in hits)
    # Halving avoids overflow when finite scores span almost the full float range.
    divisor = 2 if math.isinf(high - low) else 1
    low /= divisor
    span = high / divisor - low
    return [
        replace(
            hit,
            score=(hit.score / divisor - low) / span if span else 1.0,
        )
        for hit in hits
    ]


def _created_at(stored: StoredThread) -> float:
    try:
        value = datetime.fromisoformat(stored.created_at.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except (ValueError, OverflowError, OSError):
        return -math.inf


class Recall:
    def __init__(self, store_for: Callable, *, call_tool: Optional[Callable] = None):
        """``store_for(workspace_id) -> ThreadStore``."""
        self._store_for = store_for
        self._call_tool = call_tool

    def thread(self, principal, stored_thread_id: str) -> StoredThread:
        if (
            not isinstance(stored_thread_id, str)
            or _THREAD_ID.fullmatch(stored_thread_id) is None
            or not principal.policy.has_scope("threads:read")
        ):
            raise ThreadNotFound("thread not found")
        stored = self._store_for(principal.workspace_id).get(stored_thread_id)
        if stored is None or not _readable(principal, stored):
            raise ThreadNotFound("thread not found")
        return stored

    def chain(self, principal, stored_thread_id: str) -> list[StoredThread]:
        """This thread then its predecessors, newest first; stops at the
        first one the principal can't read; cycle-safe, at most 50 threads."""
        current = self.thread(principal, stored_thread_id)
        store = self._store_for(principal.workspace_id)
        threads = []
        seen = set()
        while len(threads) < 50:
            threads.append(current)
            seen.add(current.id)
            if len(threads) == 50:
                break
            parent_id = store.parent_of(current.id)
            if not parent_id or parent_id in seen:
                break
            try:
                current = self.thread(principal, parent_id)
            except ThreadNotFound:
                break
        return threads

    def recall(
        self,
        principal,
        stored_thread_id: str,
        query: str,
        *,
        limit: int = 8,
        follow_chain: bool = True,
    ) -> list[RecallHit]:
        """Return one ranked excerpt per overlapping message range.

        For oversized messages, use ``get`` on that range for every piece;
        the selected excerpt need not contain every query match.
        """
        query = query.strip()
        if not query:
            self.thread(principal, stored_thread_id)
            return []
        limit = max(1, min(limit, 50))
        threads = (
            self.chain(principal, stored_thread_id)
            if follow_chain
            else [self.thread(principal, stored_thread_id)]
        )
        ids = [stored.id for stored in threads]
        allowed = set(ids)
        store = self._store_for(principal.workspace_id)
        hits = _normalise(store.search(ids, query, limit=limit), allowed)
        if self._call_tool is not None:
            try:
                from .palace_mirror import palace_search

                hits.extend(
                    _normalise(
                        palace_search(self._call_tool, principal, ids, query, limit), allowed
                    )
                )
            except Exception:
                _logger.warning("Memory search unavailable during handoff recall")

        created = {stored.id: _created_at(stored) for stored in threads}
        order = {stored.id: index for index, stored in enumerate(threads)}
        ranked = sorted(
            hits,
            key=lambda hit: (
                -hit.score,
                -created[hit.stored_thread_id],
                order[hit.stored_thread_id],
                hit.chunk_index,
            ),
        )
        unique = []
        for hit in ranked:
            if not any(
                hit.stored_thread_id == previous.stored_thread_id
                and hit.seq_start <= previous.seq_end
                and previous.seq_start <= hit.seq_end
                for previous in unique
            ):
                unique.append(hit)
        return unique[:limit]

    def get(
        self, principal, stored_thread_id: str, start: int = 0, end: Optional[int] = None
    ) -> list[ThreadMessage]:
        """At most ``MAX_RANGE`` messages."""
        stored = self.thread(principal, stored_thread_id)
        start = max(0, start)
        end = min(end, start + MAX_RANGE - 1) if end is not None else start + MAX_RANGE - 1
        if end < start:
            return []
        return self._store_for(principal.workspace_id).messages(stored.id, start, end)
