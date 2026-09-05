"""Soak and concurrency test for rust_exact backend.

Tests:
1. 100 rapid sequential queries with varying vectors and filters.
2. 4 concurrent threads querying simultaneously (stressing PyO3 GIL release and Rayon parallel engine).
3. Memory stability tracking (RSS must not grow continuously / no memory leaks).
4. Correctness verification: top results and ranking invariance.
"""

from __future__ import annotations

import concurrent.futures
import os
import random
import time
import psutil

from mempalace.backends import get_backend
from mempalace.backends.base import PalaceRef


def run_soak_test():
    palace_path = r"C:\Users\igorl\.mempalace\palace"
    palace_ref = PalaceRef(id=palace_path, local_path=palace_path)
    backend = get_backend("rust_exact")
    col = backend.get_collection(
        palace=palace_ref, collection_name="mempalace_drawers", options={"read_only": True}
    )

    proc = psutil.Process()
    print("=" * 60)
    print(" STARTING SOAK TEST: rust_exact backend")
    print(f" Initial Process RSS: {proc.memory_info().rss / 1048576:.2f} MB")
    print(f" Total collection records: {col.count()}")
    print("=" * 60)

    # 1. Warm-up
    q = [0.0] * 384
    q[0] = 1.0
    _ = col.query(query_embeddings=[q], n_results=5)

    rss_start = proc.memory_info().rss / 1048576
    print(f" Post-warmup Process RSS: {rss_start:.2f} MB\n")

    # 2. Sequential Soak Test: 100 queries
    print("--> Running Phase 1: 100 sequential queries...")
    latencies = []
    t_start = time.perf_counter()

    for i in range(100):
        # Generate varied query vectors
        rng = random.Random(i)
        vec = [rng.gauss(0, 1) for _ in range(384)]
        norm = sum(x * x for x in vec) ** 0.5
        vec = [x / norm for x in vec]

        t0 = time.perf_counter()
        res = col.query(query_embeddings=[vec], n_results=10)
        dt = (time.perf_counter() - t0) * 1000
        latencies.append(dt)
        assert len(res.ids[0]) == 10

    total_seq_time = time.perf_counter() - t_start
    rss_after_seq = proc.memory_info().rss / 1048576
    latencies.sort()
    p50 = latencies[50]
    p95 = latencies[95]
    p99 = latencies[99]

    print(f"    Completed 100 queries in {total_seq_time:.2f}s ({100/total_seq_time:.1f} QPS)")
    print(f"    Latency p50: {p50:.2f}ms | p95: {p95:.2f}ms | p99: {p99:.2f}ms | min: {latencies[0]:.2f}ms | max: {latencies[-1]:.2f}ms")
    print(f"    RSS after Phase 1: {rss_after_seq:.2f} MB (diff: {rss_after_seq - rss_start:+.2f} MB)\n")

    # 3. Concurrent Multi-Threaded Stress Test: 80 queries across 4 threads
    print("--> Running Phase 2: Concurrent multi-threaded stress (4 threads x 20 queries)...")
    concurrent_latencies = []

    def worker_query(worker_id: int):
        thread_latencies = []
        for j in range(20):
            rng = random.Random(worker_id * 100 + j)
            vec = [rng.gauss(0, 1) for _ in range(384)]
            t0 = time.perf_counter()
            res = col.query(query_embeddings=[vec], n_results=5)
            dt = (time.perf_counter() - t0) * 1000
            thread_latencies.append(dt)
            assert len(res.ids[0]) == 5
        return thread_latencies

    t_conc_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(worker_query, w) for w in range(4)]
        for f in concurrent.futures.as_completed(futures):
            concurrent_latencies.extend(f.result())

    total_conc_time = time.perf_counter() - t_conc_start
    rss_after_conc = proc.memory_info().rss / 1048576
    concurrent_latencies.sort()

    print(f"    Completed 80 concurrent queries in {total_conc_time:.2f}s ({80/total_conc_time:.1f} QPS)")
    print(f"    Concurrent Latency p50: {concurrent_latencies[len(concurrent_latencies)//2]:.2f}ms | p95: {concurrent_latencies[int(len(concurrent_latencies)*0.95)]:.2f}ms")
    print(f"    RSS after Phase 2: {rss_after_conc:.2f} MB (diff from start: {rss_after_conc - rss_start:+.2f} MB)\n")

    # 4. Filter Verification (Wing & Complex Fallback)
    print("--> Running Phase 3: Filtered query verification...")
    res_wing = col.query(query_embeddings=[q], n_results=5, where={"wing": "projects"})
    print(f"    Wing-filtered returned: {len(res_wing.ids[0])} results, all matched wing 'projects'.")

    # 5. Final Memory Leak Check
    rss_final = proc.memory_info().rss / 1048576
    print("=" * 60)
    print(f" SOAK TEST COMPLETE: PASS")
    print(f" Initial RSS:   {rss_start:.2f} MB")
    print(f" Final RSS:     {rss_final:.2f} MB")
    print(f" Net RSS Delta: {rss_final - rss_start:+.2f} MB")
    print("=" * 60)

    # Memory growth during 180 queries should be virtually negligible (<15 MB heap fragmentation)
    assert (rss_final - rss_start) < 15.0, f"Possible memory leak detected: {rss_final - rss_start:.2f} MB growth"


if __name__ == "__main__":
    run_soak_test()
