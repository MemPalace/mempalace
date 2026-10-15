"""Live-substrate conformance run for the pgvector backend (RFC 001).

Mirrors the fake-client arms of ``test_pgvector_backend.py`` against a real
PostgreSQL + pgvector server, plus live-only arms the in-memory fake cannot
exercise: the real ``<=>`` operator class, JSONB pushdown vs local-fallback
equivalence, multi-connection concurrent writers, and the advisory-lock
serialization of ``run_maintenance("reindex")``.

Gate: ``MEMPALACE_PGVECTOR_LIVE_DSN`` (a scratch database — every test creates
its own namespaced tables; never point this at a production palace).
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from _backend_conformance import assert_partition_isolation

from mempalace.backends import (
    BackendError,
    BackendMismatchError,
    CollectionNotInitializedError,
    DimensionMismatchError,
    PalaceRef,
)
from mempalace.backends.pgvector import (
    PgVectorBackend,
    _bm25_scores,
    _matches_where,
    _tokenize,
    _trgm_index_name,
)

LIVE_DSN = os.environ.get("MEMPALACE_PGVECTOR_LIVE_DSN")

pytestmark = pytest.mark.skipif(
    not LIVE_DSN, reason="set MEMPALACE_PGVECTOR_LIVE_DSN (scratch DB) to run"
)


@pytest.fixture
def live(request, tmp_path):
    """Backend + collection on the live server, namespaced per test."""
    namespace = "conf_" + request.node.name.replace("[", "_").replace("]", "")[:40]
    backend = PgVectorBackend()
    created = []
    created_lock = threading.Lock()

    def make(path, name="drawers", create=True, ns=namespace, dsn=LIVE_DSN, backend_=None):
        b = backend_ or backend
        ref = PalaceRef(id=str(path), local_path=str(path), namespace=ns)
        col = b.get_collection(
            palace=ref, collection_name=name, create=create, options={"dsn": dsn, "namespace": ns}
        )
        # The concurrent tests call make() from worker threads; plain list
        # append is not guaranteed safe on every Python build.
        with created_lock:
            created.append(col)
        return col

    yield backend, make, namespace
    for col in created:
        try:
            col._client.drop_table(col._table)
        except Exception:
            pass
    backend.close()


def _seed(col):
    col.add(
        ids=["a", "b", "c"],
        documents=[
            "alpha backend note",
            "rareterm pgvector backend note",
            "frontend design note",
        ],
        metadatas=[
            {"wing": "project", "room": "backend", "rank": 1},
            {"wing": "project", "room": "backend", "rank": 3},
            {"wing": "project", "room": "frontend", "rank": 2},
        ],
        embeddings=[[1, 0], [0.9, 0.1], [0, 1]],
    )


def test_live_add_query_filters_lexical_and_marker(live, tmp_path):
    backend, make, _ns = live
    col = make(tmp_path)
    assert not os.path.isfile(tmp_path / "pgvector_backend.json")
    _seed(col)

    assert PgVectorBackend.detect(str(tmp_path))
    assert os.path.isfile(tmp_path / "pgvector_backend.json")
    assert col.count() == 3

    result = col.query(
        query_embeddings=[[1, 0]],
        n_results=3,
        where={"wing": "project"},
        include=["documents", "metadatas", "distances", "embeddings"],
    )
    # ORDER BY distance ASC is part of the query contract — assert the
    # exact ranking, not just membership.
    assert result.ids[0] == ["a", "b", "c"]
    assert result.embeddings[0][0] == pytest.approx([1.0, 0.0])

    hits = col.lexical_search(query="rareterm backend", n_results=2, where={"wing": "project"}).hits
    assert [hit.id for hit in hits] == ["b", "a"]


def test_live_requires_explicit_embeddings(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    with pytest.raises(ValueError, match="explicit embeddings"):
        col.add(ids=["a"], documents=["no vector"], metadatas=[{}])


def test_live_dimension_mismatch(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    with pytest.raises(DimensionMismatchError):
        col.upsert(ids=["b"], documents=["two"], metadatas=[{}], embeddings=[[1, 0, 0]])


def test_live_duplicate_ids_in_batch_rejected(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    with pytest.raises(ValueError, match="unique"):
        col.add(
            ids=["a", "a"], documents=["x", "y"], metadatas=[{}, {}], embeddings=[[1, 0], [0, 1]]
        )


def test_live_complex_filters_pushdown_vs_local_fallback(live, tmp_path):
    """$or / $contains route to local fallback, equality/$gte push down to
    JSONB SQL — on the live server both paths must agree with the fake."""
    _backend, make, _ns = live
    col = make(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=["alpha", "beta", "gamma"],
        metadatas=[
            {"wing": "x", "rank": 1, "tags": "core,vector"},
            {"wing": "y", "rank": 3, "tags": "sqlite,exact"},
            {"wing": "z", "rank": 2, "tags": "old"},
        ],
        embeddings=[[1, 0], [0.9, 0.1], [0, 1]],
    )

    or_hits = col.get(where={"$or": [{"wing": "x"}, {"wing": "z"}]})
    assert set(or_hits.ids) == {"a", "c"}

    contains = col.get(where={"tags": {"$contains": "sqlite"}})
    assert contains.ids == ["b"]

    ranked = col.query(query_embeddings=[[1, 0]], n_results=3, where={"rank": {"$gte": 2}})
    assert ranked.ids[0] == ["b", "c"]

    eq_pushdown = col.get(where={"wing": "y"})
    assert eq_pushdown.ids == ["b"]


def test_live_marker_rejects_target_change(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    backend2 = PgVectorBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    try:
        with pytest.raises(BackendMismatchError):
            backend2.get_collection(
                palace=palace,
                collection_name="drawers",
                create=True,
                options={"dsn": "postgresql://other-host:5432/other"},
            )
    finally:
        backend2.close()


def test_live_marker_backend_mismatch(live, tmp_path):
    from mempalace.palace import resolve_backend_name

    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    assert resolve_backend_name(str(tmp_path)) == "pgvector"
    with pytest.raises(BackendMismatchError):
        resolve_backend_name(str(tmp_path), explicit="qdrant")


def test_live_rejects_pure_remote_palace(live):
    backend = PgVectorBackend()
    palace = PalaceRef(id="tenant-remote", local_path=None, namespace="tenant-remote")
    try:
        with pytest.raises(BackendError, match="local palace path"):
            backend.get_collection(
                palace=palace, collection_name="drawers", create=True, options={"dsn": LIVE_DSN}
            )
    finally:
        backend.close()


def test_live_missing_table_after_marker_is_not_initialized(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    col._client.drop_table(col._table)

    assert col.health().ok is False
    with pytest.raises(CollectionNotInitializedError):
        col.count()


def test_live_cross_palace_isolation_conformance(live, tmp_path):
    backend, make, _ns = live
    cols = [make(tmp_path / label) for label in ("alpha", "beta")]
    assert cols[0]._table != cols[1]._table
    assert_partition_isolation(backend, cols[0], cols[1], embedding=[1.0, 0.0])


def test_live_cross_namespace_isolation_conformance(live, tmp_path):
    """The cschnatz arm: same DSN, two namespaces, no leakage either way."""
    assert "supports_namespace_isolation" in PgVectorBackend.capabilities
    backend, make, ns = live
    col_a = make(tmp_path / "tenant-a", ns=f"{ns}_a")
    col_b = make(tmp_path / "tenant-b", ns=f"{ns}_b")
    assert col_a._table != col_b._table
    assert_partition_isolation(backend, col_a, col_b, embedding=[1.0, 0.0])


def test_live_cosine_operator_ranking_ground_truth(live, tmp_path):
    """The real ``<=>`` operator class must rank by cosine distance exactly
    as the fake's local math claims (our #1679 Q2-adjacent point: distance
    semantics should be a contract fact; here we verify the live operator)."""
    _backend, make, _ns = live
    col = make(tmp_path)
    col.add(
        ids=["same", "close", "orthogonal", "opposite"],
        documents=["d1", "d2", "d3", "d4"],
        metadatas=[{}, {}, {}, {}],
        embeddings=[[1, 0], [0.9, 0.1], [0, 1], [-1, 0]],
    )
    result = col.query(query_embeddings=[[1, 0]], n_results=4, include=["distances"])
    assert result.ids[0] == ["same", "close", "orthogonal", "opposite"]
    distances = result.distances[0]
    assert distances[0] == pytest.approx(0.0, abs=1e-6)
    assert distances[2] == pytest.approx(1.0, abs=1e-6)
    assert distances[3] == pytest.approx(2.0, abs=1e-6)


def test_live_concurrent_writers_distinct_connections(live, tmp_path):
    """8 backends (8 connections) upserting distinct rows into the same
    table concurrently — the multi-daemon-writer shape from production."""
    _backend, make, ns = live
    seed_col = make(tmp_path)
    seed_col.upsert(ids=["seed"], documents=["seed"], metadatas=[{}], embeddings=[[1, 0]])

    errors = []

    def writer(worker):
        backend = PgVectorBackend()
        # The marker file is already written by the seed step. upsert()
        # rewrites it on every call with a plain open("w"), so 8 backends
        # sharing one local_path would race on the same file — a test-design
        # artifact (and a known sharing-violation hazard on Windows), not
        # the contract under test here. Stub it for the concurrent phase.
        backend._write_marker = lambda *args, **kwargs: None
        try:
            col = make(tmp_path, backend_=backend)
            for i in range(25):
                col.upsert(
                    ids=[f"w{worker}-r{i}"],
                    documents=[f"row {i} from worker {worker}"],
                    metadatas=[{"worker": worker}],
                    embeddings=[[1.0, float(i) / 100]],
                )
        except Exception as exc:  # noqa: BLE001 - collected for the report
            errors.append(repr(exc))
        finally:
            backend.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(writer, range(8)))

    assert errors == [], f"concurrent writers raised: {errors[:3]}"
    assert seed_col.count() == 1 + 8 * 25


def test_live_reindex_advisory_lock_race(live, tmp_path):
    """Two connections racing run_maintenance('reindex') — the #1732
    advisory-lock behavior: exactly one 'ran', the loser learns
    'already_running' (or 'noop' after the winner finishes), nobody stacks
    a second ACCESS EXCLUSIVE build and nobody raises."""
    _backend, make, ns = live
    col = make(tmp_path)
    col.add(
        ids=[f"r{i}" for i in range(50)],
        documents=[f"doc {i}" for i in range(50)],
        metadatas=[{} for _ in range(50)],
        embeddings=[[1.0, float(i)] for i in range(50)],
    )
    assert col.maintenance_state()["vector_index"] is None

    barrier = threading.Barrier(2)
    statuses, errors = [], []

    def race():
        backend = PgVectorBackend()
        try:
            racer = make(tmp_path, backend_=backend)
            barrier.wait(timeout=10)
            result = racer.run_maintenance("reindex")
            statuses.append(result.status)
        except Exception as exc:  # noqa: BLE001 - collected for the report
            errors.append(repr(exc))
        finally:
            backend.close()

    threads = [threading.Thread(target=race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], f"reindex race raised: {errors}"
    # The index does not exist beforehand, so exactly one racer must win
    # the advisory lock and build it.
    assert statuses.count("ran") == 1
    assert all(s in {"ran", "already_running", "noop"} for s in statuses), statuses
    state = col.maintenance_state()
    assert state["vector_index"] == "hnsw"
    assert state["index_build_complete"] is True


def test_live_analyze_maintenance(live, tmp_path):
    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])
    result = col.run_maintenance("analyze")
    assert result.status == "ran"


def test_live_trgm_index_created_with_table(live, tmp_path):
    """A fresh collection carries the trigram index the substring filter needs.

    The index opclass must match the predicate, or Postgres silently plans a
    sequential scan and the pushdown buys nothing but correctness.
    """
    _backend, make, _ns = live
    col = make(tmp_path)
    col.upsert(ids=["a"], documents=["one"], metadatas=[{}], embeddings=[[1, 0]])

    rows = col._client._execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
        "AND tablename = %s AND indexname = %s",
        [col._table, _trgm_index_name(col._table)],
        fetch=True,
    )
    assert rows, "create_table must build the trigram index"
    indexdef = rows[0][0].lower()
    assert "gin" in indexdef and "gin_trgm_ops" in indexdef and "document" in indexdef


def test_live_large_document_still_writes(live, tmp_path):
    """A drawer too big for a tsvector must still be storable.

    Postgres caps a tsvector at 1 MB and an expression index enforces that cap
    at write time, so indexing ``to_tsvector(document)`` would turn a large
    transcript from a slow write into a failed one. A trigram index has no such
    cap; this is the regression test for that choice.
    """
    _backend, make, _ns = live
    col = make(tmp_path)
    big = " ".join(f"w{i}" for i in range(300_000))
    col.add(ids=["big"], documents=[big], metadatas=[{"wing": "p"}], embeddings=[[1, 0]])

    assert col.count() == 1
    assert [hit.id for hit in col.lexical_search(query="w299999", n_results=5).hits] == ["big"]


def test_live_token_counts_are_written_and_exact(live, tmp_path):
    """The stored length must equal what ``_bm25_scores`` computes, exactly."""
    _backend, make, _ns = live
    col = make(tmp_path)
    documents = {
        "en": "memory palace note about alpha and beta",
        "ar": "قصر الذاكرة يحفظ كلماتك",
        "cjk": "记忆宫殿笔记 alpha",
        "dev": "contact atk@atkabli.com about /etc/nginx/nginx.conf and npm.install 3.14",
    }
    ids = list(documents)
    col.add(
        ids=ids,
        documents=[documents[i] for i in ids],
        metadatas=[{"wing": "p"} for _ in ids],
        embeddings=[[1, 0] for _ in ids],
    )

    rows = col._client._execute(f'SELECT id, token_count FROM "{col._table}"', fetch=True)
    assert {row[0]: row[1] for row in rows} == {
        doc_id: len(_tokenize(text)) for doc_id, text in documents.items()
    }


def _develop_lexical_search(col, query, n_results, where=None):
    """The pre-pushdown implementation: scroll everything, score, sort, cut."""
    rows = col._scroll(where=where, with_embedding=False)
    rows = [row for row in rows if _matches_where(row["metadata"], where)]
    scores = _bm25_scores(query, [row["document"] for row in rows])
    hits = [(row["id"], score) for row, score in zip(rows, scores) if score > 0]
    hits.sort(key=lambda hit: (-hit[1], hit[0]))
    return hits[:n_results]


def test_live_pushdown_equals_full_scroll_when_matches_outnumber_the_window(live, tmp_path):
    """The reviewer's scenario, against a real server.

    60 drawers, 50 of them matching, a window of 9. Anything that ranks
    server-side and cuts to 9 in SQL returns a different 9 than BM25 does; the
    filter-only pushdown returns the same ids with the same scores.
    """
    _backend, make, _ns = live
    col = make(tmp_path)
    documents = {"short00": "we shipped the alpha and beta change today"}
    for i in range(30):
        documents[f"alpha{i:02d}"] = (
            "alpha " + " ".join(f"filler{j}" for j in range(60)) + f" release note {i}"
        )
    for i in range(15):
        documents[f"beta{i:02d}"] = (
            "beta " + " ".join(f"other{j}" for j in range(60)) + f" changelog {i}"
        )
    for i in range(4):
        documents[f"short{i + 1:02d}"] = f"alpha beta quick note number {i}"
    for i in range(10):
        documents[f"noise{i:02d}"] = f"unrelated gamma delta epsilon text {i}"
    ids = list(documents)
    col.add(
        ids=ids,
        documents=[documents[i] for i in ids],
        metadatas=[{"wing": "p"} for _ in ids],
        embeddings=[[1, 0] for _ in ids],
    )

    everything = _develop_lexical_search(col, "alpha beta", 10_000)
    assert len(everything) == 50, "the corpus must have more matches than the window"
    # Guard against a vacuous pass: if the pushdown had silently fallen back to
    # the local scroll, every assertion below would hold for the wrong reason.
    assert col.maintenance_state()["lexical_pushdown"] is True
    scrolled = []
    original_scroll = col._client.scroll_rows
    col._client.scroll_rows = lambda *a, **k: (
        scrolled.append(1),
        original_scroll(*a, **k),
    )[1]

    for n_results in (1, 9, 30, 200):
        want = _develop_lexical_search(col, "alpha beta", n_results)
        scrolled.clear()
        got = [
            (hit.id, hit.score)
            for hit in col.lexical_search(query="alpha beta", n_results=n_results).hits
        ]
        assert scrolled == [], "the pushdown, not the scroll, must be under test"
        assert [i for i, _ in got] == [i for i, _ in want], f"n_results={n_results}"
        for (_, a), (_, b) in zip(got, want):
            assert a == pytest.approx(b)

    # And the specific loss reported: the top BM25 hit survives a window of 9.
    scrolled.clear()
    window = [hit.id for hit in col.lexical_search(query="alpha beta", n_results=9).hits]
    col._client.scroll_rows = original_scroll
    assert scrolled == []
    assert everything[0][0] in window


def test_live_pushdown_finds_developer_shaped_tokens(live, tmp_path):
    r"""Postgres full-text parsing would swallow these; a substring filter does not.

    ``to_tsvector('simple', ...)`` emits ``atk@atkabli.com``,
    ``/etc/nginx/nginx.conf`` and ``npm.install`` as single lexemes, so a
    tsquery built from ``\w{2,}`` tokens misses every one of them while BM25
    scores them above zero. Each of these is a lost hit under a tsquery
    predicate.
    """
    _backend, make, _ns = live
    col = make(tmp_path)
    documents = {
        "email": "ping atk@atkabli.com for access",
        "url": "see https://example.com/deep/path today",
        "path": "open /etc/nginx/nginx.conf now",
        "pkg": "run npm.install script",
        "host": "server db01.internal.corp is down",
        "num": "cost 3.14 usd",
        "under": "def foo_bar(): pass",
    }
    ids = list(documents)
    col.add(
        ids=ids,
        documents=[documents[i] for i in ids],
        metadatas=[{"wing": "p"} for _ in ids],
        embeddings=[[1, 0] for _ in ids],
    )

    for query, expected in [
        ("atkabli", "email"),
        ("example", "url"),
        ("nginx", "path"),
        ("npm", "pkg"),
        ("internal", "host"),
        ("14", "num"),
        ("foo_bar", "under"),
    ]:
        hits = [hit.id for hit in col.lexical_search(query=query, n_results=5).hits]
        assert expected in hits, f"{query!r} lost {expected!r}"
        assert hits == [i for i, _ in _develop_lexical_search(col, query, 5)]


def test_live_lexical_search_is_multilingual(live, tmp_path):
    """A mixed-script palace stays searchable, and matches the local path exactly."""
    _backend, make, _ns = live
    col = make(tmp_path)
    col.add(
        ids=["ar", "cjk", "en"],
        documents=["قصر الذاكرة يحفظ كلماتك", "记忆 宫殿", "memory palace note"],
        metadatas=[{"wing": "p"}, {"wing": "p"}, {"wing": "p"}],
        embeddings=[[1, 0], [0, 1], [0.5, 0.5]],
    )

    assert [h.id for h in col.lexical_search(query="الذاكرة", n_results=5).hits] == ["ar"]
    assert [h.id for h in col.lexical_search(query="记忆", n_results=5).hits] == ["cjk"]
    # OR semantics: one query, hits from two languages.
    mixed = {h.id for h in col.lexical_search(query="الذاكرة 记忆", n_results=5).hits}
    assert mixed == {"ar", "cjk"}
    # Metacharacters are content, not syntax. These reach SQL as bound LIKE
    # patterns, so none of them can change the shape of the query.
    for hostile in ("memory & !palace", "a:*", "a <-> b", "'", "\\", "50%", "a_c", "%"):
        assert [h.id for h in col.lexical_search(query=hostile, n_results=5).hits] == [
            i for i, _ in _develop_lexical_search(col, hostile, 5)
        ]


def test_live_wildcards_in_a_query_stay_literal(live, tmp_path):
    """``%`` and ``_`` are LIKE wildcards; a query carrying them must not widen."""
    _backend, make, _ns = live
    col = make(tmp_path)
    col.add(
        ids=["lit", "other"],
        documents=["discount a_c applied", "discount abc applied"],
        metadatas=[{"wing": "p"}, {"wing": "p"}],
        embeddings=[[1, 0], [0, 1]],
    )

    # 'a_c' must match the literal underscore only, not 'abc'.
    assert [h.id for h in col.lexical_search(query="a_c", n_results=5).hits] == ["lit"]
