"""RFC 009 drawer/closet privacy with facts excluded; bounded defaults."""

from types import SimpleNamespace

import pytest

from mempalace.backends.chroma import ChromaBackend
from mempalace.backends.compact import CompactBackend
from mempalace.backends.milvus import MilvusBackend
from mempalace.backends.pgvector import PgVectorBackend
from mempalace.backends.qdrant import QdrantBackend
from mempalace.backends.sqlite_exact import SQLiteExactBackend
from mempalace.palace import get_collection
from mempalace.searcher import _resolve_candidate_strategy, search_memories
from tests._overview_helpers import overview_result


@pytest.mark.parametrize("backend", [ChromaBackend, SQLiteExactBackend, CompactBackend])
def test_indexed_backends_default_to_union(backend):
    col = SimpleNamespace(_backend=backend, supports_bounded_lexical_filter=lambda where: True)
    assert _resolve_candidate_strategy(None, col) == "union"
    assert _resolve_candidate_strategy("vector", col) == "vector"


@pytest.mark.parametrize("backend", [PgVectorBackend, QdrantBackend, MilvusBackend])
def test_scan_backends_keep_vector_default(backend):
    col = SimpleNamespace(_backend=backend, supports_bounded_lexical_filter=lambda where: True)
    assert "bounded_lexical_search" not in backend.capabilities
    assert _resolve_candidate_strategy(None, col) == "vector"
    assert _resolve_candidate_strategy("union", col) == "vector"


@pytest.mark.parametrize("backend", [PgVectorBackend, QdrantBackend])
def test_default_search_never_calls_scan_backend_lexical(tmp_path, monkeypatch, backend):
    from mempalace import searcher
    from mempalace.backends.base import QueryResult

    def lexical(**kwargs):
        pytest.fail("default search must not scan a server collection for lexical candidates")

    col = SimpleNamespace(
        _backend=backend,
        metadata={"hnsw:space": "cosine"},
        query=lambda **kw: QueryResult(
            ids=[["a"]], documents=[["ordinary note"]], metadatas=[[{}]], distances=[[0.5]]
        ),
        lexical_search=lexical,
    )
    monkeypatch.setattr(searcher, "get_collection", lambda *args, **kwargs: col)
    monkeypatch.setattr(searcher, "get_closets_collection", lambda *args, **kwargs: None)
    result = searcher.search_memories("ordinary", str(tmp_path), include_facts=False)
    assert not result.get("error")
    assert result["results"][0]["drawer_id"] == "a"
    assert "candidate_strategy_fallback" not in result


@pytest.mark.parametrize(
    "reason", ["long_query_no_literal", "no_terms_after_stopwords", "no_discriminative_terms"]
)
def test_only_query_shape_skip_reasons_leave_the_lexical_boundary(reason):
    from mempalace.backends.base import LexicalResult
    from mempalace.searcher import _merge_bm25_union_candidates

    col = SimpleNamespace(
        distance_metric="cosine",
        union_lexical_search=lambda **kw: LexicalResult(hits=[], skip_reason=reason),
    )
    stats = {}
    _merge_bm25_union_candidates([], col, "query", None, None, n_results=5, stats=stats, where=None)
    assert stats == ({} if reason == "no_discriminative_terms" else {"lexical_skipped": reason})


def test_compact_default_finds_keyword_only_drawer(tmp_path, monkeypatch):
    from mempalace.backends import embedding_wrapper
    from mempalace.stored_text import KEYWORD_ONLY

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "compact")
    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts, **kw: [[1.0, 0.0]] * len(texts)
    )
    palace = str(tmp_path / "compact")
    col = get_collection(palace, create=True)
    col.add(
        ids=["vector", "keyword"],
        documents=["routine note", "ZXQ739251 /srv/kestrel/manifest.lock"],
        embeddings=[[0.01, 1.0], None],
        metadatas=[{"wing": "shared"}, {"wing": "shared", KEYWORD_ONLY: True}],
    )
    result = search_memories("ZXQ739251", palace, wing="shared", include_facts=False)
    assert not result.get("error")
    assert result["results"][0]["drawer_id"] == "keyword"
    assert result["results"][0]["matched_via"] == "bm25_backend"


def test_wing_union_stays_scoped_when_vector_index_is_disabled(tmp_path, monkeypatch, capsys):
    from mempalace import searcher

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "chroma")
    backend = ChromaBackend()
    col = backend.get_collection(str(tmp_path), "mempalace_drawers", create=True)
    col.add(
        ids=["visible", "hidden"],
        documents=["ZXQ739251 visible words", "ZXQ739251 hidden words"],
        embeddings=[[1.0, 0.0]] * 2,
        metadatas=[{"wing": "public"}, {"wing": "private"}],
    )
    backend.close()
    for wings, expected in ((["public"], {"visible"}), ([], set())):
        result = search_memories(
            "ZXQ739251", str(tmp_path), wings=wings, vector_disabled=True, include_facts=False
        )
        assert {hit["drawer_id"] for hit in result["results"]} == expected
    monkeypatch.setattr(searcher, "_hnsw_capacity_diverged", lambda path: True)
    searcher.search("ZXQ739251", str(tmp_path), wings=["public"], facts=[])
    output = capsys.readouterr().out
    assert "visible words" in output and "hidden words" not in output


def test_vector_pool_widening_reuses_this_searchs_lexical_result(tmp_path, monkeypatch):
    from mempalace import searcher
    from mempalace.backends import embedding_wrapper

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts, **kw: [[1.0, 0.0]] * len(texts)
    )
    col = get_collection(str(tmp_path), create=True)
    col.add(
        ids=[f"_reg_d{i}" for i in range(100)] + ["needle"],
        documents=["identical ordinary note"] * 100 + ["ZXQ739251 keyword-only needle"],
        embeddings=[[0.01, 1.0]] * 100 + [[0.0, 0.0]],
        metadatas=[{"source_file": f"{i}.md"} for i in range(101)],
    )
    calls, vector_calls = [], []
    lexical, vector = col.union_lexical_search, col.query

    def fetch(**kw):
        calls.append(kw)
        return lexical(**kw)

    def query(**kw):
        vector_calls.append(kw)
        return vector(**kw)

    monkeypatch.setattr(col, "union_lexical_search", fetch)
    monkeypatch.setattr(col, "query", query)
    monkeypatch.setattr(searcher, "get_collection", lambda *a, **kw: col)
    result = search_memories("ZXQ739251", str(tmp_path), n_results=5, include_facts=False)
    assert "needle" in {hit["drawer_id"] for hit in result["results"]}
    assert len(vector_calls) > 1
    assert len(calls) == 1


def test_vector_failure_joins_the_bounded_lexical_read():
    import threading

    from mempalace.backends.base import LexicalResult
    from mempalace.searcher import _query_drawer_candidates

    failed, finished = threading.Event(), threading.Event()

    def vector(**kw):
        failed.set()
        raise RuntimeError("vector unavailable")

    def lexical(**kw):
        assert failed.wait(2)
        finished.set()
        return LexicalResult(hits=[])

    col = SimpleNamespace(query=vector, union_lexical_search=lexical)
    with pytest.raises(RuntimeError, match="vector unavailable"):
        _query_drawer_candidates(
            col, {}, "query", 5, None, None, None, "union", {}, frozenset(), False
        )
    assert finished.is_set()


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact", "compact"])
@pytest.mark.parametrize("oldest_best", [False, True])
def test_saturated_union_pool_scores_best_match_beyond_early_row_limit(
    tmp_path, monkeypatch, backend, oldest_best
):
    from mempalace.backends import embedding_wrapper

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts, **kw: [[1.0, 0.0]] * len(texts)
    )
    palace = str(tmp_path / "palace")
    col = get_collection(palace, create=True)
    rows = [
        (
            f"d{i}",
            ("quartz" if i % 2 else "migration") + " engineering commentary " * 20,
            [0.01, 1.0],
        )
        for i in range(400)
    ]
    rows.insert(0 if oldest_best else len(rows), ("needle", "quartz migration", [0.0, 0.0]))
    col.add(
        ids=[row[0] for row in rows],
        documents=[row[1] for row in rows],
        embeddings=[row[2] for row in rows],
        metadatas=[{"wing": "public", "room": "notes"}] * len(rows),
    )
    # Each selected term is under the scoped cutoff. All matches, not only
    # the first 5*limit rows, must be scored before taking the lexical top K.
    # An oldest needle catches a recent-only LIMIT; a newest one catches the reverse.
    lexical = col.union_lexical_search(
        query="quartz migration", n_results=1, where={"wing": "public"}
    )
    assert [h.id for h in lexical.hits] == ["needle"]
    result = search_memories(
        "quartz migration", palace, wing="public", n_results=1, include_facts=False
    )
    assert result["results"][0]["drawer_id"] == "needle"
    assert result["results"][0]["matched_via"] == "bm25_backend"


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact"])
def test_topic_word_survives_an_incidental_rare_literal(tmp_path, monkeypatch, backend):
    from mempalace.backends import embedding_wrapper

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts, **kw: [[1.0, 0.0]] * len(texts)
    )
    palace = str(tmp_path / "palace")
    col = get_collection(palace, create=True)
    col.add(
        ids=[f"noise{i}" for i in range(60)] + ["needle"],
        documents=[
            "Review in March 2024" if i % 6 == 0 else "Routine budget planning" for i in range(60)
        ]
        + ["Kestrel migration: rollback window is Sunday night."],
        embeddings=[[0.01, 1.0]] * 60 + [[0.0, 0.0]],
        metadatas=[{"wing": "shared"}] * 61,
    )
    result = search_memories(
        "March 2024 kestrel migration rollback window",
        palace,
        wing="shared",
        include_facts=False,
    )
    needle = next(hit for hit in result["results"] if hit["drawer_id"] == "needle")
    assert needle["matched_via"] == "bm25_backend"


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact", "compact"])
def test_single_wing_closets_do_not_apply_the_drawer_df_cutoff(tmp_path, monkeypatch, backend):
    from mempalace.palace import get_closets_collection
    from mempalace.searcher import _closet_boosts

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    closets = get_closets_collection(str(tmp_path), create=True)
    for start in range(0, 1101, 500):
        indices = list(range(start, min(start + 500, 1101)))
        closets.add(
            ids=[f"c{i}" for i in indices],
            documents=[f"quartz rollout|team|→d{i}" for i in indices],
            embeddings=[[1.0, 0.0]] * len(indices),
            metadatas=[{"wing": "public", "source_file": f"{i}.md"} for i in indices],
        )
    old = closets.lexical_search(query="quartz", n_results=10, where={"wing": "public"})
    actual = closets.union_lexical_search(
        query="quartz", n_results=10, where={"wing": "public"}, closet=True
    )
    assert len(old.hits) == 10
    assert [h.id for h in actual.hits] == [h.id for h in old.hits]
    boosts = _closet_boosts(closets, query="quartz", n_results=5, where={"wing": "public"})
    assert list(boosts) == [h.metadata["source_file"] for h in old.hits]


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact", "compact"])
def test_saturated_closet_term_disables_all_boosts(tmp_path, monkeypatch, backend):
    from mempalace.palace import get_closets_collection
    from mempalace.searcher import _closet_boosts

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    closets = get_closets_collection(str(tmp_path), create=True)
    for start in range(0, 5000, 500):
        closets.add(
            ids=[f"c{i}" for i in range(start, start + 500)],
            documents=["quartz rollout|team|→drawer"] * 500,
            embeddings=[[1.0, 0.0]] * 500,
            metadatas=[
                {"wing": "public", "source_file": f"{i}.md"} for i in range(start, start + 500)
            ],
        )
    assert _closet_boosts(closets, query="quartz", n_results=5, where={"wing": "public"}) == {}


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact", "compact"])
@pytest.mark.parametrize("seed", range(2))
def test_scope_local_terms_ids_closets_and_full_responses_are_private(
    tmp_path, monkeypatch, backend, seed
):
    """Scope > term LIMIT, foreign df crossing cutoff, two principals, one worker."""
    import random

    from commonplace.gateway import Mediator
    from mempalace.backends import embedding_wrapper
    from mempalace.backends.base import QueryResult
    from mempalace.palace import get_closets_collection

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts, **kw: [[1.0, 0.0]] * len(texts)
    )
    if backend == "chroma":
        # Filtered HNSW itself depends on unreadable vectors. Isolate lexical
        # privacy by replacing ANN with exact cosine over the supplied scope.
        from mempalace.backends.chroma import ChromaCollection

        def exact(self, **kwargs):
            records = self.get(
                where=kwargs.get("where"), include=["documents", "metadatas", "embeddings"]
            )
            ranked = []
            for i, vector in enumerate(records.embeddings):
                norm = sum(v * v for v in vector) ** 0.5
                distance = 1 - (vector[0] / norm if norm else 0)
                ranked.append(
                    (distance, records.ids[i], records.documents[i], records.metadatas[i])
                )
            ranked.sort(key=lambda row: (row[0], row[1]))
            ranked = ranked[: kwargs["n_results"]]
            return QueryResult(
                ids=[[r[1] for r in ranked]],
                documents=[[r[2] for r in ranked]],
                metadatas=[[r[3] for r in ranked]],
                distances=[[r[0] for r in ranked]],
            )

        monkeypatch.setattr(ChromaCollection, "query", exact)
    palace = str(tmp_path / "palace")
    col = get_collection(palace, create=True)
    rng = random.Random(seed)
    words = ["lantern", "marigold", "kestrel", "migration", "deploy"]
    count = 1105
    col.add(
        ids=[f"d{i}" for i in range(count)],
        documents=[
            "quartz " + " ".join(rng.choices(words, k=4)) + f" record{i}" for i in range(count)
        ],
        embeddings=[[0.01 + i / 100000, 1.0] for i in range(count)],
        metadatas=[
            {"wing": "public" if i < 1101 else "shared", "room": "notes", "source_file": f"{i}.md"}
            for i in range(count)
        ],
    )
    if backend == "compact":
        from mempalace.stored_text import KEYWORD_ONLY

        col.add(
            ids=["keyword-public", "keyword-shared", "keyword-private"],
            documents=["photon731 lantern exact tool output"] * 3,
            embeddings=[None] * 3,
            metadatas=[
                {"wing": wing, "room": "notes", KEYWORD_ONLY: True}
                for wing in ("public", "shared", "private")
            ],
        )
    closets = get_closets_collection(palace, create=True)
    closets.add(
        ids=["alpha", "beta"],
        documents=["lantern rollout|team|→d0", "kestrel rollout|team|→d1"],
        embeddings=[[1.0, 0.0]] * 2,
        metadatas=[
            {"wing": "public", "source_file": "0.md"},
            {"wing": "public", "source_file": "1.md"},
        ],
    )
    principal = SimpleNamespace(
        workspace_id="fixture-workspace",
        policy=SimpleNamespace(can_read_wing=lambda wing: wing == "public"),
    )
    other = SimpleNamespace(
        workspace_id="fixture-workspace",
        policy=SimpleNamespace(can_read_wing=lambda wing: wing in {"public", "shared"}),
    )
    calls = []

    def call_tool(who, name, args):
        assert who.workspace_id == "fixture-workspace"
        overview = overview_result(
            name, args, dict.fromkeys(("public", "shared", "private"), "notes")
        )
        if overview is not None:
            return overview
        calls.append(args)
        return search_memories(
            args["query"], palace, wings=args["wings"], n_results=args["limit"], include_facts=False
        )

    mediator = object.__new__(Mediator)
    mediator.workers = SimpleNamespace(call_tool=call_tool)
    queries = ["lantern kestrel", "quartz", "photon731", "session summary"] + [
        " ".join(rng.sample(words, 2)) for _ in range(2)
    ]

    def outputs():
        out = []
        for query in queries:
            lexical = col.union_lexical_search(query=query, n_results=20, where={"wing": "public"})
            out.append(
                (
                    lexical.selected_terms,
                    [(h.id, h.score) for h in lexical.hits],
                    search_memories(query, palace, wing="public", include_facts=False),
                    mediator._fan_out_search(
                        principal, "mempalace_search", {"query": query, "limit": 5}
                    ),
                )
            )
        return out

    # Reject an over-cutoff term as a whole instead of ranking a partial pool.
    saturated = col.union_lexical_search(query="quartz", n_results=20, where={"wing": "public"})
    assert saturated.selected_terms == [] and saturated.hits == []
    before = outputs()
    mediator._fan_out_search(other, "mempalace_search", {"query": "marigold photon731", "limit": 5})
    assert outputs() == before
    col.add(
        ids=[f"secret{i}" for i in range(1200)],
        documents=[
            "photon731 lantern lantern " + " ".join(rng.choices(words, k=4)) for _ in range(1200)
        ],
        embeddings=[[0.0, 1.0]] * 1200,
        metadatas=[{"wing": "private", "source_file": "0.md"}] * 1200,
    )
    closets.add(
        ids=[f"private-pointer{i}" for i in range(100)],
        documents=["lantern lantern|team|→hidden"] * 100,
        embeddings=[[1.0, 0.0]] * 100,
        metadatas=[{"wing": "private"}] * 100,
    )
    assert outputs() == before
    assert all(set(args["wings"]) <= {"public", "shared"} for args in calls)
    assert all("wing" not in args for args in calls)


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact", "compact"])
def test_unpushable_filters_resolve_explicit_union_to_vector(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    col = get_collection(str(tmp_path / "palace"), create=True)
    where = {"wing": {"$contains": "public"}}
    assert not col.supports_bounded_lexical_filter(where)
    stats = {}
    assert _resolve_candidate_strategy("union", col, where, stats) == "vector"
    assert stats == {"lexical_skipped": "lexical_unbounded_for_filter"}


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact", "compact"])
def test_term_fetch_budget_and_cjk_single_fetch(tmp_path, monkeypatch, backend):
    from mempalace.backends.scoped_lexical import ScopedFTS

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    col = get_collection(str(tmp_path / "palace"), create=True)
    col.add(
        ids=["literals", "cjk"],
        documents=[" ".join(f"id{i}" for i in range(40)), "项目部署配置故障排查"],
        embeddings=[[1.0, 0.0]] * 2,
    )
    calls = []
    fetch = ScopedFTS.fetch

    def record(self, term, limit):
        calls.append(term)
        return fetch(self, term, limit)

    monkeypatch.setattr(ScopedFTS, "fetch", record)
    result = col.union_lexical_search(query=" ".join(f"id{i}" for i in range(40)))
    assert len(result.selected_terms) == 8 and len(calls) == 8
    calls.clear()
    col.union_lexical_search(query=" ".join(f"miss{i}" for i in range(40)))
    assert len(calls) == 16
    calls.clear()
    result = col.union_lexical_search(query="项目部署配置故障排查")
    assert [h.id for h in result.hits] == ["cjk"]
    assert len(calls) == 1


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact", "compact"])
def test_closet_statistics_exclude_drawers_even_in_the_same_wing(tmp_path, monkeypatch, backend):
    """Legacy table-wide IDF can swap alpha/beta; closet-only scope pins their order."""
    from mempalace.backends import embedding_wrapper
    from mempalace.palace import get_closets_collection

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts, **kw: [[1.0, 0.0]] * len(texts)
    )
    palace = str(tmp_path / "palace")
    drawers = get_collection(palace, create=True)
    closets = get_closets_collection(palace, create=True)
    closets.add(
        ids=["ca", "cb"],
        documents=["alpha rollout|team|→a", "beta rollout|team|→b"],
        embeddings=[[1.0, 0.0]] * 2,
        metadatas=[
            {"wing": "public", "source_file": "a.md"},
            {"wing": "public", "source_file": "b.md"},
        ],
    )

    def closet_order():
        result = closets.union_lexical_search(
            query="alpha beta", n_results=10, where={"wing": "public"}, closet=True
        )
        return [hit.id for hit in result.hits]

    assert closet_order() == ["ca", "cb"]
    drawers.add(
        ids=["a", "b"] + [f"noise{i}" for i in range(300)],
        documents=["Decision detail."] * 2 + ["alpha alpha rollout"] * 300,
        embeddings=[[1.0, 0.0]] * 2 + [[0.01, 1.0]] * 300,
        metadatas=[{"wing": "public", "source_file": name} for name in ("a.md", "b.md")]
        + [{"wing": "public", "source_file": f"noise{i}.md"} for i in range(300)],
    )
    assert closet_order() == ["ca", "cb"]
    if backend != "chroma":
        # Develop's SQLite FTS ranks with drawers from the same table, making
        # beta rarer. That cross-collection influence is intentionally removed.
        legacy = closets.lexical_search(query="alpha beta", n_results=10, where={"wing": "public"})
        assert [hit.id for hit in legacy.hits] == ["cb", "ca"]
    from mempalace import searcher
    from mempalace.backends.base import QueryResult

    # Keep vector candidates deterministic so closet statistics control boosts.
    monkeypatch.setattr(searcher, "get_collection", lambda *args, **kw: drawers)
    monkeypatch.setattr(
        drawers,
        "query",
        lambda **kw: QueryResult(
            ids=[["a", "b"]],
            documents=[["Decision detail."] * 2],
            metadatas=[
                [
                    {"wing": "public", "source_file": "a.md"},
                    {"wing": "public", "source_file": "b.md"},
                ]
            ],
            distances=[[0.2, 0.2]],
        ),
    )
    result = search_memories(
        "alpha beta",
        palace,
        wing="public",
        n_results=2,
        candidate_strategy="vector",
        include_facts=False,
    )
    assert [(hit["drawer_id"], hit["closet_boost"]) for hit in result["results"]] == [
        ("a", 0.40),
        ("b", 0.25),
    ]


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact", "compact"])
def test_cli_compatible_wing_union_never_prints_private_drawer(monkeypatch, config, kg, backend):
    from _mcp_server_helpers import _patch_mcp_server

    from commonplace.gateway import Mediator, _tool_response, _tool_result
    from commonplace.policy import Policy
    from mempalace import mcp_server
    from mempalace.backends import embedding_wrapper

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts, **kw: [[1.0, 0.0]] * len(texts)
    )
    _patch_mcp_server(monkeypatch, config, kg)
    palace = config.palace_path
    col = get_collection(palace, create=True)
    col.add(
        ids=[f"d{i}" for i in range(30)] + ["public-needle", "private-needle"],
        documents=["routine note"] * 30 + ["ZXQ739251 PUBLIC_NEEDLE", "ZXQ739251 PRIVATE_SECRET"],
        embeddings=[[0.01, 1.0]] * 30 + [[0.0, 0.0]] * 2,
        metadatas=[{"wing": "public"}] * 31 + [{"wing": "private"}],
    )
    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp_server, "_vector_disabled", False)
    session_col = col
    if backend == "chroma":
        session_col = mcp_server._checked_chroma_collection(col._inner._collection, create=False)
        assert _resolve_candidate_strategy(None, session_col) == "union"
    monkeypatch.setattr(mcp_server, "_get_collection", lambda: session_col)
    direct = mcp_server.tool_search(
        "ZXQ739251", wings=["public"], cli_compatible=True, include_facts=False
    )
    assert "PUBLIC_NEEDLE" in direct["cli_output"] and "PRIVATE_SECRET" not in direct["cli_output"]
    monkeypatch.setattr(mcp_server, "_get_collection", lambda: col)
    without_hub = mcp_server.tool_search(
        "ZXQ739251", wings=["public"], cli_compatible=True, include_facts=False
    )
    assert direct == without_hub
    mediator = object.__new__(Mediator)

    def call_tool(principal, name, args):
        assert principal.workspace_id == "fixture-workspace"
        overview = overview_result(name, args, {"public": "notes", "private": "notes"})
        if overview is not None:
            return overview
        return mcp_server.tool_search(**args)

    mediator.workers = SimpleNamespace(call_tool=call_tool)
    mediator.gateway = SimpleNamespace()
    principal = SimpleNamespace(
        workspace_id="fixture-workspace",
        policy=Policy.build(
            profile="full", scopes=["memory:read", "private:read"], read_wings=["*"]
        ),
    )
    args = {
        "query": "ZXQ739251",
        "wings": ["public"],
        "cli_compatible": True,
        "include_facts": False,
    }
    result = mediator._fan_out_search(principal, "mempalace_search", args)
    assert "PUBLIC_NEEDLE" in result["cli_output"] and "PRIVATE_SECRET" not in result["cli_output"]
    member = SimpleNamespace(
        workspace_id="fixture-workspace",
        policy=Policy.build(profile="full", scopes=["memory:read"], read_wings=["public"]),
    )
    protected_room = "thr_0123456789abcdef"
    col.add(
        ids=["protected-public-needle"],
        documents=["ZXQ739251 PRIVATE_SECRET"],
        embeddings=[[0.0, 0.0]],
        metadatas=[{"wing": "public", "room": protected_room}],
    )
    acquired = mediator._fan_out_search(member, "mempalace_search", args)
    assert any("PUBLIC_NEEDLE" in row["text"] for row in acquired["results"])
    assert any(
        "PRIVATE_SECRET" in row["text"]
        and row["wing"] == "public"
        and row["room"] == protected_room
        for row in acquired["results"]
    ), "Protected public-room drawer never reached the gate"
    response, _ = mediator._gate_response(
        member, "mempalace_search", "search", 1, _tool_response(1, acquired), cli_args=args
    )
    gated = _tool_result(response)
    assert "PUBLIC_NEEDLE" in gated["cli_output"]
    assert "PRIVATE_SECRET" not in gated["cli_output"], "Protected public-room drawer leaked"
    assert gated["results"] == []


def test_moderately_common_pool_preranks_rowids_before_loading_text():
    from contextlib import contextmanager

    from mempalace.backends.scoped_lexical import scope_local_search

    terms = ["quartz", "marigold", "lantern", "kestrel", "migration", "rollout", "deploy", "copper"]
    loaded = []

    @contextmanager
    def lexical_scope(where):
        def fetch(term, limit):
            i = terms.index(term)
            return [0] + list(range(1 + 2000 * i, 2000 * (i + 1)))

        def load(ids):
            loaded.extend(ids)
            return {i: " ".join(terms) if i == 0 else "quartz noise" for i in ids}

        yield SimpleNamespace(
            count=40000,
            fetch=fetch,
            lengths=lambda ids: {i: 8 if i == 0 else 2 for i in ids},
            load_texts=load,
            hydrate=lambda ids: {i: (str(i), " ".join(terms), {}) for i in ids},
        )

    col = SimpleNamespace(lexical_scope=lexical_scope, lexical_min_token_length=2)
    result = scope_local_search(col, " ".join(terms), 5)
    assert len(loaded) == 100 and result.hits[0].id == "0"


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact", "compact"])
@pytest.mark.parametrize("matches", [151, 1000])
def test_one_word_pool_keeps_the_short_newest_note(tmp_path, monkeypatch, backend, matches):
    from mempalace.backends import embedding_wrapper

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts, **kw: [[1.0, 0.0]] * len(texts)
    )
    col = get_collection(str(tmp_path), create=True)
    col.add(
        ids=[f"old{i}" for i in range(matches - 1)] + ["newest"],
        documents=[f"marigold record{i} " + "routine " * 30 for i in range(matches - 1)]
        + ["marigold"],
        embeddings=[[0.01, 1.0]] * (matches - 1) + [[0.0, 0.0]],
        metadatas=[{"wing": "public"}] * matches,
    )
    reference = col.lexical_search(query="marigold", n_results=1, where={"wing": "public"})
    assert reference.hits[0].id == "newest"
    result = search_memories(
        "marigold", str(tmp_path), wing="public", n_results=5, include_facts=False
    )
    assert result["results"][0]["drawer_id"] == "newest"
    assert result["results"][0]["matched_via"] == "bm25_backend"


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact", "compact"])
def test_prerank_equal_lengths_prefer_the_newest_rowid(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend)
    col = get_collection(str(tmp_path), create=True)
    col.add(
        ids=[f"d{i}" for i in range(200)],
        documents=["marigold"] * 200,
        embeddings=[[1.0, 0.0]] * 200,
        metadatas=[{"wing": "public"}] * 200,
    )
    result = col.union_lexical_search(query="marigold", n_results=1, where={"wing": "public"})
    assert [hit.id for hit in result.hits] == ["d199"]
