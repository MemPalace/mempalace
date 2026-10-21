#!/usr/bin/env python3
"""
layers.py — 4-Layer Memory Stack for mempalace
===================================================

Load only what you need, when you need it.

    Layer 0: Identity       (~100 tokens)   — Always loaded. "Who am I?"
    Layer 1: Essential Story (~500-800)      — Always loaded. Top moments from the palace.
    Layer 2: On-Demand      (~200-500 each)  — Loaded when a topic/wing comes up.
    Layer 3: Deep Search    (unlimited)      — Full semantic search.

Wake-up cost: ~600-900 tokens (L0+L1). Leaves 95%+ of context free.

Reads through the configured storage backend (ChromaDB default, pluggable)
via ``palace.get_collection`` and ~/.mempalace/identity.txt. This stack never
writes, and every open asks for ``read_only=True``; see ``_open_for_read`` for
which backends honour that.
"""

import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

from . import paths
from .config import MempalaceConfig
from .metadata_types import is_bookkeeping
from .search_util import (
    _distance_to_similarity,
    _first_or_empty,
    _metric_for_collection,
    build_where_filter,
)
from .timeutil import recency_key


def _get_collection(*args, **kwargs):
    """Open a collection. The palace package is imported on first use."""
    from .palace import get_collection

    return get_collection(*args, **kwargs)


def _open_for_read(palace_path: str):
    """Open the drawers collection for a pure read.

    The whole stack below only reads, so it asks for ``read_only=True``. On
    ``sqlite_exact`` a writable open takes the palace mine lock to initialise its
    schema, and every one of these call sites runs while some other MemPalace
    process may legitimately hold that lock — the hub, a daemon, a long mine.
    The failure was silent and actively misleading: the ``except Exception``
    around each call turned the lock conflict into "No palace found. Run:
    mempalace mine <dir>", which invited a re-mine of a perfectly healthy palace.

    ``read_only`` is a request, not a guarantee. Backends that support it open
    without schema initialization, migrations, or metadata writes, and take no
    lock. ChromaDB ignores it; its collection open takes no mine lock either,
    though its client still writes ``chroma.sqlite3``.
    """
    return _get_collection(palace_path, create=False, read_only=True)


def _read_open_failure(exc: Exception) -> str:
    """Explain a failed read-open honestly instead of blaming a missing palace.

    A genuinely absent palace keeps the historical wording — callers and tests
    match on it. Only a lock conflict, which the old wording misreported as a
    missing palace (inviting a pointless re-mine), gets its own message.
    """
    from .palace import MineAlreadyRunning

    if isinstance(exc, MineAlreadyRunning):
        return (
            "## Palace is busy — another MemPalace process holds the write lock.\n"
            f"{exc}\n"
            "This backend could not open the palace without that lock; "
            "stop that process or wait for it to finish."
        )
    return "No palace found. Run: mempalace mine <dir>"


# Wall-clock budget for the whole recent-memories fetch, retries and read
# time included. Wake-up runs inside hooks with a 500 ms limit.
_RECENT_READ_BUDGET = 0.3
_READ_ATTEMPTS = 3
_READ_RETRY_DELAY = 0.05


def _capability_missing():
    from .backends.base import UnsupportedCapabilityError

    return (NotImplementedError, TypeError, UnsupportedCapabilityError)


def _palace_missing():
    from .backends.base import PalaceNotFoundError

    return PalaceNotFoundError


# The one reader thread a budgeted fetch may have in flight. A read that
# overran its deadline keeps running; until it ends, later fetches fail fast
# instead of piling up threads on a hung backend.
_inflight_reader: "threading.Thread | None" = None
_inflight_guard = threading.Lock()


def _read_before(deadline, read):
    """``read()``, but stop waiting for it at ``deadline`` (``None``: inline).

    A read blocked in SQLite's busy timeout cannot be interrupted, so a
    budgeted read runs on a daemon thread that is abandoned if it overruns; its
    result is discarded. Only one such thread runs per process.
    """
    global _inflight_reader
    if deadline is None:
        return read()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("the palace read did not finish within the wake-up budget")
    box = {}

    def run():
        try:
            box["value"] = read()
        except BaseException as exc:  # handed back to the caller below
            box["error"] = exc

    with _inflight_guard:
        if _inflight_reader is not None and _inflight_reader.is_alive():
            raise TimeoutError("an earlier palace read is still running")
        worker = threading.Thread(target=run, name="mempalace-recent-read", daemon=True)
        _inflight_reader = worker
        worker.start()
    worker.join(remaining)
    if worker.is_alive():
        raise TimeoutError("the palace read did not finish within the wake-up budget")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _read_with_retry(read, deadline, no_retry: tuple):
    """``read()``, retried briefly while ``deadline`` allows, then re-raised.

    Errors in ``no_retry`` (a missing capability or palace) and a timed-out
    read are not retried; they will not change on a second try.
    """
    for attempt in range(_READ_ATTEMPTS):
        try:
            return _read_before(deadline, read)
        except (TimeoutError,) + no_retry:
            raise
        except Exception:
            remaining = _READ_RETRY_DELAY if deadline is None else deadline - time.monotonic()
            if attempt == _READ_ATTEMPTS - 1 or remaining <= 0:
                raise
            time.sleep(min(_READ_RETRY_DELAY, remaining))


def fetch_recent_window(
    col, *, wing=None, max_scan: int = 2000, budget=_RECENT_READ_BUDGET
) -> tuple[list, list]:
    """``(documents, metadatas)`` for the ``max_scan`` most recently filed drawers.

    Uses the backend's ``get_recent`` capability, which pushes ``ORDER BY
    filed_at DESC LIMIT n`` into storage where the backend can and otherwise
    falls back to the scan-then-sort default in ``BaseCollection``. A collection
    without the capability is paged through ``get`` instead.

    The whole fetch fits in ``budget`` seconds. A read that fails is retried
    within it and then raised: a palace that cannot be read is not an empty
    one, and a scan cut short is not the whole window. A palace or collection
    that does not exist yet is not a read failure: it has no recent drawers.
    ``budget=None`` reads inline on the calling thread with no deadline, for a
    caller that holds a lock the read must not outlive.
    """
    # Resolved before the clock starts.
    missing = _capability_missing()
    palace_missing = _palace_missing()
    no_retry = missing + (palace_missing,)
    deadline = None if budget is None else time.monotonic() + budget
    where = build_where_filter(wing, col=col) or None
    try:
        getter = getattr(col, "get_recent", None)
        if getter is not None:
            try:
                result = _read_with_retry(
                    lambda: getter(
                        limit=max_scan,
                        where=where,
                        order_field="filed_at",
                        include=["documents", "metadatas"],
                    ),
                    deadline,
                    no_retry,
                )
                return _without_bookkeeping(result.documents or [], result.metadatas or [])
            except missing:
                pass  # capability missing — page it manually

        # Fetch in batches to avoid SQLite variable limit (~999)
        batch_size = 500
        docs, metas = [], []
        offset = 0
        while True:
            kwargs = {"include": ["documents", "metadatas"], "limit": batch_size, "offset": offset}
            if where:
                kwargs["where"] = where
            batch = _read_with_retry(lambda kwargs=kwargs: col.get(**kwargs), deadline, no_retry)
            batch_docs = batch.get("documents") or []
            batch_metas = batch.get("metadatas") or []
            if not batch_docs:
                break
            docs.extend(batch_docs)
            metas.extend(batch_metas)
            offset += len(batch_docs)
            if len(batch_docs) < batch_size or len(docs) >= max_scan:
                break
        return _without_bookkeeping(docs, metas)
    except palace_missing:
        return [], []


def _without_bookkeeping(docs, metas) -> tuple[list, list]:
    """Drop registry rows and sentinels: they track sources, they hold no memory."""
    kept = [(d, m) for d, m in zip(docs, metas) if not is_bookkeeping(m)]
    return [d for d, _ in kept], [m for _, m in kept]


# Ceiling on how far a query widens past bookkeeping rows to fill a page.
_MAX_QUERY_POOL = 1000


def _get_drawer_rows(col, kwargs: dict, n: int, max_scan: int = 2000) -> tuple[list, list, bool]:
    """Page ``col.get`` until ``n`` drawers that are not bookkeeping rows.

    Returns ``(docs, metas, capped)``. The scan stops at the end of the scope or
    after ``max_scan`` rows; ``capped`` says it stopped there short of ``n``.
    """
    docs, metas = [], []
    offset = 0
    page = max(n, 500)
    while len(docs) < n:
        if offset >= max_scan:
            return docs, metas, True
        batch = col.get(**kwargs, limit=page, offset=offset)
        batch_docs = batch.get("documents") or []
        kept_docs, kept_metas = _without_bookkeeping(batch_docs, batch.get("metadatas") or [])
        docs.extend(kept_docs)
        metas.extend(kept_metas)
        if len(batch_docs) < page:
            break
        offset += len(batch_docs)
    return docs[:n], metas[:n], False


def _query_drawer_rows(col, kwargs: dict, n: int) -> list:
    """``(doc, meta, distance)`` for the ``n`` nearest drawers, widening the
    pool while bookkeeping rows leave it short."""
    pool = max(n, 1)
    while True:
        results = col.query(**kwargs, n_results=pool)
        rows = list(
            zip(
                _first_or_empty(results, "documents"),
                _first_or_empty(results, "metadatas"),
                _first_or_empty(results, "distances"),
            )
        )
        kept = [row for row in rows if not is_bookkeeping(row[1])]
        if len(kept) >= n or len(rows) < pool or pool >= _MAX_QUERY_POOL:
            return kept[:n]
        pool = min(pool * 4, _MAX_QUERY_POOL)


def recent_read_failure(exc: Exception) -> str:
    """The wake-up line for a recent-memories read that failed."""
    return (
        f"Palace could not be read right now ({type(exc).__name__}); "
        "wake-up skipped its recent memories. Try again in a moment."
    )


# ---------------------------------------------------------------------------
# Layer 0 — Identity
# ---------------------------------------------------------------------------


class Layer0:
    """
    ~100 tokens. Always loaded.
    Reads from ~/.mempalace/identity.txt — a plain-text file the user writes.

    Example identity.txt:
        I am Atlas, a personal AI assistant for Alice.
        Traits: warm, direct, remembers everything.
        People: Alice (creator), Bob (Alice's partner).
        Project: A journaling app that helps people process emotions.
    """

    def __init__(self, identity_path: str = None):
        if identity_path is None:
            identity_path = paths.state_str("identity.txt")
        self.path = identity_path
        self._text = None

    def render(self) -> str:
        """Return the identity text, or a sensible default."""
        if self._text is not None:
            return self._text

        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                self._text = f.read().strip()
        else:
            self._text = (
                "## L0 — IDENTITY\nNo identity configured. Create ~/.mempalace/identity.txt"
            )

        return self._text

    def token_estimate(self) -> int:
        return len(self.render()) // 4


# ---------------------------------------------------------------------------
# Layer 1 — Essential Story (auto-generated from palace)
# ---------------------------------------------------------------------------


class Layer1:
    """
    ~500-800 tokens. Always loaded.
    Auto-generated from the highest-weight / most-recent drawers in the palace.
    Groups by room, picks the top N moments, compresses to a compact summary.
    """

    MAX_DRAWERS = 15  # at most 15 moments in wake-up
    MAX_CHARS = 3200  # hard cap on total L1 text (~800 tokens)
    MAX_SCAN = 2000  # size of the candidate window pulled for L1 generation

    def __init__(self, palace_path: str = None, wing: str = None):
        cfg = MempalaceConfig()
        self.palace_path = palace_path or cfg.palace_path
        self.wing = wing

    def _fetch_candidates(self, col) -> tuple[list, list]:
        """The L1 candidate window: the MAX_SCAN most recently filed drawers."""
        return fetch_recent_window(col, wing=self.wing, max_scan=self.MAX_SCAN)

    def generate(self) -> str:
        """Pull top drawers from the palace and format as compact L1 text."""
        # The open runs on this thread, outside the read budget, so a broken
        # install (an ImportError while loading the backend) fails wake-up
        # here whatever the timing.
        try:
            col = _open_for_read(self.palace_path)
        except ImportError:
            raise
        except Exception as exc:
            message = _read_open_failure(exc)
            return message if message.startswith("## ") else f"## L1 — {message}"

        try:
            docs, metas = self._fetch_candidates(col)
        except ImportError:
            raise  # a broken install, not a palace read: fail like develop did
        except Exception as exc:
            return f"## L1 — {recent_read_failure(exc)}"

        if not docs:
            return "## L1 — No memories yet."

        # Score each drawer: prefer high importance, then most-recent filing.
        # NOTE: the ingest pipeline (miner, convo_miner, diary, add_drawer)
        # records provenance metadata — wing/room/source/chunk/filed_at — but
        # never an evaluative importance/weight field. So `importance` is
        # absent on virtually every drawer and ties at the default, which used
        # to collapse the sort to insertion order (oldest first). `filed_at`
        # is present on every drawer, so it is the *effective* ordering signal:
        # newest first. This keeps importance as the primary key for the day a
        # scoring pass populates it, while making the "recent filing" half of
        # the promise true today with data we already have.
        # The candidate window this sorts is now the MAX_SCAN *most recently
        # filed* drawers rather than the first MAX_SCAN the backend happened to
        # hand back, so on a palace larger than MAX_SCAN the newest drawers are
        # actually in the running.
        scored = []
        for doc, meta in zip(docs, metas):
            meta = meta or {}
            doc = doc or ""
            importance = 3.0
            # Try multiple metadata keys that might carry weight info
            for key in ("importance", "emotional_weight", "weight"):
                val = meta.get(key)
                if val is not None:
                    try:
                        importance = float(val)
                    except (ValueError, TypeError):
                        pass
                    break
            # By instant: naive local and UTC filed_at do not sort as text.
            # A missing or odd value sorts oldest.
            scored.append((importance, recency_key(meta.get("filed_at")), meta, doc))

        # Sort by importance desc, then recency (filed_at) desc; take top N.
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        top = [(imp, meta, doc) for imp, _recency, meta, doc in scored[: self.MAX_DRAWERS]]

        # Group by room for readability
        by_room = defaultdict(list)
        for imp, meta, doc in top:
            room = meta.get("room", "general")
            by_room[room].append((imp, meta, doc))

        # Build compact text
        lines = ["## L1 — ESSENTIAL STORY"]

        total_len = 0
        for room, entries in sorted(by_room.items()):
            room_line = f"\n[{room}]"
            lines.append(room_line)
            total_len += len(room_line)

            for _imp, meta, doc in entries:
                source = Path(meta.get("source_file", "")).name if meta.get("source_file") else ""

                # Truncate doc to keep L1 compact
                snippet = doc.strip().replace("\n", " ")
                if len(snippet) > 200:
                    snippet = snippet[:197] + "..."

                entry_line = f"  - {snippet}"
                if source:
                    entry_line += f"  ({source})"

                if total_len + len(entry_line) > self.MAX_CHARS:
                    lines.append("  ... (more in L3 search)")
                    return "\n".join(lines)

                lines.append(entry_line)
                total_len += len(entry_line)

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 2 — On-Demand (wing/room filtered retrieval)
# ---------------------------------------------------------------------------


class Layer2:
    """
    ~200-500 tokens per retrieval.
    Loaded when a specific topic or wing comes up in conversation.
    Queries ChromaDB with a wing/room filter.
    """

    def __init__(self, palace_path: str = None):
        cfg = MempalaceConfig()
        self.palace_path = palace_path or cfg.palace_path

    def retrieve(self, wing: str = None, room: str = None, n_results: int = 10) -> str:
        """Retrieve drawers filtered by wing and/or room."""
        try:
            col = _open_for_read(self.palace_path)
        except Exception as exc:
            return _read_open_failure(exc)

        where = build_where_filter(wing, room, col=col)

        kwargs = {"include": ["documents", "metadatas"]}
        if where:
            kwargs["where"] = where

        try:
            docs, metas, capped = _get_drawer_rows(col, kwargs, n_results)
        except Exception as e:
            return f"Retrieval error: {e}"

        label = f"wing={wing}" if wing else ""
        if room:
            label += f" room={room}" if label else f"room={room}"
        capped_note = "  (stopped after scanning 2000 rows; narrow the scope for more)"
        if not docs:
            return f"No drawers found for {label}." + (capped_note if capped else "")

        lines = [f"## L2 — ON-DEMAND ({len(docs)} drawers)"]
        if capped:
            lines.append(capped_note)
        for doc, meta in zip(docs[:n_results], metas[:n_results]):
            meta = meta or {}
            doc = doc or ""
            room_name = meta.get("room", "?")
            source = Path(meta.get("source_file", "")).name if meta.get("source_file") else ""
            snippet = doc.strip().replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."
            entry = f"  [{room_name}] {snippet}"
            if source:
                entry += f"  ({source})"
            lines.append(entry)

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 3 — Deep Search (full semantic search via ChromaDB)
# ---------------------------------------------------------------------------


class Layer3:
    """
    Unlimited depth. Semantic search against the full palace.
    Reuses searcher.py logic against mempalace_drawers.
    """

    def __init__(self, palace_path: str = None):
        cfg = MempalaceConfig()
        self.palace_path = palace_path or cfg.palace_path

    def search(self, query: str, wing: str = None, room: str = None, n_results: int = 5) -> str:
        """Semantic search, returns compact result text."""
        try:
            col = _open_for_read(self.palace_path)
        except Exception as exc:
            return _read_open_failure(exc)

        where = build_where_filter(wing, room, col=col)

        kwargs = {"query_texts": [query], "include": ["documents", "metadatas", "distances"]}
        if where:
            kwargs["where"] = where

        try:
            rows = _query_drawer_rows(col, kwargs, n_results)
        except Exception as e:
            return f"Search error: {e}"

        if not rows:
            return "No results found."

        metric = _metric_for_collection(col)
        lines = [f'## L3 — SEARCH RESULTS for "{query}"']
        for i, (doc, meta, dist) in enumerate(rows, 1):
            meta = meta or {}
            doc = doc or ""
            similarity = round(_distance_to_similarity(dist, metric), 3)
            wing_name = meta.get("wing", "?")
            room_name = meta.get("room", "?")
            source = Path(meta.get("source_file", "")).name if meta.get("source_file") else ""

            snippet = doc.strip().replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."

            lines.append(f"  [{i}] {wing_name}/{room_name} (sim={similarity})")
            lines.append(f"      {snippet}")
            if source:
                lines.append(f"      src: {source}")
            authored = (meta.get("authored_at") or "")[:10]
            if authored:
                lines.append(f"      authored: {authored}")

        return "\n".join(lines)

    def search_raw(
        self, query: str, wing: str = None, room: str = None, n_results: int = 5
    ) -> list:
        """Return raw dicts instead of formatted text."""
        try:
            col = _open_for_read(self.palace_path)
        except Exception as exc:
            # The contract here is a list, so the reason goes to stderr rather
            # than into a return value a caller would parse as a result row.
            print(_read_open_failure(exc), file=sys.stderr)
            return []

        where = build_where_filter(wing, room, col=col)

        kwargs = {"query_texts": [query], "include": ["documents", "metadatas", "distances"]}
        if where:
            kwargs["where"] = where

        try:
            rows = _query_drawer_rows(col, kwargs, n_results)
        except Exception:
            return []

        metric = _metric_for_collection(col)
        hits = []
        for doc, meta, dist in rows:
            # ChromaDB may return None for doc/meta when a drawer's HNSW entry
            # exists but its metadata/document rows haven't been materialized
            # (partial-flush states, mid-delete, schema upgrade boundaries).
            # Degrade gracefully — the hit still appears with real distance;
            # storage fields show their fallback where content is missing.
            meta = meta or {}
            doc = doc or ""
            hits.append(
                {
                    "text": doc,
                    "wing": meta.get("wing", "unknown"),
                    "room": meta.get("room", "unknown"),
                    "source_file": Path(meta.get("source_file", "?")).name,
                    "similarity": round(_distance_to_similarity(dist, metric), 3),
                    "metadata": meta,
                }
            )
        return hits


# ---------------------------------------------------------------------------
# MemoryStack — unified interface
# ---------------------------------------------------------------------------


class MemoryStack:
    """
    The full 4-layer stack. One class, one palace, everything works.

        stack = MemoryStack()
        print(stack.wake_up())                # L0 + L1 (~600-900 tokens)
        print(stack.recall(wing="my_app"))     # L2 on-demand
        print(stack.search("pricing change"))  # L3 deep search
    """

    def __init__(self, palace_path: str = None, identity_path: str = None):
        cfg = MempalaceConfig()
        self.palace_path = palace_path or cfg.palace_path
        self.identity_path = identity_path or paths.state_str("identity.txt")

        self.l0 = Layer0(self.identity_path)
        self.l1 = Layer1(self.palace_path)
        self.l2 = Layer2(self.palace_path)
        self.l3 = Layer3(self.palace_path)

    def wake_up(self, wing: str = None) -> str:
        """
        Generate wake-up text: L0 (identity) + L1 (essential story).
        Typically ~600-900 tokens. Inject into system prompt or first message.

        Args:
            wing: Optional wing filter for L1 (project-specific wake-up).
        """
        parts = []

        # L0 is the identity layer.
        parts.append(self.l0.render())
        parts.append("")

        # L1: Essential Story
        if wing:
            self.l1.wing = wing
        try:
            parts.append(self.l1.generate())
        except ImportError:
            # mempalace cannot run here; exit non-zero so hooks inject nothing.
            raise
        except Exception as exc:  # identity is still worth waking up with
            parts.append(f"## L1 — {recent_read_failure(exc)}")

        return "\n".join(parts)

    def recall(self, wing: str = None, room: str = None, n_results: int = 10) -> str:
        """On-demand L2 retrieval filtered by wing/room."""
        return self.l2.retrieve(wing=wing, room=room, n_results=n_results)

    def search(self, query: str, wing: str = None, room: str = None, n_results: int = 5) -> str:
        """Deep L3 semantic search."""
        return self.l3.search(query, wing=wing, room=room, n_results=n_results)

    def status(self) -> dict:
        """Status of all layers."""
        result = {
            "palace_path": self.palace_path,
            "L0_identity": {
                "path": self.identity_path,
                "exists": os.path.exists(self.identity_path),
                "tokens": self.l0.token_estimate(),
            },
            "L1_essential": {
                "description": "Auto-generated from top palace drawers",
            },
            "L2_on_demand": {
                "description": "Wing/room filtered retrieval",
            },
            "L3_deep_search": {
                "description": "Full semantic search via ChromaDB",
            },
        }

        # Count drawers
        try:
            col = _open_for_read(self.palace_path)
            count = col.count()
            result["total_drawers"] = count
        except Exception as exc:
            # Zero is indistinguishable from an empty palace, so say why.
            print(_read_open_failure(exc), file=sys.stderr)
            result["total_drawers"] = 0

        return result


# ---------------------------------------------------------------------------
# Standalone command line
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    def usage():
        print("layers.py -- 4-Layer Memory Stack")
        print()
        print("Usage:")
        print("  python layers.py wake-up              Show L0 + L1")
        print("  python layers.py wake-up --wing=NAME  Wake-up for a specific project")
        print("  python layers.py recall --wing=NAME   On-demand L2 retrieval")
        print("  python layers.py search <query>       Deep L3 search")
        print("  python layers.py status               Show layer status")
        sys.exit(0)

    if len(sys.argv) < 2:
        usage()

    cmd = sys.argv[1]

    # Parse flags
    flags = {}
    positional = []
    for arg in sys.argv[2:]:
        if arg.startswith("--") and "=" in arg:
            key, val = arg.split("=", 1)
            flags[key.lstrip("-")] = val
        elif not arg.startswith("--"):
            positional.append(arg)

    palace_path = flags.get("palace")
    stack = MemoryStack(palace_path=palace_path)

    if cmd in ("wake-up", "wakeup"):
        wing = flags.get("wing")
        text = stack.wake_up(wing=wing)
        tokens = len(text) // 4
        print(f"Wake-up text (~{tokens} tokens):")
        print("=" * 50)
        print(text)

    elif cmd == "recall":
        wing = flags.get("wing")
        room = flags.get("room")
        text = stack.recall(wing=wing, room=room)
        print(text)

    elif cmd == "search":
        query = " ".join(positional) if positional else ""
        if not query:
            print("Usage: python layers.py search <query>")
            sys.exit(1)
        wing = flags.get("wing")
        room = flags.get("room")
        text = stack.search(query, wing=wing, room=room)
        print(text)

    elif cmd == "status":
        s = stack.status()
        print(json.dumps(s, indent=2))

    else:
        usage()
