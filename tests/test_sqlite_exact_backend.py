import math
import sqlite3
import threading

import pytest

import mempalace.backends.sqlite_exact as sqlite_exact_module
from mempalace.backends import (
    BackendMismatchError,
    CollectionNotInitializedError,
    DimensionMismatchError,
    PalaceRef,
    QueryResult,
    UnsupportedCapabilityError,
    available_backends,
)
from mempalace.backends.sqlite_exact import SQLiteExactBackend


def _collection(tmp_path, name="mempalace_drawers", create=True):
    backend = SQLiteExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend, backend.get_collection(palace=palace, collection_name=name, create=create)


def test_sqlite_exact_missing_collection_error_names_collection(tmp_path):
    """CollectionNotInitializedError must identify the missing collection, not
    the palace path — consistent with line 287 and the other backends."""
    backend, _ = _collection(tmp_path, name="mempalace_drawers")
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    with pytest.raises(CollectionNotInitializedError) as exc:
        backend.get_collection(palace=palace, collection_name="does_not_exist", create=False)
    assert "does_not_exist" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)

    with pytest.raises(CollectionNotInitializedError) as exc2:
        backend.delete_collection(str(tmp_path), "also_missing")
    assert "also_missing" in str(exc2.value)
    assert str(tmp_path) not in str(exc2.value)


def test_registry_exposes_sqlite_exact():
    assert "sqlite_exact" in available_backends()


def test_sqlite_exact_add_query_filters_and_persistence(tmp_path):
    backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=[
            "alpha vector memory",
            "beta sqlite exact memory",
            "gamma filtered memory",
        ],
        metadatas=[
            {"wing": "alpha", "room": "notes", "chunk_index": 0, "tags": "core,vector"},
            {"wing": "alpha", "room": "notes", "chunk_index": 1, "tags": "sqlite,exact"},
            {"wing": "gamma", "room": "archive", "chunk_index": 2, "tags": "old"},
        ],
        embeddings=[[1.0, 0.0], [0.0, 1.0], [0.2, 0.8]],
    )

    ranked = col.query(query_embeddings=[[1.0, 0.0]], n_results=3)
    assert ranked.ids[0] == ["a", "c", "b"]
    assert ranked.distances[0][0] == pytest.approx(0.0)

    filtered = col.get(
        where={
            "$and": [
                {"wing": "alpha"},
                {"chunk_index": {"$gte": 1}},
                {"tags": {"$contains": "sqlite"}},
            ]
        },
        include=["documents", "metadatas", "embeddings"],
    )
    assert filtered.ids == ["b"]
    assert filtered.documents == ["beta sqlite exact memory"]
    assert filtered.embeddings == [[0.0, 1.0]]

    col.update(ids=["b"], metadatas=[{"room": "lab"}])
    assert col.get(ids=["b"]).metadatas[0]["room"] == "lab"

    backend.close_palace(str(tmp_path))
    reopened = backend.get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="mempalace_drawers",
        create=False,
    )
    assert reopened.count() == 3
    assert reopened.get(ids=["a"]).documents == ["alpha vector memory"]


def test_sqlite_exact_write_failure_rolls_back_whole_batch(tmp_path):
    _backend, col = _collection(tmp_path)

    with pytest.raises(Exception):
        col.add(
            ids=["dup", "dup"],
            documents=["first write", "duplicate write"],
            metadatas=[{}, {}],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
        )

    assert col.count() == 0


def test_sqlite_exact_enforces_collection_dimension(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(ids=["a"], documents=["two dims"], metadatas=[{}], embeddings=[[1.0, 0.0]])

    with pytest.raises(DimensionMismatchError):
        col.add(ids=["b"], documents=["three dims"], metadatas=[{}], embeddings=[[1.0, 0.0, 0.0]])
    with pytest.raises(DimensionMismatchError):
        col.upsert(
            ids=["b"], documents=["three dims"], metadatas=[{}], embeddings=[[1.0, 0.0, 0.0]]
        )
    with pytest.raises(DimensionMismatchError):
        col.update(ids=["a"], embeddings=[[1.0, 0.0, 0.0]])
    with pytest.raises(DimensionMismatchError):
        col.query(query_embeddings=[[1.0, 0.0, 0.0]], n_results=1)

    assert col.count() == 1
    assert col.get(ids=["a"]).documents == ["two dims"]


def test_sqlite_exact_get_preserves_requested_id_order_and_duplicates(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b"],
        documents=["doc a", "doc b"],
        metadatas=[{}, {}],
        embeddings=[[1, 0], [0, 1]],
    )

    result = col.get(ids=["b", "a", "b"], include=["documents"])

    assert result.ids == ["b", "a", "b"]
    assert result.documents == ["doc b", "doc a", "doc b"]


def test_sqlite_exact_upsert_delete_and_multi_collection_isolation(tmp_path):
    backend, drawers = _collection(tmp_path, "drawers")
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    closets = backend.get_collection(palace=palace, collection_name="closets", create=True)

    drawers.upsert(
        ids=["same"], documents=["drawer one"], metadatas=[{"kind": "drawer"}], embeddings=[[1, 0]]
    )
    closets.upsert(
        ids=["same"], documents=["closet one"], metadatas=[{"kind": "closet"}], embeddings=[[0, 1]]
    )
    drawers.upsert(
        ids=["same"],
        documents=["drawer replaced"],
        metadatas=[{"kind": "drawer", "version": 2}],
        embeddings=[[1, 0]],
    )

    assert drawers.count() == 1
    assert closets.count() == 1
    assert drawers.get(ids=["same"]).documents == ["drawer replaced"]
    assert closets.get(ids=["same"]).documents == ["closet one"]

    drawers.delete(where={"version": {"$in": [2, 3]}})
    assert drawers.count() == 0
    assert closets.count() == 1


def test_sqlite_exact_lexical_search_and_python_fallback(tmp_path, monkeypatch):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=[
            "ordinary project note",
            "rareterm rareterm sqlite exact note",
            "rareterm unrelated archive",
        ],
        metadatas=[
            {"wing": "w", "room": "a"},
            {"wing": "w", "room": "b"},
            {"wing": "old", "room": "b"},
        ],
        embeddings=[[1, 0], [0, 1], [0.5, 0.5]],
    )

    hits = col.lexical_search(query="rareterm sqlite", n_results=2, where={"wing": "w"}).hits
    assert [hit.id for hit in hits] == ["b"]

    monkeypatch.setattr(col, "_fts_available", lambda _cur: False)
    fallback_hits = col.lexical_search(query="rareterm sqlite", n_results=2).hits
    assert fallback_hits[0].id == "b"


def test_sqlite_exact_lexical_search_filters_after_full_fts_window(tmp_path):
    _backend, col = _collection(tmp_path)
    ids = [f"old-{i}" for i in range(12)] + ["target"]
    col.add(
        ids=ids,
        documents=["needle shared lexical note" for _ in ids],
        metadatas=[{"wing": "old"} for _ in range(12)] + [{"wing": "target"}],
        embeddings=[[1.0, 0.0] for _ in ids],
    )

    hits = col.lexical_search(query="needle", n_results=1, where={"wing": "target"}).hits

    assert [hit.id for hit in hits] == ["target"]


def test_sqlite_exact_logical_filters_evaluate_sibling_predicates(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b"],
        documents=["alpha document", "beta document"],
        metadatas=[
            {"wing": "w", "room": "wrong", "kind": "note"},
            {"wing": "w", "room": "right", "kind": "note"},
        ],
        embeddings=[[1, 0], [0, 1]],
    )

    result = col.get(where={"$and": [{"wing": "w"}], "room": "right"})

    assert result.ids == ["b"]


def test_sqlite_exact_close_palace_marks_existing_collections_closed(tmp_path):
    backend, col = _collection(tmp_path)
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    col.add(ids=["a"], documents=["doc"], metadatas=[{}], embeddings=[[1, 0]])

    backend.close_palace(palace)

    assert not col.health().ok
    with pytest.raises(Exception):
        col.count()


def test_palace_wrapper_embeds_for_sqlite_exact(tmp_path, monkeypatch):
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    from mempalace.palace import get_collection

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
    monkeypatch.setattr(
        embedding_wrapper,
        "_embed_texts",
        lambda texts: [[float(len(text)), 1.0] for text in texts],
    )

    col = get_collection(str(tmp_path), create=True)
    col.add(ids=["a"], documents=["abcd"], metadatas=[{"wing": "w"}])

    result = col.query(query_texts=["abcd"], n_results=1)
    assert result.ids == [["a"]]


def test_backend_mismatch_protection(tmp_path, monkeypatch):
    from mempalace.palace import get_collection

    (tmp_path / "chroma.sqlite3").write_bytes(b"")
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")

    with pytest.raises(BackendMismatchError):
        get_collection(str(tmp_path), create=True)


def test_mixed_backend_artifacts_are_rejected_even_when_chroma_selected(tmp_path, monkeypatch):
    from mempalace.palace import resolve_backend_name

    (tmp_path / "chroma.sqlite3").write_bytes(b"")
    (tmp_path / "sqlite_exact.sqlite3").write_bytes(b"")
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "chroma")

    with pytest.raises(BackendMismatchError):
        resolve_backend_name(str(tmp_path))


def test_sqlite_exact_exact_ranking_uses_cosine(tmp_path):
    _backend, col = _collection(tmp_path)
    halfway = [0.5, math.sqrt(0.75)]
    col.add(
        ids=["half", "orthogonal", "same"],
        documents=["half", "orthogonal", "same"],
        metadatas=[{}, {}, {}],
        embeddings=[halfway, [0.0, 1.0], [1.0, 0.0]],
    )

    result = col.query(query_embeddings=[[1.0, 0.0]], n_results=3)
    assert result.ids[0] == ["same", "half", "orthogonal"]
    assert result.distances[0] == pytest.approx([0.0, 0.5, 1.0])


def test_search_union_uses_sqlite_exact_lexical_search(tmp_path, monkeypatch):
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    from mempalace.palace import get_collection
    from mempalace.searcher import search_memories

    def fake_embed(texts):
        vectors = []
        for text in texts:
            if text == "rareterm":
                vectors.append([1.0, 0.0])
            elif "rareterm" in text:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.5, math.sqrt(0.75)])
        return vectors

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
    monkeypatch.setattr(embedding_wrapper, "_embed_texts", fake_embed)

    col = get_collection(str(tmp_path), create=True)
    col.add(
        ids=["d1", "d2", "d3", "rare"],
        documents=[
            "ordinary support note",
            "ordinary billing note",
            "ordinary project note",
            "rareterm rareterm rareterm policy note",
        ],
        metadatas=[
            {"wing": "w", "room": "r", "source_file": "/tmp/d1.md", "chunk_index": 0},
            {"wing": "w", "room": "r", "source_file": "/tmp/d2.md", "chunk_index": 0},
            {"wing": "w", "room": "r", "source_file": "/tmp/d3.md", "chunk_index": 0},
            {"wing": "w", "room": "r", "source_file": "/tmp/rare.md", "chunk_index": 0},
        ],
    )

    result = search_memories(
        "rareterm",
        str(tmp_path),
        n_results=1,
        candidate_strategy="union",
    )

    assert result["results"][0]["source_file"] == "rare.md"
    assert result["results"][0]["matched_via"] == "bm25_backend"


def test_search_union_reports_unsupported_lexical_capability(monkeypatch, tmp_path):
    import mempalace.searcher as searcher

    class NoLexicalCollection:
        def query(self, **_kwargs):
            return QueryResult(
                ids=[["a"]],
                documents=[["ordinary note"]],
                metadatas=[[{"source_file": "/tmp/a.md", "chunk_index": 0}]],
                distances=[[0.5]],
            )

        def lexical_search(self, **_kwargs):
            raise UnsupportedCapabilityError("no lexical support")

    monkeypatch.setattr(searcher, "get_collection", lambda *_args, **_kwargs: NoLexicalCollection())
    monkeypatch.setattr(
        searcher,
        "get_closets_collection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("no closets")),
    )

    result = searcher.search_memories(
        "anything",
        str(tmp_path),
        n_results=1,
        candidate_strategy="union",
    )

    assert result["unsupported_capability"] == "supports_lexical_search"


def test_search_vector_disabled_fallback_is_chroma_only(tmp_path, monkeypatch):
    from mempalace.searcher import search_memories

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")

    result = search_memories("anything", str(tmp_path), vector_disabled=True)

    assert result["unsupported_capability"] == "chroma_hnsw_fallback"
    assert result["backend"] == "sqlite_exact"


def test_concurrent_first_open_single_connection_no_leak(tmp_path, monkeypatch):
    """Two threads first-opening the same palace concurrently must share one
    handle and one sqlite connection.

    The barrier inside the patched ``sqlite3.connect`` releases immediately
    only when both threads pass the cache-miss check together: the broken
    interleaving, which also ran ``_init_schema`` concurrently on a fresh
    file and surfaced "database is locked". With creation serialized under
    ``_clients_lock`` the second thread waits on the lock instead, the
    winner's barrier times out, and exactly one connection is ever created.
    """
    created = []
    barrier = threading.Barrier(2)
    real_connect = sqlite3.connect

    def racing_connect(*args, **kwargs):
        try:
            barrier.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    monkeypatch.setattr(sqlite_exact_module.sqlite3, "connect", racing_connect)

    backend = SQLiteExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    results = [None, None]
    errors = []

    def open_collection(i):
        try:
            results[i] = backend.get_collection(
                palace=palace, collection_name="drawers", create=True
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=open_collection, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert len(created) == 1
    assert results[0]._handle is results[1]._handle

    backend.close()
    with pytest.raises(sqlite3.ProgrammingError):
        created[0].execute("SELECT 1")
