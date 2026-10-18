"""The closet boost survives the hybrid re-rank and reads on the cosine scale."""

import math
import uuid
from unittest.mock import MagicMock, patch

import pytest

from mempalace.mcp_light_server import _enrich_search_results
from mempalace.searcher import _hybrid_rank, search_memories


def test_hybrid_rank_keeps_the_closet_boost():
    boosted = {"text": "alpha", "distance": 0.30, "closet_boost": 0.4}
    plain = {"text": "beta", "distance": 0.20, "closet_boost": 0.0}

    ranked = _hybrid_rank([plain, boosted], "unrelated", metric="cosine")

    assert ranked[0] is boosted


@pytest.mark.parametrize(
    "metric, plain_distance, weak_distance",
    [
        # Squared l2 0.30 is cosine 0.15; 1.60 is cosine 0.80, boosted to 0.40.
        ("l2", 0.30, 1.60),
        # ip is 1 - dot: 0.90 boosted by 0.40 is 0.50, still behind 0.10.
        ("ip", 0.10, 0.90),
    ],
)
def test_a_weak_boosted_hit_stays_behind_a_strong_plain_one(metric, plain_distance, weak_distance):
    """The hybrid re-rank keeps the order the vector pass gave by the boosted
    distance; it doesn't add a cosine-scale boost to a similarity on another
    metric's scale."""
    plain = {"text": "alpha", "distance": plain_distance, "closet_boost": 0.0}
    weak = {"text": "beta", "distance": weak_distance, "closet_boost": 0.4}

    ranked = _hybrid_rank([weak, plain], "unrelated", metric=metric)

    assert ranked[0] is plain


def test_a_boost_clamped_at_zero_lifts_no_further_than_an_exact_match():
    clamped = {"text": "alpha", "distance": 0.10, "closet_boost": 0.4, "authored_at": "2026-01-01"}
    exact = {"text": "beta", "distance": 0.0, "closet_boost": 0.0, "authored_at": "2026-02-01"}

    ranked = _hybrid_rank([clamped, exact], "unrelated", metric="cosine")

    # Equal vector scores; the closer raw distance breaks the tie, not the date.
    assert ranked[0] is exact


def test_boosted_hits_closer_than_their_boost_keep_vector_order():
    # Each clamps to an effective distance of 0; the newest is the farthest.
    hits = [
        {
            "text": f"doc {i}",
            "distance": 0.05 * (i + 1),
            "closet_boost": 0.4,
            "authored_at": f"2026-01-0{i + 1}",
        }
        for i in range(5)
    ]

    ranked = _hybrid_rank(list(reversed(hits)), "unrelated", metric="cosine")

    assert ranked == hits


def test_a_closet_boosted_drawer_stays_first_after_the_hybrid_rank():
    drawers_col = MagicMock()
    drawers_col.query.return_value = {
        "ids": [["d-a", "d-b"]],
        "documents": [["doc-a", "doc-b"]],
        "metadatas": [
            [
                {"source_file": "a.md", "wing": "w", "room": "r", "chunk_index": 0},
                {"source_file": "b.md", "wing": "w", "room": "r", "chunk_index": 0},
            ]
        ],
        "distances": [[0.30, 0.20]],
    }
    closets_col = MagicMock()
    closets_col.query.return_value = {
        "ids": [["c-a"]],
        "documents": [["closet for a"]],
        "metadatas": [[{"source_file": "a.md"}]],
        "distances": [[0.2]],
    }

    with (
        patch("mempalace.searcher.get_collection", return_value=drawers_col),
        patch("mempalace.searcher.get_closets_collection", return_value=closets_col),
    ):
        result = search_memories("query", "/unused", n_results=5)

    assert [h["source_file"] for h in result["results"]] == ["a.md", "b.md"]
    assert result["results"][0]["matched_via"] == "drawer+closet"


class _FixedQueryCollection:
    """A real Chroma collection queried with a fixed embedding."""

    def __init__(self, col, metric, query_vector):
        self._col = col
        self.distance_metric = metric
        self._query_vector = query_vector

    def query(self, **kwargs):
        kwargs.pop("query_texts", None)
        return self._col.query(query_embeddings=[self._query_vector], **kwargs)

    def __getattr__(self, name):
        return getattr(self._col, name)


def _unit(cosine_distance):
    angle = math.acos(1.0 - cosine_distance)
    return [math.cos(angle), math.sin(angle)]


def _l2_collection(client, name, rows):
    col = client.create_collection(f"{name}_{uuid.uuid4().hex}", metadata={"hnsw:space": "l2"})
    col.add(
        ids=[row[0] for row in rows],
        embeddings=[_unit(row[1]) for row in rows],
        documents=[row[2] for row in rows],
        metadatas=[row[3] for row in rows],
    )
    return _FixedQueryCollection(col, "l2", [1.0, 0.0])


def test_an_l2_palace_boosts_and_ranks_on_the_cosine_scale():
    import chromadb

    client = chromadb.EphemeralClient()
    meta = {"wing": "w", "room": "r", "chunk_index": 0}
    drawers = _l2_collection(
        client,
        "drawers",
        [
            ("d-a", 0.30, "doc-a", {**meta, "source_file": "a.md"}),
            ("d-b", 0.20, "doc-b", {**meta, "source_file": "b.md"}),
        ],
    )
    # Cosine distance 0.8 is squared l2 1.6: past the raw 1.5 cap, well inside it
    # on the cosine scale.
    closets = _l2_collection(
        client, "closets", [("c-a", 0.8, "closet for a", {"source_file": "a.md"})]
    )

    with (
        patch("mempalace.searcher.get_collection", return_value=drawers),
        patch("mempalace.searcher.get_closets_collection", return_value=closets),
    ):
        result = search_memories("query", "/unused", n_results=5)

    hits = result["results"]
    assert [h["source_file"] for h in hits] == ["a.md", "b.md"]
    assert hits[0]["matched_via"] == "drawer+closet"
    assert hits[0]["closet_boost"] == pytest.approx(0.4)
    assert hits[1]["effective_distance"] == pytest.approx(0.20, abs=1e-3)
    assert result["distance_metric"] == "l2"


@pytest.mark.parametrize(
    "metric, distance, confidence",
    [
        ("cosine", 0.30, "high"),
        # Squared l2 0.6 is cosine 0.3; the raw value would read as "low".
        ("l2", 0.60, "high"),
        ("l2", 1.20, "low"),
    ],
)
def test_light_server_confidence_reads_on_the_cosine_scale(metric, distance, confidence):
    res = {"results": [{"distance": distance}], "distance_metric": metric}

    assert _enrich_search_results(res)["relevance_confidence"] == confidence
