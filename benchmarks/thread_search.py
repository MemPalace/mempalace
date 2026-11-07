"""Synthetic ThreadStore ranking benchmark; timings are informational.

Run: uv run --extra dev python benchmarks/thread_search.py --output result.json
Use the identical command on both revisions, then compare the complete `results`
lists. The default corpus approximates 130 chats / 112k 800-character chunks;
no transcript files or external services are used. Includes common/rare terms,
random scopes, limits and exact ties. p50/p95 measure warm-cache search only.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from commonplace.handoff.thread_store import ThreadStore


def populate(store, chats, chunks):
    rng = random.Random(36)
    ids = [f"thread-{i:03}" for i in range(chats)]
    with store._transaction():
        for thread_id in reversed(ids):
            store._conn.execute(
                "INSERT INTO threads VALUES (?, 'synthetic', ?, '', ?, ?, '', '', '', '')",
                (thread_id, thread_id, chunks, chunks),
            )
            rows = []
            for idx in reversed(range(chunks)):
                # A repeated row creates ties whose rowids oppose logical order.
                text = (
                    "common identical tie"
                    if idx % 10 == 0
                    else " ".join(
                        ["common", f"rare{rng.randrange(2000)}"]
                        + [f"word{rng.randrange(200)}" for _ in range(100)]
                    )
                )
                text = (text + " " * 800)[:800]
                rows.append((thread_id, idx, idx, idx, text))
            store._conn.executemany("INSERT INTO chunks VALUES (?,?,?,?,?)", rows)
        store._repair_fts()
    return ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chats", type=int, default=130)
    parser.add_argument("--chunks", type=int, default=862)
    parser.add_argument("--queries", type=int, default=240)
    args = parser.parse_args()
    rng = random.Random(3600)
    with tempfile.TemporaryDirectory() as temporary:
        store = ThreadStore(str(Path(temporary) / "threads.sqlite3"))
        try:
            ids = populate(store, args.chats, args.chunks)
            cases = []
            for i in range(args.queries):
                scope = ids if i % 3 else rng.sample(ids, rng.randint(1, len(ids)))
                query = [
                    "common",
                    "identical tie",
                    f"word{rng.randrange(200)}",
                    f"rare{rng.randrange(2000)}",
                    "absent",
                    '" OR common',
                ][i % 6]
                cases.append((scope, query, rng.choice([1, 8, 19, 50])))
            for scope, query, limit in cases[:12]:
                store.search(scope, query, limit)
            times, results = [], []
            for scope, query, limit in cases:
                started = time.perf_counter()
                hits = store.search(scope, query, limit)
                times.append((time.perf_counter() - started) * 1000)
                results.append([asdict(hit) for hit in hits])
            report = {
                "chats": args.chats,
                "chunks": args.chats * args.chunks,
                "text_bytes": args.chats * args.chunks * 800,
                "queries": len(cases),
                "p50_ms": statistics.median(times),
                "p95_ms": sorted(times)[int(len(times) * 0.95) - 1],
            }
            args.output.write_text(json.dumps({**report, "results": results}) + "\n")
            print(json.dumps(report, indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
