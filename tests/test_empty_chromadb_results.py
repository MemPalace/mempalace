"""Regression tests for issue #195 — IndexError on empty ChromaDB results.

Before the fix, `searcher.search()`, `searcher.search_memories()`, and
`Layer3.search()` indexed `results["documents"][0]` without checking the
outer list, so a query against an empty collection (or a wing/room
filter that excluded everything) crashed with IndexError instead of
returning a graceful "no results" response.
"""

import pytest

from mempalace.searcher import _first_or_empty


def test_first_or_empty_handles_empty_outer_list():
    """The shape ChromaDB returns from an empty collection (issue #195)."""
    results = {"documents": [], "metadatas": [], "distances": []}
    assert _first_or_empty(results, "documents") == []
    assert _first_or_empty(results, "metadatas") == []
    assert _first_or_empty(results, "distances") == []


def test_first_or_empty_handles_outer_with_empty_inner():
    """ChromaDB also returns ``{"documents": [[]]}`` in some versions —
    must yield [] either way."""
    assert _first_or_empty({"documents": [[]]}, "documents") == []


def test_first_or_empty_handles_missing_key():
    assert _first_or_empty({}, "documents") == []


def test_first_or_empty_handles_none_inner():
    """``[None]`` (unusual but observed) must not blow up."""
    assert _first_or_empty({"documents": [None]}, "documents") == []


def test_first_or_empty_returns_inner_list_for_normal_result():
    results = {"documents": [["a", "b", "c"]]}
    assert _first_or_empty(results, "documents") == ["a", "b", "c"]


def test_raw_indexing_still_raises_to_document_the_bug():
    """Document the original failure mode so future readers understand
    why _first_or_empty exists."""
    results = {"documents": []}
    with pytest.raises(IndexError):
        _ = results["documents"][0]
