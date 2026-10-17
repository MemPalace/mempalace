"""Metadata-aware vectors must reflect the metadata the backend retains."""

import pytest
from mempalace import embedding
from mempalace.backends import embedding_wrapper as ew
from mempalace.backends.chroma import ChromaCollection
from test_embedding_wrapper import _FakeInner, _use_metadata_aware_embedder


@pytest.mark.parametrize("metadata", [None, [{"room": "new"}]])
def test_chroma_partial_upsert_embeds_retained_metadata(monkeypatch, collection, metadata):
    collection.add(
        ids=["old"],
        documents=["before"],
        metadatas=[{"title": "Retained title", "room": "old"}],
        embeddings=[[1.0] + [0.0] * 383],
    )
    ef = _use_metadata_aware_embedder(monkeypatch)
    monkeypatch.setattr(embedding, "configured_embedding_function", lambda: ef)
    ef.embed_documents = lambda input, metadatas=None: (
        ef.calls.append((input, metadatas)) or [[1.0] + [0.0] * 383 for _ in input]
    )
    wrapper = ew.EmbeddingCollection(ChromaCollection(collection))
    wrapper.upsert(ids=["old"], documents=["after"], metadatas=metadata)
    saved = collection.get(ids=["old"], include=["metadatas"])["metadatas"]
    assert ef.calls[-1][1] == saved
    assert saved[0]["title"] == "Retained title"


@pytest.mark.parametrize("merge", [False, True])
def test_mixed_existing_and_new_respects_backend_contract(monkeypatch, merge):
    ef = _use_metadata_aware_embedder(monkeypatch)
    monkeypatch.setattr(embedding, "configured_embedding_function", lambda: ef)
    inner = _FakeInner({"old": ("before", {"title": "Keep only on merge"})})
    inner.upsert_merges_metadata = merge
    ew.EmbeddingCollection(inner).upsert(
        ids=["new", "old"], documents=["a", "b"], metadatas=[{"room": "x"}, {"room": "y"}]
    )
    expected = [{"room": "x"}, {"room": "y"}]
    if merge:
        expected[1]["title"] = "Keep only on merge"
    assert ef.calls[-1][1] == expected
