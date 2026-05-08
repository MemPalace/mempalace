"""
Search performance benchmarks.

Measures query latency, recall@k, and concurrent search behavior
as palace size grows. Uses planted needles for recall measurement.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from tests.benchmarks.data_generator import PalaceDataGenerator
from tests.benchmarks.report import record_metric


@pytest.mark.benchmark
class TestSearchLatencyVsSize:
    """Query latency scaling as palace grows."""

    SIZES = [500, 1_000, 2_500, 5_000]

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_search_latency_curve(self, n_drawers, tmp_path, bench_scale):
        """Measure average search latency at different palace sizes."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=n_drawers, include_needles=False)

        from mempalace.searcher import search_memories

        queries = [
            "authentication middleware",
            "database optimization",
            "error handling patterns",
            "deployment configuration",
            "testing strategy",
        ]

        latencies = []
        for q in queries:
            start = time.perf_counter()
            result = search_memories(q, palace_path=palace_path, n_results=5)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)
            assert "error" not in result

        avg_ms = sum(latencies) / len(latencies)
        sorted_lat = sorted(latencies)
        p50_ms = sorted_lat[len(sorted_lat) // 2]
        p95_ms = sorted_lat[int(len(sorted_lat) * 0.95)]

        record_metric("search", f"avg_latency_ms_at_{n_drawers}", round(avg_ms, 1))
        record_metric("search", f"p50_ms_at_{n_drawers}", round(p50_ms, 1))
        record_metric("search", f"p95_ms_at_{n_drawers}", round(p95_ms, 1))


@pytest.mark.benchmark
class TestSearchRecallAtScale:
    """Planted needle recall — does accuracy degrade as palace grows?"""

    SIZES = [500, 1_000, 2_500, 5_000]

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_recall_at_k(self, n_drawers, tmp_path, bench_scale):
        """Recall@5 and Recall@10 using planted needles."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        palace_path = str(tmp_path / "palace")
        _, _, needle_info = gen.populate_palace_directly(
            palace_path, n_drawers=n_drawers, include_needles=True
        )

        from mempalace.searcher import search_memories

        hits_at_5 = 0
        hits_at_10 = 0
        total_needle_queries = min(10, len(needle_info))

        for needle in needle_info[:total_needle_queries]:
            result = search_memories(needle["query"], palace_path=palace_path, n_results=10)
            if "error" in result:
                continue

            texts = [h["text"] for h in result.get("results", [])]

            # Check if needle content appears in top 5
            found_at_5 = any("NEEDLE_" in t for t in texts[:5])
            found_at_10 = any("NEEDLE_" in t for t in texts[:10])

            if found_at_5:
                hits_at_5 += 1
            if found_at_10:
                hits_at_10 += 1

        recall_at_5 = hits_at_5 / max(total_needle_queries, 1)
        recall_at_10 = hits_at_10 / max(total_needle_queries, 1)

        record_metric("search_recall", f"recall_at_5_at_{n_drawers}", round(recall_at_5, 3))
        record_metric("search_recall", f"recall_at_10_at_{n_drawers}", round(recall_at_10, 3))


@pytest.mark.benchmark
class TestSearchFilteredVsUnfiltered:
    """Compare search performance with and without wing/room filters."""

    def test_filter_impact(self, tmp_path, bench_scale):
        """Measure latency and recall difference with wing filtering."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        palace_path = str(tmp_path / "palace")
        _, _, needle_info = gen.populate_palace_directly(
            palace_path, n_drawers=2_000, include_needles=True
        )

        from mempalace.searcher import search_memories

        filtered_latencies = []
        unfiltered_latencies = []
        filtered_hits = 0
        unfiltered_hits = 0
        n_queries = min(10, len(needle_info))

        for needle in needle_info[:n_queries]:
            # Unfiltered
            start = time.perf_counter()
            result_unfiltered = search_memories(
                needle["query"], palace_path=palace_path, n_results=5
            )
            unfiltered_latencies.append((time.perf_counter() - start) * 1000)
            if any("NEEDLE_" in h["text"] for h in result_unfiltered.get("results", [])[:5]):
                unfiltered_hits += 1

            # Filtered by wing
            start = time.perf_counter()
            result_filtered = search_memories(
                needle["query"],
                palace_path=palace_path,
                wing=needle["wing"],
                n_results=5,
            )
            filtered_latencies.append((time.perf_counter() - start) * 1000)
            if any("NEEDLE_" in h["text"] for h in result_filtered.get("results", [])[:5]):
                filtered_hits += 1

        avg_unfiltered = sum(unfiltered_latencies) / max(len(unfiltered_latencies), 1)
        avg_filtered = sum(filtered_latencies) / max(len(filtered_latencies), 1)
        latency_improvement = ((avg_unfiltered - avg_filtered) / max(avg_unfiltered, 0.01)) * 100

        record_metric("search_filter", "avg_unfiltered_ms", round(avg_unfiltered, 1))
        record_metric("search_filter", "avg_filtered_ms", round(avg_filtered, 1))
        record_metric("search_filter", "latency_improvement_pct", round(latency_improvement, 1))
        record_metric(
            "search_filter", "unfiltered_recall_at_5", round(unfiltered_hits / max(n_queries, 1), 3)
        )
        record_metric(
            "search_filter", "filtered_recall_at_5", round(filtered_hits / max(n_queries, 1), 3)
        )


@pytest.mark.benchmark
class TestConcurrentSearch:
    """Concurrent query performance — tests PersistentClient contention."""

    def test_concurrent_queries(self, tmp_path):
        """Issue N simultaneous queries and measure p50/p95/p99."""
        gen = PalaceDataGenerator(seed=42, scale="small")
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=2_000, include_needles=False)

        from mempalace.searcher import search_memories

        queries = [
            "authentication",
            "database",
            "deployment",
            "error handling",
            "testing",
            "monitoring",
            "caching",
            "middleware",
            "serialization",
            "validation",
        ] * 3  # 30 total queries

        def run_search(query):
            start = time.perf_counter()
            result = search_memories(query, palace_path=palace_path, n_results=5)
            elapsed = (time.perf_counter() - start) * 1000
            return elapsed, "error" not in result

        # Concurrent execution
        latencies = []
        errors = 0
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(run_search, q): q for q in queries}
            for future in as_completed(futures):
                elapsed, success = future.result()
                latencies.append(elapsed)
                if not success:
                    errors += 1

        sorted_lat = sorted(latencies)
        n = len(sorted_lat)

        record_metric("concurrent_search", "p50_ms", round(sorted_lat[n // 2], 1))
        record_metric("concurrent_search", "p95_ms", round(sorted_lat[int(n * 0.95)], 1))
        record_metric("concurrent_search", "p99_ms", round(sorted_lat[int(n * 0.99)], 1))
        record_metric("concurrent_search", "avg_ms", round(sum(sorted_lat) / n, 1))
        record_metric("concurrent_search", "error_count", errors)
        record_metric("concurrent_search", "total_queries", len(queries))
        record_metric("concurrent_search", "workers", 4)


@pytest.mark.benchmark
class TestSearchNResultsScaling:
    """How does n_results affect query latency?"""

    @pytest.mark.parametrize("n_results", [1, 5, 10, 25, 50])
    def test_n_results_latency(self, n_results, tmp_path):
        gen = PalaceDataGenerator(seed=42, scale="small")
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=2_000, include_needles=False)

        from mempalace.searcher import search_memories

        latencies = []
        for _ in range(5):
            start = time.perf_counter()
            search_memories(
                "authentication middleware", palace_path=palace_path, n_results=n_results
            )
            latencies.append((time.perf_counter() - start) * 1000)

        avg_ms = sum(latencies) / len(latencies)
        record_metric("search_n_results", f"avg_ms_at_n_{n_results}", round(avg_ms, 1))
