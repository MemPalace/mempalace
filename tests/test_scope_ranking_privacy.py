"""Fact and degraded-Chroma ranking use statistics of the readable scope only."""

from datetime import datetime, timezone

import pytest

from mempalace.backends import chroma_sqlite
from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.searcher import _bm25_only_via_sqlite, search_memories
from mempalace.searcher.facts import search_facts
from tests.test_hnsw_capacity import _seed_chroma_db, _seed_drawers


@pytest.mark.parametrize("hidden", ["wing", "sensitivity", "time"])
def test_fact_df_and_count_cover_only_the_readable_scope(hidden):
    with KnowledgeGraph.empty() as kg:
        visible = kg.add_triple("Kestrel", "uses", "marigold", wing="public")
        kg.add_triple("Other", "uses", "lantern", wing="public")
        scope = dict(wings=["public"], sensitivities=["internal"], as_of="2026-01-01")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def rows():
            return kg.search_fact_rows(
                {"kestrel", "marigold", "hiddenword"},
                candidate_limit=50,
                common_term_fraction=0.5,
                **scope,
            )

        initial = rows()
        assert len(initial) == 1 and initial[0]["id"] == visible
        assert initial[0]["documents"] == 2
        assert initial[0]["frequencies"] == {"hiddenword": 0, "kestrel": 1, "marigold": 1}
        before = search_facts(kg, "kestrel marigold hiddenword", now=now, **scope)
        for i in range(60):
            kg.add_triple(
                f"Hidden {i}",
                "uses",
                "kestrel hiddenword",
                wing="hidden" if hidden == "wing" else "public",
                sensitivity="private" if hidden == "sensitivity" else "internal",
                valid_from="2027-01-01" if hidden == "time" else None,
            )
        assert rows() == initial
        assert search_facts(kg, "kestrel marigold hiddenword", now=now, **scope) == before


def test_fact_order_ignores_hidden_wing_term_statistics():
    """Facts in another wing must not make a readable term look common or rare."""
    with KnowledgeGraph.empty() as kg:
        kg.add_triple("Reader one", "mentions", "zephyrite", wing="public")
        kg.add_triple("Reader two", "mentions", "quillon", wing="public")
        for i in range(3):
            kg.add_triple(f"Reader extra {i}", "mentions", "quillon", wing="public")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def order():
            found = search_facts(kg, "zephyrite quillon", wings=["public"], now=now, limit=2)
            return [(f["subject"], f["lexical_relevance"]) for f in found]

        before = order()
        # Within the wing, zephyrite (1 fact) is rarer than quillon (4 facts).
        assert before[0][0] == "Reader one"
        for i in range(200):
            kg.add_triple(f"Hidden {i}", "mentions", "zephyrite", wing="hidden")
        assert order() == before


def _public_drawer(text, drawer):
    return (text, {"wing": "public", "room": "notes", "source_file": f"/x/{drawer}.md"}, drawer)


def _hidden_drawer(text, n):
    return (text, {"wing": "hidden", "room": "notes", "source_file": f"/x/h{n}.md"}, f"h-{n}")


def _palace(path, drawers):
    path.mkdir()
    _seed_chroma_db(str(path), sqlite_count=0, segment_id="seg")
    _seed_drawers(str(path), "seg", drawers)
    return str(path)


def test_degraded_fallback_candidates_ignore_hidden_wing_statistics(tmp_path, monkeypatch):
    """The candidate window is picked inside the wing, not by palace-wide rank."""
    monkeypatch.setattr(chroma_sqlite, "_FTS_FILTER_DRIVEN_MAX", 1)
    public = [_public_drawer(f"lantern note{i}", f"l{i}") for i in range(3)]
    public += [_public_drawer(f"kestrel note{i}", f"k{i}") for i in range(4)]
    alone = _palace(tmp_path / "alone", public)
    crowded = _palace(
        tmp_path / "crowded", public + [_hidden_drawer(f"lantern hidden{i}", i) for i in range(40)]
    )

    def ask(palace):
        out = _bm25_only_via_sqlite(
            "kestrel lantern", palace, wing="public", n_results=3, max_candidates=3
        )
        return [(r["drawer_id"], r["text"], r["score"]) for r in out["results"]]

    expected = ask(alone)
    # Inside the wing lantern (3 drawers) is rarer than kestrel (4 drawers).
    assert {text.split()[0] for _, text, _ in expected} == {"lantern"}
    assert ask(crowded) == expected


def test_lexical_fallback_with_vectors_disabled_ranks_within_scope(tmp_path):
    palace = _palace(
        tmp_path / "palace",
        [
            _public_drawer("the quarterly budget review", "a"),
            _public_drawer("budget budget budget for the new roof", "b"),
            _public_drawer("an unrelated gardening diary", "c"),
            _hidden_drawer("budget roof secrets", 1),
        ],
    )
    out = search_memories(
        "budget roof",
        palace,
        wings=["public"],
        n_results=5,
        vector_disabled=True,
        include_facts=False,
    )
    assert out["fallback"] == "bm25_only_via_sqlite"
    texts = [r["text"] for r in out["results"]]
    assert texts[0] == "budget budget budget for the new roof"
    assert "budget roof secrets" not in texts and "an unrelated gardening diary" not in texts
    assert all(r["similarity"] is None for r in out["results"])
    assert all(r["rank_components"]["vector_weight"] == 0.0 for r in out["results"])


def test_unrestricted_degraded_fallback_keeps_the_index_rank(tmp_path, monkeypatch):
    """With no scope there is nothing hidden: FTS rank picks the pool, no local pass."""
    drawers = [_public_drawer(f"lantern note{i}", f"l{i}") for i in range(6)]
    palace = _palace(tmp_path / "all", drawers)

    def refuse(*args, **kwargs):
        raise AssertionError("an unrestricted search ranks nothing locally")

    monkeypatch.setattr(chroma_sqlite, "bm25_scores", refuse)
    out = _bm25_only_via_sqlite("lantern", palace, n_results=3, max_candidates=3)
    assert len(out["results"]) == 3
