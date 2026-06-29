"""EmbeddingCollection OneOrMany handling (PR #1706 review).

A bare ``str`` passed as ``documents``/``query_texts`` (ChromaDB's OneOrMany
shape) must be wrapped, not iterated — otherwise ``list("abc")`` embeds per
character and breaks length alignment with ids/metadatas on explicit-vector
backends.
"""

from mempalace.backends import embedding_wrapper as ew


class _FakeInner:
    """Captures what the wrapper delegates to the backend."""

    def __init__(self):
        self.calls = {}

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

    def query(self, *, query_texts=None, query_embeddings=None, **_kw):
        self.calls["query"] = {"query_texts": query_texts, "query_embeddings": query_embeddings}
        from mempalace.backends.base import QueryResult

        return QueryResult.empty()


def _patch_embed(monkeypatch):
    """Stub the embedder: one vector per input text, recording the inputs."""
    seen = {}

    def fake(texts):
        seen["texts"] = texts
        return [[0.0, 0.0] for _ in texts]

    monkeypatch.setattr(ew, "_embed_texts", fake)
    return seen


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


def test_query_wraps_bare_string(monkeypatch):
    seen = _patch_embed(monkeypatch)
    inner = _FakeInner()
    ew.EmbeddingCollection(inner).query(query_texts="find me")
    assert seen["texts"] == ["find me"]
    # query_texts is consumed into a single query embedding
    assert len(inner.calls["query"]["query_embeddings"]) == 1
    assert inner.calls["query"]["query_texts"] is None


def test_list_inputs_unaffected(monkeypatch):
    seen = _patch_embed(monkeypatch)
    inner = _FakeInner()
    ew.EmbeddingCollection(inner).add(documents=["one", "two"], ids=["a", "b"])
    assert seen["texts"] == ["one", "two"]
    assert len(inner.calls["add"]["embeddings"]) == 2


def test_add_wraps_bare_string_ids_and_dict_metadatas(monkeypatch):
    _patch_embed(monkeypatch)
    inner = _FakeInner()
    # a single id (str) and a single metadata (dict) are OneOrMany shapes too
    ew.EmbeddingCollection(inner).add(documents="solo", ids="d1", metadatas={"src": "web"})
    call = inner.calls["add"]
    assert call["ids"] == ["d1"]  # not ['d', '1']
    assert call["metadatas"] == [{"src": "web"}]  # not ['src']
    # documents / embeddings / ids / metadatas all length-aligned at 1
    assert call["documents"] == ["solo"]
    assert len(call["embeddings"]) == 1
