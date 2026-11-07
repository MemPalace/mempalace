"""Tests for the Turso (pyturso) storage backend (issue #2927).

The whole module skips when the optional ``pyturso`` package is not
installed, mirroring how ``test_milvus_backend.py`` treats its optional
driver. The shared isolation/filter conformance assertions from
``_backend_conformance`` run here instead of in
``test_backend_conformance.py`` because that file's parametrized list must
stay importable without optional dependencies.
"""

import pytest

pytest.importorskip("turso")

from _backend_conformance import (
    assert_partition_isolation,
    assert_update_skips_missing_ids,
    assert_where_filters_conform,
    assert_write_batch_validated,
)

from mempalace.backends import (
    PalaceRef,
    available_backends,
    detect_backends_for_path,
)
from mempalace.backends.base import (
    BackendClosedError,
    BackendError,
    CollectionNotInitializedError,
    EmbedderIdentity,
    PalaceNotFoundError,
    UnsupportedMaintenanceKindError,
)
from mempalace.backends.turso import (
    TursoBackend,
    _fts_query_string,
)

COLLECTION = "mempalace_drawers"


def _open(backend, path, create=True):
    return backend.get_collection(
        palace=PalaceRef(id=str(path), local_path=str(path)),
        collection_name=COLLECTION,
        create=create,
    )


def _add(col, ids, docs=None, metas=None, embs=None):
    if docs is None:
        docs = [f"document text for {i}" for i in ids]
    if embs is None:
        embs = [[float(len(i)), 1.0, 0.0] for i in ids]
    if metas is None:
        metas = [{"wing": "w1", "room": f"r{i}"} for i in range(len(ids))]
    col.add(documents=docs, ids=list(ids), metadatas=metas, embeddings=embs)


@pytest.fixture
def backend():
    be = TursoBackend()
    try:
        yield be
    finally:
        be.close()


@pytest.fixture
def col(backend, tmp_path):
    collection = _open(backend, tmp_path, create=True)
    _add(
        collection,
        ["a", "b", "c"],
        docs=[
            "the quick brown fox jumps over the lazy dog",
            "database performance tuning guide",
            "neural networks explained simply",
        ],
        metas=[
            {"wing": "w1", "room": "r1", "filed_at": "2026-01-01T00:00:00Z"},
            {"wing": "w1", "room": "r2", "filed_at": "2026-01-02T00:00:00Z"},
            {"wing": "w2", "room": "r1", "filed_at": "2026-01-03T00:00:00+01:00"},
        ],
        embs=[[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 1.0, 0.0]],
    )
    return collection


# ---------------------------------------------------------------------------
# Registry / detection / lifecycle
# ---------------------------------------------------------------------------


def test_registry_exposes_turso():
    assert "turso" in available_backends()


def test_backend_metadata():
    assert TursoBackend.name == "turso"
    assert TursoBackend.distance_metric == "cosine"
    assert "supports_lexical_search" in TursoBackend.capabilities
    assert "supports_namespace_isolation" not in TursoBackend.capabilities


def test_default_maintenance_kinds_omit_compact():
    """Default features include multiprocess_wal (concurrent hooks + MCP),
    and VACUUM is incompatible with it — so compact must not be advertised."""
    assert "compact" not in TursoBackend().maintenance_kinds
    assert {"analyze", "fts_optimize"} <= TursoBackend().maintenance_kinds


def test_compact_advertised_with_vacuum_features(monkeypatch):
    monkeypatch.setenv("MEMPALACE_TURSO_EXPERIMENTAL_FEATURES", "index_method,vacuum")
    assert "compact" in TursoBackend().maintenance_kinds


def test_namespace_refused(backend, tmp_path):
    from mempalace.backends.base import UnsupportedCapabilityError

    with pytest.raises(UnsupportedCapabilityError):
        backend.get_collection(
            palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path), namespace="tenant"),
            collection_name=COLLECTION,
            create=True,
        )


def test_missing_palace_raises(backend, tmp_path):
    with pytest.raises(PalaceNotFoundError):
        _open(backend, tmp_path / "missing", create=False)


def test_missing_collection_raises(backend, tmp_path):
    _open(backend, tmp_path, create=True)  # palace exists, no second collection
    from mempalace.backends import PalaceRef

    with pytest.raises(CollectionNotInitializedError):
        backend.get_collection(
            palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
            collection_name="other_collection",
            create=False,
        )


def test_detect(backend, tmp_path):
    assert not TursoBackend.detect(str(tmp_path))
    _open(backend, tmp_path, create=True)
    assert TursoBackend.detect(str(tmp_path))
    assert detect_backends_for_path(str(tmp_path)) == ["turso"]


def test_health(backend, tmp_path):
    assert backend.health().ok
    _open(backend, tmp_path, create=True)
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    assert backend.health(ref).ok


def test_closed_backend_and_collection(backend, tmp_path):
    collection = _open(backend, tmp_path, create=True)
    collection.close()
    with pytest.raises(BackendClosedError):
        collection.count()
    backend.close()
    with pytest.raises(BackendClosedError):
        _open(backend, tmp_path / "other", create=True)


def test_persistence_across_reopen(tmp_path):
    be1 = TursoBackend()
    col1 = _open(be1, tmp_path, create=True)
    _add(col1, ["x"])
    be1.close()
    be2 = TursoBackend()
    col2 = _open(be2, tmp_path, create=False)
    assert col2.count() == 1
    assert col2.get(ids=["x"]).ids == ["x"]
    be2.close()


# ---------------------------------------------------------------------------
# Writes and reads
# ---------------------------------------------------------------------------


def test_add_get_count_delete(col):
    assert col.count() == 3
    got = col.get(ids=["a"], include=["documents", "metadatas"])
    assert got.ids == ["a"]
    assert "quick brown fox" in got.documents[0]
    assert got.metadatas[0]["wing"] == "w1"
    col.delete(ids=["a"])
    assert col.count() == 2
    assert col.get(ids=["a"]).ids == []


def test_upsert_overwrites_without_growing(col):
    col.upsert(
        documents=["brand new text"],
        ids=["a"],
        metadatas=[{"wing": "w9", "filed_at": "2026-02-01T00:00:00Z"}],
        embeddings=[[0.5, 0.5, 0.0]],
    )
    assert col.count() == 3
    got = col.get(ids=["a"])
    assert got.metadatas[0]["wing"] == "w9"
    assert got.documents[0] == "brand new text"


def test_update_metadata_only(col):
    col.update(ids=["a"], metadatas=[{"room": "r7"}])
    meta = col.get(ids=["a"]).metadatas[0]
    assert meta["room"] == "r7"
    assert meta["wing"] == "w1"  # merge, not replace


def test_update_document_and_embedding(col):
    col.update(
        ids=["a"],
        documents=["replaced document text"],
        embeddings=[[0.0, 0.0, 1.0]],
    )
    got = col.get(ids=["a"], include=["documents", "embeddings"])
    assert got.documents[0] == "replaced document text"
    assert got.embeddings[0] == pytest.approx([0.0, 0.0, 1.0], abs=1e-6)


def test_get_where_pushdown(col):
    got = col.get(where={"wing": "w1"})
    assert sorted(got.ids) == ["a", "b"]
    got = col.get(where={"wing": {"$in": ["w2"]}})
    assert got.ids == ["c"]


def test_get_where_document(col):
    got = col.get(where_document={"$contains": "database"})
    assert got.ids == ["b"]


def test_get_limit_offset(col):
    got = col.get(limit=2)
    assert got.ids == ["a", "b"]
    got = col.get(limit=2, offset=2)
    assert got.ids == ["c"]


def test_get_all_metadata_and_rows(col):
    metas = col.get_all_metadata()
    assert len(metas) == 3
    rows = col.get_all_rows(include=["documents"])
    assert sorted(rows.ids) == ["a", "b", "c"]
    assert len(rows.documents) == 3


def test_iter_metadata(col):
    metas = list(col.iter_metadata(["wing"]))
    assert sorted(m["wing"] for m in metas) == ["w1", "w1", "w2"]
    sf = list(col.iter_metadata(("wing",), require_key="wing"))
    assert len(sf) == 3


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------


def test_query_ranking_and_distance(col):
    result = col.query(query_embeddings=[[1.0, 0.05, 0.0]], n_results=3)
    assert result.ids[0][0] == "a"
    dists = result.distances[0]
    assert dists == sorted(dists)
    assert all(d >= 0 for d in dists)
    assert result.documents[0][0].startswith("the quick brown fox")


def test_query_with_pushed_where(col):
    result = col.query(query_embeddings=[[1.0, 0.05, 0.0]], n_results=5, where={"wing": "w1"})
    assert sorted(result.ids[0]) == ["a", "b"]


def test_query_with_python_where_fallback(col):
    result = col.query(
        query_embeddings=[[0.5, 0.5, 0.0]],
        n_results=2,
        where={"$or": [{"wing": "w1"}, {"wing": "w2"}]},
    )
    assert len(result.ids[0]) == 2


def test_query_with_where_document(col):
    result = col.query(
        query_embeddings=[[1.0, 0.0, 0.0]],
        n_results=1,
        where_document={"$contains": "database"},
    )
    assert result.ids[0] == ["b"]


def test_query_include_embeddings(col):
    result = col.query(query_embeddings=[[1.0, 0.0, 0.0]], n_results=1, include=["embeddings"])
    assert result.embeddings[0][0] == pytest.approx([1.0, 0.0, 0.0], abs=1e-6)
    assert result.documents == [[]]  # not requested


def test_query_dimension_mismatch(col):
    from mempalace.backends.base import DimensionMismatchError

    with pytest.raises(DimensionMismatchError):
        col.query(query_embeddings=[[1.0, 0.0]], n_results=1)
    with pytest.raises(DimensionMismatchError):
        _add(col, ["bad"], embs=[[1.0, 2.0, 3.0, 4.0]])


def test_query_requires_embeddings(col):
    with pytest.raises(ValueError):
        col.query(query_texts=["nope"], n_results=1)


def test_multiple_query_vectors(col):
    result = col.query(query_embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], n_results=1)
    assert result.ids[0][0] == "a"
    assert result.ids[1][0] == "c"


# ---------------------------------------------------------------------------
# Lexical search
# ---------------------------------------------------------------------------


def test_lexical_search_ranks_relevant_first(col):
    # A short document dominated by the term must outrank a longer one
    # mentioning it once (Tantivy BM25 saturates term frequency, so the
    # dominant-match document is the reliable winner).
    col.upsert(
        documents=["database database database database database database"],
        ids=["d"],
        metadatas=[{"wing": "w2"}],
        embeddings=[[0.1, 0.2, 0.3]],
    )
    hits = col.lexical_search(query="database", n_results=5).hits
    assert hits, "lexical search returned no hits"
    assert hits[0].id == "d"
    assert all(h.score > 0 for h in hits)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_lexical_search_where_filter(col):
    col.upsert(
        documents=["database in another wing"],
        ids=["d"],
        metadatas=[{"wing": "w2"}],
        embeddings=[[0.1, 0.2, 0.3]],
    )
    hits = col.lexical_search(query="database", n_results=5, where={"wing": "w1"}).hits
    assert [h.id for h in hits] == ["b"]


def test_fts_query_string_sanitizes_tokens():
    # tokenize() splits on punctuation; each token is quoted and kept whole —
    # accented (non-ASCII) word characters are preserved, never stripped.
    assert _fts_query_string('don\'t "quote" me-bad^2') == '"don" OR "quote" OR "me" OR "bad"'
    assert _fts_query_string("\u0142ukasz caf\u00e9") == '"\u0142ukasz" OR "caf\u00e9"'
    assert _fts_query_string("a") == ""


# ---------------------------------------------------------------------------
# Recency / facets / maintenance / identity
# ---------------------------------------------------------------------------


def test_get_recent_orders_by_instant(col):
    # filed_at values mix UTC-suffixed and offset forms; ordering must be by
    # the instant, not the text.
    recent = col.get_recent(limit=3)
    assert recent.ids == ["c", "b", "a"]  # c = 2026-01-03T00:00+01:00 == 2026-01-02T23:00Z, newest
    recent = col.get_recent(limit=2, where={"wing": "w1"})
    assert recent.ids == ["b", "a"]


def test_facet_counts(col):
    assert col.facet_counts("wing") == {"w1": 2, "w2": 1}
    assert col.facet_counts("wing", where={"room": "r1"}) == {"w1": 1, "w2": 1}


def test_maintenance_analyze_and_fts_optimize(col):
    result = col.run_maintenance("analyze")
    assert result.status == "ran"
    result = col.run_maintenance("fts_optimize")
    assert result.status == "ran"
    with pytest.raises(UnsupportedMaintenanceKindError):
        col.run_maintenance("compact")  # not advertised under default features
    with pytest.raises(UnsupportedMaintenanceKindError):
        col.run_maintenance("reindex")  # not a turso maintenance kind


def test_maintenance_compact_runs_with_vacuum_features(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_TURSO_EXPERIMENTAL_FEATURES", "index_method,vacuum")
    be = TursoBackend()
    try:
        collection = _open(be, tmp_path, create=True)
        _add(collection, ["x", "y"])
        collection.delete(ids=["x", "y"])
        result = collection.run_maintenance("compact")
        assert result.status == "ran"
        assert "pages_before" in result.stats
    finally:
        be.close()


def test_maintenance_state(col):
    state = col.maintenance_state()
    assert state["row_count"] == 3
    assert state["fts_index"] is True
    assert "consistency_token" in state


def test_embedder_identity(backend, tmp_path):
    collection = _open(backend, tmp_path, create=True)
    assert collection.get_stored_embedder_identity() is None
    collection.set_embedder_identity(EmbedderIdentity(model_name="mymodel", dimension=3))
    _add(collection, ["a"], embs=[[1.0, 0.0, 0.0]])
    identity = collection.get_stored_embedder_identity()
    assert identity.model_name == "mymodel"
    assert identity.dimension == 3


def test_delete_collection(backend, tmp_path):
    collection = _open(backend, tmp_path, create=True)
    _add(collection, ["a"])
    backend.delete_collection(str(tmp_path), COLLECTION)
    with pytest.raises(CollectionNotInitializedError):
        collection.count()


# ---------------------------------------------------------------------------
# RFC 001 isolation conformance (shared assertions)
# ---------------------------------------------------------------------------


def test_cross_palace_isolation(tmp_path):
    backend = TursoBackend()
    try:
        cols = []
        for label in ("alpha", "beta"):
            cols.append(_open(backend, tmp_path / label, create=True))
        assert_partition_isolation(backend, cols[0], cols[1])
    finally:
        backend.close()


def test_update_skips_missing_ids(tmp_path):
    backend = TursoBackend()
    try:
        assert_update_skips_missing_ids(backend, _open(backend, tmp_path, create=True))
    finally:
        backend.close()


def test_where_filters_conform(tmp_path):
    backend = TursoBackend()
    try:
        assert_where_filters_conform(backend, _open(backend, tmp_path, create=True))
    finally:
        backend.close()


def test_write_batch_validated(tmp_path):
    backend = TursoBackend()
    try:
        assert_write_batch_validated(backend, _open(backend, tmp_path, create=True))
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Read-only contract
# ---------------------------------------------------------------------------


def _open_ro(backend, path):
    return backend.get_collection(
        palace=PalaceRef(id=str(path), local_path=str(path)),
        collection_name=COLLECTION,
        create=False,
        options={"read_only": True},
    )


def test_read_only_create_refused(backend, tmp_path):
    with pytest.raises(ValueError):
        backend.get_collection(
            palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
            collection_name=COLLECTION,
            create=True,
            options={"read_only": True},
        )
    assert not (tmp_path / "turso.db").exists()


def test_read_only_open_does_not_create_palace(backend, tmp_path):
    with pytest.raises(PalaceNotFoundError):
        _open_ro(backend, tmp_path / "missing")
    assert not (tmp_path / "missing").exists()
    # A directory without the db artifact is not a palace either.
    (tmp_path / "empty").mkdir()
    with pytest.raises(PalaceNotFoundError):
        _open_ro(backend, tmp_path / "empty")


def test_read_only_rejects_every_mutation(backend, tmp_path):
    writable = _open(backend, tmp_path, create=True)
    _add(writable, ["a"])
    ro = _open_ro(backend, tmp_path)
    with pytest.raises(BackendError):
        ro.add(documents=["x"], ids=["z"], metadatas=[{}], embeddings=[[1.0, 0.0, 0.0]])
    with pytest.raises(BackendError):
        ro.upsert(documents=["x"], ids=["a"], metadatas=[{}], embeddings=[[1.0, 0.0, 0.0]])
    with pytest.raises(BackendError):
        ro.update(ids=["a"], metadatas=[{"wing": "nope"}])
    with pytest.raises(BackendError):
        ro.delete(ids=["a"])
    with pytest.raises(BackendError):
        ro.set_embedder_identity(EmbedderIdentity(model_name="m", dimension=3))
    with pytest.raises(BackendError):
        ro.run_maintenance("analyze")
    with pytest.raises(BackendError):
        ro.run_maintenance("fts_optimize")
    # Nothing changed under the guard.
    assert writable.count() == 1
    assert writable.get(ids=["a"]).metadatas[0]["wing"] == "w1"


def test_read_only_sees_committed_writes(backend, tmp_path):
    writable = _open(backend, tmp_path, create=True)
    _add(writable, ["a"])
    ro = _open_ro(backend, tmp_path)
    assert ro.count() == 1
    _add(writable, ["b"])
    # Each statement opens a fresh MVCC read: the read-only handle observes
    # the committed change without being reopened.
    assert ro.count() == 2
    assert ro.get(ids=["b"]).ids == ["b"]


def test_close_palace_invalidates_both_handles(backend, tmp_path):
    writable = _open(backend, tmp_path, create=True)
    ro = _open_ro(backend, tmp_path)
    assert writable.health().ok
    assert ro.health().ok
    backend.close_palace(PalaceRef(id=str(tmp_path), local_path=str(tmp_path)))
    assert not writable.health().ok
    assert not ro.health().ok
    with pytest.raises(BackendClosedError):
        writable.count()
    # Reopen gets fresh handles of both kinds.
    writable2 = _open(backend, tmp_path, create=False)
    ro2 = _open_ro(backend, tmp_path)
    assert writable2.count() == 0
    assert ro2.count() == 0


# ---------------------------------------------------------------------------
# Scoped top-k parity
# ---------------------------------------------------------------------------


def test_scoped_top_k_does_not_return_out_of_scope_matches(backend, tmp_path):
    """The globally nearest records sit outside the requested room; a scoped
    query must return only in-room matches ranked by distance, never a
    globally ranked set truncated by the filter."""
    collection = _open(backend, tmp_path, create=True)
    # r-near is closest to the query, r-far matches the scope.
    rows = [
        ("near-other-room", "r-near", [1.0, 0.0]),
        ("mid-other-room", "r-near", [0.8, 0.2]),
        ("scoped-far", "r-far", [0.2, 0.8]),
    ]
    collection.add(
        documents=[f"drawer {i}" for i, _, _ in rows],
        ids=[r[0] for r in rows],
        metadatas=[{"wing": "w", "room": r[1]} for r in rows],
        embeddings=[r[2] for r in rows],
    )
    result = collection.query(query_embeddings=[[1.0, 0.0]], n_results=3, where={"room": "r-far"})
    assert result.ids[0] == ["scoped-far"]
    result = collection.query(
        query_embeddings=[[1.0, 0.0]], n_results=3, where={"$or": [{"room": "r-far"}]}
    )
    assert result.ids[0] == ["scoped-far"]


# ---------------------------------------------------------------------------
# get_recent parity
# ---------------------------------------------------------------------------


def test_get_recent_parity_forms_and_ties(backend, tmp_path):
    collection = _open(backend, tmp_path, create=True)
    rows = [
        # id, filed_at
        ("ok-newest", "2026-03-01T12:00:00Z"),
        ("ok-older-utc", "2026-02-01T12:00:00Z"),
        ("ok-older-naive", "2026-02-02T00:00:00"),  # +00 naive == 02-01T? later text
        ("unparseable", "zzz-not-a-date"),
        ("missing", None),
    ]
    collection.add(
        documents=[f"d {i}" for i, _ in enumerate(rows)],
        ids=[r[0] for r in rows],
        metadatas=[{} if r[1] is None else {"filed_at": r[1]} for r in rows],
        embeddings=[[1.0, 0.0, 0.0] for _ in rows],
    )
    recent = collection.get_recent(limit=5, include=["metadatas"])
    ids = recent.ids
    # Valid instants first (newest first: 03-01 beats both 02-* forms),
    # unparseable next, missing last.
    assert ids.index("ok-newest") < ids.index("ok-older-utc") < ids.index("unparseable")
    assert ids.index("unparseable") < ids.index("missing")
    assert ids[-1] == "missing"


def test_get_recent_custom_order_field_and_projection(backend, tmp_path):
    collection = _open(backend, tmp_path, create=True)
    collection.add(
        documents=["d1", "d2"],
        ids=["a", "b"],
        metadatas=[{"touched_at": "2026-01-01T00:00:00Z"}, {"touched_at": "2026-05-01T00:00:00Z"}],
        embeddings=[[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
    )
    recent = collection.get_recent(limit=1, order_field="touched_at")
    assert recent.ids == ["b"]
    # Projection-only include does not return documents that were not asked for.
    recent = collection.get_recent(limit=2, order_field="touched_at", include=["metadatas"])
    assert recent.documents == []


# ---------------------------------------------------------------------------
# Stored-text edge cases (verbatim storage policy)
# ---------------------------------------------------------------------------


def test_stored_text_edge_cases_round_trip(backend, tmp_path):
    collection = _open(backend, tmp_path, create=True)
    nul_doc = "contains\x00nul and \\ backslash"
    prefix_doc = "\x01mpesc: looks escaped already"
    weird_meta_value = "\x01mpesc: prefixed metadata value"
    collection.add(
        documents=[nul_doc, prefix_doc],
        ids=["nul", "prefix"],
        metadatas=[{"k": weird_meta_value}, {"k": "plain"}],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )
    got = collection.get(ids=["nul", "prefix"], include=["documents", "metadatas"])
    assert got.documents[0] == nul_doc
    assert got.documents[1] == prefix_doc
    assert got.metadatas[0]["k"] == weird_meta_value
    # The escape flag never leaks to callers.
    assert all("_mempalace_escaped" not in m for m in got.metadatas)
    # Through vector search and lexical search too.
    hit = collection.query(query_embeddings=[[1.0, 0.0, 0.0]], n_results=1)
    assert hit.documents[0][0] == nul_doc
    lex = collection.lexical_search(query="backslash", n_results=1).hits
    assert lex and lex[0].document == nul_doc
    # And the delete path resolves the same row.
    collection.delete(ids=["nul", "prefix"])
    assert collection.count() == 0


# ---------------------------------------------------------------------------
# Reviewer-feedback regressions
# ---------------------------------------------------------------------------


def test_get_all_rows_every_include_shape(backend, tmp_path):
    """get_all_rows must resolve columns from the projection, not fixed
    indexes: the metadata-only shape selects two columns and used to read
    row[2]."""
    collection = _open(backend, tmp_path, create=True)
    _add(collection, ["a", "b"])
    got = collection.get_all_rows()  # defaults to metadatas only
    assert sorted(got.ids) == ["a", "b"]
    assert got.documents == []
    assert len(got.metadatas) == 2
    got = collection.get_all_rows(include=["metadatas"])
    assert sorted(got.ids) == ["a", "b"]
    assert got.documents == []
    assert len(got.metadatas) == 2
    got = collection.get_all_rows(include=["documents"])
    assert sorted(got.ids) == ["a", "b"]
    assert len(got.documents) == 2
    assert got.metadatas == []
    got = collection.get_all_rows(include=["documents", "metadatas"])
    assert len(got.documents) == 2
    assert len(got.metadatas) == 2


def test_bulk_reads_return_decoded_text(backend, tmp_path):
    """get_all_metadata / get_all_rows / iter_metadata / facet_counts must
    decode stored-form text like get() does: no escape prefixes, no internal
    escape flag."""
    collection = _open(backend, tmp_path, create=True)
    nul_doc = "a\x00b embedded nul"
    secret = "\x01mpesc:hunter2"
    collection.add(
        documents=[nul_doc, "plain doc text"],
        ids=["esc", "plain"],
        metadatas=[{"secret": secret, "topic": "x"}, {"topic": "plain"}],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )
    reference = collection.get(ids=["esc", "plain"], include=["documents", "metadatas"])

    for meta in collection.get_all_metadata():
        assert "_mempalace_escaped" not in meta
        assert meta.get("secret") in (secret, None)
    assert collection.get_all_metadata() == [m for m in reference.metadatas] or sorted(
        (m.get("secret"), m.get("topic")) for m in collection.get_all_metadata()
    ) == sorted((m.get("secret"), m.get("topic")) for m in reference.metadatas)

    rows = collection.get_all_rows(include=["documents", "metadatas"])
    esc = rows.ids.index("esc")
    assert rows.documents[esc] == nul_doc
    assert rows.metadatas[esc]["secret"] == secret
    assert all("_mempalace_escaped" not in m for m in rows.metadatas)

    itered = list(collection.iter_metadata())
    assert all("_mempalace_escaped" not in (m or {}) for m in itered)
    by_iter = {m["topic"]: m for m in itered if m and "topic" in m}
    assert by_iter["x"]["secret"] == secret

    facets = collection.facet_counts("secret")
    assert facets == {secret: 1}
    assert "_mempalace_escaped" not in facets


def test_lexical_search_matches_accented_terms(backend, tmp_path):
    """Unicode word characters must survive the FTS query translation:
    stripping them made accented documents unfindable."""
    collection = _open(backend, tmp_path, create=True)
    accented = "\u0142ukasz pije caf\u00e9 w Krakowie"
    collection.add(
        documents=[accented, "plain database text"],
        ids=["acc", "plain"],
        metadatas=[{"wing": "w1"}, {"wing": "w1"}],
        embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    )
    hits = collection.lexical_search(query="\u0142ukasz caf\u00e9", n_results=5).hits
    assert [h.id for h in hits] == ["acc"]
    hits = collection.lexical_search(query="caf\u00e9", n_results=5).hits
    assert [h.id for h in hits] == ["acc"]
    # The accentless case still matches its own document.
    hits = collection.lexical_search(query="database", n_results=5).hits
    assert [h.id for h in hits] == ["plain"]


def test_get_recent_mixed_offsets_compare_by_instant(backend, tmp_path):
    """A per-form text-ordered LIMIT truncated away the true newest record
    when two offset forms differ: 2026-01-02T00:30:00+02:00 (instant
    2026-01-01T22:30:00Z) must not beat 2026-01-01T23:45:00-02:00 (instant
    2026-01-02T01:45:00Z) at limit=1."""
    collection = _open(backend, tmp_path, create=True)
    collection.add(
        documents=["older-but-textually-larger", "true-newest"],
        ids=["A", "B"],
        metadatas=[
            {"filed_at": "2026-01-02T00:30:00+02:00"},
            {"filed_at": "2026-01-01T23:45:00-02:00"},
        ],
        embeddings=[[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
    )
    recent = collection.get_recent(limit=1)
    assert recent.ids == ["B"]
    recent = collection.get_recent(limit=2)
    assert recent.ids == ["B", "A"]


def test_get_recent_mixed_precision_compares_by_instant(backend, tmp_path):
    """Fractional precision also reorders text vs instant: the fractional
    second is the true newest but is textually smaller than the plain Z
    form."""
    collection = _open(backend, tmp_path, create=True)
    collection.add(
        documents=["fractional", "plain-utc", "naive"],
        ids=["C", "D", "E"],
        metadatas=[
            {"filed_at": "2026-01-01T23:45:00.500000Z"},
            {"filed_at": "2026-01-01T23:45:00Z"},
            {"filed_at": "2026-01-01T23:45:00"},
        ],
        embeddings=[[1.0, 0.0, 0.0] for _ in range(3)],
    )
    recent = collection.get_recent(limit=1)
    assert recent.ids == ["C"]
    recent = collection.get_recent(limit=3)
    assert recent.ids == ["C", "D", "E"]
