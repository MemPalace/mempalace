"""
Ingestion throughput benchmarks.

Measures mining performance at scale:
  - Files/sec and drawers/sec through the full mine() pipeline
  - Peak RSS during mining
  - Chunking throughput isolated from ChromaDB
  - Re-ingest skip overhead (finding #11: file_already_mined check)
"""

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
class TestMineThroughput:
    """Measure the full mine() pipeline throughput."""

    @pytest.mark.parametrize("n_files", [20, 50, 100])
    def test_mine_files_per_second(self, n_files, tmp_path, bench_scale):
        """End-to-end mining throughput: generate files, mine, count drawers."""
        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        project_path, wing, rooms, files_written = gen.generate_project_tree(
            tmp_path / "project", n_files=n_files
        )
        palace_path = str(tmp_path / "palace")

        from mempalace.miner import mine

        start = time.perf_counter()
        mine(project_path, palace_path)
        elapsed = time.perf_counter() - start

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        drawer_count = col.count()

        files_per_sec = files_written / max(elapsed, 0.001)
        drawers_per_sec = drawer_count / max(elapsed, 0.001)

        record_metric("ingest", f"files_per_sec_at_{n_files}", round(files_per_sec, 1))
        record_metric("ingest", f"drawers_per_sec_at_{n_files}", round(drawers_per_sec, 1))
        record_metric("ingest", f"elapsed_sec_at_{n_files}", round(elapsed, 2))
        record_metric("ingest", f"drawers_created_at_{n_files}", drawer_count)

    def test_mine_peak_rss(self, tmp_path, bench_scale):
        """Track peak RSS during a mining run."""
        import threading

        gen = PalaceDataGenerator(seed=42, scale=bench_scale)
        project_path, wing, rooms, files_written = gen.generate_project_tree(
            tmp_path / "project", n_files=100
        )
        palace_path = str(tmp_path / "palace")

        from mempalace.miner import mine

        rss_samples = []
        stop_sampling = threading.Event()

        def sample_rss():
            while not stop_sampling.is_set():
                rss_samples.append(_get_rss_mb())
                stop_sampling.wait(0.1)

        sampler = threading.Thread(target=sample_rss, daemon=True)
        sampler.start()

        rss_before = _get_rss_mb()
        mine(project_path, palace_path)
        stop_sampling.set()
        sampler.join(timeout=1)

        peak_rss = max(rss_samples) if rss_samples else _get_rss_mb()
        rss_delta = peak_rss - rss_before

        record_metric("ingest", "peak_rss_mb", round(peak_rss, 1))
        record_metric("ingest", "rss_delta_mb", round(rss_delta, 1))


@pytest.mark.benchmark
class TestChunkThroughput:
    """Isolate chunking performance from ChromaDB insertion."""

    @pytest.mark.parametrize("content_size_kb", [1, 10, 100])
    def test_chunk_text_throughput(self, content_size_kb):
        """Measure chunk_text speed for different content sizes."""
        from mempalace.miner import chunk_text

        gen = PalaceDataGenerator(seed=42)
        # Generate content of target size
        content = gen._random_text(content_size_kb * 500, content_size_kb * 1200)
        # Pad to approximate target KB
        while len(content) < content_size_kb * 1024:
            content += "\n" + gen._random_text(200, 500)

        n_iterations = 50
        start = time.perf_counter()
        total_chunks = 0
        for _ in range(n_iterations):
            chunks = chunk_text(content, "bench_file.py")
            total_chunks += len(chunks)
        elapsed = time.perf_counter() - start

        chunks_per_sec = total_chunks / max(elapsed, 0.001)
        kb_per_sec = (len(content) * n_iterations / 1024) / max(elapsed, 0.001)

        record_metric(
            "chunking", f"chunks_per_sec_at_{content_size_kb}kb", round(chunks_per_sec, 1)
        )
        record_metric("chunking", f"kb_per_sec_at_{content_size_kb}kb", round(kb_per_sec, 1))


@pytest.mark.benchmark
class TestReingestSkipOverhead:
    """Finding #11: file_already_mined() check overhead at scale."""

    def test_skip_check_cost(self, tmp_path):
        """Mine files, then re-mine — measure cost of skip checks."""
        gen = PalaceDataGenerator(seed=42, scale="small")
        project_path, wing, rooms, files_written = gen.generate_project_tree(
            tmp_path / "project", n_files=50
        )
        palace_path = str(tmp_path / "palace")

        from mempalace.miner import mine

        # First mine
        mine(project_path, palace_path)
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
        initial_count = col.count()

        # Re-mine (all files should be skipped)
        start = time.perf_counter()
        mine(project_path, palace_path)
        skip_elapsed = time.perf_counter() - start

        # Verify no new drawers added
        final_count = col.count()
        assert final_count == initial_count, "Re-mine should not add new drawers"

        record_metric("reingest", "skip_check_elapsed_sec", round(skip_elapsed, 2))
        record_metric("reingest", "files_checked", files_written)
        record_metric(
            "reingest",
            "skip_check_per_file_ms",
            round(skip_elapsed * 1000 / max(files_written, 1), 1),
        )
