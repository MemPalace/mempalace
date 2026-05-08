"""
Recall threshold test — find the per-bucket size where retrieval breaks.

The palace_boost tests showed room-filtered recall of 1.0, but only because
each room had ~333 drawers. This test concentrates ALL drawers into a single
wing+room to find the actual embedding model limit.
"""

import hashlib
import os
from datetime import datetime

import chromadb
import pytest

from tests.benchmarks.data_generator import PalaceDataGenerator
from tests.benchmarks.report import record_metric


NEEDLE_TOPICS = [
    "Fibonacci sequence optimization uses memoization with O(n) space complexity",
    "PostgreSQL vacuum autovacuum threshold set to 50 percent for table users",
    "Redis cluster failover timeout configured at 30 seconds with sentinel monitoring",
    "Kubernetes horizontal pod autoscaler targets 70 percent CPU utilization",
    "GraphQL subscription uses WebSocket transport with heartbeat interval 25 seconds",
    "JWT token rotation policy requires refresh every 15 minutes with sliding window",
    "Elasticsearch index sharding strategy uses 5 primary shards with 1 replica each",
    "Docker multi-stage build reduces image size from 1.2GB to 180MB for production",
    "Apache Kafka consumer group rebalance timeout set to 45 seconds",
    "MongoDB change streams resume token persisted every 100 operations",
]

NEEDLE_QUERIES = [
    "Fibonacci sequence optimization memoization",
    "PostgreSQL vacuum autovacuum threshold",
    "Redis cluster failover timeout sentinel",
    "Kubernetes horizontal pod autoscaler CPU",
    "GraphQL subscription WebSocket heartbeat",
    "JWT token rotation policy refresh",
    "Elasticsearch index sharding primary replica",
    "Docker multi-stage build image size production",
    "Apache Kafka consumer group rebalance",
    "MongoDB change streams resume token",
]


def _populate_single_room(palace_path, n_drawers, n_needles=10):
    """Pack all drawers into one wing+room, plant needles, return queries."""
    gen = PalaceDataGenerator(seed=42, scale="small")
    os.makedirs(palace_path, exist_ok=True)
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers")

    batch_size = 500
    docs, ids, metas = [], [], []

    # Plant needles
    for i in range(n_needles):
        needle_id = f"NEEDLE_{i:04d}"
        content = f"{needle_id}: {NEEDLE_TOPICS[i]}. Unique planted needle for threshold test."
        drawer_id = f"drawer_single_room_{hashlib.md5(needle_id.encode()).hexdigest()[:16]}"
        docs.append(content)
        ids.append(drawer_id)
        metas.append(
            {
                "wing": "concentrated",
                "room": "single_room",
                "source_file": f"needle_{i}.txt",
                "chunk_index": 0,
                "added_by": "threshold_bench",
                "filed_at": datetime.now().isoformat(),
            }
        )

    # Fill with noise — all in the SAME room
    remaining = n_drawers - len(docs)
    for i in range(remaining):
        content = gen._random_text(400, 800)
        drawer_id = f"drawer_single_room_{hashlib.md5(f'noise_{i}'.encode()).hexdigest()[:16]}"
        docs.append(content)
        ids.append(drawer_id)
        metas.append(
            {
                "wing": "concentrated",
                "room": "single_room",
                "source_file": f"noise_{i:06d}.txt",
                "chunk_index": i % 10,
                "added_by": "threshold_bench",
                "filed_at": datetime.now().isoformat(),
            }
        )

        if len(docs) >= batch_size:
            col.add(documents=docs, ids=ids, metadatas=metas)
            docs, ids, metas = [], [], []

    if docs:
        col.add(documents=docs, ids=ids, metadatas=metas)

    return client, col


@pytest.mark.benchmark
class TestRecallThresholdSingleRoom:
    """
    All drawers in one room — isolates the embedding model's retrieval limit.

    Room filtering can't help here. This is the true ceiling.
    """

    SIZES = [250, 500, 1_000, 2_000, 3_000, 5_000]

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_single_room_recall(self, n_drawers, tmp_path):
        """Recall@5 and @10 with all drawers in one bucket."""
        palace_path = str(tmp_path / "palace")
        _populate_single_room(palace_path, n_drawers, n_needles=10)

        from mempalace.searcher import search_memories

        hits_at_5 = 0
        hits_at_10 = 0
        n_queries = len(NEEDLE_QUERIES)

        for i, query in enumerate(NEEDLE_QUERIES):
            result = search_memories(
                query,
                palace_path=palace_path,
                wing="concentrated",
                room="single_room",
                n_results=10,
            )
            if "error" in result:
                continue

            texts = [h["text"] for h in result.get("results", [])]
            needle_id = f"NEEDLE_{i:04d}"

            found_at_5 = any(needle_id in t for t in texts[:5])
            found_at_10 = any(needle_id in t for t in texts[:10])

            if found_at_5:
                hits_at_5 += 1
            if found_at_10:
                hits_at_10 += 1

        recall_5 = hits_at_5 / n_queries
        recall_10 = hits_at_10 / n_queries

        record_metric("single_room_recall", f"recall_at_5_at_{n_drawers}", round(recall_5, 3))
        record_metric("single_room_recall", f"recall_at_10_at_{n_drawers}", round(recall_10, 3))

    @pytest.mark.parametrize("n_drawers", SIZES)
    def test_single_room_no_filter_recall(self, n_drawers, tmp_path):
        """Same test but WITHOUT wing/room filter — pure unfiltered search."""
        palace_path = str(tmp_path / "palace")
        _populate_single_room(palace_path, n_drawers, n_needles=10)

        from mempalace.searcher import search_memories

        hits_at_5 = 0
        hits_at_10 = 0
        n_queries = len(NEEDLE_QUERIES)

        for i, query in enumerate(NEEDLE_QUERIES):
            result = search_memories(query, palace_path=palace_path, n_results=10)
            if "error" in result:
                continue

            texts = [h["text"] for h in result.get("results", [])]
            needle_id = f"NEEDLE_{i:04d}"

            if any(needle_id in t for t in texts[:5]):
                hits_at_5 += 1
            if any(needle_id in t for t in texts[:10]):
                hits_at_10 += 1

        recall_5 = hits_at_5 / n_queries
        recall_10 = hits_at_10 / n_queries

        record_metric("single_room_unfiltered", f"recall_at_5_at_{n_drawers}", round(recall_5, 3))
        record_metric("single_room_unfiltered", f"recall_at_10_at_{n_drawers}", round(recall_10, 3))
