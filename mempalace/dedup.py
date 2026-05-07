"""
dedup.py — Detect and remove near-duplicate drawers
====================================================

When the same files are mined multiple times, near-identical drawers
accumulate. This module finds drawers from the same source_file that
are too similar (cosine distance < threshold), keeps the longest/richest
version, and deletes the rest.

No API calls — uses ChromaDB's built-in embedding similarity.

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

from .backends.chroma import ChromaBackend


COLLECTION_NAME = "mempalace_drawers"
# Cosine DISTANCE threshold (not similarity). Lower = stricter.
# 0.15 = ~85% cosine similarity — catches near-identical chunks.
# For looser dedup of paraphrased content, try 0.3–0.4.
DEFAULT_THRESHOLD = 0.15
MIN_DRAWERS_TO_CHECK = 5


def _get_palace_path():
    """Resolve palace path from config."""
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().palace_path
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".mempalace", "palace")


def get_source_groups(col, min_count=MIN_DRAWERS_TO_CHECK, source_pattern=None, wing=None):
    """Group drawers by source_file, return groups with min_count+ entries.

    If wing is specified, only considers drawers in that wing. This catches
    cross-wing duplicates when the same source was mined into multiple wings.
    """
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
    col = ChromaBackend().get_collection(palace_path, COLLECTION_NAME)

    groups = get_source_groups(col)

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

    col = ChromaBackend().get_collection(palace_path, COLLECTION_NAME)

    print(f"  Palace: {palace_path}")
    print(f"  Drawers: {col.count():,}")
    print(f"  Threshold: {threshold}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'─' * 55}")

    if wing:
        print(f"  Wing: {wing}")
    groups = get_source_groups(col, min_count, source_pattern, wing=wing)
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

    print(f"\n{'─' * 55}")
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
