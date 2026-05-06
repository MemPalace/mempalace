#!/usr/bin/env python3
"""closet_boost_ablation.py — reproduce the closet-boost A/B finding.

Backs the comment block in ``mempalace/searcher.py`` that records the
2026-04-27 ablation against a 151K-drawer palace. Run on any palace
to compare default vs. zero-boost ranking and see whether the boost
displaces "right" answers.

Usage::

    python scripts/closet_boost_ablation.py /path/to/palace [--n-results 5]

Provide queries via stdin, one per line, or pass ``--probe-set
default`` to use the built-in 5-probe smoke test. Prints a per-query
delta showing which result rows got their rank changed by the boost
and how much. Exit code 0 unconditionally — this is observation, not
pass/fail.

Reads :data:`mempalace.searcher.CLOSET_RANK_BOOSTS` and patches it to
``(0,)*5`` for the zero-boost arm, then restores the original. No palace
writes; safe on a live palace as long as no concurrent search is
expected to be deterministic during the run window.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Tuple

# Ensure repo root is importable when run from the repo without install.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Defer the heavy import (pulls in chromadb) until we actually need to
# run a query — this keeps ``--help`` usable even without the package
# installed in the active interpreter.
searcher = None  # type: ignore[assignment]


_DEFAULT_PROBES: Tuple[str, ...] = (
    "How does the stop hook save diary entries?",
    "ChromaDB HNSW segment quarantine",
    "wing assignment from transcript path",
    "verbatim mode toggle",
    "BM25 fallback when vector underdelivers",
)


def run_query(query: str, palace_path: str, n_results: int) -> List[dict]:
    """Run a single search and return its result list (may be empty)."""
    result = searcher.search_memories(query, palace_path, n_results=n_results)
    return result.get("results") or []


def fingerprint(hit: dict) -> str:
    """Identity for diffing across boost/no-boost runs.

    drawer_id is the most stable identity; fall back to (source, text-prefix)
    for older palaces / mock data that doesn't surface drawer_id.
    """
    did = hit.get("drawer_id")
    if did:
        return did
    src = hit.get("source_file") or "?"
    text = (hit.get("text") or "")[:64]
    return f"{src}::{text}"


def compare(query: str, palace_path: str, n_results: int) -> dict:
    """Run query under default boosts and zeroed boosts; report the delta."""
    default_hits = run_query(query, palace_path, n_results)

    saved = searcher.CLOSET_RANK_BOOSTS
    try:
        searcher.CLOSET_RANK_BOOSTS = tuple(0.0 for _ in saved)
        searcher._collection_cache = None  # force re-rank on next call
        zero_hits = run_query(query, palace_path, n_results)
    finally:
        searcher.CLOSET_RANK_BOOSTS = saved

    default_order = [fingerprint(h) for h in default_hits]
    zero_order = [fingerprint(h) for h in zero_hits]

    boost_fired = sum(
        1 for h in default_hits if h.get("matched_via") == "drawer+closet"
    )
    same_set = set(default_order) == set(zero_order)
    same_order = default_order == zero_order

    return {
        "query": query,
        "default_n": len(default_hits),
        "zero_n": len(zero_hits),
        "boost_fired_rows": boost_fired,
        "same_result_set": same_set,
        "same_order": same_order,
    }


def main(argv: List[str] | None = None) -> int:
    global searcher
    parser = argparse.ArgumentParser(
        description="Closet-boost A/B ablation reproducer."
    )
    parser.add_argument("palace", help="Path to the palace directory.")
    parser.add_argument(
        "--n-results",
        type=int,
        default=5,
        help="Top-K for each query (default: 5).",
    )
    parser.add_argument(
        "--probe-set",
        choices=("default", "stdin"),
        default="stdin" if not sys.stdin.isatty() else "default",
        help=(
            "'default' = built-in 5-probe set; 'stdin' = read queries from "
            "stdin, one per line. Defaults to 'stdin' when stdin is a pipe."
        ),
    )
    args = parser.parse_args(argv)

    # Now that the user actually wants to run a query, import the
    # palace stack. This pulls in chromadb, so we defer it past
    # ``--help``.
    from mempalace import searcher as _searcher  # noqa: E402

    searcher = _searcher

    if args.probe_set == "stdin":
        queries = [line.strip() for line in sys.stdin if line.strip()]
        if not queries:
            print("No queries on stdin; falling back to default set.")
            queries = list(_DEFAULT_PROBES)
    else:
        queries = list(_DEFAULT_PROBES)

    print(f"Palace: {args.palace}")
    print(f"Queries: {len(queries)}")
    print(f"n_results: {args.n_results}")
    print(f"CLOSET_RANK_BOOSTS = {searcher.CLOSET_RANK_BOOSTS}")
    print(f"CLOSET_DISTANCE_CAP = {searcher.CLOSET_DISTANCE_CAP}")
    print()

    fired_total = 0
    set_change_total = 0
    order_change_total = 0
    for q in queries:
        delta = compare(q, args.palace, args.n_results)
        fired_total += delta["boost_fired_rows"]
        set_change_total += 0 if delta["same_result_set"] else 1
        order_change_total += 0 if delta["same_order"] else 1
        print(
            f"  q={q!r:60s}  fired={delta['boost_fired_rows']}  "
            f"same_set={delta['same_result_set']}  same_order={delta['same_order']}"
        )

    print()
    print(f"Summary:  total_boost_fires={fired_total}  "
          f"queries_with_set_change={set_change_total}  "
          f"queries_with_order_change={order_change_total}")
    print()
    print("Reading the result:")
    print("  fired ≈ 20% of rows on a chat-heavy palace (per 2026-04-27 finding).")
    print("  same_set=True everywhere → boost re-orders within candidates,")
    print("  doesn't displace right answers with wrong ones.")
    print("  Where same_order=False, boost changed *which chunk* of a source")
    print("  file ranks first, not which file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
