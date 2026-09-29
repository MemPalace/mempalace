"""Tests for backend-declared distance metrics (RFC 001) and the
metric-aware distance→similarity conversion in the searcher.

Before this, the searcher hard-coded ``max(0, 1 - distance)`` everywhere,
which is correct only for cosine. A backend reporting L2 or inner-product
distances (or a legacy Chroma palace built without ``hnsw:space=cosine``)
was silently mis-ranked — L2 distances routinely exceed 1.0 and floored
every result's similarity to 0. The contract now lets a backend declare its
metric and the searcher converts accordingly.
"""

import math
import types

import pytest

from mempalace.backends.base import BaseBackend, BaseCollection
from mempalace.backends.chroma import ChromaCollection
from mempalace.searcher import (
    _distance_to_similarity,
    _hybrid_rank,
    _metric_for_collection,
)


# ---------------------------------------------------------------------------
# Contract surface
# ---------------------------------------------------------------------------


def test_basebackend_declares_cosine_default():
    assert BaseBackend.distance_metric == "cosine"


def test_basecollection_reports_cosine_default():
    # A minimal concrete collection inherits the cosine default.
    class _Col(BaseCollection):
        def add(self, **k): ...
        def upsert(self, **k): ...
        def query(self, **k): ...
        def get(self, **k): ...
        def delete(self, **k): ...
        def count(self):
            return 0

    assert _Col().distance_metric == "cosine"


# ---------------------------------------------------------------------------
# _distance_to_similarity — per-metric math
# ---------------------------------------------------------------------------


def test_cosine_conversion():
    assert _distance_to_similarity(0.0, "cosine") == 1.0
    assert _distance_to_similarity(2.0, "cosine") == 0.0
    # cosine distance > 1 must floor at 0, never go negative.
    assert _distance_to_similarity(1.5, "cosine") == 0.0


def test_l2_conversion_is_monotonic_and_bounded():
    assert _distance_to_similarity(0.0, "l2") == 1.0
    assert _distance_to_similarity(1.0, "l2") == pytest.approx(0.5)
    # Strictly decreasing, and a large L2 distance does NOT floor to 0 the
    # way the old cosine formula did — that was the bug.
    far = _distance_to_similarity(5.0, "l2")
    near = _distance_to_similarity(1.0, "l2")
    assert 0.0 < far < near < 1.0


def test_l2_distance_above_one_keeps_signal():
    # The regression this fixes: under cosine, d=1.7 -> 0.0 (no signal).
    # Under a correctly-declared L2 metric, it stays positive and ordered.
    assert _distance_to_similarity(1.7, "cosine") == 0.0
    assert _distance_to_similarity(1.7, "l2") > 0.0


def test_ip_conversion_monotonic_decreasing():
    # Inner-product distance is signed/unbounded (lower = closer). Logistic
    # squash keeps it in (0, 1) and monotonic.
    assert _distance_to_similarity(-5.0, "ip") > _distance_to_similarity(0.0, "ip")
    assert _distance_to_similarity(0.0, "ip") == pytest.approx(0.5)
    assert _distance_to_similarity(0.0, "ip") > _distance_to_similarity(5.0, "ip")


def test_ip_does_not_overflow_on_large_distance():
    # Exponent is clamped so a huge positive distance can't raise OverflowError.
    val = _distance_to_similarity(1e6, "ip")
    assert val == pytest.approx(0.0, abs=1e-9)
    assert not math.isinf(val) and not math.isnan(val)


def test_none_distance_maps_to_zero():
    # BM25-only candidates carry distance=None -> no vector signal.
    assert _distance_to_similarity(None, "cosine") == 0.0
    assert _distance_to_similarity(None, "l2") == 0.0


def test_unknown_metric_falls_back_to_cosine():
    assert _distance_to_similarity(0.3, "weird") == _distance_to_similarity(0.3, "cosine")
    assert _distance_to_similarity(0.3, None) == _distance_to_similarity(0.3, "cosine")


# ---------------------------------------------------------------------------
# _metric_for_collection — resolution + delegation + safety
# ---------------------------------------------------------------------------


def test_metric_resolver_reads_declared_metric():
    col = types.SimpleNamespace(distance_metric="l2")
    assert _metric_for_collection(col) == "l2"


def test_metric_resolver_normalizes_case_and_garbage():
    assert _metric_for_collection(types.SimpleNamespace(distance_metric="L2")) == "l2"
    assert _metric_for_collection(types.SimpleNamespace(distance_metric="nonsense")) == "cosine"
    assert _metric_for_collection(types.SimpleNamespace(distance_metric=None)) == "cosine"


def test_metric_resolver_defaults_when_absent():
    assert _metric_for_collection(object()) == "cosine"


def test_metric_resolver_follows_embeddingcollection_delegation():
    inner = types.SimpleNamespace(distance_metric="ip")

    class _Wrapper:
        def __init__(self, i):
            self._i = i

        def __getattr__(self, name):
            return getattr(self._i, name)

    assert _metric_for_collection(_Wrapper(inner)) == "ip"


def test_metric_resolver_survives_raising_attribute():
    class _Boom:
        @property
        def distance_metric(self):
            raise RuntimeError("backend down")

    assert _metric_for_collection(_Boom()) == "cosine"


def test_real_embeddingcollection_delegates_metric_not_shadowed():
    # Regression: BaseCollection defines distance_metric as a property, so on
    # the real EmbeddingCollection subclass it resolves directly and
    # __getattr__ never fires. Without an explicit override the wrapper would
    # report the base "cosine" default and mask a wrapped non-cosine backend.
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    class _Inner(BaseCollection):
        distance_metric = "l2"

        def add(self, **k): ...
        def upsert(self, **k): ...
        def query(self, **k): ...
        def get(self, **k): ...
        def delete(self, **k): ...
        def count(self):
            return 0

    wrapped = EmbeddingCollection(_Inner())
    assert wrapped.distance_metric == "l2"
    assert _metric_for_collection(wrapped) == "l2"


# ---------------------------------------------------------------------------
# ChromaCollection — legacy L2 palace reports its real metric
# ---------------------------------------------------------------------------


def _chroma_col_with_metadata(meta):
    fake_inner = types.SimpleNamespace(metadata=meta)
    return ChromaCollection(fake_inner)


def test_chroma_reports_cosine_when_set():
    assert _chroma_col_with_metadata({"hnsw:space": "cosine"}).distance_metric == "cosine"


def test_chroma_legacy_l2_palace_reports_l2():
    # A pre-cosine palace: the property surfaces the real space so the
    # searcher maps distances correctly instead of flooring to 0.
    assert _chroma_col_with_metadata({"hnsw:space": "l2"}).distance_metric == "l2"


def test_chroma_missing_or_unknown_metadata_reports_l2():
    # Absent/empty/garbage hnsw:space means the collection never had cosine
    # set, so it is genuinely using Chroma's HNSW default (L2). Reporting
    # cosine here would reintroduce the floor-to-0 bug this fixes.
    assert _chroma_col_with_metadata({}).distance_metric == "l2"
    assert _chroma_col_with_metadata({"hnsw:space": ""}).distance_metric == "l2"
    assert _chroma_col_with_metadata({"hnsw:space": "bogus"}).distance_metric == "l2"


# ---------------------------------------------------------------------------
# _hybrid_rank — ranking actually respects the metric
# ---------------------------------------------------------------------------


def test_hybrid_rank_l2_keeps_far_candidate_ranked_above_unknown():
    # Two candidates with identical (zero) lexical overlap to the query, so
    # only the vector term decides. Under cosine, a d=1.6 hit floors to 0 and
    # ties a distance-None hit; under L2 it stays positive and ranks above.
    results = [
        {"text": "alpha", "distance": None},
        {"text": "beta", "distance": 1.6},
    ]
    ranked = _hybrid_rank(results, "zzzznomatch", metric="l2")
    assert ranked[0]["text"] == "beta"  # real vector signal beats vector-unknown


def test_hybrid_rank_cosine_unchanged_behavior():
    # Cosine path must be byte-for-byte the old behavior (max(0, 1-d)).
    results = [
        {"text": "near", "distance": 0.1},
        {"text": "far", "distance": 0.9},
    ]
    ranked = _hybrid_rank(results, "zzzznomatch", metric="cosine")
    assert ranked[0]["text"] == "near"
    assert ranked[1]["text"] == "far"


def test_hybrid_rank_empty_is_noop():
    assert _hybrid_rank([], "q", metric="l2") == []
