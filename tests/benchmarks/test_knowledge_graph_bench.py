"""
Knowledge graph benchmarks — SQLite temporal KG at scale.

Tests triple insertion throughput, query latency, temporal accuracy,
and SQLite concurrent access behavior.
"""

import threading
import time

import pytest

from tests.benchmarks.data_generator import PalaceDataGenerator
from tests.benchmarks.report import record_metric


@pytest.mark.benchmark
class TestTripleInsertionRate:
    """Measure triples/sec at different scales."""

    @pytest.mark.parametrize("n_triples", [200, 1_000, 5_000])
    def test_insertion_throughput(self, n_triples, tmp_path):
        gen = PalaceDataGenerator(seed=42, scale="small")
        entities, triples = gen.generate_kg_triples(
            n_entities=min(n_triples // 2, 200), n_triples=n_triples
        )

        from mempalace.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))

        # Insert entities first
        for name, etype in entities:
            kg.add_entity(name, etype)

        # Measure triple insertion
        start = time.perf_counter()
        for subject, predicate, obj, valid_from, valid_to in triples:
            kg.add_triple(subject, predicate, obj, valid_from=valid_from, valid_to=valid_to)
        elapsed = time.perf_counter() - start

        triples_per_sec = n_triples / max(elapsed, 0.001)

        record_metric("kg_insert", f"triples_per_sec_at_{n_triples}", round(triples_per_sec, 1))
        record_metric("kg_insert", f"elapsed_sec_at_{n_triples}", round(elapsed, 3))


@pytest.mark.benchmark
class TestQueryEntityLatency:
    """Query latency for entities with varying relationship counts."""

    def test_query_latency_vs_relationships(self, tmp_path):
        """Create entities with 10, 50, 100 relationships and measure query time."""
        from mempalace.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))

        # Create a hub entity connected to many others
        kg.add_entity("Hub", "person")
        target_counts = [10, 50, 100]

        for target in target_counts:
            for i in range(target):
                entity_name = f"Node_{target}_{i}"
                kg.add_entity(entity_name, "project")
                kg.add_triple("Hub", "works_on", entity_name, valid_from="2025-01-01")

        # Measure query for Hub (which has sum(target_counts) relationships)
        latencies = []
        for _ in range(20):
            start = time.perf_counter()
            kg.query_entity("Hub")
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)

        avg_ms = sum(latencies) / len(latencies)
        total_rels = sum(target_counts)

        record_metric("kg_query", f"avg_ms_with_{total_rels}_rels", round(avg_ms, 2))
        record_metric("kg_query", "total_relationships", total_rels)


@pytest.mark.benchmark
class TestTimelinePerformance:
    """timeline() with no entity filter does a full table scan."""

    @pytest.mark.parametrize("n_triples", [200, 1_000, 5_000])
    def test_timeline_latency(self, n_triples, tmp_path):
        from mempalace.knowledge_graph import KnowledgeGraph

        gen = PalaceDataGenerator(seed=42)
        entities, triples = gen.generate_kg_triples(
            n_entities=min(n_triples // 2, 200), n_triples=n_triples
        )

        kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))
        for name, etype in entities:
            kg.add_entity(name, etype)
        for subject, predicate, obj, valid_from, valid_to in triples:
            kg.add_triple(subject, predicate, obj, valid_from=valid_from, valid_to=valid_to)

        # Measure timeline (no filter = full scan with LIMIT 100)
        latencies = []
        for _ in range(10):
            start = time.perf_counter()
            kg.timeline()
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)

        avg_ms = sum(latencies) / len(latencies)
        record_metric("kg_timeline", f"avg_ms_at_{n_triples}", round(avg_ms, 2))


@pytest.mark.benchmark
class TestTemporalQueryAccuracy:
    """Verify temporal filtering correctness at scale."""

    def test_as_of_filtering(self, tmp_path):
        """Insert triples with known temporal ranges, verify as_of queries."""
        from mempalace.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))

        kg.add_entity("Alice", "person")
        kg.add_entity("ProjectA", "project")
        kg.add_entity("ProjectB", "project")

        # Alice worked on ProjectA from 2024-01 to 2024-06
        kg.add_triple(
            "Alice", "works_on", "ProjectA", valid_from="2024-01-01", valid_to="2024-06-30"
        )
        # Alice worked on ProjectB from 2024-07 onwards
        kg.add_triple("Alice", "works_on", "ProjectB", valid_from="2024-07-01")

        # Add noise triples
        gen = PalaceDataGenerator(seed=42)
        entities, triples = gen.generate_kg_triples(n_entities=50, n_triples=500)
        for name, etype in entities:
            kg.add_entity(name, etype)
        for subject, predicate, obj, valid_from, valid_to in triples:
            kg.add_triple(subject, predicate, obj, valid_from=valid_from, valid_to=valid_to)

        # Query Alice as of March 2024 — should find ProjectA
        result_march = kg.query_entity("Alice", as_of="2024-03-15")
        # Query Alice as of September 2024 — should find ProjectB
        result_sept = kg.query_entity("Alice", as_of="2024-09-15")

        record_metric(
            "kg_temporal",
            "march_query_results",
            len(result_march) if isinstance(result_march, list) else 0,
        )
        record_metric(
            "kg_temporal",
            "sept_query_results",
            len(result_sept) if isinstance(result_sept, list) else 0,
        )


@pytest.mark.benchmark
class TestSQLiteConcurrentAccess:
    """Test concurrent read/write behavior with SQLite (finding #8)."""

    def test_concurrent_writers(self, tmp_path):
        """N threads writing triples simultaneously — count lock failures."""
        from mempalace.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))

        # Pre-create entities
        for i in range(100):
            kg.add_entity(f"Entity_{i}", "concept")

        n_threads = 4
        triples_per_thread = 50
        lock_failures = []
        successes = []

        def writer(thread_id):
            fails = 0
            ok = 0
            for i in range(triples_per_thread):
                try:
                    kg.add_triple(
                        f"Entity_{thread_id * 10}",
                        "relates_to",
                        f"Entity_{(thread_id * 10 + i) % 100}",
                        valid_from="2025-01-01",
                    )
                    ok += 1
                except Exception:
                    fails += 1
            lock_failures.append(fails)
            successes.append(ok)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        elapsed = time.perf_counter() - start

        total_failures = sum(lock_failures)
        total_successes = sum(successes)

        record_metric("kg_concurrent", "total_failures", total_failures)
        record_metric("kg_concurrent", "total_successes", total_successes)
        record_metric("kg_concurrent", "elapsed_sec", round(elapsed, 2))
        record_metric("kg_concurrent", "threads", n_threads)
        record_metric("kg_concurrent", "triples_per_thread", triples_per_thread)

    def test_concurrent_read_write(self, tmp_path):
        """Readers and writers running simultaneously."""
        from mempalace.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))

        # Seed some data
        for i in range(50):
            kg.add_entity(f"E_{i}", "concept")
        for i in range(200):
            kg.add_triple(f"E_{i % 50}", "links", f"E_{(i + 1) % 50}", valid_from="2025-01-01")

        read_errors = []
        write_errors = []

        def reader():
            fails = 0
            for i in range(50):
                try:
                    kg.query_entity(f"E_{i % 50}")
                except Exception:
                    fails += 1
            read_errors.append(fails)

        def writer():
            fails = 0
            for i in range(50):
                try:
                    kg.add_triple(
                        f"E_{i % 50}", "new_rel", f"E_{(i + 7) % 50}", valid_from="2025-06-01"
                    )
                except Exception:
                    fails += 1
            write_errors.append(fails)

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
            threading.Thread(target=writer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        record_metric("kg_concurrent_rw", "read_errors", sum(read_errors))
        record_metric("kg_concurrent_rw", "write_errors", sum(write_errors))


@pytest.mark.benchmark
class TestKGStats:
    """Measure stats() performance as graph grows."""

    @pytest.mark.parametrize("n_triples", [200, 1_000, 5_000])
    def test_stats_latency(self, n_triples, tmp_path):
        from mempalace.knowledge_graph import KnowledgeGraph

        gen = PalaceDataGenerator(seed=42)
        entities, triples = gen.generate_kg_triples(
            n_entities=min(n_triples // 2, 200), n_triples=n_triples
        )

        kg = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))
        for name, etype in entities:
            kg.add_entity(name, etype)
        for subject, predicate, obj, valid_from, valid_to in triples:
            kg.add_triple(subject, predicate, obj, valid_from=valid_from, valid_to=valid_to)

        latencies = []
        for _ in range(10):
            start = time.perf_counter()
            kg.stats()
            elapsed_ms = (time.perf_counter() - start) * 1000
            latencies.append(elapsed_ms)

        avg_ms = sum(latencies) / len(latencies)
        record_metric("kg_stats", f"avg_ms_at_{n_triples}", round(avg_ms, 2))
