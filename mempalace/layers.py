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
from pathlib import Path

from .config import MempalaceConfig
from .palace import get_collection as _get_collection
from .searcher import (
    _distance_to_similarity,
    _first_or_empty,
    _metric_for_collection,
    build_where_filter,
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
    """Verbatim curated memories selected for startup context."""

    MAX_DRAWERS = 15
    MAX_CHARS = 3200
    MAX_SCAN = 2000
    MAX_PER_SOURCE = 2
    MAX_SNIPPET_CHARS = 400

    def __init__(self, palace_path: str = None, wing: str = None):
        cfg = MempalaceConfig()
        self.palace_path = palace_path or cfg.palace_path
        self.wing = wing

    @staticmethod
    def _importance(meta: dict) -> float:
        for key in ("importance", "emotional_weight", "weight"):
            value = meta.get(key)
            if value is not None:
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return 3.0
        return 3.0

    @staticmethod
    def _recency(meta: dict) -> tuple[str, str]:
        for key in ("authored_at", "content_date", "filed_at"):
            value = meta.get(key)
            if value:
                return key, str(value)
        return "", ""

    @staticmethod
    def _chunk_key(meta: dict) -> tuple[int, object]:
        value = meta.get("chunk_index")
        try:
            return 0, int(value)
        except (TypeError, ValueError):
            return 1, str(value or "")

    def generate(self) -> str:
        """Select explicitly curated drawers and render their content verbatim."""
        try:
            col = _get_collection(self.palace_path, create=False)
        except Exception:
            return "## L1 — No palace found. Run: mempalace mine <dir>"

        batch_size = 500
        candidates = []
        offset = 0
        rows_scanned = 0
        while rows_scanned < self.MAX_SCAN:
            clauses = [{"memory_kind": "curated"}]
            if self.wing:
                clauses.append({"wing": self.wing})
            where = clauses[0] if len(clauses) == 1 else {"$and": clauses}
            page_limit = min(batch_size, self.MAX_SCAN - rows_scanned)
            try:
                batch = col.get(
                    include=["documents", "metadatas"],
                    limit=page_limit,
                    offset=offset,
                    where=where,
                )
            except Exception:
                break
            batch_docs = batch.get("documents") or []
            batch_metas = batch.get("metadatas") or []
            batch_ids = batch.get("ids") or []
            row_count = max(len(batch_ids), len(batch_docs), len(batch_metas))
            if not row_count:
                break
            for index, (doc, meta) in enumerate(zip(batch_docs, batch_metas)):
                meta = meta or {}
                if meta.get("memory_kind") != "curated":
                    continue
                if meta.get("is_sentinel") or meta.get("ingest_mode") == "registry":
                    continue
                doc = doc or ""
                if not doc:
                    continue
                drawer_id = (
                    str(batch_ids[index])
                    if index < len(batch_ids)
                    else f"scan-{offset + index:012d}"
                )
                date_kind, recency = self._recency(meta)
                source = str(meta.get("source_file") or "")
                candidates.append(
                    (
                        self._importance(meta),
                        recency,
                        source,
                        self._chunk_key(meta),
                        drawer_id,
                        date_kind,
                        meta,
                        doc,
                    )
                )
            rows_scanned += row_count
            offset += row_count
            if row_count < page_limit:
                break

        if not candidates:
            return "## L1 — No curated memories."

        # Stable passes keep score/recency descending while every exact tie is
        # deterministic by source, chunk, drawer id, then verbatim content.
        candidates.sort(key=lambda item: (item[2], item[3], item[4], item[7]))
        candidates.sort(key=lambda item: item[1], reverse=True)
        candidates.sort(key=lambda item: item[0], reverse=True)

        selected = []
        source_counts = {}
        for item in candidates:
            source_key = item[2] or f"drawer:{item[4]}"
            if source_counts.get(source_key, 0) >= self.MAX_PER_SOURCE:
                continue
            source_counts[source_key] = source_counts.get(source_key, 0) + 1
            selected.append(item)
            if len(selected) >= self.MAX_DRAWERS:
                break

        rendered = "## L1 — ESSENTIAL STORY (curated, verbatim)"
        for _importance, date, source, _chunk_key, _drawer_id, date_kind, meta, doc in selected:
            source_display = source if len(source) <= 240 else "…" + source[-239:]
            provenance = [
                f"{meta.get('wing', '?')}/{meta.get('room', '?')}",
                f"source={source_display or '?'}",
                f"chunk={meta.get('chunk_index', '?')}",
            ]
            if date:
                provenance.append(f"{date_kind}={date}")
            prefix = "\n\n- " + " | ".join(provenance) + "\n"
            remaining = self.MAX_CHARS - len(rendered) - len(prefix)
            if remaining <= 0:
                break
            snippet = doc[: min(self.MAX_SNIPPET_CHARS, remaining)]
            if not snippet:
                continue
            rendered += prefix + snippet

        return rendered


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

        metric = _metric_for_collection(col)
        lines = [f'## L3 — SEARCH RESULTS for "{query}"']
        for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
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

        metric = _metric_for_collection(col)
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
        self.identity_path = identity_path or os.path.expanduser("~/.mempalace/identity.txt")

        self.l0 = Layer0(self.identity_path)
        self.l1 = Layer1(self.palace_path)
        self.l2 = Layer2(self.palace_path)
        self.l3 = Layer3(self.palace_path)

    def wake_up(self, wing: str = None, identity_only: bool = False) -> str:
        """Generate L0 identity plus curated L1, or only L0 when requested."""
        identity = self.l0.render()
        if identity_only:
            return identity
        if wing:
            self.l1.wing = wing
        return "\n".join((identity, "", self.l1.generate()))

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
        print("  python -m mempalace.layers wake-up                  Show L0 + L1")
        print("  python -m mempalace.layers wake-up --identity-only  Show only L0")
        print("  python -m mempalace.layers wake-up --wing=NAME      Project wake-up")
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
        elif arg.startswith("--"):
            flags[arg.lstrip("-")] = True
        else:
            positional.append(arg)

    palace_path = flags.get("palace")
    stack = MemoryStack(palace_path=palace_path)

    if cmd in ("wake-up", "wakeup"):
        wing = flags.get("wing")
        text = stack.wake_up(wing=wing, identity_only="identity-only" in flags)
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
