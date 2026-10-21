"""
MCP server tool performance benchmarks.

Validates production readiness findings:
  - Finding #3: tool_status() unbounded col.get(include=["metadatas"]) → OOM
  - Finding #7: _get_collection() re-instantiates PersistentClient every call
  - Finding #3 variants: tool_list_wings(), tool_get_taxonomy() same pattern

Calls MCP tool handler functions directly with monkeypatched _config.
"""

import time

import chromadb
import pytest

from tests.benchmarks.data_generator import PalaceDataGenerator
from tests.benchmarks.report import record_metric


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_palace(tmp_path, n_drawers, scale="small"):
    """Create a palace with exactly n_drawers, return palace_path."""
    gen = PalaceDataGenerator(seed=42, scale=scale)
    palace_path = str(tmp_path / "palace")
    gen.populate_palace_directly(palace_path, n_drawers=n_drawers, include_needles=False)
    return palace_path


def _patch_mcp_config(monkeypatch, palace_path, tmp_path):
    """Monkeypatch mcp_server._config and _kg to point at test dirs."""
    from mempalace.config import MempalaceConfig
    from mempalace.knowledge_graph import KnowledgeGraph

    cfg = MempalaceConfig(config_dir=str(tmp_path / "cfg"))
    # Override palace_path directly on the object
    monkeypatch.setattr(cfg, "_file_config", {"palace_path": palace_path})

    import mempalace.mcp_server as mcp_mod

    kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))
    monkeypatch.setattr(mcp_mod, "_config", cfg)
    monkeypatch.setattr(mcp_mod, "_get_kg", lambda: kg)


def _get_rss_mb():
    """Get current process RSS in MB."""
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except ImportError:
        import resource

        # ru_maxrss is in KB on Linux, bytes on macOS
        import platform

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if platform.system() == "Darwin":
            return usage / (1024 * 1024)
        return usage / 1024


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.benchmark
class TestToolStatusOOM:
    """Finding #3: tool_status loads ALL metadata into memory."""

    SIZES = [500, 1_000, 2_500, 5_000]

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_tool_status_rss_growth(self, n_drawers, tmp_path, monkeypatch):
        """Measure RSS growth from tool_status at different palace sizes."""
        palace_path = _make_palace(tmp_path, n_drawers)
        _patch_mcp_config(monkeypatch, palace_path, tmp_path)

        from mempalace.mcp_server import tool_status

        rss_before = _get_rss_mb()
        result = tool_status()
        rss_after = _get_rss_mb()

        rss_delta = rss_after - rss_before
        assert "error" not in result, f"tool_status failed: {result}"
        assert result["total_drawers"] == n_drawers

        record_metric("mcp_status", f"rss_delta_mb_at_{n_drawers}", round(rss_delta, 2))

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_tool_status_latency(self, n_drawers, tmp_path, monkeypatch):
        """Measure tool_status response time at different palace sizes."""
        palace_path = _make_palace(tmp_path, n_drawers)
        _patch_mcp_config(monkeypatch, palace_path, tmp_path)

        from mempalace.mcp_server import tool_status

        # Warm up
        tool_status()

        start = time.perf_counter()
        result = tool_status()
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert "error" not in result
        record_metric("mcp_status", f"latency_ms_at_{n_drawers}", round(elapsed_ms, 1))


@pytest.mark.benchmark
class TestToolListWingsUnbounded:
    """Finding #3 variant: tool_list_wings also fetches ALL metadata."""

    @pytest.mark.parametrize("n_drawers", [500, 1_000, 2_500, 5_000])
    def test_list_wings_latency(self, n_drawers, tmp_path, monkeypatch):
        palace_path = _make_palace(tmp_path, n_drawers)
        _patch_mcp_config(monkeypatch, palace_path, tmp_path)

        from mempalace.mcp_server import tool_list_wings

        start = time.perf_counter()
        result = tool_list_wings()
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert "wings" in result
        record_metric("mcp_list_wings", f"latency_ms_at_{n_drawers}", round(elapsed_ms, 1))


@pytest.mark.benchmark
class TestToolGetTaxonomyUnbounded:
    """Finding #3 variant: tool_get_taxonomy also fetches ALL metadata."""

    @pytest.mark.parametrize("n_drawers", [500, 1_000, 2_500, 5_000])
    def test_get_taxonomy_latency(self, n_drawers, tmp_path, monkeypatch):
        palace_path = _make_palace(tmp_path, n_drawers)
        _patch_mcp_config(monkeypatch, palace_path, tmp_path)

        from mempalace.mcp_server import tool_get_taxonomy

        start = time.perf_counter()
        result = tool_get_taxonomy()
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert "taxonomy" in result
        record_metric("mcp_taxonomy", f"latency_ms_at_{n_drawers}", round(elapsed_ms, 1))


@pytest.mark.benchmark
class TestClientReinstantiation:
    """Finding #7: _get_collection() creates new PersistentClient every call."""

    def test_reinstantiation_overhead(self, tmp_path, monkeypatch):
        """Measure cost of 50 _get_collection() calls vs a cached client."""
        palace_path = _make_palace(tmp_path, 500)
        _patch_mcp_config(monkeypatch, palace_path, tmp_path)

        from mempalace.mcp_server import _get_collection

        n_calls = 50

        # Measure re-instantiation (current behavior)
        start = time.perf_counter()
        for _ in range(n_calls):
            col = _get_collection()
            assert col is not None
        uncached_ms = (time.perf_counter() - start) * 1000

        # Measure cached client (what it should be)
        client = chromadb.PersistentClient(path=palace_path)
        cached_col = client.get_collection("mempalace_drawers")
        start = time.perf_counter()
        for _ in range(n_calls):
            _ = cached_col.count()
        cached_ms = (time.perf_counter() - start) * 1000

        overhead_ratio = uncached_ms / max(cached_ms, 0.01)

        record_metric("client_reinstantiation", "uncached_total_ms", round(uncached_ms, 1))
        record_metric("client_reinstantiation", "cached_total_ms", round(cached_ms, 1))
        record_metric("client_reinstantiation", "overhead_ratio", round(overhead_ratio, 2))
        record_metric("client_reinstantiation", "n_calls", n_calls)


@pytest.mark.benchmark
class TestToolSearchLatency:
    """tool_search uses query() not get(), should scale better."""

    @pytest.mark.parametrize("n_drawers", [500, 1_000, 2_500, 5_000])
    def test_search_latency(self, n_drawers, tmp_path, monkeypatch):
        palace_path = _make_palace(tmp_path, n_drawers)
        _patch_mcp_config(monkeypatch, palace_path, tmp_path)

        from mempalace.mcp_server import tool_search

        queries = ["authentication middleware", "database migration", "error handling"]
        latencies = []
        for q in queries:
            start = time.perf_counter()
            result = tool_search(query=q, limit=5)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)
            assert "error" not in result

        avg_ms = sum(latencies) / len(latencies)
        record_metric("mcp_search", f"avg_latency_ms_at_{n_drawers}", round(avg_ms, 1))


@pytest.mark.benchmark
class TestDuplicateCheckCost:
    """tool_add_drawer calls tool_check_duplicate first — measure overhead."""

    @pytest.mark.parametrize("n_drawers", [500, 1_000, 2_500])
    def test_duplicate_check_latency(self, n_drawers, tmp_path, monkeypatch):
        palace_path = _make_palace(tmp_path, n_drawers)
        _patch_mcp_config(monkeypatch, palace_path, tmp_path)

        from mempalace.mcp_server import tool_check_duplicate

        test_content = "This is unique test content for duplicate checking benchmark."
        start = time.perf_counter()
        result = tool_check_duplicate(content=test_content)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert "error" not in result
        record_metric("mcp_duplicate_check", f"latency_ms_at_{n_drawers}", round(elapsed_ms, 1))
