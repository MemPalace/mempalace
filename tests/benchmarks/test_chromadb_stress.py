"""
ChromaDB stress tests — find the breaking point.

Tests the raw ChromaDB patterns used by mempalace to determine:
  - At what collection size does col.get(include=["metadatas"]) become dangerous?
  - How does query latency degrade as collection grows?
  - How much faster is batched insertion vs sequential?
"""

import os
import time

import chromadb
import pytest

from tests.benchmarks.data_generator import PalaceDataGenerator
from tests.benchmarks.report import record_metric


def _get_rss_mb():
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except ImportError:
        import resource
        import platform

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if platform.system() == "Darwin":
            return usage / (1024 * 1024)
        return usage / 1024


@pytest.mark.benchmark
class TestGetAllMetadatasOOM:
    """
    The specific pattern causing finding #3:
    col.get(include=["metadatas"]) with NO limit.

    Measures RSS growth to find when this becomes dangerous.
    """

    SIZES = [1_000, 2_500, 5_000, 10_000]

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_get_all_metadatas_rss(self, n_drawers, tmp_path, bench_scale):
        """RSS growth from fetching all metadata at once."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=n_drawers, include_needles=False)

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")

        rss_before = _get_rss_mb()
        start = time.perf_counter()
        all_meta = col.get(include=["metadatas"])["metadatas"]
        elapsed_ms = (time.perf_counter() - start) * 1000
        rss_after = _get_rss_mb()

        assert len(all_meta) == n_drawers
        rss_delta = rss_after - rss_before

        record_metric("chromadb_get_all", f"rss_delta_mb_at_{n_drawers}", round(rss_delta, 2))
        record_metric("chromadb_get_all", f"latency_ms_at_{n_drawers}", round(elapsed_ms, 1))


@pytest.mark.benchmark
class TestQueryDegradation:
    """Measure query latency as collection grows."""

    SIZES = [1_000, 2_500, 5_000, 10_000]

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_query_latency_at_size(self, n_drawers, tmp_path, bench_scale):
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=n_drawers, include_needles=False)

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")

        queries = [
            "authentication middleware optimization",
            "database connection pooling strategy",
            "error handling retry logic",
            "deployment pipeline configuration",
            "load balancer health check",
        ]

        latencies = []
        for q in queries:
            start = time.perf_counter()
            results = col.query(query_texts=[q], n_results=5, include=["documents", "distances"])
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)
            assert results["documents"][0]  # got results

        avg_ms = sum(latencies) / len(latencies)
        p95_ms = sorted(latencies)[int(len(latencies) * 0.95)]

        record_metric("chromadb_query", f"avg_latency_ms_at_{n_drawers}", round(avg_ms, 1))
        record_metric("chromadb_query", f"p95_latency_ms_at_{n_drawers}", round(p95_ms, 1))


@pytest.mark.benchmark
class TestBulkInsertPerformance:
    """Compare batch insertion vs sequential add_drawer pattern."""

    def test_sequential_vs_batched(self, tmp_path):
        """The current miner uses single-document add(). How much faster is batching?"""
        n_docs = 500
        gen = PalaceDataGenerator(seed=42)

        # Generate content
        contents = [gen._random_text(400, 800) for _ in range(n_docs)]

        # Sequential insertion (mimics add_drawer pattern)
        palace_seq = str(tmp_path / "seq")
        os.makedirs(palace_seq)
        client_seq = chromadb.PersistentClient(path=palace_seq)
        col_seq = client_seq.get_or_create_collection("mempalace_drawers")

        start = time.perf_counter()
        for i, content in enumerate(contents):
            col_seq.add(
                documents=[content],
                ids=[f"seq_{i}"],
                metadatas=[{"wing": "test", "room": "bench", "chunk_index": i}],
            )
        sequential_ms = (time.perf_counter() - start) * 1000

        # Batched insertion
        palace_batch = str(tmp_path / "batch")
        os.makedirs(palace_batch)
        client_batch = chromadb.PersistentClient(path=palace_batch)
        col_batch = client_batch.get_or_create_collection("mempalace_drawers")

        batch_size = 100
        start = time.perf_counter()
        for batch_start in range(0, n_docs, batch_size):
            batch_end = min(batch_start + batch_size, n_docs)
            batch_docs = contents[batch_start:batch_end]
            batch_ids = [f"batch_{i}" for i in range(batch_start, batch_end)]
            batch_metas = [
                {"wing": "test", "room": "bench", "chunk_index": i}
                for i in range(batch_start, batch_end)
            ]
            col_batch.add(documents=batch_docs, ids=batch_ids, metadatas=batch_metas)
        batched_ms = (time.perf_counter() - start) * 1000

        speedup = sequential_ms / max(batched_ms, 0.01)

        assert col_seq.count() == n_docs
        assert col_batch.count() == n_docs

        record_metric("chromadb_insert", "sequential_ms", round(sequential_ms, 1))
        record_metric("chromadb_insert", "batched_ms", round(batched_ms, 1))
        record_metric("chromadb_insert", "speedup_ratio", round(speedup, 2))
        record_metric("chromadb_insert", "n_docs", n_docs)
        record_metric("chromadb_insert", "batch_size", batch_size)


@pytest.mark.benchmark
@pytest.mark.slow
class TestMaxCollectionSize:
    """Incrementally grow collection to find practical limits."""

    def test_incremental_growth(self, tmp_path, bench_scale):
        """Add drawers in batches, measure latency per batch."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        cfg = gen.cfg
        target = min(cfg["drawers"], 10_000)  # cap at 10K for this test

        palace_path = str(tmp_path / "palace")
        os.makedirs(palace_path)
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")

        batch_size = 500
        batch_times = []
        total_inserted = 0

        for batch_num in range(0, target, batch_size):
            n = min(batch_size, target - batch_num)
            docs = [gen._random_text(400, 800) for _ in range(n)]
            ids = [f"growth_{batch_num + i}" for i in range(n)]
            metas = [
                {"wing": gen.wings[i % len(gen.wings)], "room": "bench", "chunk_index": i}
                for i in range(batch_num, batch_num + n)
            ]

            start = time.perf_counter()
            col.add(documents=docs, ids=ids, metadatas=metas)
            batch_ms = (time.perf_counter() - start) * 1000
            total_inserted += n
            batch_times.append({"at_size": total_inserted, "batch_ms": round(batch_ms, 1)})

        assert col.count() == total_inserted

        # Record first and last batch times to show degradation
        record_metric("chromadb_growth", "first_batch_ms", batch_times[0]["batch_ms"])
        record_metric("chromadb_growth", "last_batch_ms", batch_times[-1]["batch_ms"])
        record_metric("chromadb_growth", "total_inserted", total_inserted)
        record_metric("chromadb_growth", "batch_times", batch_times)
