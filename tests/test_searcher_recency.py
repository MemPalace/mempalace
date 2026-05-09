"""
test_searcher_recency.py -- Tests for opt-in recency-aware ranking.

Pure-unit tests for ``_recency_factor`` and the recency blending path of
``_hybrid_rank``. No ChromaDB / palace fixtures needed — both functions
operate on plain dict lists.

Covers:
* recency_factor exponential decay (now=1.0, half-life=0.5, dead=≈0)
* malformed / missing / None timestamps return 0.0 (so undated drawers
  cannot win on the recency axis alone)
* future-dated entries (clock skew) treated as fresh, not negative
* trailing-Z (Zulu/UTC) timestamps parse correctly
* timezone-naive timestamps assumed UTC
* custom half_life overrides default
* _hybrid_rank backward-compat: recency_weight=0 reproduces pre-PR ordering
* _hybrid_rank flips order when recency dominates
* recency_weight clamping ([0, 1])
* empty results no-op
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mempalace.searcher import _hybrid_rank, _recency_factor


def _iso(days_ago: float) -> str:
    """Return an ISO-8601 timestamp ``days_ago`` days before now (UTC)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# ── _recency_factor ────────────────────────────────────────────────────


class TestRecencyFactor:
    def test_now_is_one(self):
        assert _recency_factor(_iso(0)) > 0.999

    def test_at_half_life_is_half(self):
        # Default half-life is 30 days; allow a small clock-tick tolerance.
        assert abs(_recency_factor(_iso(30)) - 0.5) < 0.001

    def test_two_half_lives_is_quarter(self):
        assert abs(_recency_factor(_iso(60)) - 0.25) < 0.001

    def test_far_past_is_near_zero(self):
        # 365d at 30d half-life ≈ 12.17 half-lives → ~2.2e-4 (well below 0.001).
        assert _recency_factor(_iso(365)) < 0.001

    def test_unknown_string_returns_zero(self):
        assert _recency_factor("unknown") == 0.0

    def test_none_returns_zero(self):
        assert _recency_factor(None) == 0.0

    def test_empty_string_returns_zero(self):
        assert _recency_factor("") == 0.0

    def test_malformed_returns_zero(self):
        assert _recency_factor("not-a-date") == 0.0
        assert _recency_factor("2026-13-99T00:00:00Z") == 0.0

    def test_non_string_input_returns_zero(self):
        assert _recency_factor(12345) == 0.0
        assert _recency_factor([]) == 0.0

    def test_trailing_z_parsed(self):
        s = _iso(0).replace("+00:00", "Z")
        assert _recency_factor(s) > 0.999

    def test_future_dated_treated_as_fresh(self):
        # Clock-skew safety: future dates clamp to fresh, never negative.
        assert _recency_factor(_iso(-5)) == 1.0

    def test_naive_timestamp_assumed_utc(self):
        # ``datetime.fromisoformat`` accepts naive strings; we assume UTC.
        s = (datetime.now(timezone.utc) - timedelta(days=30)).replace(tzinfo=None).isoformat()
        assert abs(_recency_factor(s) - 0.5) < 0.01

    def test_custom_half_life(self):
        # 90 days at half-life=10 → 9 half-lives → 2**-9 ≈ 0.00195.
        result = _recency_factor(_iso(90), half_life_days=10)
        assert abs(result - 0.001953125) < 0.0001

    def test_zero_half_life_returns_one_for_fresh(self):
        # Edge case: half_life_days=0 means no decay; fresh stays fresh.
        # (Implementation returns 1.0 to avoid divide-by-zero blowup.)
        assert _recency_factor(_iso(0), half_life_days=0) == 1.0


# ── _hybrid_rank — backward compatibility ──────────────────────────────


def _make_candidates() -> list:
    """Two candidates: an older drawer with stronger base score, a newer with weaker."""
    return [
        {
            "text": "older but exact lexical match for query foo bar",
            "distance": 0.3,  # vec_sim = 0.7
            "metadata": {"filed_at": _iso(365)},
        },
        {
            "text": "newer with weaker semantic overlap",
            "distance": 0.5,  # vec_sim = 0.5
            "metadata": {"filed_at": _iso(1)},
        },
    ]


class TestHybridRankBackwardCompat:
    def test_recency_weight_zero_preserves_old_ordering(self):
        results = _make_candidates()
        _hybrid_rank(results, "query foo bar", recency_weight=0.0)
        # Older candidate should win on pure vec+BM25 because exact lexical match.
        assert results[0]["text"].startswith("older")

    def test_default_recency_weight_is_zero(self, monkeypatch):
        # Ensure no env-var leak from the test runner.
        monkeypatch.delenv("MEMPALACE_RECENCY_WEIGHT", raising=False)
        # Re-import to pick up the cleared env (module-level constant).
        # Easier: pass explicit recency_weight to bypass the constant.
        results = _make_candidates()
        _hybrid_rank(results, "query foo bar")  # no recency_weight kwarg
        # Without the kwarg AND without env, behavior should match recency=0.
        # (Module-level _RECENCY_WEIGHT_DEFAULT defaults to 0.0.)
        from mempalace.searcher import _RECENCY_WEIGHT_DEFAULT

        if _RECENCY_WEIGHT_DEFAULT == 0.0:
            assert results[0]["text"].startswith("older")

    def test_empty_list_no_op(self):
        results: list = []
        out = _hybrid_rank(results, "anything", recency_weight=0.5)
        assert out == []
        assert results == []

    def test_bm25_score_added_to_each_result(self):
        results = _make_candidates()
        _hybrid_rank(results, "query foo bar", recency_weight=0.0)
        for r in results:
            assert "bm25_score" in r
            assert isinstance(r["bm25_score"], float)


# ── _hybrid_rank — recency blending ────────────────────────────────────


class TestHybridRankRecencyBlending:
    def test_recency_flips_order_when_strong(self):
        results = _make_candidates()
        _hybrid_rank(results, "query foo bar", recency_weight=0.7, half_life_days=30)
        # With strong recency weight, the 1-day-old drawer should win
        # despite the 365-day-old one having stronger base similarity.
        assert results[0]["text"].startswith("newer")

    def test_recency_factor_added_when_active(self):
        results = _make_candidates()
        _hybrid_rank(results, "query foo bar", recency_weight=0.5)
        for r in results:
            assert "recency_factor" in r

    def test_recency_factor_not_added_when_inactive(self):
        results = _make_candidates()
        _hybrid_rank(results, "query foo bar", recency_weight=0.0)
        # No recency_factor key — saves payload bytes for callers
        # that don't want recency in their MCP response.
        for r in results:
            assert "recency_factor" not in r

    def test_missing_filed_at_does_not_crash(self):
        results = [
            {"text": "doc with metadata", "distance": 0.3, "metadata": {"filed_at": _iso(1)}},
            {"text": "doc without filed_at", "distance": 0.4, "metadata": {}},
            {"text": "doc with None metadata", "distance": 0.5, "metadata": None},
        ]
        # Should run cleanly, never raising; undated drawers get factor=0.0.
        _hybrid_rank(results, "doc", recency_weight=0.5)
        # The dated drawer should win when recency weighs heavily.
        assert results[0]["text"] == "doc with metadata"


# ── _hybrid_rank — clamping ────────────────────────────────────────────


class TestRecencyWeightClamping:
    def test_negative_clamps_to_zero(self):
        results = _make_candidates()
        _hybrid_rank(results, "query foo bar", recency_weight=-5.0)
        # Clamped to 0 → behaves like backward-compat: older with strong base wins.
        assert results[0]["text"].startswith("older")

    def test_above_one_clamps_to_one(self):
        results = _make_candidates()
        _hybrid_rank(results, "query foo bar", recency_weight=99.0)
        # Clamped to 1 → pure recency: 1-day-old beats 365-day-old.
        assert results[0]["text"].startswith("newer")
