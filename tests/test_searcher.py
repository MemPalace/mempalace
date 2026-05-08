"""
test_searcher.py -- Tests for both search() (CLI) and search_memories() (API).

Uses the real ChromaDB fixtures from conftest.py for integration tests,
plus mock-based tests for error paths.
"""

from unittest.mock import MagicMock, patch

import pytest

from mempalace.searcher import SearchError, search, search_memories


# ── search_memories (API) ──────────────────────────────────────────────


class TestSearchMemories:
    def test_basic_search(self, palace_path, seeded_collection):
        result = search_memories("JWT authentication", palace_path)
        assert "results" in result
        assert len(result["results"]) > 0
        assert result["query"] == "JWT authentication"

    def test_wing_filter(self, palace_path, seeded_collection):
        result = search_memories("planning", palace_path, wing="notes")
        assert all(r["wing"] == "notes" for r in result["results"])

    def test_room_filter(self, palace_path, seeded_collection):
        result = search_memories("database", palace_path, room="backend")
        assert all(r["room"] == "backend" for r in result["results"])

    def test_wing_and_room_filter(self, palace_path, seeded_collection):
        result = search_memories("code", palace_path, wing="project", room="frontend")
        assert all(r["wing"] == "project" and r["room"] == "frontend" for r in result["results"])

    def test_n_results_limit(self, palace_path, seeded_collection):
        result = search_memories("code", palace_path, n_results=2)
        assert len(result["results"]) <= 2

    def test_no_palace_returns_error(self, tmp_path):
        result = search_memories("anything", str(tmp_path / "missing"))
        assert "error" in result

    def test_result_fields(self, palace_path, seeded_collection):
        result = search_memories("authentication", palace_path)
        hit = result["results"][0]
        assert "text" in hit
        assert "wing" in hit
        assert "room" in hit
        assert "source_file" in hit
        assert "similarity" in hit
        assert isinstance(hit["similarity"], float)
        assert "created_at" in hit

    def test_created_at_contains_filed_at(self, palace_path, seeded_collection):
        """created_at surfaces the filed_at metadata from the drawer."""
        result = search_memories("JWT authentication", palace_path)
        hit = result["results"][0]
        assert hit["created_at"] == "2026-01-01T00:00:00"

    def test_created_at_fallback_when_filed_at_missing(self):
        """created_at defaults to 'unknown' when filed_at is absent."""
        mock_col = MagicMock()
        mock_col.query.return_value = {
            "ids": [["drawer_no_date"]],
            "documents": [["Some text without a date"]],
            "metadatas": [[{"wing": "project", "room": "backend", "source_file": "x.py"}]],
            "distances": [[0.1]],
        }

        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            result = search_memories("test", "/fake/path")
        hit = result["results"][0]
        assert hit["created_at"] == "unknown"

    def test_search_memories_query_error(self):
        """search_memories returns error dict when query raises."""
        mock_col = MagicMock()
        mock_col.query.side_effect = RuntimeError("query failed")

        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            result = search_memories("test", "/fake/path")
        assert "error" in result
        assert "query failed" in result["error"]

    def test_search_memories_vector_path_uses_explicit_collection_name(self):
        mock_col = MagicMock()
        mock_col.query.return_value = {
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
            "ids": [[]],
        }

        with patch("mempalace.searcher.get_collection", return_value=mock_col) as get_collection:
            search_memories("test", "/fake/path", collection_name="custom_drawers")

        get_collection.assert_called_once_with(
            "/fake/path",
            collection_name="custom_drawers",
            create=False,
        )

    def test_search_memories_filters_in_result(self, palace_path, seeded_collection):
        result = search_memories("test", palace_path, wing="project", room="backend")
        assert result["filters"]["wing"] == "project"
        assert result["filters"]["room"] == "backend"

    def test_search_memories_handles_none_metadata(self):
        """API path: `None` entries in the drawer results' metadatas list must
        fall back to the sentinel strings (wing/room 'unknown', source '?')
        rather than raising `AttributeError: 'NoneType' object has no
        attribute 'get'` while the rest of the result set renders."""
        mock_col = MagicMock()
        mock_col.query.return_value = {
            "documents": [["first doc", "second doc"]],
            "metadatas": [[{"source_file": "a.md", "wing": "w", "room": "r"}, None]],
            "distances": [[0.1, 0.2]],
            "ids": [["d1", "d2"]],
        }

        def mock_get_collection(path, collection_name=None, create=False):
            # First call: drawers. Second call: closets — raise so hybrid
            # degrades to pure drawer search (the catch block covers it).
            if not hasattr(mock_get_collection, "_called"):
                mock_get_collection._called = True
                return mock_col
            raise RuntimeError("no closets")

        with patch("mempalace.searcher.get_collection", side_effect=mock_get_collection):
            result = search_memories("anything", "/fake/path")
        assert "results" in result
        assert len(result["results"]) == 2
        # The None-metadata hit renders with sentinel values, not a crash.
        none_hit = result["results"][1]
        assert none_hit["text"] == "second doc"
        assert none_hit["wing"] == "unknown"
        assert none_hit["room"] == "unknown"

    def test_effective_distance_clamped_to_valid_cosine_range(self):
        """A strong closet boost (up to 0.40) applied to a low-distance drawer
        can drive ``dist - boost`` negative. That violates the cosine-distance
        invariant ``[0, 2]``: the API returns ``similarity > 1.0`` and the
        internal ``_sort_key`` sinks below ordinary positive distances,
        inverting the ranking so the best hybrid matches sort last.

        With the clamp, ``effective_distance`` stays in ``[0, 2]``,
        ``similarity`` stays in ``[0, 1]``, and the sort order is stable.
        """
        # Drawer a.md gets a tiny base distance (0.08) — nearly exact match.
        # Drawer b.md gets a larger base distance (0.35).
        drawers_col = MagicMock()
        drawers_col.query.return_value = {
            "documents": [["doc-a", "doc-b"]],
            "metadatas": [
                [
                    {"source_file": "a.md", "wing": "w", "room": "r", "chunk_index": 0},
                    {"source_file": "b.md", "wing": "w", "room": "r", "chunk_index": 0},
                ]
            ],
            "distances": [[0.08, 0.35]],
            "ids": [["d-a", "d-b"]],
        }
        # A strong closet at rank 0 points at a.md → boost = 0.40,
        # which exceeds a.md's base distance and would go negative without
        # the clamp. No closet for b.md.
        closets_col = MagicMock()
        closets_col.query.return_value = {
            "documents": [["closet-preview-a"]],
            "metadatas": [[{"source_file": "a.md"}]],
            "distances": [[0.2]],  # within CLOSET_DISTANCE_CAP (1.5)
            "ids": [["c-a"]],
        }

        with (
            patch("mempalace.searcher.get_collection", return_value=drawers_col),
            patch("mempalace.searcher.get_closets_collection", return_value=closets_col),
        ):
            result = search_memories("query", "/fake/path", n_results=5)

        hits = result["results"]
        assert hits, "should return results"

        # Invariants on every hit.
        for h in hits:
            assert (
                0.0 <= h["similarity"] <= 1.0
            ), f"similarity out of range: {h['similarity']} for {h['source_file']}"
            assert 0.0 <= h["effective_distance"] <= 2.0, (
                f"effective_distance out of range: {h['effective_distance']} "
                f"for {h['source_file']}"
            )

        # With the clamp, the closet-boosted a.md still ranks ahead of b.md —
        # the boost still wins, but it no longer flips the ranking.
        assert hits[0]["source_file"] == "a.md"
        assert hits[0]["matched_via"] == "drawer+closet"


# ── BM25 internals: None / empty document safety ─────────────────────


class TestBM25NoneSafety:
    """Regression tests for the AttributeError observed in production when
    Chroma returned ``None`` documents inside a hybrid-rerank pass.

    Trace from the daemon log (2026-04-24 21:07:05):
        File "mempalace/searcher.py", line 81, in _bm25_scores
            tokenized = [_tokenize(d) for d in documents]
        File "mempalace/searcher.py", line 52, in _tokenize
            return _TOKEN_RE.findall(text.lower())
        AttributeError: 'NoneType' object has no attribute 'lower'
    """

    def test_tokenize_handles_none(self):
        from mempalace.searcher import _tokenize

        assert _tokenize(None) == []

    def test_tokenize_handles_empty_string(self):
        from mempalace.searcher import _tokenize

        assert _tokenize("") == []

    def test_bm25_scores_does_not_crash_on_none_documents(self):
        """A ``None`` mixed into the corpus must yield score 0.0 for that doc
        and finite scores for the rest, not raise AttributeError."""
        from mempalace.searcher import _bm25_scores

        scores = _bm25_scores(
            "postgres migration", ["postgres migration done", None, "kafka rebalance"]
        )
        assert len(scores) == 3
        assert scores[1] == 0.0
        assert scores[0] > 0.0


# ── search() (CLI print function) ─────────────────────────────────────


class TestSearchCLI:
    def test_search_prints_results(self, palace_path, seeded_collection, capsys):
        search("JWT authentication", palace_path)
        captured = capsys.readouterr()
        assert "JWT" in captured.out or "authentication" in captured.out

    def test_search_with_wing_filter(self, palace_path, seeded_collection, capsys):
        search("planning", palace_path, wing="notes")
        captured = capsys.readouterr()
        assert "Results for" in captured.out

    def test_search_with_room_filter(self, palace_path, seeded_collection, capsys):
        search("database", palace_path, room="backend")
        captured = capsys.readouterr()
        assert "Room:" in captured.out

    def test_search_with_wing_and_room(self, palace_path, seeded_collection, capsys):
        search("code", palace_path, wing="project", room="frontend")
        captured = capsys.readouterr()
        assert "Wing:" in captured.out
        assert "Room:" in captured.out

    def test_search_no_palace_raises(self, tmp_path):
        with pytest.raises(SearchError, match="No palace found"):
            search("anything", str(tmp_path / "missing"))

    def test_search_no_results(self, palace_path, collection, capsys):
        """Empty collection returns no results message."""
        # collection is empty (no seeded data)
        result = search("xyzzy_nonexistent_query", palace_path, n_results=1)
        captured = capsys.readouterr()
        # Either prints "No results" or returns None
        assert result is None or "No results" in captured.out

    def test_search_query_error_raises(self):
        """search raises SearchError when query fails."""
        mock_col = MagicMock()
        mock_col.query.side_effect = RuntimeError("boom")

        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            with pytest.raises(SearchError, match="Search error"):
                search("test", "/fake/path")

    def test_search_n_results(self, palace_path, seeded_collection, capsys):
        search("code", palace_path, n_results=1)
        captured = capsys.readouterr()
        # Should have output with at least one result block
        assert "[1]" in captured.out

    def test_search_applies_bm25_hybrid_rerank(self, capsys):
        """CLI search must call the same hybrid rerank that the MCP path uses.

        Regression for a bug where the CLI only consulted ChromaDB cosine
        distance: a drawer whose body contained every query term still
        scored zero similarity if its embedding happened to be far from
        the query (e.g. the drawer was a shell-output fragment that
        embeds as "file tree noise"). Hybrid rerank fixes this by
        combining BM25 with cosine — lexical matches rise above pure
        vector noise.

        Simulates: three candidates, all with distance >= 1.0 (cosine = 0);
        candidate 2 contains every query term. After the fix, candidate 2
        should rank first and display a non-zero bm25 score.
        """
        mock_col = MagicMock()
        mock_col.metadata = {"hnsw:space": "cosine"}
        mock_col.query.return_value = {
            "documents": [
                [
                    "unrelated directory listing -rw-rw-r-- file.txt",
                    "foo bar baz is a multi-word phrase",
                    "another unrelated chunk about colors",
                ]
            ],
            "metadatas": [
                [
                    {"source_file": "a.md", "wing": "w", "room": "r"},
                    {"source_file": "b.md", "wing": "w", "room": "r"},
                    {"source_file": "c.md", "wing": "w", "room": "r"},
                ]
            ],
            "distances": [[1.5, 1.5, 1.5]],
        }
        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            search("foo bar baz", "/fake/path")
        captured = capsys.readouterr()
        first_block, _, _ = captured.out.partition("[2]")
        # Lexical match must rank first
        assert (
            "b.md" in first_block
        ), f"expected lexical match 'b.md' at rank 1, got:\n{captured.out}"
        # Non-zero bm25 reported
        assert "bm25=" in first_block
        assert "bm25=0.0" not in first_block
        # Cosine still reported for transparency
        assert "cosine=" in first_block

    def test_search_warns_when_palace_uses_wrong_distance_metric(self, capsys):
        """Legacy palaces created without `hnsw:space=cosine` silently
        use L2, which breaks similarity interpretation. CLI must warn
        the user and point them at `mempalace repair` rather than
        pretending the `Match` scores are meaningful."""
        mock_col = MagicMock()
        mock_col.metadata = {}  # legacy: no hnsw:space set
        mock_col.query.return_value = {
            "documents": [["some drawer content"]],
            "metadatas": [[{"source_file": "a.md", "wing": "w", "room": "r"}]],
            "distances": [[1.2]],
        }
        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            search("anything", "/fake/path")
        captured = capsys.readouterr()
        assert "mempalace repair" in captured.err
        assert "cosine" in captured.err.lower()

    def test_search_does_not_warn_when_palace_is_correctly_configured(self, capsys):
        mock_col = MagicMock()
        mock_col.metadata = {"hnsw:space": "cosine"}
        mock_col.query.return_value = {
            "documents": [["some drawer content"]],
            "metadatas": [[{"source_file": "a.md", "wing": "w", "room": "r"}]],
            "distances": [[0.3]],
        }
        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            search("anything", "/fake/path")
        captured = capsys.readouterr()
        assert "mempalace repair" not in captured.err

    def test_search_handles_none_metadata_without_crash(self, palace_path, capsys):
        """ChromaDB can return `None` entries in the metadatas list when a
        drawer has no metadata. The CLI print path must not crash on them
        mid-render — it used to raise `AttributeError: 'NoneType' object has
        no attribute 'get'` after printing earlier results."""
        mock_col = MagicMock()
        mock_col.query.return_value = {
            "documents": [["first doc", "second doc"]],
            "metadatas": [[{"source_file": "a.md", "wing": "w", "room": "r"}, None]],
            "distances": [[0.1, 0.2]],
        }
        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            search("anything", "/fake/path")
        captured = capsys.readouterr()
        assert "[1]" in captured.out
        assert "[2]" in captured.out
        # Second result renders with fallback '?' values instead of crashing
        assert "second doc" in captured.out
