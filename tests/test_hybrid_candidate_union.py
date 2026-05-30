"""Tests for ``candidate_strategy="union"`` in ``search_memories``.

The default ``"vector"`` strategy gathers candidates from the vector index
only. Docs with strong BM25 signal but vector embeddings far from the query
get skipped — terminology guides looked up by narrative-shaped queries are
the canonical case.

The ``"union"`` strategy also pulls top-K BM25-only candidates from sqlite
FTS5 and merges them into the rerank pool. Both signal sources contribute
candidates; the hybrid rerank picks the best from a richer pool.

The ``"contains"`` strategy additionally injects drawers whose document
text contains the query as a literal substring (Chroma
``where_document={"$contains": query}``). This catches proper-noun and
exact-string matches that produce embeddings too weak to clear the
vector candidate cutoff — issue #1125.

Default behavior is unchanged ("vector") — these tests exercise opt-in
modes.
"""

from unittest.mock import MagicMock, patch

import pytest

from mempalace.palace import get_collection
from mempalace.searcher import search_memories


def _seed_drawers(palace_path):
    """Seed a corpus where the right doc for one query is BM25-strong but
    vector-distant.

    D1-D3 are short narrative tickets that semantically cluster around
    "customer support / order / shipped" vocabulary. D4 is a meta-document
    of bullet rules ("brand voice") that contains rare keywords like
    "Absolutely" and "apologize" the query repeats verbatim — strong BM25
    signal but stylistically far from the narrative tickets.
    """
    col = get_collection(palace_path, create=True)
    col.upsert(
        ids=["D1", "D2", "D3", "D4"],
        documents=[
            "Customer wrote in asking why their order shipped without "
            "the promo sticker. Standard reply explaining the threshold.",
            "Order delivery delayed three days; customer requested a "
            "refund. Support agent processed return via ticket queue.",
            "Customer asked about the missing freebie; the reply "
            "explained the campaign mechanics and shipped status.",
            "Brand voice rules: dry, sturdy, never effusive. "
            "Never 'Absolutely!' Never apologize for policy — explain it. "
            "Avoid premium / curated / elevated vocabulary.",
        ],
        metadatas=[
            {"wing": "shop", "room": "support", "source_file": "ticket_D1.md"},
            {"wing": "shop", "room": "support", "source_file": "ticket_D2.md"},
            {"wing": "shop", "room": "support", "source_file": "ticket_D3.md"},
            {"wing": "shop", "room": "guides", "source_file": "brand_voice_D4.md"},
        ],
    )


_NARRATIVE_QUERY = (
    "A support agent is drafting a reply to a customer asking why their "
    "order shipped without a free sticker. Draft the reply, but never say "
    "'Absolutely!' and do not apologize for policy."
)


class TestCandidateUnion:
    def test_default_vector_strategy_unchanged(self, tmp_path):
        """Default behavior must be identical to omitting the parameter."""
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        without = search_memories(_NARRATIVE_QUERY, palace, n_results=5)
        with_default = search_memories(
            _NARRATIVE_QUERY, palace, n_results=5, candidate_strategy="vector"
        )
        ids_a = [h["source_file"] for h in without["results"]]
        ids_b = [h["source_file"] for h in with_default["results"]]
        assert ids_a == ids_b, "explicit candidate_strategy='vector' must match default"

    def test_union_surfaces_bm25_strong_vector_distant_doc(self, tmp_path):
        """The brand-voice doc has strong BM25 signal for the query but is
        stylistically far from the narrative tickets. Union mode must
        retrieve it; vector-only mode is allowed to miss it."""
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        result = search_memories(_NARRATIVE_QUERY, palace, n_results=5, candidate_strategy="union")
        ids = [h["source_file"] for h in result["results"]]
        assert "brand_voice_D4.md" in ids, (
            f"union mode must surface BM25-strong docs even when vector signal is weak; got {ids}"
        )

    def test_union_preserves_vector_hits(self, tmp_path):
        """Union mode must not drop docs that vector-only mode finds —
        the rerank pool grows, it doesn't shrink."""
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        vector = search_memories(_NARRATIVE_QUERY, palace, n_results=5, candidate_strategy="vector")
        union = search_memories(_NARRATIVE_QUERY, palace, n_results=5, candidate_strategy="union")
        vec_ids = {h["source_file"] for h in vector["results"]}
        union_ids = {h["source_file"] for h in union["results"]}
        # In a 4-doc corpus with n_results=5, both should return all 4.
        # The invariant is: union should not lose anything vector found.
        missing = vec_ids - union_ids
        assert not missing, f"union dropped docs that vector found: {missing}"

    def test_union_handles_empty_palace(self, tmp_path):
        """No drawers — union mode should return empty results, not crash."""
        palace = str(tmp_path / "palace")
        get_collection(palace, create=True)  # create empty collection
        result = search_memories("anything", palace, n_results=5, candidate_strategy="union")
        assert result.get("results", []) == []

    def test_invalid_candidate_strategy_raises(self, tmp_path):
        """Bad arg should raise rather than silently fall back."""
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        import pytest

        with pytest.raises(ValueError, match="candidate_strategy"):
            search_memories("anything", palace, n_results=5, candidate_strategy="bogus")

    def test_invalid_strategy_raises_even_when_vector_disabled(self, tmp_path):
        """Validation must happen before the ``vector_disabled`` early return —
        invalid values must fail consistently regardless of routing."""
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        import pytest

        with pytest.raises(ValueError, match="candidate_strategy"):
            search_memories(
                "anything",
                palace,
                n_results=5,
                vector_disabled=True,
                candidate_strategy="bogus",
            )

    def test_union_respects_n_results_limit(self, tmp_path):
        """When the merged candidate set is larger than ``n_results``, the
        result must be trimmed back to the requested size — the MCP
        ``limit`` contract depends on this invariant."""
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        # 4-doc corpus, n_results=2 → union pool can grow to ~8 candidates,
        # rerank reorders them, but final list must respect the cap.
        result = search_memories(_NARRATIVE_QUERY, palace, n_results=2, candidate_strategy="union")
        assert len(result["results"]) <= 2, (
            f"union must trim to n_results=2; got {len(result['results'])} results"
        )

    def test_union_skipped_when_max_distance_set(self, tmp_path):
        """``max_distance`` is a vector-distance threshold; BM25-only
        candidates have ``distance=None`` and cannot satisfy it. Union
        must not silently inject them when a strict threshold is set,
        otherwise the existing ``max_distance`` guarantee regresses."""
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        # Sanity: without max_distance, union surfaces the BM25-strong doc.
        unfiltered = search_memories(
            _NARRATIVE_QUERY, palace, n_results=5, candidate_strategy="union"
        )
        assert "brand_voice_D4.md" in {h["source_file"] for h in unfiltered["results"]}

        # With a tight max_distance, union must NOT inject BM25-only hits —
        # every returned hit must have a real (non-None) distance.
        filtered = search_memories(
            _NARRATIVE_QUERY,
            palace,
            n_results=5,
            candidate_strategy="union",
            max_distance=0.5,
        )
        for h in filtered["results"]:
            assert h.get("distance") is not None, (
                f"union under max_distance must not inject BM25-only "
                f"(distance=None) candidates; offending hit: {h}"
            )
            assert h["distance"] <= 0.5, f"hit violates max_distance=0.5: distance={h['distance']}"

    def test_union_dedup_is_chunk_precise_not_basename(self, tmp_path):
        """Two files with the same basename in different directories must
        not collide — union must dedup on full path (or chunk-level key),
        not on basename alone. Otherwise a BM25-strong README from one
        directory silently shadows a BM25-strong README from another.
        """
        palace = str(tmp_path / "palace")
        col = get_collection(palace, create=True)
        col.upsert(
            ids=["A_README", "B_README", "narrative"],
            documents=[
                # Both README files share the basename README.md but live
                # in different directories. Each contains distinctive
                # terminology a query might surface via BM25.
                "PROJECT ALPHA: configuration for the Frobnitz subsystem. "
                "Set FROBNITZ_TIMEOUT=30 to enable widget rotation.",
                "PROJECT BETA: configuration for the Wibble subsystem. "
                "Set WIBBLE_THRESHOLD=0.5 to enable signal smoothing.",
                "Engineers occasionally chat about how the legacy "
                "subsystems all need their config knobs tweaked.",
            ],
            metadatas=[
                {"wing": "code", "room": "docs", "source_file": "alpha/README.md"},
                {"wing": "code", "room": "docs", "source_file": "beta/README.md"},
                {"wing": "code", "room": "docs", "source_file": "chat.md"},
            ],
        )
        # Query that hits BM25 for BOTH READMEs (distinct vocab from each).
        # Vector-only might pick the chat doc as semantically "closest";
        # union must surface both READMEs without basename collision.
        result = search_memories(
            "FROBNITZ_TIMEOUT WIBBLE_THRESHOLD configuration",
            palace,
            n_results=5,
            candidate_strategy="union",
        )
        sources = [h["source_file"] for h in result["results"]]
        readme_count = sum(1 for s in sources if s == "README.md")
        assert readme_count >= 2, (
            f"union must surface both README.md files from different dirs "
            f"(basename collision would drop one); got sources={sources}"
        )


# ── candidate_strategy="contains" (#1125) ──────────────────────────────


def _make_mocked_collection(vector_hits=None, contains_hits=None):
    """Build a mock collection whose ``.query()`` and ``.get()`` return
    fully-controlled candidates. Lets the contains-strategy tests pin
    each path's contribution independently — small live corpora are too
    permissive (vector returns all candidates) to discriminate which
    strategy surfaced a result."""

    def _doc_payload(hits):
        if not hits:
            return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}
        return {
            "documents": [[h["document"] for h in hits]],
            "metadatas": [[h["metadata"] for h in hits]],
            "distances": [[h.get("distance", 0.1) for h in hits]],
            "ids": [[h["id"] for h in hits]],
        }

    def _get_payload(hits):
        hits = hits or []
        return MagicMock(
            documents=[h["document"] for h in hits],
            metadatas=[h["metadata"] for h in hits],
            ids=[h["id"] for h in hits],
        )

    mock_col = MagicMock()
    mock_col.metadata = {"hnsw:space": "cosine"}
    mock_col.query.return_value = _doc_payload(vector_hits)

    def fake_get(**kwargs):
        # The closet path (if any) goes through ``query`` not ``get``;
        # only the contains merger uses ``get(where_document=...)``.
        # Other ``.get`` callers receive an empty result.
        if "where_document" in kwargs:
            return _get_payload(contains_hits)
        return _get_payload([])

    mock_col.get.side_effect = fake_get
    return mock_col


def _hit(*, id, source, document, wing="ops", room="infra", chunk_index=0):
    return {
        "id": id,
        "document": document,
        "metadata": {
            "wing": wing,
            "room": room,
            "source_file": source,
            "chunk_index": chunk_index,
            "filed_at": "2026-01-01T00:00:00",
        },
    }


class TestContainsStrategy:
    """``contains`` strategy pulls drawers whose ``documents`` contain the
    query as a literal substring and unions them into the rerank pool.

    Tests use mocked collections rather than live ChromaDB so we can pin
    each retrieval path's contribution and prove the strategy actually
    does work — a small live corpus over-fetches every drawer into the
    candidate pool anyway, masking which path surfaced a result.
    """

    def test_invalid_strategy_still_raises(self):
        with pytest.raises(ValueError, match="bogus"):
            with patch(
                "mempalace.searcher.get_collection",
                return_value=_make_mocked_collection(),
            ):
                search_memories("anything", "/fake/path", candidate_strategy="bogus")

    def test_default_vector_strategy_unchanged_after_contains_added(self):
        vector_only = [_hit(id="V1", source="rev_plan.md", document="quarterly revenue")]
        with patch(
            "mempalace.searcher.get_collection",
            return_value=_make_mocked_collection(vector_hits=vector_only),
        ):
            without = search_memories("revenue", "/fake/path", n_results=2)
            explicit = search_memories(
                "revenue", "/fake/path", n_results=2, candidate_strategy="vector"
            )
        assert [h["source_file"] for h in without["results"]] == [
            h["source_file"] for h in explicit["results"]
        ], "explicit candidate_strategy='vector' must match default"

    def test_contains_surfaces_substring_match_vector_misses(self):
        """Vector returns unrelated drawers; only the contains path
        surfaces the literal-substring drawer. Strategy must merge it
        into the rerank pool."""
        vector_hits = [
            _hit(id="V1", source="rev_plan.md", document="quarterly revenue planning"),
            _hit(id="V2", source="db_migration.md", document="postgres migration log"),
        ]
        contains_hits = [
            _hit(
                id="C1",
                source="procurement.md",
                document="Xerathon-7 chassis arrives Tuesday",
            ),
        ]
        with patch(
            "mempalace.searcher.get_collection",
            return_value=_make_mocked_collection(
                vector_hits=vector_hits, contains_hits=contains_hits
            ),
        ):
            result = search_memories(
                "Xerathon-7",
                "/fake/path",
                n_results=3,
                candidate_strategy="contains",
            )
        sources = [h["source_file"] for h in result["results"]]
        assert "procurement.md" in sources, (
            f"contains strategy must merge the where_document substring "
            f"match into the rerank pool; got sources={sources}"
        )

    def test_contains_invokes_where_document_filter(self):
        """Contract test — the merger calls ``.get`` exactly once with
        ``where_document={"$contains": query}``. Any drift breaks the
        issue-#1125 promise."""
        mock_col = _make_mocked_collection()
        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            search_memories(
                "Alice",
                "/fake/path",
                n_results=5,
                candidate_strategy="contains",
            )

        where_doc_calls = [c for c in mock_col.get.call_args_list if "where_document" in c.kwargs]
        assert len(where_doc_calls) == 1, (
            f"contains must call .get with where_document exactly once; "
            f"got {len(where_doc_calls)} calls"
        )
        kwargs = where_doc_calls[0].kwargs
        assert kwargs["where_document"] == {"$contains": "Alice"}
        # ``limit`` must be a positive int — exact value is the merger's
        # tuning knob, but it must be set to avoid full-collection scans.
        assert isinstance(kwargs.get("limit"), int) and kwargs["limit"] > 0, (
            f"contains must pass a positive int limit to bound the fetch; "
            f"got limit={kwargs.get('limit')!r}"
        )

    def test_contains_dedup_does_not_dupe_vector_hits(self):
        """Same drawer surfaced by BOTH vector and contains paths must
        appear exactly once in the merged pool. Without dedup the count
        would be 2."""
        shared = _hit(
            id="SHARED",
            source="procurement.md",
            document="Xerathon-7 chassis arrives Tuesday",
        )
        with patch(
            "mempalace.searcher.get_collection",
            return_value=_make_mocked_collection(vector_hits=[shared], contains_hits=[shared]),
        ):
            result = search_memories(
                "Xerathon-7",
                "/fake/path",
                n_results=4,
                candidate_strategy="contains",
            )
        sources = [h["source_file"] for h in result["results"]]
        assert sources.count("procurement.md") == 1, (
            f"shared drawer must dedup to exactly one entry; got sources={sources}"
        )

    def test_contains_respects_wing_room_filters(self):
        """The ``where`` filter (wing/room) must be forwarded to the
        ``.get()`` call — otherwise the strategy leaks cross-room
        candidates that the vector path filters out at query time."""
        mock_col = _make_mocked_collection()
        with patch("mempalace.searcher.get_collection", return_value=mock_col):
            search_memories(
                "Alice",
                "/fake/path",
                n_results=4,
                wing="ops",
                room="sales",
                candidate_strategy="contains",
            )

        where_doc_calls = [c for c in mock_col.get.call_args_list if "where_document" in c.kwargs]
        assert where_doc_calls, "contains must call .get with where_document"
        passed_where = where_doc_calls[0].kwargs.get("where")
        # Implementation may pass a single-key dict or a $and combination;
        # the contract is only that wing AND room are both expressed.
        flat = str(passed_where)
        assert "ops" in flat and "sales" in flat, (
            f"wing/room must be forwarded to the contains .get(); got where={passed_where!r}"
        )

    def test_contains_forwards_collection_name(self):
        """Explicit ``collection_name`` (e.g. a tenant-prefixed collection)
        must be forwarded into the contains merger's ``get_collection``
        call so multi-collection deployments don't silently fall back to
        the default collection."""
        captured = {}

        def fake_get_collection(palace_path, *, collection_name=None, create=True):
            captured.setdefault("calls", []).append(collection_name)
            mock_col = _make_mocked_collection()
            return mock_col

        with patch("mempalace.searcher.get_collection", side_effect=fake_get_collection):
            search_memories(
                "Alice",
                "/fake/path",
                n_results=2,
                collection_name="tenant_abc_drawers",
                candidate_strategy="contains",
            )

        assert "tenant_abc_drawers" in captured["calls"], (
            f"contains merger must forward collection_name to get_collection; "
            f"saw calls={captured['calls']}"
        )


class TestHybridRankTolerantOfMissingDistance:
    """``_hybrid_rank`` accepts ``distance=None`` — required for BM25-only
    candidates injected by union mode."""

    def test_distance_none_scored_as_zero_vector_sim(self):
        from mempalace.searcher import _hybrid_rank

        results = [
            {"text": "alpha beta gamma", "distance": 0.2},  # close vector match
            {"text": "alpha alpha alpha", "distance": None},  # BM25-only — heavy term repetition
        ]
        # Query matches "alpha" heavily; the BM25-only candidate with no
        # vector signal should still rank competitively on BM25 alone.
        ranked = _hybrid_rank(results, "alpha")
        assert all("bm25_score" in r for r in ranked), "rerank should add bm25_score"
        # Both must survive — neither should crash on distance=None.
        assert len(ranked) == 2
