"""
Memory stack (layers.py) benchmarks.

Tests MemoryStack.wake_up(), Layer1.generate(), and Layer2/L3
at scale. Layer1 has the same unbounded col.get() as tool_status.
"""

import time

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
class TestWakeUpCost:
    """Measure wake_up() time (L0 + L1) at different palace sizes."""

    SIZES = [500, 1_000, 2_500, 5_000]

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_wakeup_latency(self, n_drawers, tmp_path, bench_scale):
        """L0+L1 generation time grows with palace size because L1 fetches all."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=n_drawers, include_needles=False)

        # Create identity file
        identity_path = str(tmp_path / "identity.txt")
        with open(identity_path, "w") as f:
            f.write("I am a test AI. Traits: precise, fast.\n")

        from mempalace.layers import MemoryStack

        stack = MemoryStack(palace_path=palace_path, identity_path=identity_path)

        latencies = []
        for _ in range(5):
            start = time.perf_counter()
            text = stack.wake_up()
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)
            assert "L0" in text or "L1" in text or "IDENTITY" in text or "ESSENTIAL" in text

        avg_ms = sum(latencies) / len(latencies)
        record_metric("layers_wakeup", f"avg_ms_at_{n_drawers}", round(avg_ms, 1))


@pytest.mark.benchmark
class TestLayer1UnboundedFetch:
    """Layer1.generate() fetches ALL drawers — same pattern as tool_status."""

    SIZES = [500, 1_000, 2_500, 5_000]

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_layer1_rss_growth(self, n_drawers, tmp_path):
        """Track RSS from Layer1 fetching all drawers at different sizes."""
        gen = PalaceDataGenerator(seed=42, scale="small")
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=n_drawers, include_needles=False)

        from mempalace.layers import Layer1

        layer = Layer1(palace_path=palace_path)

        rss_before = _get_rss_mb()
        start = time.perf_counter()
        text = layer.generate()
        elapsed_ms = (time.perf_counter() - start) * 1000
        rss_after = _get_rss_mb()

        rss_delta = rss_after - rss_before
        assert "L1" in text

        record_metric("layer1", f"latency_ms_at_{n_drawers}", round(elapsed_ms, 1))
        record_metric("layer1", f"rss_delta_mb_at_{n_drawers}", round(rss_delta, 2))

    def test_layer1_wing_filtered(self, tmp_path):
        """Wing-filtered Layer1 should fetch fewer drawers."""
        gen = PalaceDataGenerator(seed=42, scale="small")
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=2_000, include_needles=False)

        from mempalace.layers import Layer1

        wing = gen.wings[0]

        # Unfiltered
        layer_all = Layer1(palace_path=palace_path)
        start = time.perf_counter()
        layer_all.generate()
        unfiltered_ms = (time.perf_counter() - start) * 1000

        # Wing-filtered
        layer_wing = Layer1(palace_path=palace_path, wing=wing)
        start = time.perf_counter()
        layer_wing.generate()
        filtered_ms = (time.perf_counter() - start) * 1000

        record_metric("layer1_filter", "unfiltered_ms", round(unfiltered_ms, 1))
        record_metric("layer1_filter", "filtered_ms", round(filtered_ms, 1))
        if unfiltered_ms > 0:
            record_metric(
                "layer1_filter", "speedup_pct", round((1 - filtered_ms / unfiltered_ms) * 100, 1)
            )


@pytest.mark.benchmark
class TestWakeUpTokenBudget:
    """Verify L0+L1 stays within token budget even at large palace sizes."""

    SIZES = [500, 1_000, 2_500, 5_000]

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_token_budget(self, n_drawers, tmp_path):
        """L1 has MAX_CHARS=3200 cap. Verify it holds at scale."""
        gen = PalaceDataGenerator(seed=42, scale="small")
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=n_drawers, include_needles=False)

        identity_path = str(tmp_path / "identity.txt")
        with open(identity_path, "w") as f:
            f.write("I am a benchmark AI.\n")

        from mempalace.layers import MemoryStack

        stack = MemoryStack(palace_path=palace_path, identity_path=identity_path)
        text = stack.wake_up()
        token_estimate = len(text) // 4

        # Budget is ~600-900 tokens. Allow up to 1200 for safety margin.
        record_metric("wakeup_budget", f"tokens_at_{n_drawers}", token_estimate)
        record_metric("wakeup_budget", f"chars_at_{n_drawers}", len(text))

        assert (
            token_estimate < 1200
        ), f"Wake-up exceeded budget: ~{token_estimate} tokens at {n_drawers} drawers"


@pytest.mark.benchmark
class TestLayer2Retrieval:
    """Layer2 on-demand retrieval with filters."""

    def test_layer2_latency(self, tmp_path, bench_scale):
        """L2 retrieval with wing filter at scale."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=2_000, include_needles=False)

        from mempalace.layers import Layer2

        layer = Layer2(palace_path=palace_path)
        wing = gen.wings[0]

        latencies = []
        for _ in range(10):
            start = time.perf_counter()
            layer.retrieve(wing=wing, n_results=10)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)

        avg_ms = sum(latencies) / len(latencies)
        record_metric("layer2", "avg_retrieval_ms", round(avg_ms, 1))


@pytest.mark.benchmark
class TestLayer3Search:
    """Layer3 semantic search through the MemoryStack interface."""

    def test_layer3_latency(self, tmp_path, bench_scale):
        """L3 search latency through MemoryStack."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        palace_path = str(tmp_path / "palace")
        gen.populate_palace_directly(palace_path, n_drawers=2_000, include_needles=False)

        identity_path = str(tmp_path / "identity.txt")
        with open(identity_path, "w") as f:
            f.write("I am a benchmark AI.\n")

        from mempalace.layers import MemoryStack

        stack = MemoryStack(palace_path=palace_path, identity_path=identity_path)

        queries = ["authentication", "database", "deployment", "testing", "monitoring"]
        latencies = []
        for q in queries:
            start = time.perf_counter()
            stack.search(q, n_results=5)
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)

        avg_ms = sum(latencies) / len(latencies)
        record_metric("layer3", "avg_search_ms", round(avg_ms, 1))
