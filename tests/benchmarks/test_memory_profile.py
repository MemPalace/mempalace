"""
Memory profiling benchmarks — detect leaks and measure RSS growth.

Uses tracemalloc for heap snapshots and psutil/resource for RSS.
Targets the highest-risk code paths:
  - Repeated search() calls (PersistentClient re-instantiation)
  - Repeated tool_status() calls (unbounded metadata fetch)
  - Layer1.generate() (fetches all drawers)
"""

import tracemalloc

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
class TestSearchMemoryProfile:
    """Track RSS growth over repeated search_memories() calls."""

    def test_search_rss_growth(self, tmp_path):
        """Issue 200 searches and track RSS every 50 calls."""
        gen = PalaceDataGenerator(seed=42, scale="small")
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=1_000, include_needles=False)

        from mempalace.searcher import search_memories

        n_calls = 200
        check_interval = 50
        queries = ["authentication", "database", "deployment", "error handling", "testing"]
        rss_readings = []
        rss_readings.append(("start", _get_rss_mb()))

        for i in range(n_calls):
            q = queries[i % len(queries)]
            search_memories(q, palace_path=palace_path, n_results=5)
            if (i + 1) % check_interval == 0:
                rss_readings.append((f"after_{i + 1}", _get_rss_mb()))

        start_rss = rss_readings[0][1]
        end_rss = rss_readings[-1][1]
        growth = end_rss - start_rss

        record_metric("memory_search", "rss_start_mb", round(start_rss, 2))
        record_metric("memory_search", "rss_end_mb", round(end_rss, 2))
        record_metric("memory_search", "rss_growth_mb", round(growth, 2))
        record_metric("memory_search", "n_calls", n_calls)
        record_metric(
            "memory_search", "growth_per_100_calls_mb", round(growth / (n_calls / 100), 2)
        )


@pytest.mark.benchmark
class TestToolStatusMemoryProfile:
    """Track RSS growth from repeated tool_status() calls."""

    def test_tool_status_repeated_calls(self, tmp_path, monkeypatch):
        """tool_status loads ALL metadata each call — does it leak?"""
        gen = PalaceDataGenerator(seed=42, scale="small")
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=2_000, include_needles=False)

        from mempalace.config import MempalaceConfig
        from mempalace.knowledge_graph import KnowledgeGraph
        import mempalace.mcp_server as mcp_mod

        cfg = MempalaceConfig(config_dir=str(tmp_path / "cfg"))
        monkeypatch.setattr(cfg, "_file_config", {"palace_path": palace_path})
        kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))
        monkeypatch.setattr(mcp_mod, "_config", cfg)
        monkeypatch.setattr(mcp_mod, "_get_kg", lambda: kg)

        from mempalace.mcp_server import tool_status

        n_calls = 50
        rss_readings = []
        rss_readings.append(("start", _get_rss_mb()))

        for i in range(n_calls):
            result = tool_status()
            assert result["total_drawers"] == 2_000
            if (i + 1) % 10 == 0:
                rss_readings.append((f"after_{i + 1}", _get_rss_mb()))

        start_rss = rss_readings[0][1]
        end_rss = rss_readings[-1][1]
        growth = end_rss - start_rss

        record_metric("memory_tool_status", "rss_start_mb", round(start_rss, 2))
        record_metric("memory_tool_status", "rss_end_mb", round(end_rss, 2))
        record_metric("memory_tool_status", "rss_growth_mb", round(growth, 2))
        record_metric("memory_tool_status", "n_calls", n_calls)
        record_metric("memory_tool_status", "palace_size", 2_000)


@pytest.mark.benchmark
class TestLayer1MemoryProfile:
    """Layer1.generate() fetches ALL drawers — same risk as tool_status."""

    def test_layer1_repeated_generate(self, tmp_path):
        """Layer1 fetches all drawers for scoring. Track memory over repeats."""
        gen = PalaceDataGenerator(seed=42, scale="small")
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=2_000, include_needles=False)

        from mempalace.layers import Layer1

        layer = Layer1(palace_path=palace_path)

        n_calls = 30
        rss_readings = []
        rss_readings.append(("start", _get_rss_mb()))

        for i in range(n_calls):
            text = layer.generate()
            assert "L1" in text
            if (i + 1) % 10 == 0:
                rss_readings.append((f"after_{i + 1}", _get_rss_mb()))

        start_rss = rss_readings[0][1]
        end_rss = rss_readings[-1][1]
        growth = end_rss - start_rss

        record_metric("memory_layer1", "rss_start_mb", round(start_rss, 2))
        record_metric("memory_layer1", "rss_end_mb", round(end_rss, 2))
        record_metric("memory_layer1", "rss_growth_mb", round(growth, 2))
        record_metric("memory_layer1", "n_calls", n_calls)


@pytest.mark.benchmark
class TestHeapSnapshot:
    """Use tracemalloc to identify top memory allocators during search."""

    def test_search_heap_top_allocators(self, tmp_path):
        """Identify which code paths allocate the most memory during search."""
        gen = PalaceDataGenerator(seed=42, scale="small")
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=1_000, include_needles=False)

        from mempalace.searcher import search_memories

        tracemalloc.start()
        snap_before = tracemalloc.take_snapshot()

        for i in range(100):
            search_memories("test query", palace_path=palace_path, n_results=5)

        snap_after = tracemalloc.take_snapshot()
        tracemalloc.stop()

        stats = snap_after.compare_to(snap_before, "lineno")
        top_allocators = []
        for stat in stats[:10]:
            top_allocators.append(
                {
                    "file": str(stat.traceback),
                    "size_kb": round(stat.size / 1024, 1),
                    "count": stat.count,
                }
            )

        total_growth_kb = sum(s["size_kb"] for s in top_allocators)
        record_metric("heap_search", "top_10_growth_kb", round(total_growth_kb, 1))
        record_metric("heap_search", "n_searches", 100)
