"""Read stored threads: recall, ranges and the chain. Owner: part T09.

Access: ordinary principals read a stored thread only if they have ``threads:read``,
the thread is in its workspace, and its policy can read the thread's wing
(``THREADS_WING`` unless the store says otherwise). Anything else looks
exactly like a missing thread (``ThreadNotFound``), never "forbidden".
Separately consented imported-history readers receive only a bounded import
view after live connection, ownership, workspace and filer checks.

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


def _filer_matches(principal, stored: StoredThread, *, reader=False) -> bool:
    if not stored.source_thread_id.startswith(_GRANT_PREFIX):
        return True
    parts = stored.source_thread_id.split(":", 3)
    if len(parts) == 3 and not reader:
        # Legacy owner reads retain their original filer binding; no storage rewrite.
        return parts[1] != "" and parts[1] == principal.user_id
    return len(parts) == 4 and all(parts) and parts[1] == principal.user_id


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
    def __init__(
        self,
        store_for: Callable,
        *,
        call_tool: Optional[Callable] = None,
        private=None,
        import_eligible=None,
    ):
        """``store_for(workspace_id) -> ThreadStore``."""
        self._store_for = store_for
        self._call_tool = call_tool
        self._private = private
        self._import_eligible = import_eligible

    def import_reader(self, principal):
        from commonplace.import_grant import READER_SCOPE

        try:
            return bool(
                not principal.person
                and principal.auth in ("pat", "oauth")
                and principal.policy.has_scope(READER_SCOPE)
                and principal.policy.has_scope("memory:read")
                and self._import_eligible is not None
                and self._import_eligible(principal)
            )
        except Exception:
            return False

    def _import_view(self, principal, stored):
        if stored is None:
            raise ThreadNotFound("thread not found")
        if not (
            self.import_reader(principal)
            and stored.workspace_id == principal.workspace_id
            and stored.wing == "private_chats"
            and stored.source_thread_id.startswith(_GRANT_PREFIX)
            and _filer_matches(principal, stored, reader=True)
            and self._private is not None
        ):
            raise ThreadNotFound("thread not found")
        try:
            current = self._store_for(principal.workspace_id).get(stored.id)
            return self._private.import_view(principal, current)
        except Exception:
            raise ThreadNotFound("thread not found") from None

    def _uses_import_view(self, principal, stored):
        return (
            stored.source_thread_id.startswith(_GRANT_PREFIX)
            and not principal.person
            and principal.policy.has_scope("memory:import:read")
        )

    def can_read(self, principal, stored_id: str, *, strict: bool = False) -> bool:
        """Keep ordinary denial semantics; owner progress needs lookup faults to propagate."""
        if not isinstance(stored_id, str) or _THREAD_ID.fullmatch(stored_id) is None:
            return False
        if not principal.policy.has_scope("threads:read") and not self.import_reader(principal):
            return False
        try:
            stored = self._store_for(principal.workspace_id).get(stored_id)
            if stored is None:
                return False
            if self._uses_import_view(principal, stored):
                self._import_view(principal, stored)
                return True
            return bool(
                _readable(principal, stored)
                and (self._private is None or self._private.readable(principal, stored))
            )
        except Exception:
            if strict:
                raise
            return False

    def thread(self, principal, stored_thread_id: str) -> StoredThread:
        if not isinstance(stored_thread_id, str) or _THREAD_ID.fullmatch(stored_thread_id) is None:
            raise ThreadNotFound("thread not found")
        if not principal.policy.has_scope("threads:read") and not self.import_reader(principal):
            raise ThreadNotFound("thread not found")
        stored = self._store_for(principal.workspace_id).get(stored_thread_id)
        if stored is None:
            raise ThreadNotFound("thread not found")
        if self._uses_import_view(principal, stored):
            view = self._import_view(principal, stored)
            from .thread_store import canonical_sha256

            return replace(
                stored,
                message_count=view.count,
                chunk_count=0,
                sha256=canonical_sha256(view.messages()),
            )
        if not _readable(principal, stored):
            raise ThreadNotFound("thread not found")
        if self._private is not None:
            if not self._private.readable(principal, stored):
                raise ThreadNotFound("thread not found")
            view = self._private.view(stored)
            if view is not None:
                stored = replace(stored, message_count=view.count)
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
        # Reader hits are generated from the authorized view. Never normalize a
        # holder's FTS or palace hits merely because its room is readable.
        if self._uses_import_view(principal, threads[0]):
            return self._view_hits(principal, threads, query, limit)
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
        if self._uses_import_view(principal, stored):
            return self._import_view(principal, stored).messages(start, end)
        if self._private is not None:
            view = self._private.view(stored)
            if view is not None:
                return view.messages(start, end)
        return self._store_for(principal.workspace_id).messages(stored.id, start, end)

    def _view_hits(self, principal, threads, query, limit):
        from .thread_store import chunk_messages

        hits = []
        terms = query.casefold().split()
        for stored in threads:
            view = self._import_view(principal, stored)
            for chunk in chunk_messages(view.messages()):
                if all(term in chunk.text.casefold() for term in terms):
                    hits.append(
                        RecallHit(
                            stored.id, chunk.index, chunk.seq_start, chunk.seq_end, 1.0, chunk.text
                        )
                    )
        return hits[:limit]

    def import_candidates(self, principal, query, *, limit=8):
        """Bounded indexed candidates, followed by exact-view matching and authorization."""
        from commonplace.rules import checks

        if not self.import_reader(principal) or not query.strip():
            return []
        checks.remaining(0)
        store = self._store_for(principal.workspace_id)
        source_ids = store.import_source_candidates(principal.workspace_id, query, limit=100)
        ids = set(source_ids)
        for source_id in source_ids:
            checks.remaining(0)
            ids.update(self._private.imports_for_source(principal, source_id))
        found = []
        for stored_id in sorted(ids):
            checks.remaining(0)
            stored = store.get(stored_id)
            try:
                view = self._import_view(principal, stored)
            except (ThreadNotFound, AttributeError):
                continue
            if not all(
                term in "\n".join(m.text for m in view.messages()).casefold()
                for term in query.casefold().split()
            ):
                continue
            found.append(self._view_drawer(stored, view))
            if len(found) >= limit:
                break
        return found

    @staticmethod
    def _view_drawer(stored, view):
        from .thread_store import canonical_sha256

        digest = canonical_sha256(view.messages())
        # A virtual drawer always re-resolves live authority and the exact view.
        return {
            "drawer_id": f"imported:{stored.id}:{digest}",
            "wing": "private_chats",
            "room": stored.id,
            "content": "\n".join(m.text for m in view.messages() if m.text),
            "text": "\n".join(m.text for m in view.messages() if m.text),
            "metadata": {"source_thread": stored.id, "import_view_sha256": digest},
        }

    def import_drawer(self, principal, drawer_id):
        match = re.fullmatch(r"imported:(thr_[0-9a-f]+):([0-9a-f]{64})", drawer_id or "")
        if match is None:
            raise ThreadNotFound("thread not found")
        stored = self._store_for(principal.workspace_id).get(match[1])
        view = self._import_view(principal, stored)
        record = self._view_drawer(stored, view)
        if record["drawer_id"] != drawer_id:
            raise ThreadNotFound("thread not found")
        return record

    def proves_import(self, principal, source_id, start, end):
        """Source-room authority alone never proves a shared holder's range."""
        if not self.import_reader(principal) or not isinstance(source_id, str):
            return False
        store = self._store_for(principal.workspace_id)
        ids = {source_id, *self._private.imports_for_source(principal, source_id)}
        for stored_id in ids:
            stored = store.get(stored_id)
            if stored is None:
                continue
            try:
                if self._import_view(principal, stored).proves(source_id, start, end):
                    return True
            except ThreadNotFound:
                continue
        return False

    def import_record_allowed(self, principal, node):
        """Reconstruct the internal mirror unit, verifying source, range, id and words."""
        from mempalace.ids import make_mirror_unit_id

        from .palace_mirror import class_units

        if not self.import_reader(principal):
            return False
        drawer_id = node.get("drawer_id")
        if isinstance(drawer_id, str) and drawer_id.startswith("imported:"):
            expected = self.import_drawer(principal, drawer_id)
            return all(node.get(k) == expected[k] for k in ("wing", "room")) and all(
                node[k] == expected["content"] for k in ("content", "text") if k in node
            )
        metadata = node.get("metadata") or {}
        source = node.get("source_path") or node.get("source_file") or metadata.get("source_file")
        if not isinstance(source, str):
            return False
        match = re.fullmatch(
            r"handoff:(thr_[0-9a-f]+)#v2:([0-9]+)-([0-9]+):(conversation|tool_call|tool_output|system):([0-9]+)",
            source,
        )
        if not match:
            return False
        source_id, lo, hi, kind, part = match.groups()
        lo, hi, part = int(lo), int(hi), int(part)
        if not self.proves_import(principal, source_id, lo, hi):
            return False
        if node.get("room") != source_id or node.get("wing") != "private_threads":
            return False
        store = self._store_for(principal.workspace_id)
        rows = store.messages(source_id, lo, hi)
        if not rows or rows[0].seq != lo or rows[-1].seq != hi:
            return False
        for unit in class_units(rows):
            if (
                unit.message_class != kind
                or unit.index != part
                or unit.seq_start != lo
                or unit.seq_end != hi
            ):
                continue
            expected_id = make_mirror_unit_id(source_id, lo, hi, kind, unit.text)
            if drawer_id != expected_id:
                continue
            words = [node[k] for k in ("content", "text") if k in node]
            if not words or any(value != unit.text for value in words):
                continue
            return True
        return False

    def ordinary_record(self, principal, source_id):
        """Whether ordinary authority already permits this whole stored source."""
        if not isinstance(source_id, str) or _THREAD_ID.fullmatch(source_id) is None:
            return False
        stored = self._store_for(principal.workspace_id).get(source_id)
        return bool(
            stored is not None
            and not self._uses_import_view(principal, stored)
            and _readable(principal, stored)
            and (self._private is None or self._private.readable(principal, stored))
        )

    def import_listing(self, principal, *, limit=20, offset=0, room=None):
        if not self.import_reader(principal):
            return []
        store = self._store_for(principal.workspace_id)
        # Filter and authorize before pagination. A room lookup must not depend
        # on where that room falls in the account's bounded candidate window.
        rows = (
            [store.get(room)]
            if room is not None
            else store.list_source_range(
                principal.workspace_id,
                f"import-private:{principal.user_id}:",
                limit=100,
            )
        )
        results = []
        for stored in rows:
            try:
                results.append(self._view_drawer(stored, self._import_view(principal, stored)))
            except ThreadNotFound:
                continue
        return results[offset : offset + limit]
