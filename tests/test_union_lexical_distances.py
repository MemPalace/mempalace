"""Union search must keep vector distances for BM25-only hits (#2660).

With ``candidate_strategy="union"``, candidates the vector index ranked
between n+1 and 3n are dropped (with their distances) before the lexical
merge. When BM25 brings such a drawer back, it re-enters with
``distance=None`` and is scored on BM25 alone, so results depend on
``n_results``. The stored embedding is available — the merge must use it.
"""

import math

import pytest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mempalace import searcher
from mempalace.searcher import _merge_bm25_union_candidates


def _mock_collection():
    col = Mock()
    col.distance_metric = "cosine"
    # One lexical hit: strong BM25, vector similarity 0.70 to the query.
    col.lexical_search.return_value = SimpleNamespace(
        hits=[
            SimpleNamespace(
                id="D-target",
                metadata={
                    "wing": "w",
                    "room": "r",
                    "source_file": "target.md",
                },
                document="The kestrel migration is blocked.",
                score=9.5,
            )
        ]
    )
    # Stored embedding: unit vector at cosine similarity 0.70 to [1, 0].
    col.get.return_value = {
        "ids": ["D-target"],
        "embeddings": [[0.70, math.sqrt(1 - 0.70**2)]],
    }
    return col


def test_union_merge_keeps_vector_distance_without_max_distance():
    """A BM25-recovered drawer carries its real distance, not None."""
    col = _mock_collection()
    hits: list = []
    with patch(
        "mempalace.backends.embedding_wrapper._embed_texts",
        return_value=[[1.0, 0.0]],
    ):
        _merge_bm25_union_candidates(
            hits, col, "kestrel migration", "w", "r", n_results=2
        )
    assert len(hits) == 1
    # cosine distance = 1 - 0.70
    assert hits[0]["distance"] == pytest.approx(0.30)
    assert hits[0]["similarity"] == pytest.approx(0.70, abs=1e-3)
    assert hits[0]["matched_via"] == "bm25_backend"


def test_union_merge_still_admits_hits_without_stored_embeddings():
    """No stored embedding -> distance stays None (previous behavior)."""
    col = _mock_collection()
    col.get.return_value = {"ids": [], "embeddings": []}
    hits: list = []
    with patch(
        "mempalace.backends.embedding_wrapper._embed_texts",
        return_value=[[1.0, 0.0]],
    ):
        _merge_bm25_union_candidates(
            hits, col, "kestrel migration", "w", "r", n_results=2
        )
    assert len(hits) == 1
    assert hits[0]["distance"] is None
