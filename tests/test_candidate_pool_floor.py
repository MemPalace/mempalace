"""Regression tests for the rerank-pool floor (#2175).

The drawer vector query over-fetches a rerank pool and the hybrid re-ranker
only sees what that pool contains. When the pool was exactly ``n_results * 3``
the default limit produced a 15-candidate pool, so on a corpus with many
near-duplicate machine-text chunks those chunks could exhaust the pool before a
more distinctive drawer was retrieved. The same query then returned different
documents for different ``limit`` values, with the best match missing at the
default limit.

The pool is now floored at ``_MIN_RERANK_POOL`` so the re-ranker always sees a
fixed minimum depth, while still growing for callers that ask for more results.
"""

from mempalace.searcher import (
    _MIN_RERANK_POOL,
    _candidate_pool_limits,
    _candidate_pool_size,
)


def test_small_limits_keep_the_floored_pool():
    """A small limit must not shrink the pool below the re-ranker's depth."""
    assert _candidate_pool_size(1, False) == _MIN_RERANK_POOL
    assert _candidate_pool_size(5, False) == _MIN_RERANK_POOL


def test_pool_still_grows_with_the_requested_limit():
    """Above the floor the historical ``n_results * 3`` over-fetch is kept."""
    assert _candidate_pool_size(20, False) == 60
    assert _candidate_pool_size(200, False) == 600


def test_date_window_pool_is_unchanged():
    """The date-window widening keeps its own documented bounds."""
    assert _candidate_pool_size(5, True) == 75
    assert _candidate_pool_size(100, True) == 500


def test_union_mode_queries_the_floored_pool():
    """Union mode merges only ``n_results`` hits but still queries the floor."""
    assert _candidate_pool_limits("union", 5) == (_MIN_RERANK_POOL, 5)
