"""Regression tests for #1082 — MCP wing-scoped search on convos-mined wings.

Repro conditions (from the issue / #1035 / #1245 / #1315 lineage):

  * A convos-mined wing ("chats_general") in the same palace as a
    projects-mined wing ("sample_repo").
  * ChromaDB's HNSW layer in 1.5.x has stale neighbor pointers in the
    vector region corresponding to the convos wing.  A filtered query
    (``where={"wing": "chats_general"}``, ``n_results = pool_size``) trips
    the pointer and Chroma raises ``"Error finding id"`` (Rust-core
    HNSW/SQLite rowid mismatch).  The *unfiltered* pool used by
    ``_query_drawers_with_filter_fallback`` has the same breadth
    (``n_results * 15``), so it *also* trips the same pointer.  The CLI's
    narrower request (``n_results`` = 5) walks a shorter HNSW segment and
    misses the pointer, which is why the same query succeeds via CLI.

These tests pin the contract that the fallback chain retries at
``n_results`` as a last resort before giving up — the exact width the
CLI uses — so wing-scoped MCP search on convos wings recovers a hit
set instead of surfacing ``"Search error: Error executing plan:
Internal error: Error finding id"``.

See issue #1082, helpers at ``mempalace/searcher.py``
(``_query_drawers_with_filter_fallback``, ``search_memories``).
"""

from unittest.mock import MagicMock

import pytest

from mempalace.searcher import _query_drawers_with_filter_fallback


def _make_col(*, fail_filtered_widths=frozenset(), fail_unfiltered_widths=frozenset()):
    """Build a fake Chroma collection whose ``query`` raises for widths that
    hit a stale HNSW pointer and returns a small successful result otherwise.

    The wing filter must be echoed from the successful result so the
    Python-side post-filter keeps the right hits.
    """
    col = MagicMock()
    col.query.side_effect = (
        lambda
        query_texts=None,
        n_results=None,
        where=None,
        include=None,
        **kwargs: (
            _fail("Error finding id")
            if (n_results in fail_filtered_widths and where)
            else (
                _fail("Error finding id")
                if (n_results in fail_unfiltered_widths and not where)
                else {
                    "ids": [["drawer_chats_general_decision_abc123def4567890123456"]],
                    "documents": [["decisions on the async refactor"]],
                    "metadatas": [[{"wing": "chats_general", "room": "decision"}]],
                    "distances": [[0.4]],
                }
            )
        )
    )
    return col


def _fail(msg):
    # Raising a plain Exception mirrors ChromaDB's wrapped internal
    # ``ValueError`` — the ``except Exception`` sites in
    # ``_query_drawers_with_filter_fallback`` are not type-specific.
    raise Exception(msg)


def _dkwargs(pool_n, wing):
    return {
        "query_texts": ["decisions"],
        "n_results": pool_n,
        "include": ["documents", "metadatas", "distances"],
        "where": {"wing": wing},
    }


def test_progressive_fallback_survives_double_error_finding_id_convos_wing():
    """Filtered query (pool width) AND the wide unfiltered retry both
    ``"Error finding id"`` — the current fallback gives up on the second
    unfiltered attempt.  With the progressive-n fix, the last-resort
    unfiltered ``n_results`` (5) succeeds and the Python-side post-filter
    keeps the convos-wing hit.
    """
    convos_wing = "chats_general"
    n_results = 5
    # Search_memories computes pool_size = candidate_strategy * n_results
    # (4 * n = 20 for vector).  The filter-fallback helper then computes
    # the unfiltered retry width as ``n_results * 15`` (``min(n*15, 500)``
    # = 75).  Both widths hit the stale HNSW pointer in the convos cluster.
    col = _make_col(fail_filtered_widths={20}, fail_unfiltered_widths={75})

    result = _query_drawers_with_filter_fallback(
        drawers_col=col,
        dkwargs=_dkwargs(pool_n=20, wing=convos_wing),
        query="decisions",
        n_results=n_results,
        wing=convos_wing,
        room=None,
        source_file=None,
    )

    ids_0 = result["ids"][0]
    docs_0 = result["documents"][0]
    metas_0 = result["metadatas"][0]
    dists_0 = result["distances"][0]

    # Hit was recovered from the narrow unfiltered retry.
    assert ids_0, "expected at least one hit after progressive fallback"
    assert ids_0 == ["drawer_chats_general_decision_abc123def4567890123456"]
    assert "decisions" in docs_0[0]
    assert metas_0[0]["wing"] == convos_wing
    assert dists_0[0] == 0.4

    # The helper was exercised: at least one query raised and recovered.
    # (We don't assert exact call counts — the helper is allowed to try
    # several widths in any order before succeeding.)
    assert col.query.call_count >= 3, (
        "expected filtered failure + wide-unfiltered failure + narrow-"
        "unfiltered success (≥3 attempts); got "
        f"{col.query.call_count}"
    )


def test_progressive_fallback_drops_nonmatching_wing_after_recovery():
    """Successful unfiltered result must be post-filtered on the requested
    wing, even after two ``"Error finding id"`` attempts."""
    convos_wing = "chats_general"
    other_wing = "sample_repo"
    col = MagicMock()
    col.query.side_effect = [
        Exception("Error finding id"),  # filtered (pool)
        Exception("Error finding id"),  # wide unfiltered
        {
            "ids": [
                [
                    "drawer_chats_general_decision_abc123def4567890123456",
                    f"drawer_{other_wing}_planning_deadbeefcafe1234567890",
                ]
            ],
            "documents": [["decisions on the async refactor", "planning a launch"]],
            "metadatas": [
                [
                    {"wing": convos_wing, "room": "decision"},
                    {"wing": other_wing, "room": "planning"},
                ]
            ],
            "distances": [[0.4, 0.5]],
        },
    ]

    result = _query_drawers_with_filter_fallback(
        drawers_col=col,
        dkwargs=_dkwargs(pool_n=20, wing=convos_wing),
        query="decisions",
        n_results=5,
        wing=convos_wing,
        room=None,
        source_file=None,
    )

    assert [m["wing"] for m in (result["metadatas"][0] or [{}])] == [convos_wing]
    assert len(result["ids"][0]) == 1


def test_progressive_fallback_gives_up_when_all_widths_fail():
    """When every candidate width (filtered + unfiltered at every
    progressive n) hits ``"Error finding id"``, the helper must surface
    the last error — not swallow it and return an empty result.  The
    caller (``search_memories``) wraps that into the user-facing
    ``"Search error: ..."`` dict, matching pre-fix behaviour for a
    genuinely unrecoverable index.
    """
    col = MagicMock()
    col.query.side_effect = Exception(
        "Error executing plan: Internal error: Error finding id"
    )

    with pytest.raises(Exception, match="Error finding id"):
        _query_drawers_with_filter_fallback(
            drawers_col=col,
            dkwargs=_dkwargs(pool_n=20, wing="chats_general"),
            query="decisions",
            n_results=5,
            wing="chats_general",
            room=None,
            source_file=None,
        )
