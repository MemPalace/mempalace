"""EmbeddingCollection OneOrMany handling (PR #1706 review).

A bare ``str`` passed as ``documents``/``query_texts`` (ChromaDB's OneOrMany
shape) must be wrapped, not iterated — otherwise ``list("abc")`` embeds per
character and breaks length alignment with ids/metadatas on explicit-vector
backends.
"""

import pytest

from mempalace.backends import embedding_wrapper as ew

_REAL_EMBED_TEXTS = ew._embed_texts


class _FakeInner:
    """Captures what the wrapper delegates to the backend."""

    def __init__(self, rows=None):
        self.calls = {}
        self.rows = rows or {}
        self.get_calls = []

    def add(self, *, documents, ids, metadatas=None, embeddings=None):
        self.calls["add"] = {
            "documents": documents,
            "ids": ids,
            "metadatas": metadatas,
            "embeddings": embeddings,
        }

    def upsert(self, *, documents, ids, metadatas=None, embeddings=None):
        self.calls["upsert"] = {
            "documents": documents,
            "ids": ids,
            "metadatas": metadatas,
            "embeddings": embeddings,
        }

    def update(self, *, ids, documents=None, metadatas=None, embeddings=None):
        self.calls["update"] = {
            "documents": documents,
            "ids": ids,
            "metadatas": metadatas,
            "embeddings": embeddings,
        }

    def get(self, *, ids=None, include=None, **_kwargs):
        from mempalace.backends.base import GetResult

        self.get_calls.append({"ids": ids, "include": include})
        found = [(record_id, self.rows[record_id]) for record_id in ids if record_id in self.rows]
        return GetResult(
            ids=[record_id for record_id, _ in found],
            documents=[row[0] for _, row in found],
            metadatas=[row[1] for _, row in found],
        )

    def query(self, *, query_texts=None, query_embeddings=None, **_kw):
        self.calls["query"] = {"query_texts": query_texts, "query_embeddings": query_embeddings}
        from mempalace.backends.base import QueryResult

        return QueryResult.empty()


def _patch_embed(monkeypatch):
    """Stub the embedder: one vector per input text, recording the inputs."""
    seen = {}

    def fake(texts, **kwargs):
        seen["texts"] = texts
        seen.update(kwargs)
        return [[0.0, 0.0] for _ in texts]

    monkeypatch.setattr(ew, "_embed_texts", fake)
    monkeypatch.setattr(ew, "_get_document_embedder", lambda: None)
    return seen


class _MetadataAwareEmbedder:
    def __init__(self):
        self.calls = []

    def embed_documents(self, input, metadatas=None):
        self.calls.append((list(input), metadatas))
        return [[float(index + 1), 0.0] for index, _ in enumerate(input)]


def _use_metadata_aware_embedder(monkeypatch):
    embedder = _MetadataAwareEmbedder()
    monkeypatch.setattr(ew, "_get_document_embedder", lambda: embedder)
    monkeypatch.setattr(ew, "_embed_texts", _REAL_EMBED_TEXTS)
    return embedder


def test_as_list_wraps_bare_string():
    assert ew._as_list("hello world") == ["hello world"]
    assert ew._as_list({"k": 1}) == [{"k": 1}]  # bare dict wrapped, not -> ["k"]
    src = ["a", "b"]
    assert ew._as_list(src) is src  # list returned as-is (no copy)
    assert ew._as_list(("a", "b")) == ["a", "b"]  # other iterables materialized


def test_add_wraps_bare_string_document(monkeypatch):
    seen = _patch_embed(monkeypatch)
    inner = _FakeInner()
    ew.EmbeddingCollection(inner).add(documents="hello world", ids=["d1"])
    # embedded as one whole document, not per character
    assert seen["texts"] == ["hello world"]
    # and the backend receives a list, length-aligned with ids
    assert inner.calls["add"]["documents"] == ["hello world"]
    assert len(inner.calls["add"]["embeddings"]) == 1


def test_upsert_wraps_bare_string_document(monkeypatch):
    seen = _patch_embed(monkeypatch)
    inner = _FakeInner()
    ew.EmbeddingCollection(inner).upsert(documents="solo", ids=["d1"])
    assert seen["texts"] == ["solo"]
    assert inner.calls["upsert"]["documents"] == ["solo"]
    assert len(inner.calls["upsert"]["embeddings"]) == 1


def test_update_wraps_bare_string_document(monkeypatch):
    seen = _patch_embed(monkeypatch)
    inner = _FakeInner()
    ew.EmbeddingCollection(inner).update(ids=["d1"], documents="changed")
    assert seen["texts"] == ["changed"]
    assert inner.calls["update"]["documents"] == ["changed"]
    assert len(inner.calls["update"]["embeddings"]) == 1


def test_document_update_preserves_stored_title_and_filename_for_embedding(monkeypatch):
    embedder = _use_metadata_aware_embedder(monkeypatch)
    inner = _FakeInner(
        rows={"d1": ("old body", {"title": "Stored title", "source_file": "/docs/guide.md"})}
    )

    ew.EmbeddingCollection(inner).update(ids="d1", documents="changed body")

    assert inner.get_calls == [{"ids": ["d1"], "include": ["documents", "metadatas"]}]
    assert embedder.calls == [
        (
            ["changed body"],
            [{"title": "Stored title", "source_file": "/docs/guide.md"}],
        )
    ]
    assert inner.calls["update"]["embeddings"] == [[1.0, 0.0]]


@pytest.mark.parametrize(
    (
        "changed_metadata",
        "initial_metadata",
        "expected_metadata_key",
        "expected_path",
        "expected_title",
    ),
    [
        (
            {"title": "New title"},
            {"source_file": "/docs/guide.md"},
            "source_file",
            "/docs/guide.md",
            "New title",
        ),
        (
            {"source_file": "/docs/new.md"},
            {"source_file": "/docs/guide.md"},
            "source_file",
            "/docs/new.md",
            None,
        ),
        (
            {"source_path": "/docs/new.md"},
            {"source_path": "/docs/guide.md"},
            "source_path",
            "/docs/new.md",
            None,
        ),
    ],
)
def test_title_or_source_metadata_update_reembeds_existing_document(
    monkeypatch,
    changed_metadata,
    initial_metadata,
    expected_metadata_key,
    expected_path,
    expected_title,
):
    embedder = _use_metadata_aware_embedder(monkeypatch)
    inner = _FakeInner(rows={"d1": ("existing document body", initial_metadata)})

    ew.EmbeddingCollection(inner).update(ids=["d1"], metadatas=[changed_metadata])

    assert embedder.calls[0][0] == ["existing document body"]
    assert embedder.calls[0][1][0][expected_metadata_key] == expected_path
    if expected_title is not None:
        assert embedder.calls[0][1][0]["title"] == expected_title
    assert inner.calls["update"]["metadatas"] == [changed_metadata]
    assert inner.calls["update"]["embeddings"] is not None


def test_native_media_title_update_preserves_native_vector(monkeypatch):
    embedder = _use_metadata_aware_embedder(monkeypatch)
    inner = _FakeInner(
        rows={"asset": ("Image asset: photo.png", {"media_type": "image", "title": "photo.png"})}
    )

    ew.EmbeddingCollection(inner).update(ids=["asset"], metadatas=[{"title": "Family photo"}])

    assert embedder.calls == []
    assert inner.calls["update"]["embeddings"] is None


def test_native_media_document_update_requires_explicit_vector_or_reingest(monkeypatch):
    _use_metadata_aware_embedder(monkeypatch)
    inner = _FakeInner(
        rows={"asset": ("Image asset: photo.png", {"media_type": "image", "title": "photo.png"})}
    )

    with pytest.raises(ValueError, match="re-ingest the source"):
        ew.EmbeddingCollection(inner).update(ids=["asset"], documents=["edited descriptor"])


def test_provider_title_falls_back_to_source_path():
    from mempalace.embeddinggemma2 import _metadata_title

    assert _metadata_title({"source_path": "/docs/guide.md"}) == "guide.md"


def test_same_effective_title_metadata_does_not_reembed(monkeypatch):
    embedder = _use_metadata_aware_embedder(monkeypatch)
    inner = _FakeInner(
        rows={"d1": ("existing body", {"title": "Same title", "source_file": "/docs/a.md"})}
    )

    ew.EmbeddingCollection(inner).update(ids=["d1"], metadatas=[{"title": "Same title"}])

    assert inner.get_calls
    assert embedder.calls == []
    assert inner.calls["update"]["embeddings"] is None


def test_native_media_type_update_requires_reingestion(monkeypatch):
    _use_metadata_aware_embedder(monkeypatch)
    inner = _FakeInner(rows={"asset": ("Image asset: photo.png", {"media_type": "image"})})
    with pytest.raises(ValueError, match="re-ingesting"):
        ew.EmbeddingCollection(inner).update(ids=["asset"], metadatas=[{"media_type": "audio"}])
    assert "update" not in inner.calls


def test_non_title_metadata_update_does_not_read_or_reembed(monkeypatch):
    monkeypatch.setattr(
        ew,
        "_get_document_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("must not resolve embedder")),
    )
    inner = _FakeInner(rows={"d1": ("body", {"title": "Existing"})})

    ew.EmbeddingCollection(inner).update(ids=["d1"], metadatas=[{"wing": "alpha"}])

    assert inner.get_calls == []
    assert inner.calls["update"]["embeddings"] is None


def test_explicit_update_embeddings_bypass_reads_and_reembedding(monkeypatch):
    monkeypatch.setattr(
        ew,
        "_get_document_embedder",
        lambda: (_ for _ in ()).throw(AssertionError("must not resolve embedder")),
    )
    inner = _FakeInner(rows={"d1": ("old body", {"title": "Old"})})
    explicit = [[0.25, 0.75]]

    ew.EmbeddingCollection(inner).update(
        ids=["d1"], documents=["new body"], metadatas=[{"title": "New"}], embeddings=explicit
    )

    assert inner.get_calls == []
    assert inner.calls["update"]["embeddings"] is explicit


def test_query_wraps_bare_string(monkeypatch):
    seen = _patch_embed(monkeypatch)
    inner = _FakeInner()
    ew.EmbeddingCollection(inner).query(query_texts="find me")
    assert seen["texts"] == ["find me"]
    # query_texts is consumed into a single query embedding
    assert len(inner.calls["query"]["query_embeddings"]) == 1
    assert inner.calls["query"]["query_texts"] is None
    assert seen["query"] is True


def test_list_inputs_unaffected(monkeypatch):
    seen = _patch_embed(monkeypatch)
    inner = _FakeInner()
    ew.EmbeddingCollection(inner).add(documents=["one", "two"], ids=["a", "b"])
    assert seen["texts"] == ["one", "two"]
    assert len(inner.calls["add"]["embeddings"]) == 2


def test_add_wraps_bare_string_ids_and_dict_metadatas(monkeypatch):
    seen = _patch_embed(monkeypatch)
    inner = _FakeInner()
    # a single id (str) and a single metadata (dict) are OneOrMany shapes too
    ew.EmbeddingCollection(inner).add(documents="solo", ids="d1", metadatas={"src": "web"})
    call = inner.calls["add"]
    assert call["ids"] == ["d1"]  # not ['d', '1']
    assert call["metadatas"] == [{"src": "web"}]  # not ['src']
    # documents / embeddings / ids / metadatas all length-aligned at 1
    assert call["documents"] == ["solo"]
    assert len(call["embeddings"]) == 1
    assert seen["metadatas"] == [{"src": "web"}]


def test_facet_counts_forwards_to_inner():
    """``BaseCollection.facet_counts`` is a concrete method (raises) — Python
    MRO resolves it on the wrapper subclass before ``__getattr__`` ever fires,
    so without an explicit forwarder the wrapper would raise
    ``UnsupportedCapabilityError`` and silently degrade every wrapped backend
    (qdrant/pgvector/sqlite_exact) to client-side counting in mcp_server."""

    class _Inner:
        def __init__(self):
            self.calls = []

        def facet_counts(self, field, where=None, limit=1000):
            self.calls.append((field, where, limit))
            return {"alpha": 2, "beta": 1}

    inner = _Inner()
    out = ew.EmbeddingCollection(inner).facet_counts("wing", where={"wing": "alpha"}, limit=50)
    assert out == {"alpha": 2, "beta": 1}
    assert inner.calls == [("wing", {"wing": "alpha"}, 50)]


def test_get_all_metadata_forwards_to_inner():
    """Same MRO-shadow pattern as ``facet_counts``: ``BaseCollection.get_all_
    metadata`` ships a concrete default that pages through ``self.get()``.
    Without this forwarder the wrapper runs the base default, the inner
    backend's overridden ``get_all_metadata`` (e.g. pgvector's
    ``with_document=False`` fast path from #1892) is unreachable, and every
    metadata-only fetch transfers the full document column over the wire."""

    class _Inner:
        def __init__(self):
            self.calls = []

        def get_all_metadata(self, where=None):
            self.calls.append(where)
            return [{"wing": "alpha"}, {"wing": "beta"}]

        # Provide a ``get`` so a missing forwarder would silently succeed via
        # the BaseCollection default rather than crash — this is exactly the
        # shape that hid the bug in production. Returning a sentinel here
        # makes a wrong delegation observable: the test would receive
        # ``[{"WRONG"}]``, not the inner's real list.
        def get(self, **_kw):
            from mempalace.backends.base import GetResult

            return GetResult(ids=["bad"], documents=[""], metadatas=[{"WRONG": True}])

    inner = _Inner()
    out = ew.EmbeddingCollection(inner).get_all_metadata(where={"wing": "alpha"})
    assert out == [{"wing": "alpha"}, {"wing": "beta"}]
    assert inner.calls == [{"wing": "alpha"}]


def test_wrapper_forwards_all_concrete_basecollection_methods():
    """Every concrete (non-abstract) public method on ``BaseCollection`` must
    be explicitly defined on ``EmbeddingCollection``. Without an override,
    Python MRO resolves the call to ``BaseCollection``'s default before
    ``__getattr__`` ever fires, silently shadowing the wrapped backend's real
    implementation.

    The bug class this catches: any new ``BaseCollection`` method with a
    concrete default body (raises, returns ``{}``, returns a no-op, pages
    through ``self.get()``) becomes a silent regression for every wrapped
    backend the moment a backend overrides it. ``facet_counts`` (#1868) and
    ``get_all_metadata`` (#1796 / #1892) both hit this; ``lexical_search``
    would have hit it earlier if the wrapper hadn't been updated by hand.

    If this test fails, add an explicit forwarder on ``EmbeddingCollection``
    that calls ``self._inner.<name>(...)``."""
    import inspect

    from mempalace.backends.base import BaseCollection
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    concrete: set[str] = set()
    for name, member in inspect.getmembers(BaseCollection):
        if name.startswith("_"):
            continue
        if isinstance(member, property):
            if member.fget and not getattr(member.fget, "__isabstractmethod__", False):
                concrete.add(name)
            continue
        if callable(member) and not getattr(member, "__isabstractmethod__", False):
            concrete.add(name)

    forwarded = set(vars(EmbeddingCollection).keys())
    missing = sorted(concrete - forwarded)
    assert not missing, (
        "EmbeddingCollection must explicitly forward these concrete "
        "BaseCollection methods (otherwise MRO resolves the call here and "
        "shadows the inner backend's implementation): "
        f"{missing}"
    )
