#!/usr/bin/env python3
"""
layers.py — 4-Layer Memory Stack for mempalace
===================================================

Load only what you need, when you need it.

    Layer 0: Identity       (~100 tokens)   — Always loaded. "Who am I?"
    Layer 1: Essential Story (~500-800)      — Always loaded. Top moments from the palace.
    Layer 2: On-Demand      (~200-500 each)  — Loaded when a topic/wing comes up.
    Layer 3: Deep Search    (unlimited)      — Full ChromaDB semantic search.

Wake-up cost: ~600-900 tokens (L0+L1). Leaves 95%+ of context free.

Reads directly from ChromaDB (mempalace_drawers)
and ~/.mempalace/identity.txt.
"""

import os
import sys
import hashlib
from pathlib import Path
from collections import defaultdict

from .config import MempalaceConfig
from .palace import get_collection as _get_collection
from .searcher import _first_or_empty, build_where_filter


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
            identity_path = os.path.expanduser("~/.mempalace/identity.txt")
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
    MAX_SCAN = 2000  # don't scan more than this for L1 generation

    def __init__(self, palace_path: str = None, wing: str = None):
        cfg = MempalaceConfig()
        self.palace_path = palace_path or cfg.palace_path
        self.wing = wing

    def generate(self) -> str:
        """Pull top drawers from ChromaDB and format as compact L1 text."""
        try:
            col = _get_collection(self.palace_path, create=False)
        except Exception:
            return "## L1 — No palace found. Run: mempalace mine <dir>"

        # Fetch all drawers in batches to avoid SQLite variable limit (~999)
        _BATCH = 500
        docs, metas = [], []
        offset = 0
        while True:
            kwargs = {"include": ["documents", "metadatas"], "limit": _BATCH, "offset": offset}
            if self.wing:
                kwargs["where"] = {"wing": self.wing}
            try:
                batch = col.get(**kwargs)
            except Exception:
                break
            batch_docs = batch.get("documents", [])
            batch_metas = batch.get("metadatas", [])
            if not batch_docs:
                break
            docs.extend(batch_docs)
            metas.extend(batch_metas)
            offset += len(batch_docs)
            if len(batch_docs) < _BATCH or len(docs) >= self.MAX_SCAN:
                break

        if not docs:
            return "## L1 — No memories yet."

        # Score each drawer: prefer high importance, then recency + source priority
        # for drawers that do not populate an importance key. The previous
        # implementation defaulted importance to 3 for every drawer, so every
        # drawer tied and the sort was effectively non-deterministic (issue #1629).
        scored = []
        for doc, meta in zip(docs, metas):
            meta = meta or {}
            doc = doc or ""
            scored.append(self._score_drawer(doc, meta))

        # Assign recency_rank to the fallback bucket (bucket=1).
        fallback = [s for s in scored if s[0] == 1]
        rank_lookup: dict[tuple[str, int, str], int] = {}
        for rank, item in enumerate(
            sorted(
                fallback,
                key=lambda s: (s[3].split("|", 1)[1], -s[2], s[3].split("|", 1)[0]),
                reverse=True,
            )
        ):
            rk = item[3].split("|", 1)[1]
            rank_lookup[(rk, item[2], item[3].split("|", 1)[0])] = rank

        rescored = []
        for s in scored:
            if s[0] == 0:
                rescored.append(s)
                continue
            rk = s[3].split("|", 1)[1]
            rank = rank_lookup.get((rk, s[2], s[3].split("|", 1)[0]), 0)
            rescored.append((s[0], rank, s[2], s[3], s[4], s[5]))

        rescored.sort(key=lambda x: (x[0], x[1], -x[2], x[3]))
        top = rescored[: self.MAX_DRAWERS]

        # Group by room for readability
        by_room = defaultdict(list)
        for _bucket, _recency, _source_pri, _doc_id, meta, doc in top:
            room = meta.get("room", "general")
            by_room[room].append((meta, doc))

        # Build compact text
        lines = ["## L1 — ESSENTIAL STORY"]

        total_len = 0
        for room, entries in sorted(by_room.items()):
            room_line = f"\n[{room}]"
            lines.append(room_line)
            total_len += len(room_line)

            for meta, doc in entries:
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

    def _score_drawer(
        self, doc: str, meta: dict
    ) -> tuple[int, int, int, str, dict, str]:
        """Return a sortable ranking tuple for a single drawer.

        Tuple shape: (bucket, recency_rank, source_priority, doc_id, meta, doc).
        bucket=0 means the drawer carries an explicit importance key (or one
        of its aliases), bucket=1 means fallback. recency_rank is assigned
        in a second pass in generate() so the most-recently-filed non-importance
        drawer gets rank 0. source_priority is a small per-source bonus so
        user-source drawers win over tool_output / system sources when
        recency ties. doc_id is a stable per-drawer identifier used as the
        final tiebreaker (and for grouping by filing timestamp).
        """
        explicit_importance = None
        for key in ("importance", "emotional_weight", "weight"):
            val = meta.get(key)
            if val is None:
                continue
            try:
                explicit_importance = float(val)
            except (ValueError, TypeError):
                continue
            break

        if explicit_importance is not None:
            return (0, 0, 0, _drawer_id(doc, meta), meta, doc)

        filed_at = meta.get("filed_at") or meta.get("created_at") or ""
        recency_key = str(filed_at) if filed_at else ""
        source_pri = 0
        src = str(meta.get("source", "")).lower()
        if not src and meta.get("source_file"):
            source_pri = 2
        elif "user" in src:
            source_pri = 2
        elif "tool" in src:
            source_pri = 1
        elif "system" in src:
            source_pri = 0
        return (
            1,
            0,
            source_pri,
            _drawer_id(doc, meta) + "|" + recency_key,
            meta,
            doc,
        )


def _drawer_id(doc: str, meta: dict) -> str:
    """Stable identifier for a drawer used as a sort tiebreaker."""
    sid = meta.get("id") or meta.get("drawer_id")
    if sid:
        return str(sid)
    src = meta.get("source_file", "")
    if src and doc:
        return f"{src}:{hashlib.sha1(doc.encode('utf-8')).hexdigest()[:12]}"
    return hashlib.sha1(doc.encode('utf-8')).hexdigest()[:16]


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
            col = _get_collection(self.palace_path, create=False)
        except Exception:
            return "No palace found."

        where = build_where_filter(wing, room)

        kwargs = {"include": ["documents", "metadatas"], "limit": n_results}
        if where:
            kwargs["where"] = where

        try:
            results = col.get(**kwargs)
        except Exception as e:
            return f"Retrieval error: {e}"

        docs = results.get("documents", [])
        metas = results.get("metadatas", [])

        if not docs:
            label = f"wing={wing}" if wing else ""
            if room:
                label += f" room={room}" if label else f"room={room}"
            return f"No drawers found for {label}."

        lines = [f"## L2 — ON-DEMAND ({len(docs)} drawers)"]
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
            col = _get_collection(self.palace_path, create=False)
        except Exception:
            return "No palace found."

        where = build_where_filter(wing, room)

        kwargs = {
            "query_texts": [query],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        try:
            results = col.query(**kwargs)
        except Exception as e:
            return f"Search error: {e}"

        docs = _first_or_empty(results, "documents")
        metas = _first_or_empty(results, "metadatas")
        dists = _first_or_empty(results, "distances")

        if not docs:
            return "No results found."

        lines = [f'## L3 — SEARCH RESULTS for "{query}"']
        for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
            meta = meta or {}
            doc = doc or ""
            similarity = round(max(0.0, 1 - dist), 3)
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

        return "\n".join(lines)

    def search_raw(
        self, query: str, wing: str = None, room: str = None, n_results: int = 5
    ) -> list:
        """Return raw dicts instead of formatted text."""
        try:
            col = _get_collection(self.palace_path, create=False)
        except Exception:
            return []

        where = build_where_filter(wing, room)

        kwargs = {
            "query_texts": [query],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        try:
            results = col.query(**kwargs)
        except Exception:
            return []

        hits = []
        for doc, meta, dist in zip(
            _first_or_empty(results, "documents"),
            _first_or_empty(results, "metadatas"),
            _first_or_empty(results, "distances"),
        ):
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
                    "similarity": round(1 - dist, 3),
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
        self.identity_path = identity_path or os.path.expanduser("~/.mempalace/identity.txt")

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

        # L0: Identity
        parts.append(self.l0.render())
        parts.append("")

        # L1: Essential Story
        if wing:
            self.l1.wing = wing
        parts.append(self.l1.generate())

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
            col = _get_collection(self.palace_path, create=False)
            count = col.count()
            result["total_drawers"] = count
        except Exception:
            result["total_drawers"] = 0

        return result


# ---------------------------------------------------------------------------
# CLI (standalone)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    def usage():
        print("layers.py — 4-Layer Memory Stack")
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
