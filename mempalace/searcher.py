#!/usr/bin/env python3
"""
searcher.py — Find anything. Exact words.

Semantic search against the palace.
Returns verbatim text — the actual words, never summaries.
"""

import logging
import math
from datetime import datetime, timezone
from pathlib import Path

from .palace import get_collection

logger = logging.getLogger("mempalace_mcp")


class SearchError(Exception):
    """Raised when search cannot proceed (e.g. no palace found)."""


def _parse_filed_at(meta: dict):
    """UTC-aware datetime from a drawer's timestamp metadata, or None."""
    raw = meta.get("filed_at") or meta.get("date") or meta.get("source_mtime")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _is_durable(meta: dict, durable_rooms, durable_wings) -> bool:
    """Durable drawers are exempt from decay so canonical old notes don't sink."""
    if str(meta.get("type", "")).lower() in ("wisdom", "durable"):
        return True
    return meta.get("room") in durable_rooms or meta.get("wing") in durable_wings


def _decay_multiplier(filed_at, now, half_life_days: float) -> float:
    """exp(-ln2 * age_days / half_life); 1.0 when age is unknown or non-positive."""
    if filed_at is None or half_life_days <= 0:
        return 1.0
    age_days = (now - filed_at).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_days / half_life_days)


def build_where_filter(wing: str = None, room: str = None) -> dict:
    """Build ChromaDB where filter for wing/room filtering."""
    if wing and room:
        return {"$and": [{"wing": wing}, {"room": room}]}
    elif wing:
        return {"wing": wing}
    elif room:
        return {"room": room}
    return {}


def search(query: str, palace_path: str, wing: str = None, room: str = None, n_results: int = 5):
    """
    Search the palace. Returns verbatim drawer content.
    Optionally filter by wing (project) or room (aspect).
    """
    try:
        col = get_collection(palace_path, create=False)
    except Exception:
        print(f"\n  No palace found at {palace_path}")
        print("  Run: mempalace init <dir> then mempalace mine <dir>")
        raise SearchError(f"No palace found at {palace_path}")

    where = build_where_filter(wing, room)

    try:
        kwargs = {
            "query_texts": [query],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = col.query(**kwargs)

    except Exception as e:
        print(f"\n  Search error: {e}")
        raise SearchError(f"Search error: {e}") from e

    docs = results["documents"][0]
    metas = results["metadatas"][0]
    dists = results["distances"][0]

    if not docs:
        print(f'\n  No results found for: "{query}"')
        return

    print(f"\n{'=' * 60}")
    print(f'  Results for: "{query}"')
    if wing:
        print(f"  Wing: {wing}")
    if room:
        print(f"  Room: {room}")
    print(f"{'=' * 60}\n")

    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
        similarity = round(max(0.0, 1 - dist), 3)
        source = Path(meta.get("source_file", "?")).name
        wing_name = meta.get("wing", "?")
        room_name = meta.get("room", "?")

        print(f"  [{i}] {wing_name} / {room_name}")
        print(f"      Source: {source}")
        print(f"      Match:  {similarity}")
        print()
        # Print the verbatim text, indented
        for line in doc.strip().split("\n"):
            print(f"      {line}")
        print()
        print(f"  {'─' * 56}")

    print()


def search_memories(
    query: str,
    palace_path: str,
    wing: str = None,
    room: str = None,
    n_results: int = 5,
    max_distance: float = 0.0,
    decay_half_life_days: float = 0.0,
    durable_rooms=(),
    durable_wings=(),
    overfetch: int = 6,
) -> dict:
    """Programmatic search — returns a dict instead of printing.

    Used by the MCP server and other callers that need data.

    Args:
        query: Natural language search query.
        palace_path: Path to the ChromaDB palace directory.
        wing: Optional wing filter.
        room: Optional room filter.
        n_results: Max results to return.
        max_distance: Max cosine distance threshold. The palace collection uses
            cosine distance (hnsw:space=cosine) — 0 = identical, 2 = opposite.
            Results with distance > this value are filtered out. A value of
            0.0 disables filtering. Typical useful range: 0.3–1.0.
        decay_half_life_days: Ebbinghaus half-life for recency re-ranking. 0
            disables decay (behaviour is byte-identical to before). When > 0,
            over-fetch, multiply each similarity by a half-life factor over the
            drawer's filed_at age, re-rank, then truncate to n_results.
        durable_rooms/durable_wings: rooms/wings exempt from decay (canonical
            notes shouldn't sink); drawers with type in {wisdom,durable} are also exempt.
        overfetch: candidate multiplier when decaying (fetch n_results*overfetch, capped).
    """
    try:
        col = get_collection(palace_path, create=False)
    except Exception as e:
        logger.error("No palace found at %s: %s", palace_path, e)
        return {
            "error": "No palace found",
            "hint": "Run: mempalace init <dir> && mempalace mine <dir>",
        }

    where = build_where_filter(wing, room)
    decay_on = decay_half_life_days and decay_half_life_days > 0
    fetch_n = min(n_results * overfetch, 200) if decay_on else n_results

    try:
        kwargs = {
            "query_texts": [query],
            "n_results": fetch_n,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = col.query(**kwargs)
    except Exception as e:
        return {"error": f"Search error: {e}"}

    docs = results["documents"][0]
    metas = results["metadatas"][0]
    dists = results["distances"][0]

    now = datetime.now(timezone.utc)
    hits = []
    for doc, meta, dist in zip(docs, metas, dists):
        # Filter on raw distance before rounding to avoid precision loss
        if max_distance > 0.0 and dist > max_distance:
            continue
        similarity = round(max(0.0, 1 - dist), 3)
        hit = {
            "text": doc,
            "wing": meta.get("wing", "unknown"),
            "room": meta.get("room", "unknown"),
            "source_file": Path(meta.get("source_file", "?")).name,
            "similarity": similarity,
            "distance": round(dist, 4),
        }
        if decay_on:
            durable = _is_durable(meta, durable_rooms, durable_wings)
            filed = _parse_filed_at(meta)
            mult = 1.0 if durable else _decay_multiplier(filed, now, decay_half_life_days)
            hit["age_days"] = round((now - filed).total_seconds() / 86400.0, 1) if filed else None
            hit["durable"] = durable
            hit["decayed_score"] = round(similarity * mult, 4)
        hits.append(hit)

    if decay_on:
        hits.sort(key=lambda h: h["decayed_score"], reverse=True)
        hits = hits[:n_results]

    return {
        "query": query,
        "filters": {"wing": wing, "room": room},
        "total_before_filter": len(docs),
        "results": hits,
    }
