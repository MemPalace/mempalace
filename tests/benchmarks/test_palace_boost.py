"""
Palace boost validation — does wing/room filtering actually help?

Quantifies the retrieval improvement from the palace spatial metaphor.
Uses planted needles to measure recall with and without filtering
at different scales.
"""

import time

import pytest

from tests.benchmarks.data_generator import PalaceDataGenerator
from tests.benchmarks.report import record_metric


@pytest.mark.benchmark
class TestFilteredVsUnfilteredRecall:
    """Quantify palace boost: recall improvement from wing/room filtering."""

    SIZES = [1_000, 2_500, 5_000]

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_palace_boost_recall(self, n_drawers, tmp_path, bench_scale):
        """Compare recall@5 with/without wing filter at increasing scale."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        palace_path = str(tmp_path / "palace")
        _, _, needle_info = gen.populate_palace_directly(
            palace_path, n_drawers=n_drawers, include_needles=True
        )

        from mempalace.searcher import search_memories

        n_queries = min(10, len(needle_info))
        unfiltered_hits = 0
        wing_filtered_hits = 0
        room_filtered_hits = 0

        for needle in needle_info[:n_queries]:
            # Unfiltered search
            result = search_memories(needle["query"], palace_path=palace_path, n_results=5)
            texts = [h["text"] for h in result.get("results", [])]
            if any("NEEDLE_" in t for t in texts[:5]):
                unfiltered_hits += 1

            # Wing-filtered search
            result = search_memories(
                needle["query"], palace_path=palace_path, wing=needle["wing"], n_results=5
            )
            texts = [h["text"] for h in result.get("results", [])]
            if any("NEEDLE_" in t for t in texts[:5]):
                wing_filtered_hits += 1

            # Wing+room filtered search
            result = search_memories(
                needle["query"],
                palace_path=palace_path,
                wing=needle["wing"],
                room=needle["room"],
                n_results=5,
            )
            texts = [h["text"] for h in result.get("results", [])]
            if any("NEEDLE_" in t for t in texts[:5]):
                room_filtered_hits += 1

        recall_none = unfiltered_hits / max(n_queries, 1)
        recall_wing = wing_filtered_hits / max(n_queries, 1)
        recall_room = room_filtered_hits / max(n_queries, 1)

        boost_wing = recall_wing - recall_none
        boost_room = recall_room - recall_none

        record_metric("palace_boost", f"recall_unfiltered_at_{n_drawers}", round(recall_none, 3))
        record_metric("palace_boost", f"recall_wing_filtered_at_{n_drawers}", round(recall_wing, 3))
        record_metric("palace_boost", f"recall_room_filtered_at_{n_drawers}", round(recall_room, 3))
        record_metric("palace_boost", f"wing_boost_at_{n_drawers}", round(boost_wing, 3))
        record_metric("palace_boost", f"room_boost_at_{n_drawers}", round(boost_room, 3))


@pytest.mark.benchmark
class TestFilterLatencyBenefit:
    """Does filtering reduce query latency by narrowing the search space?"""

    def test_filter_speedup(self, tmp_path, bench_scale):
        """Compare latency: no filter vs wing vs wing+room."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=5_000, include_needles=False)

        from mempalace.searcher import search_memories

        wing = gen.wings[0]
        room = gen.rooms_by_wing[wing][0]
        query = "authentication middleware optimization"
        n_runs = 10

        # No filter
        latencies_none = []
        for _ in range(n_runs):
            start = time.perf_counter()
            search_memories(query, palace_path=palace_path, n_results=5)
            latencies_none.append((time.perf_counter() - start) * 1000)

        # Wing filter
        latencies_wing = []
        for _ in range(n_runs):
            start = time.perf_counter()
            search_memories(query, palace_path=palace_path, wing=wing, n_results=5)
            latencies_wing.append((time.perf_counter() - start) * 1000)

        # Wing + room filter
        latencies_room = []
        for _ in range(n_runs):
            start = time.perf_counter()
            search_memories(query, palace_path=palace_path, wing=wing, room=room, n_results=5)
            latencies_room.append((time.perf_counter() - start) * 1000)

        avg_none = sum(latencies_none) / len(latencies_none)
        avg_wing = sum(latencies_wing) / len(latencies_wing)
        avg_room = sum(latencies_room) / len(latencies_room)

        record_metric("filter_latency", "avg_unfiltered_ms", round(avg_none, 1))
        record_metric("filter_latency", "avg_wing_filtered_ms", round(avg_wing, 1))
        record_metric("filter_latency", "avg_room_filtered_ms", round(avg_room, 1))
        if avg_none > 0:
            record_metric(
                "filter_latency", "wing_speedup_pct", round((1 - avg_wing / avg_none) * 100, 1)
            )
            record_metric(
                "filter_latency", "room_speedup_pct", round((1 - avg_room / avg_none) * 100, 1)
            )


@pytest.mark.benchmark
class TestBoostAtIncreasingScale:
    """Does the palace boost increase as the palace grows?"""

    def test_boost_scaling(self, tmp_path, bench_scale):
        """Measure wing-filtered recall improvement at multiple sizes."""
        sizes = [500, 1_000, 2_500]
        boosts = []

        for size in sizes:
            gen = PalaceDataGenerator(seed=42, scale=bench_scale)
            palace_path = str(tmp_path / f"palace_{size}")
            _, _, needle_info = gen.populate_palace_directly(
                palace_path, n_drawers=size, include_needles=True
            )

            from mempalace.searcher import search_memories

            n_queries = min(8, len(needle_info))
            unfiltered_hits = 0
            filtered_hits = 0

            for needle in needle_info[:n_queries]:
                result = search_memories(needle["query"], palace_path=palace_path, n_results=5)
                if any("NEEDLE_" in h["text"] for h in result.get("results", [])[:5]):
                    unfiltered_hits += 1

                result = search_memories(
                    needle["query"], palace_path=palace_path, wing=needle["wing"], n_results=5
                )
                if any("NEEDLE_" in h["text"] for h in result.get("results", [])[:5]):
                    filtered_hits += 1

            recall_none = unfiltered_hits / max(n_queries, 1)
            recall_filtered = filtered_hits / max(n_queries, 1)
            boost = recall_filtered - recall_none
            boosts.append({"size": size, "boost": boost})

        record_metric("boost_scaling", "boosts_by_size", boosts)
        # Check if boost increases with scale (the hypothesis)
        if len(boosts) >= 2:
            trend_positive = boosts[-1]["boost"] >= boosts[0]["boost"]
            record_metric("boost_scaling", "trend_positive", trend_positive)
