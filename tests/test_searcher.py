"""
test_searcher.py -- Tests for both search() (CLI) and search_memories() (API).

Uses the real ChromaDB fixtures from conftest.py for integration tests,
plus mock-based tests for error paths.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from _chroma_palace_helper import make_minimal_chroma_sqlite

from mempalace.backends import BackendMismatchError
from mempalace.searcher import (
    SearchError,
    _result_drawer_id,
    build_where_filter,
    get_collection,
    search,
    search_memories,
)


# ── build_where_filter (unit) ──────────────────────────────────────────


class TestBuildWhereFilter:
    """build_where_filter composes a ChromaDB where clause from optional
    wing / room / source_file constraints (#1815). ChromaDB needs a ``$and``
    only when ≥2 clauses are present; a single clause is returned bare and
    zero clauses yield an empty filter."""

    def test_no_filters_returns_empty(self):
        assert build_where_filter() == {}

    def test_wing_only(self):
        assert build_where_filter(wing="backend") == {"wing": "backend"}

    def test_room_only(self):
        assert build_where_filter(room="auth") == {"room": "auth"}

    def test_wing_and_room(self):
        assert build_where_filter(wing="backend", room="auth") == {
            "$and": [{"wing": "backend"}, {"room": "auth"}]
        }

    def test_source_file_only(self):
        assert build_where_filter(source_file="auth.py") == {"source_file": "auth.py"}

    def test_wing_and_source_file(self):
        assert build_where_filter(wing="backend", source_file="auth.py") == {
            "$and": [{"wing": "backend"}, {"source_file": "auth.py"}]
        }

    def test_room_and_source_file(self):
        assert build_where_filter(room="auth", source_file="auth.py") == {
            "$and": [{"room": "auth"}, {"source_file": "auth.py"}]
        }

    def test_wing_room_and_source_file(self):
        assert build_where_filter(wing="backend", room="auth", source_file="auth.py") == {
            "$and": [{"wing": "backend"}, {"room": "auth"}, {"source_file": "auth.py"}]
        }


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

    def test_source_file_filter(self, palace_path, seeded_collection):
        result = search_memories("authentication module", palace_path, source_file="auth.py")
        assert result["results"], "exact source_file match should return its drawer"
        assert all(r["source_file"] == "auth.py" for r in result["results"])

    def test_source_file_with_wing_filter(self, palace_path, seeded_collection):
        result = search_memories("database", palace_path, wing="project", source_file="db.py")
        assert result["results"]
        assert all(
            r["source_file"] == "db.py" and r["wing"] == "project" for r in result["results"]
        )

    def test_nonmatching_source_file_returns_empty_not_error(self, palace_path, seeded_collection):
        result = search_memories("authentication", palace_path, source_file="nope.md")
        assert "error" not in result
        assert result["results"] == []

    def test_filters_envelope_includes_source_file(self, palace_path, seeded_collection):
        result = search_memories("authentication", palace_path, source_file="auth.py")
        assert result["filters"]["source_file"] == "auth.py"

    def test_result_exposes_full_source_path(self, palace_path, seeded_collection):
        # The displayed source_file is a basename; source_path carries the full
        # stored value so a caller can round-trip it back into a source_file filter.
        result = search_memories("authentication module", palace_path)
        hit = result["results"][0]
        assert hit["source_file"] == "auth.py"
        assert hit["source_path"] == "auth.py"

    def test_source_file_filter_matches_full_path_not_basename(self, palace_path):
        from mempalace.palace import get_collection

        col = get_collection(palace_path, create=True)
        col.upsert(
            ids=["fp1"],
            documents=["The deploy script restarts the gunicorn workers nightly."],
            metadatas=[{"wing": "ops", "room": "deploy", "source_file": "/srv/app/deploy.sh"}],
        )
        # The full stored path matches and round-trips via source_path.
        hit = search_memories(
            "deploy gunicorn workers", palace_path, source_file="/srv/app/deploy.sh"
        )
        assert [h["source_path"] for h in hit["results"]] == ["/srv/app/deploy.sh"]
        assert [h["source_file"] for h in hit["results"]] == ["deploy.sh"]
        # The basename does NOT match — exact full-path semantics only (issue v1).
        miss = search_memories("deploy gunicorn workers", palace_path, source_file="deploy.sh")
        assert miss["results"] == []

    def test_source_file_filter_honored_in_bm25_fallback(self, palace_path, seeded_collection):
        # vector_disabled routes through _bm25_only_via_sqlite (#1222); the
        # source_file filter must hold there too, not silently no-op.
        result = search_memories(
            "authentication module",
            palace_path,
            source_file="auth.py",
            vector_disabled=True,
            collection_name="mempalace_drawers",
        )
        assert "error" not in result
        assert result["results"], "BM25 fallback should still find the auth drawer"
        assert all(r["source_file"] == "auth.py" for r in result["results"])

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
        # Callers (and CI assertions) index ``results`` unconditionally —
        # error envelopes must not omit the key (Windows KeyError flake).
        assert result["results"] == []

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
            assert 0.0 <= h["similarity"] <= 1.0, (
                f"similarity out of range: {h['similarity']} for {h['source_file']}"
            )
            assert 0.0 <= h["effective_distance"] <= 2.0, (
                f"effective_distance out of range: {h['effective_distance']} for {h['source_file']}"
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


@pytest.fixture
def fake_palace_path(tmp_path):
    """tmp_path with chroma.sqlite3 touched so searcher.search's
    filesystem-first state checks (#1498) pass through to the mocked
    backend instead of raising on State A / State B."""
    p = tmp_path / "palace"
    p.mkdir()
    make_minimal_chroma_sqlite(p)
    return str(p)


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

    def test_search_query_error_raises(self, fake_palace_path):
        """search raises SearchError when query fails."""
        mock_col = MagicMock()
        mock_col.query.side_effect = RuntimeError("boom")

        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            with pytest.raises(SearchError, match="Search error"):
                search("test", fake_palace_path)

    def test_search_n_results(self, palace_path, seeded_collection, capsys):
        search("code", palace_path, n_results=1)
        captured = capsys.readouterr()
        # Should have output with at least one result block
        assert "[1]" in captured.out

    def test_search_applies_bm25_hybrid_rerank(self, fake_palace_path, capsys):
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
            search("foo bar baz", fake_palace_path)
        captured = capsys.readouterr()
        first_block, _, _ = captured.out.partition("[2]")
        # Lexical match must rank first
        assert "b.md" in first_block, (
            f"expected lexical match 'b.md' at rank 1, got:\n{captured.out}"
        )
        # Non-zero bm25 reported
        assert "bm25=" in first_block
        assert "bm25=0.0" not in first_block
        # Metric-labeled vector similarity still reported for transparency.
        # Label is now "<metric>_sim=" (honest about the backend's metric)
        # rather than a hard-coded "cosine=".
        assert "cosine_sim=" in first_block

    def test_search_warns_when_palace_uses_wrong_distance_metric(self, fake_palace_path, capsys):
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
            search("anything", fake_palace_path)
        captured = capsys.readouterr()
        assert "mempalace repair" in captured.err
        assert "cosine" in captured.err.lower()

    def test_search_does_not_warn_when_palace_is_correctly_configured(
        self, fake_palace_path, capsys
    ):
        mock_col = MagicMock()
        mock_col.metadata = {"hnsw:space": "cosine"}
        mock_col.query.return_value = {
            "documents": [["some drawer content"]],
            "metadatas": [[{"source_file": "a.md", "wing": "w", "room": "r"}]],
            "distances": [[0.3]],
        }
        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            search("anything", fake_palace_path)
        captured = capsys.readouterr()
        assert "mempalace repair" not in captured.err

    def test_search_handles_none_metadata_without_crash(self, fake_palace_path, capsys):
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
            search("anything", fake_palace_path)
        captured = capsys.readouterr()
        assert "[1]" in captured.out
        assert "[2]" in captured.out
        # Second result renders with fallback '?' values instead of crashing
        assert "second doc" in captured.out

    def test_search_handles_none_document_without_crash(self, fake_palace_path, capsys):
        mock_col = MagicMock()
        mock_col.metadata = {"hnsw:space": "cosine"}
        mock_col.query.return_value = {
            "documents": [["first doc", None]],
            "metadatas": [[{"source_file": "a.md", "wing": "w", "room": "r"}, None]],
            "distances": [[0.1, 0.2]],
        }
        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            search("anything", fake_palace_path)
        captured = capsys.readouterr()
        assert "[1]" in captured.out
        assert "[2]" in captured.out

    def test_search_routes_to_bm25_when_hnsw_diverged(self, fake_palace_path, capsys):
        """Regression: `mempalace search` on a diverged HNSW segment must not
        segfault ChromaDB's Rust bindings.

        The MCP path gates this via ``_vector_disabled`` (#1222); the CLI
        path was missing the gate, so any query into a diverged palace
        exited 139 (SIGBUS) at ``chromadb/api/rust.py:_query`` with zero
        diagnostic output. This test verifies the CLI now probes
        ``hnsw_capacity_status`` and routes to the BM25-only sqlite
        fallback instead of calling ``col.query()`` against a segment
        that would crash it.
        """
        bm25_result = {
            "query": "anything",
            "filters": {},
            "total_before_filter": 1,
            "results": [
                {
                    "text": "diary entry that matches the query",
                    "wing": "wing_test",
                    "room": "diary",
                    "source_file": "test.jsonl",
                    "bm25_score": 1.5,
                    "distance": None,
                }
            ],
            "fallback": "bm25_only_via_sqlite",
            "fallback_reason": "vector_search_disabled",
        }
        with (
            patch("mempalace.searcher.resolve_backend_name", return_value="chroma"),
            patch(
                "mempalace.backends.chroma.hnsw_capacity_status",
                return_value={"diverged": True, "message": "test divergence"},
            ),
            patch("mempalace.searcher._bm25_only_via_sqlite", return_value=bm25_result),
            patch("mempalace.searcher.get_collection") as mock_get_collection,
        ):
            search("anything", fake_palace_path)
        captured = capsys.readouterr()
        # Routed to BM25 before opening Chroma at all. Client construction and
        # identity enforcement can touch the same damaged native index, so a
        # query-only guard is insufficient.
        mock_get_collection.assert_not_called()
        # User got actionable output, not a silent crash.
        assert "mempalace repair" in captured.out
        assert "diary entry that matches" in captured.out

    def test_search_proceeds_to_vector_when_hnsw_healthy(self, fake_palace_path, capsys):
        """Paired guard: when HNSW is healthy, the divergence probe must NOT
        short-circuit to BM25 — vector search proceeds normally.

        Prevents a regression where the gate accidentally always fires.
        """
        mock_col = MagicMock()
        mock_col.metadata = {"hnsw:space": "cosine"}
        mock_col.query.return_value = {
            "documents": [["a matching doc"]],
            "metadatas": [[{"source_file": "a.md", "wing": "w", "room": "r"}]],
            "distances": [[0.1]],
        }
        with (
            patch("mempalace.searcher.resolve_backend_name", return_value="chroma"),
            patch(
                "mempalace.backends.chroma.hnsw_capacity_status",
                return_value={"diverged": False, "status": "ok"},
            ),
            patch("mempalace.searcher._bm25_only_via_sqlite") as mock_bm25,
            patch("mempalace.searcher.get_collection", return_value=mock_col),
        ):
            search("anything", fake_palace_path)
        captured = capsys.readouterr()
        # Vector path ran.
        mock_col.query.assert_called_once()
        # BM25 fallback was NOT invoked.
        mock_bm25.assert_not_called()
        assert "a matching doc" in captured.out

    def test_search_forwards_stop_words_to_bm25_fallback_when_hnsw_diverged(self, fake_palace_path):
        """The diverged-index detour still ranks by BM25, so the stop-word
        filter has to travel with it.

        ``_vector_disabled_search`` already forwards ``stop_words`` on the
        MCP side. Without the same wiring here, a diverged palace would rank
        CLI results by different rules than a healthy one — and silently,
        since the fallback prints results either way.
        """
        seen = {}

        def _spy_bm25(**kwargs):
            seen.update(kwargs)
            return {"query": "the cat", "filters": {}, "total_before_filter": 0, "results": []}

        with (
            patch("mempalace.searcher.resolve_backend_name", return_value="chroma"),
            patch(
                "mempalace.backends.chroma.hnsw_capacity_status",
                return_value={"diverged": True, "message": "test divergence"},
            ),
            patch("mempalace.searcher._resolve_stop_words", return_value=frozenset({"the"})),
            patch("mempalace.searcher._bm25_only_via_sqlite", side_effect=_spy_bm25),
        ):
            search("the cat", fake_palace_path)

        assert seen["stop_words"] == frozenset({"the"})

    def test_search_does_not_run_chroma_probe_for_other_backends(self, fake_palace_path, capsys):
        """The HNSW guard is Chroma-specific and must not fence other backends."""
        mock_col = MagicMock()
        mock_col.query.return_value = {
            "documents": [["backend-native result"]],
            "metadatas": [[{"source_file": "native.md", "wing": "w", "room": "r"}]],
            "distances": [[0.1]],
        }
        with (
            patch("mempalace.searcher.resolve_backend_name", return_value="sqlite_exact"),
            patch("mempalace.backends.chroma.hnsw_capacity_status") as mock_probe,
            patch("mempalace.searcher.get_collection", return_value=mock_col),
        ):
            search("anything", fake_palace_path)

        mock_probe.assert_not_called()
        mock_col.query.assert_called_once()
        assert "backend-native result" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "resolution_error",
        [BackendMismatchError("mixed backend artifacts"), KeyError("unknown_backend")],
    )
    def test_search_delegates_backend_resolution_errors_to_open_diagnostic(
        self, fake_palace_path, resolution_error
    ):
        """The early HNSW fence must not replace normal CLI diagnostics."""
        with (
            patch("mempalace.searcher.resolve_backend_name", side_effect=resolution_error),
            patch("mempalace.searcher._hnsw_capacity_diverged") as mock_probe,
            patch("mempalace.searcher._open_collection_or_explain", return_value=None) as mock_open,
        ):
            with pytest.raises(SearchError):
                search("anything", fake_palace_path)

        mock_probe.assert_not_called()
        mock_open.assert_called_once_with(fake_palace_path, opener=get_collection)


# ── _tokenize stop-word filter ─────────────────────────────────────────


def test_tokenize_default_keeps_all_tokens():
    """Without stop_words, behaviour matches the pre-i18n tokenizer."""
    from mempalace.searcher import _tokenize

    assert _tokenize("The cat sat on the mat") == ["the", "cat", "sat", "on", "the", "mat"]


def test_tokenize_filters_stop_words():
    """When stop_words is given, matching tokens are dropped."""
    from mempalace.searcher import _tokenize

    tokens = _tokenize("The cat sat on the mat", stop_words=frozenset({"the", "on"}))
    assert "the" not in tokens
    assert "on" not in tokens
    assert tokens == ["cat", "sat", "mat"]


def test_tokenize_stop_words_empty_is_no_op():
    """Empty frozenset is the same as default — full backwards compat."""
    from mempalace.searcher import _tokenize

    assert _tokenize("hello world", stop_words=frozenset()) == _tokenize("hello world")


def test_bm25_scores_filters_stop_words_from_query_and_docs():
    """BM25 with a stop-words set uses the filtered vocabulary on both sides."""
    from mempalace.searcher import _bm25_scores

    query = "the quick fox"
    docs = ["the quick fox", "the lazy dog"]
    stop_words = frozenset({"the"})

    filtered = _bm25_scores(query, docs, stop_words=stop_words)
    unfiltered = _bm25_scores(query, docs)

    # Both should rank the first doc higher than the second (fox + quick match).
    assert filtered[0] > filtered[1]
    assert unfiltered[0] > unfiltered[1]
    # Filtered scoring differs because IDF no longer counts "the" across docs.
    assert filtered != unfiltered


def test_bm25_scores_all_stopwords_query_returns_zeros():
    """If every query term is a stop word, BM25 short-circuits to all-zero."""
    from mempalace.searcher import _bm25_scores

    scores = _bm25_scores(
        "the and of", ["a doc", "another"], stop_words=frozenset({"the", "and", "of"})
    )
    assert scores == [0.0, 0.0]


def test_bm25_scores_all_stopword_docs_returns_zero_vector():
    """Every doc emptied by stop-word filter yields zero scores without divide errors."""
    from mempalace.searcher import _bm25_scores

    scores = _bm25_scores("fox", ["the the the", "the the"], stop_words=frozenset({"the", "fox"}))
    assert scores == [0.0, 0.0]


@pytest.fixture(autouse=True)
def _isolate_stopword_cache_and_env(monkeypatch):
    """Reset the stop-word resolution surface before each test.

    Two things would otherwise leak across tests in this module:

    * ``_stopwords_for_canonical`` is ``@lru_cache``'d, so the first test to
      load a locale pins it for the rest of the run. Clearing here turns
      every locale lookup into a fresh load.
    * ``_resolve_stop_words(None)`` reads ``MEMPALACE_LANG`` /
      ``MEMPAL_LANG`` env vars before consulting ``MempalaceConfig``. A
      developer running tests with one of those exported in their shell
      would silently bypass the ``MempalaceConfig`` mocks below and see
      different results than CI. Strip them here.
    """
    from mempalace import searcher

    searcher._stopwords_for_canonical.cache_clear()
    monkeypatch.delenv("MEMPALACE_LANG", raising=False)
    monkeypatch.delenv("MEMPAL_LANG", raising=False)
    yield


def test_resolve_stop_words_falls_back_silently_when_config_raises(monkeypatch):
    """If MempalaceConfig() blows up, return an empty set so search keeps working."""
    from mempalace import searcher

    def boom(*args, **kwargs):
        raise OSError("config.json unreadable")

    monkeypatch.setattr(searcher, "MempalaceConfig", boom)
    assert searcher._resolve_stop_words(None) == frozenset()


def test_resolve_stop_words_none_with_no_explicit_lang_returns_empty(monkeypatch):
    """Unconfigured palaces must not suddenly filter stop words."""
    from mempalace import searcher

    class FakeCfg:
        lang_explicit = None

    monkeypatch.setattr(searcher, "MempalaceConfig", FakeCfg)
    assert searcher._resolve_stop_words(None) == frozenset()


def test_resolve_stop_words_none_with_explicit_lang_applies_filter(monkeypatch):
    """When the user opts in via lang_explicit, the locale's stop words load."""
    from mempalace import searcher

    class FakeCfg:
        lang_explicit = "ja"

    monkeypatch.setattr(searcher, "MempalaceConfig", FakeCfg)
    sw = searcher._resolve_stop_words(None)
    assert "した" in sw


def test_resolve_stop_words_uses_env_var_before_config(monkeypatch):
    """The env-var fast path must avoid constructing MempalaceConfig at all
    on the hot search path when the user has set MEMPALACE_LANG."""
    from mempalace import searcher

    monkeypatch.setenv("MEMPALACE_LANG", "ja")

    sentinel_calls = []

    class TripwireCfg:
        def __init__(self):
            sentinel_calls.append("config-loaded")

        lang_explicit = None

    monkeypatch.setattr(searcher, "MempalaceConfig", TripwireCfg)
    sw = searcher._resolve_stop_words(None)
    assert "した" in sw
    assert sentinel_calls == [], "MempalaceConfig was constructed despite MEMPALACE_LANG being set"


def test_resolve_stop_words_canonicalizes_cache_key():
    """Case variants of the same locale must hit the same lru_cache slot.

    Without canonicalization, ``"en"`` and ``"EN"`` would each consume a
    cache entry pointing at the same set; ``maxsize=16`` could be exhausted
    by a tenant rotating through capitalizations.
    """
    from mempalace import searcher

    a = searcher._resolve_stop_words("en")
    b = searcher._resolve_stop_words("EN")
    c = searcher._resolve_stop_words("En")
    assert a is b is c


def test_resolve_stop_words_caches_per_lang():
    """Repeat lookups for the same lang hit the lru_cache and return the same object."""
    from mempalace import searcher

    a = searcher._resolve_stop_words("ja")
    b = searcher._resolve_stop_words("ja")
    assert a is b


def test_resolve_stop_words_none_reflects_config_change_between_calls(monkeypatch):
    """The None-arg path must re-read config on every call; a stale cache key
    would pin the first result for the lifetime of the process (igorls, #977)."""
    from mempalace import searcher

    class FakeCfgUnset:
        lang_explicit = None

    monkeypatch.setattr(searcher, "MempalaceConfig", FakeCfgUnset)
    first = searcher._resolve_stop_words(None)
    assert first == frozenset()

    class FakeCfgJa:
        lang_explicit = "ja"

    monkeypatch.setattr(searcher, "MempalaceConfig", FakeCfgJa)
    second = searcher._resolve_stop_words(None)
    assert "した" in second, (
        f"cache pinned stale empty set for None after config change; got {second!r}"
    )


# ── stop_words propagation through BM25-only / union-merge paths (post-#1306) ──
#
# #1306 added a second BM25 scoring site inside `_bm25_only_via_sqlite` that
# the original PR didn't cover (it landed on develop after this branch was
# opened). These tests pin the propagation chain so the BM25 fallback and the
# `candidate_strategy="union"` merge tokenize at the same locale as
# `_hybrid_rank`.


def test_bm25_only_via_sqlite_forwards_stop_words_to_bm25_scores(monkeypatch, tmp_path):
    """`_bm25_only_via_sqlite` must pass `stop_words` into `_bm25_scores`.

    Without this, vector-disabled (#1222) palaces silently lose stop-word
    filtering on the BM25 fallback path.
    """
    from mempalace import searcher

    captured = {}
    real_bm25 = searcher._bm25_scores

    def _spy(query, docs, **kwargs):
        captured["stop_words"] = kwargs.get("stop_words")
        return real_bm25(query, docs, **kwargs)

    monkeypatch.setattr(searcher, "_bm25_scores", _spy)

    # Build a minimal chroma.sqlite3 with one matching drawer so the scoring
    # path is reached. Schema mirrors what `_bm25_only_via_sqlite` reads:
    # the embeddings/segments/collections JOIN added by #1306 to scope
    # candidate selection by collection name, plus embeddings.embedding_id,
    # which the metadata SELECT reads as the stored drawer ID so results
    # round-trip through mempalace_get_drawer.
    db = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE VIRTUAL TABLE embedding_fulltext_search USING fts5(string_value, tokenize='trigram');
        CREATE TABLE embedding_metadata (id INTEGER, key TEXT, string_value TEXT, int_value INTEGER);
        CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT);
        CREATE TABLE embeddings (id INTEGER PRIMARY KEY, segment_id TEXT, embedding_id TEXT, created_at TEXT);
        INSERT INTO collections VALUES ('c1', 'mempalace_drawers');
        INSERT INTO segments VALUES ('s1', 'c1');
        INSERT INTO embeddings VALUES (1, 's1', 'drawer-cat-1', '2026-05-03');
        INSERT INTO embedding_fulltext_search (rowid, string_value) VALUES (1, 'the cat sat');
        INSERT INTO embedding_metadata VALUES (1, 'chroma:document', 'the cat sat', NULL);
        INSERT INTO embedding_metadata VALUES (1, 'wing', 'general', NULL);
        INSERT INTO embedding_metadata VALUES (1, 'room', 'inbox', NULL);
        INSERT INTO embedding_metadata VALUES (1, 'source_file', '/x/cat.md', NULL);
        INSERT INTO embedding_metadata VALUES (1, 'filed_at', '2026-05-03', NULL);
        """
    )
    conn.commit()
    conn.close()

    searcher._bm25_only_via_sqlite(
        "the cat",
        str(tmp_path),
        n_results=5,
        collection_name="mempalace_drawers",
        stop_words=frozenset({"the"}),
    )

    assert captured["stop_words"] == frozenset({"the"})


def test_finalize_candidate_hits_forwards_stop_words_to_hybrid_rank(monkeypatch):
    """`_finalize_candidate_hits` must forward `stop_words` into the final
    `_hybrid_rank` re-rank — the BM25 site on the vector/union path. (The
    union candidate gather runs through the backend's own ``lexical_search``,
    which does its own tokenization and takes no mempalace stop words.)"""
    from mempalace import searcher

    captured = {}

    def _hybrid_spy(results, query, **kwargs):
        captured["stop_words"] = kwargs.get("stop_words")
        return results

    monkeypatch.setattr(searcher, "_hybrid_rank", _hybrid_spy)

    searcher._finalize_candidate_hits(
        candidate_strategy="vector",
        hits=[],
        drawers_col=None,
        query="q",
        wing=None,
        room=None,
        n_results=5,
        max_distance=0.0,
        stop_words=frozenset({"a", "an"}),
    )

    assert captured["stop_words"] == frozenset({"a", "an"})


def test_search_memories_vector_disabled_uses_resolved_stop_words(monkeypatch, tmp_path):
    """`vector_disabled=True` must route `_resolve_stop_words(lang)` into the
    BM25 fallback, not skip stop-word resolution as it did pre-fix."""
    from mempalace import searcher

    captured = {}

    def _stub(*args, **kwargs):
        captured["stop_words"] = kwargs.get("stop_words")
        return {"results": [], "fallback": "bm25_only_via_sqlite"}

    monkeypatch.setattr(searcher, "_bm25_only_via_sqlite", _stub)
    monkeypatch.setattr(searcher, "_resolve_stop_words", lambda lang: frozenset({"de", "es"}))

    searcher.search_memories(
        query="q",
        palace_path=str(tmp_path),
        vector_disabled=True,
    )

    assert captured["stop_words"] == frozenset({"de", "es"})


def test_search_cli_threads_resolved_stop_words_to_hybrid_rank(monkeypatch, tmp_path):
    """The `mempalace search ...` CLI handler must resolve stop_words and
    pass them to `_hybrid_rank`, matching the MCP `search_memories` path so
    `MEMPALACE_LANG` filtering works for CLI users too."""
    from mempalace import searcher

    # Satisfy the State-A/B filesystem-first checks added in #1498 so
    # `search()` reaches the `_hybrid_rank` call this test exercises.
    make_minimal_chroma_sqlite(tmp_path)

    captured = {}

    def _fake_get_collection(palace_path, **kwargs):
        class _Col:
            def query(self, **kwargs):
                return {
                    "documents": [["the cat sat"]],
                    "metadatas": [[{"wing": "general"}]],
                    "distances": [[0.5]],
                }

        return _Col()

    def _hybrid_spy(results, query, **kwargs):
        captured["stop_words"] = kwargs.get("stop_words")
        return results

    monkeypatch.setattr(searcher, "get_collection", _fake_get_collection)
    monkeypatch.setattr(searcher, "_warn_if_legacy_metric", lambda col: None)
    monkeypatch.setattr(searcher, "_resolve_stop_words", lambda lang: frozenset({"the"}))
    monkeypatch.setattr(searcher, "_hybrid_rank", _hybrid_spy)

    searcher.search(query="cat", palace_path=str(tmp_path))

    assert captured["stop_words"] == frozenset({"the"})


# ── _result_drawer_id logical-id resolution (#2185) ────────────────────


class TestResultDrawerId:
    """The drawer_id on a search hit must round-trip through
    ``mempalace_get_drawer``. Chunk rows carry their logical-group id under
    ``parent_drawer_id`` (``tool_add_drawer``) or ``parent_entry_id``
    (``tool_diary_write``); both must resolve to the logical id (#2185).
    """

    def test_plain_drawer_returns_stored_id(self):
        assert _result_drawer_id({"wing": "w"}, "drawer_abc") == "drawer_abc"

    def test_parent_drawer_id_wins_over_stored_chunk_id(self):
        meta = {"parent_drawer_id": "drawer_abc", "chunk_index": 2}
        assert _result_drawer_id(meta, "drawer_abc_chunk_000002") == "drawer_abc"

    def test_parent_entry_id_resolves_for_diary_chunks(self):
        """Regression for #2185: before the fix a chunked diary hit reported
        the physical chunk id, so fetching it returned one chunk of the entry
        instead of the whole entry."""
        meta = {"parent_entry_id": "diary_wing_lily_20260808_1", "chunk_index": 3}
        stored = "diary_wing_lily_20260808_1_chunk_000003"
        assert _result_drawer_id(meta, stored) == "diary_wing_lily_20260808_1"

    def test_missing_metadata_falls_back_to_stored_id(self):
        assert _result_drawer_id(None, "drawer_abc") == "drawer_abc"
