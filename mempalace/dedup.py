"""
dedup.py — Detect and remove near-duplicate drawers
====================================================

When the same files are mined multiple times, near-identical drawers
accumulate. This module finds drawers from the same source_file that
are too similar (cosine distance < threshold), keeps the longest/richest
version, and deletes the rest.

Uses the configured storage backend's similarity search. With the default
local backends (Chroma, sqlite_exact) this stays on-machine with no external
calls; with a remote backend (e.g. Qdrant) it issues queries to that backend.

Usage (standalone):
    python -m mempalace.dedup                          # dedup all
    python -m mempalace.dedup --dry-run                # preview only
    python -m mempalace.dedup --threshold 0.10         # stricter (near-identical only)
    python -m mempalace.dedup --threshold 0.35         # looser (catches paraphrased content)
    python -m mempalace.dedup --wing my_project        # scope to one wing
    python -m mempalace.dedup --stats                  # stats only
    python -m mempalace.dedup --source "my_project"    # filter by source

Usage (from CLI):
    mempalace dedup [--dry-run] [--threshold 0.15] [--stats]
"""

import argparse
import os
import time
from collections import defaultdict

from .palace import get_collection


COLLECTION_NAME = "mempalace_drawers"
# Cosine DISTANCE threshold (not similarity). Lower = stricter.
# 0.15 = ~85% cosine similarity — catches near-identical chunks.
# For looser dedup of paraphrased content, try 0.3–0.4.
DEFAULT_THRESHOLD = 0.15
MIN_DRAWERS_TO_CHECK = 5
FIND_DUPLICATES_INITIAL_K = 32
FIND_DUPLICATES_MAX_NEIGHBORS = 512


def _get_palace_path():
    """Resolve palace path from config."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().palace_path
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".mempalace", "palace")


def get_source_groups(
    col, min_count=MIN_DRAWERS_TO_CHECK, source_pattern=None, wing=None, palace_path=None
):
    """Group drawers by source_file, return groups with min_count+ entries.

    If wing is specified, only considers drawers in that wing. This catches
    cross-wing duplicates when the same source was mined into multiple wings.

    ``palace_path``, when passed, preflights HNSW divergence before count():
    a diverged segment can hit the #1222 SIGSEGV/panic class, which a
    try/except around count() cannot catch. Omitted by existing callers
    that don't have a palace_path handy (e.g. tests) -- the check is simply
    skipped in that case, matching this function's pre-existing behavior.
    """
    if palace_path is not None:
        from .backends.chroma import hnsw_capacity_status

        capacity_info = hnsw_capacity_status(palace_path, COLLECTION_NAME)
        if capacity_info.get("diverged"):
            print(f"\n  HNSW index is diverged: {capacity_info.get('message', '')}")
            print("  Run `mempalace repair --mode from-sqlite --archive-existing` first.")
            return {}

    total = col.count()
    groups = defaultdict(list)

    offset = 0
    batch_size = 1000
    while offset < total:
        kwargs = {"limit": batch_size, "offset": offset, "include": ["metadatas"]}
        if wing:
            kwargs["where"] = {"wing": wing}
        batch = col.get(**kwargs)
        if not batch["ids"]:
            break
        for did, meta in zip(batch["ids"], batch["metadatas"]):
            src = meta.get("source_file", "unknown")
            if source_pattern and source_pattern.lower() not in src.lower():
                continue
            groups[src].append(did)
        offset += len(batch["ids"])

    return {src: ids for src, ids in groups.items() if len(ids) >= min_count}


def _logical_drawer_id(row_id, metadata):
    metadata = metadata if isinstance(metadata, dict) else {}
    parent_id = metadata.get("parent_drawer_id")
    if isinstance(parent_id, str) and parent_id.strip():
        return parent_id
    return row_id


def _scope_where(wing=None, room=None):
    where = {}
    if wing:
        where["wing"] = wing
    if room:
        where["room"] = room
    return where or None


def _fetch_duplicate_rows(col, where=None, batch_size=1000):
    rows = []
    offset = 0
    while True:
        batch = col.get(
            where=where,
            include=["documents", "metadatas"],
            limit=batch_size,
            offset=offset,
        )
        ids = batch.get("ids") or []
        if not ids:
            break
        documents = batch.get("documents") or [None] * len(ids)
        metadatas = batch.get("metadatas") or [{}] * len(ids)
        for row_id, document, metadata in zip(ids, documents, metadatas):
            rows.append(
                {
                    "row_id": row_id,
                    "document": document,
                    "metadata": metadata if isinstance(metadata, dict) else {},
                    "logical_id": _logical_drawer_id(row_id, metadata),
                }
            )
        if len(ids) < batch_size:
            break
        offset += len(ids)
    return rows


def _represent_logical_drawers(rows):
    by_logical = {}
    row_to_logical = {}
    for item in rows:
        row_to_logical[item["row_id"]] = item["logical_id"]
        document = item.get("document") or ""
        if not document:
            continue
        current = by_logical.get(item["logical_id"])
        if current is None or len(document) > len(current.get("document") or ""):
            by_logical[item["logical_id"]] = item
    return list(by_logical.values()), row_to_logical


def _component_root(parent, node):
    while parent[node] != node:
        parent[node] = parent[parent[node]]
        node = parent[node]
    return node


def _component_union(parent, a, b):
    root_a = _component_root(parent, a)
    root_b = _component_root(parent, b)
    if root_a != root_b:
        parent[root_b] = root_a


def find_duplicate_clusters(
    col,
    *,
    wing=None,
    room=None,
    threshold=DEFAULT_THRESHOLD,
    max_clusters=None,
    initial_k=FIND_DUPLICATES_INITIAL_K,
    max_neighbors=FIND_DUPLICATES_MAX_NEIGHBORS,
):
    """Return read-only connected components of near-duplicate logical drawers.

    Candidate rows are fetched through the backend interface and collapsed by
    ``parent_drawer_id`` before clustering, so chunks from the same logical
    drawer never duplicate each other. Neighbor search grows K while the
    boundary result is still under ``threshold`` and stops at
    ``min(physical_rows, max_neighbors)``; dense duplicate regions beyond that
    bound are intentionally capped to keep the read-only tool predictable.
    """
    threshold = float(threshold)
    if max_clusters is not None:
        max_clusters = int(max_clusters)
    where = _scope_where(wing=wing, room=room)
    rows = _fetch_duplicate_rows(col, where=where)
    candidates, row_to_logical = _represent_logical_drawers(rows)
    neighbor_bound = min(len(rows), int(max_neighbors)) if rows else 0
    params = {
        "wing": wing,
        "room": room,
        "threshold": threshold,
        "max_clusters": max_clusters,
        "initial_k": int(initial_k),
        "neighbor_bound": neighbor_bound,
    }
    if len(candidates) < 2 or neighbor_bound < 2:
        return {"clusters": [], "params": params}

    parent = {item["logical_id"]: item["logical_id"] for item in candidates}
    edges = {}
    initial_k = max(2, int(initial_k))

    for item in candidates:
        query_id = item["logical_id"]
        document = item.get("document")
        if not document:
            continue
        k = min(initial_k, neighbor_bound)
        seen_query_size = None
        while k and k != seen_query_size:
            seen_query_size = k
            results = col.query(
                query_texts=[document],
                n_results=k,
                include=["distances"],
                where=where,
            )
            result_ids = (results.get("ids") or [[]])[0] or []
            distances = (results.get("distances") or [[]])[0] or []
            for neighbor_row_id, distance in zip(result_ids, distances):
                if neighbor_row_id not in row_to_logical:
                    continue
                neighbor_id = row_to_logical.get(neighbor_row_id, neighbor_row_id)
                if neighbor_row_id == item["row_id"] or neighbor_id == query_id:
                    continue
                distance = float(distance)
                if distance >= threshold:
                    continue
                a, b = sorted((query_id, neighbor_id))
                previous = edges.get((a, b))
                if previous is None or distance < previous:
                    edges[(a, b)] = distance
                if neighbor_id not in parent:
                    parent[neighbor_id] = neighbor_id
                _component_union(parent, query_id, neighbor_id)

            kth_distance = float(distances[-1]) if len(distances) >= k else None
            if kth_distance is not None and kth_distance < threshold and k < neighbor_bound:
                k = min(neighbor_bound, k * 2)
                continue
            break

    components = defaultdict(set)
    for a, b in edges:
        root = _component_root(parent, a)
        components[root].update((a, b))

    clusters = []
    for drawer_ids in components.values():
        if len(drawer_ids) < 2:
            continue
        cluster_pairs = [
            {"a": a, "b": b, "distance": distance}
            for (a, b), distance in sorted(edges.items())
            if a in drawer_ids and b in drawer_ids
        ]
        clusters.append(
            {
                "drawer_ids": sorted(drawer_ids),
                "pairs": cluster_pairs,
                "size": len(drawer_ids),
            }
        )

    clusters.sort(key=lambda cluster: (-cluster["size"], cluster["drawer_ids"]))
    truncated = False
    if max_clusters is not None:
        max_clusters = max(0, int(max_clusters))
        truncated = len(clusters) > max_clusters
        clusters = clusters[:max_clusters]

    result = {"clusters": clusters, "params": params}
    if truncated:
        result["truncated"] = True
    return result


def dedup_source_group(col, drawer_ids, threshold=DEFAULT_THRESHOLD, dry_run=True):
    """Dedup drawers within one source_file group.

    Greedy: sort by doc length (longest first), keep if not too similar
    to any already-kept drawer. Returns (kept_ids, deleted_ids).
    """
    data = col.get(ids=drawer_ids, include=["documents", "metadatas"])
    items = list(zip(data["ids"], data["documents"], data["metadatas"]))
    items.sort(key=lambda x: len(x[1] or ""), reverse=True)

    kept = []
    to_delete = []

    for did, doc, _meta in items:
        if not doc or len(doc) < 20:
            to_delete.append(did)
            continue

        if not kept:
            kept.append((did, doc))
            continue

        try:
            results = col.query(
                query_texts=[doc],
                n_results=min(len(kept), 5),
                include=["distances"],
            )
            dists = results["distances"][0] if results["distances"] else []
            kept_ids_set = {k[0] for k in kept}

            is_dup = False
            for rid, dist in zip(results["ids"][0], dists):
                if rid in kept_ids_set and dist < threshold:
                    is_dup = True
                    break

            if is_dup:
                to_delete.append(did)
            else:
                kept.append((did, doc))
        except Exception:
            kept.append((did, doc))

    if to_delete and not dry_run:
        for i in range(0, len(to_delete), 500):
            col.delete(ids=to_delete[i : i + 500])

    return [k[0] for k in kept], to_delete


def show_stats(palace_path=None):
    """Show duplication statistics without making changes."""
    palace_path = palace_path or _get_palace_path()
    col = get_collection(palace_path, COLLECTION_NAME)

    groups = get_source_groups(col, palace_path=palace_path)

    total_drawers = sum(len(ids) for ids in groups.values())
    print(f"\n  Sources with {MIN_DRAWERS_TO_CHECK}+ drawers: {len(groups)}")
    print(f"  Total drawers in those sources: {total_drawers:,}")

    print("\n  Top 15 by drawer count:")
    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)
    for src, ids in sorted_groups[:15]:
        print(f"    {len(ids):4d}  {src[:65]}")

    estimated_dups = sum(int(len(ids) * 0.4) for ids in groups.values() if len(ids) > 20)
    print(f"\n  Estimated duplicates (groups > 20): ~{estimated_dups:,}")


def dedup_palace(
    palace_path=None,
    threshold=DEFAULT_THRESHOLD,
    dry_run=True,
    source_pattern=None,
    min_count=MIN_DRAWERS_TO_CHECK,
    wing=None,
):
    """Main entry point: deduplicate near-identical drawers across the palace."""
    palace_path = palace_path or _get_palace_path()

    print(f"\n{'=' * 55}")
    print("  MemPalace Deduplicator")
    print(f"{'=' * 55}")

    col = get_collection(palace_path, COLLECTION_NAME)

    # Preflight HNSW divergence before this function's own count() print --
    # get_source_groups's palace_path guard (added alongside this one) only
    # covers its own internal count(), not this earlier one. count() on a
    # diverged segment can hard-crash the process (#1222); a try/except
    # cannot catch that, so it must never be reached at all when diverged.
    from .backends.chroma import hnsw_capacity_status

    capacity_info = hnsw_capacity_status(palace_path, COLLECTION_NAME)
    if capacity_info.get("diverged"):
        print(f"\n  HNSW index is diverged: {capacity_info.get('message', '')}")
        print("  Run `mempalace repair --mode from-sqlite --archive-existing` first.")
        return

    print(f"  Palace: {palace_path}")
    print(f"  Drawers: {col.count():,}")
    print(f"  Threshold: {threshold}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'-' * 55}")

    if wing:
        print(f"  Wing: {wing}")
    groups = get_source_groups(col, min_count, source_pattern, wing=wing, palace_path=palace_path)
    print(f"\n  Sources to check: {len(groups)}")

    t0 = time.time()
    total_kept = 0
    total_deleted = 0

    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)

    for i, (src, drawer_ids) in enumerate(sorted_groups):
        kept, deleted = dedup_source_group(col, drawer_ids, threshold, dry_run)
        total_kept += len(kept)
        total_deleted += len(deleted)

        if deleted:
            print(
                f"  [{i + 1:3d}/{len(groups)}] "
                f"{src[:50]:50s} {len(drawer_ids):4d} → {len(kept):4d}  "
                f"(-{len(deleted)})"
            )

    elapsed = time.time() - t0

    print(f"\n{'-' * 55}")
    print(f"  Done in {elapsed:.1f}s")
    print(
        f"  Drawers: {total_kept + total_deleted:,} → {total_kept:,}  (-{total_deleted:,} removed)"
    )
    print(f"  Palace after: {col.count():,} drawers")

    if dry_run:
        print("\n  [DRY RUN] No changes written. Re-run without --dry-run to apply.")

    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deduplicate near-identical drawers")
    parser.add_argument("--palace", default=None, help="Palace directory path")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Cosine distance threshold (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without deleting")
    parser.add_argument("--stats", action="store_true", help="Show stats only")
    parser.add_argument("--wing", default=None, help="Scope dedup to a single wing")
    parser.add_argument("--source", default=None, help="Filter by source file pattern")
    args = parser.parse_args()

    path = os.path.expanduser(args.palace) if args.palace else None

    if args.stats:
        show_stats(palace_path=path)
    else:
        dedup_palace(
            palace_path=path,
            threshold=args.threshold,
            dry_run=args.dry_run,
            source_pattern=args.source,
            wing=args.wing,
        )
