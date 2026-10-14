"""Keep metadata-aware embedding inputs consistent with guarded publication."""

import contextlib

import pytest

from mempalace import embedding
from mempalace.backends import chroma as cb, embedding_wrapper as ew
from test_chroma_metadata_contracts import wrapper


@pytest.mark.parametrize("kind", ["document_title_drift", "title_document_drift"])
def test_update_does_not_publish_embedding_for_outdated_input(monkeypatch, collection, kind):
    collection.add(
        ids=["known"], documents=["before"], metadatas=[{"title": "OLD"}], embeddings=[[1.0, 0.0]]
    )
    if kind == "document_title_drift":
        concurrent = {"metadatas": [{"title": "NEW TITLE"}]}
        requested = {"documents": ["after"]}
    else:
        concurrent = {"documents": ["concurrent document"], "embeddings": [[99.0, 1.0]]}
        requested = {"metadatas": [{"title": "Requested title"}]}
    wrapped, embed = wrapper(
        monkeypatch,
        collection,
        hook=lambda: cb.ChromaCollection(collection).update(ids=["known"], **concurrent),
    )
    try:
        wrapped.update(ids=["known"], **requested)
    except ValueError as exc:
        assert "changed while embedding" in str(exc)
        saved = collection.get(ids=["known"], include=["documents", "metadatas", "embeddings"])
        if kind == "document_title_drift":
            assert saved["documents"] == ["before"]
            assert saved["metadatas"] == [{"title": "NEW TITLE"}]
            assert saved["embeddings"].tolist() == [[1.0, 0.0]]
        else:
            assert saved["documents"] == ["concurrent document"]
            assert saved["metadatas"] == [{"title": "OLD"}]
            assert saved["embeddings"][0].tolist() == pytest.approx([99.0, 1.0])
        return
    saved = collection.get(ids=["known"], include=["documents", "metadatas", "embeddings"])
    used_docs, used_meta = embed.calls[-1]
    assert (used_docs, used_meta) == (saved["documents"], saved["metadatas"]), {
        "embedded": embed.calls[-1],
        "saved": saved,
    }


def test_update_removal_metadata_matches_persisted_metadata(monkeypatch, collection):
    collection.add(
        ids=["known"],
        documents=["before"],
        metadatas=[{"title": "OLD", "room": "kept"}],
        embeddings=[[1.0, 0.0]],
    )
    wrapped, embed = wrapper(monkeypatch, collection)
    wrapped.update(ids=["known"], documents=["after"], metadatas=[{"title": None}])
    saved = collection.get(ids=["known"], include=["metadatas"])["metadatas"]
    assert embed.calls[-1][1] == saved


def test_update_without_interleaving_remains_valid(monkeypatch, collection):
    collection.add(
        ids=["known"], documents=["before"], metadatas=[{"title": "OLD"}], embeddings=[[1.0, 0.0]]
    )
    wrapped, embed = wrapper(monkeypatch, collection)
    wrapped.update(ids=["known"], documents=["after"], metadatas=[{"title": "Changed"}])
    saved = collection.get(ids=["known"], include=["documents", "metadatas"])
    assert embed.calls[-1] == (saved["documents"], saved["metadatas"])


def test_embedder_resolution_occurs_inside_embedding_section(monkeypatch, collection):
    from test_chroma_metadata_contracts import Embedder, _REAL_EMBED_TEXTS

    depth = 0
    resolved_at = []

    @contextlib.contextmanager
    def section():
        nonlocal depth
        depth += 1
        try:
            yield
        finally:
            depth -= 1

    def resolve():
        resolved_at.append(depth)
        return Embedder()

    monkeypatch.setattr(ew, "_embed_texts", _REAL_EMBED_TEXTS)
    monkeypatch.setattr(ew, "_get_document_embedder", resolve)
    monkeypatch.setattr(embedding, "configured_embedding_function", resolve)
    embedding.set_embedding_section_hook(section)
    try:
        ew.EmbeddingCollection(cb.ChromaCollection(collection)).upsert(
            ids=["new"], documents=["doc"], metadatas=[{"title": "title"}]
        )
    finally:
        embedding.set_embedding_section_hook(None)
    assert resolved_at and all(level > 0 for level in resolved_at), resolved_at


def test_gemma2_lazy_model_load_entry_runs_inside_section(monkeypatch, collection):
    from mempalace.embeddinggemma2 import EmbeddingGemma2EmbeddingFunction
    from test_chroma_metadata_contracts import _REAL_EMBED_TEXTS

    ef = EmbeddingGemma2EmbeddingFunction()
    assert ef._model is None
    depth = 0
    load_at = []

    @contextlib.contextmanager
    def section():
        nonlocal depth
        depth += 1
        try:
            yield
        finally:
            depth -= 1

    # Instrument the real lazy-load entry point without loading a model.
    monkeypatch.setattr(ef, "_load_and_check", lambda: load_at.append(depth))
    monkeypatch.setattr(
        ef, "_encode_shrinking_on_oom", lambda inputs, **kw: [[1.0, 0.0] for _ in inputs]
    )
    monkeypatch.setattr(ew, "_get_document_embedder", lambda: ef)
    monkeypatch.setattr(ew, "_embed_texts", _REAL_EMBED_TEXTS)
    embedding.set_embedding_section_hook(section)
    try:
        ew.EmbeddingCollection(cb.ChromaCollection(collection)).upsert(
            ids=["new"], documents=["doc"], metadatas=[{"title": "title"}]
        )
    finally:
        embedding.set_embedding_section_hook(None)
    assert load_at == [1]


def test_gemma2_none_title_and_absent_title_choose_same_prompt():
    from mempalace.embeddinggemma2 import _metadata_title

    assert _metadata_title({"title": None, "source_file": "/tmp/fallback.txt"}) == (
        _metadata_title({"source_file": "/tmp/fallback.txt"})
    )


def test_wrapper_reports_inner_merge_capability(collection):
    inner = cb.ChromaCollection(collection)
    assert ew.EmbeddingCollection(inner).upsert_merges_metadata == inner.upsert_merges_metadata


def test_successful_guarded_upsert_counts_one_write(monkeypatch, collection):
    collection.add(
        ids=["known"], documents=["before"], metadatas=[{"title": "OLD"}], embeddings=[[1.0, 0.0]]
    )
    wrapped, _ = wrapper(monkeypatch, collection)
    before = cb.chroma_write_serial()
    wrapped.upsert(ids=["known"], documents=["after"])
    assert cb.chroma_write_serial() - before == 1


@pytest.mark.parametrize("mutation", ["delete", "metadata"])
def test_update_drift_refuses_entire_batch(monkeypatch, collection, mutation):
    collection.add(
        ids=["a", "b"],
        documents=["before a", "before b"],
        metadatas=[{"title": "A"}, {"title": "B"}],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
    )
    inner = cb.ChromaCollection(collection)

    def concurrent():
        if mutation == "delete":
            inner.delete(ids=["b"])
        else:
            inner.update(ids=["b"], metadatas=[{"title": "Changed"}])

    wrapped, _ = wrapper(monkeypatch, collection, hook=concurrent)
    with pytest.raises(ValueError, match="changed while embedding"):
        wrapped.update(ids=["b", "a"], documents=["after b", "after a"])
    result = collection.get(ids=["a"], include=["documents", "metadatas", "embeddings"])
    assert result["documents"] == ["before a"]
    assert result["metadatas"] == [{"title": "A"}]
    assert result["embeddings"].tolist() == [[1.0, 0.0]]


def test_guard_uses_configured_lock_once_and_refuses_threaded_writer(
    monkeypatch, collection, tmp_path
):
    from threading import Thread

    collection.add(
        ids=["known"], documents=["before"], metadatas=[{"title": "OLD"}], embeddings=[[1.0, 0.0]]
    )
    inner = cb.ChromaCollection(collection, palace_path=str(tmp_path))
    errors = []

    def write():
        try:
            inner.update(ids=["known"], metadatas=[{"title": "CONCURRENT"}])
        except Exception as exc:
            errors.append(exc)

    def during_embedding():
        thread = Thread(target=write, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "embedding must run without the backend write lock"
        assert not errors

    wrapped, _ = wrapper(monkeypatch, collection, hook=during_embedding)
    wrapped._inner = inner
    with pytest.raises(ValueError, match="changed while embedding"):
        wrapped.update(ids=["known"], documents=["after"])
    assert collection.get(ids=["known"])["documents"] == ["before"]


def test_nested_wrapper_and_title_update_count_one_write(monkeypatch, collection):
    collection.add(
        ids=["known"], documents=["before"], metadatas=[{"title": "OLD"}], embeddings=[[1.0, 0.0]]
    )
    wrapped, embed = wrapper(monkeypatch, collection)
    nested = ew.EmbeddingCollection(wrapped)
    before = cb.chroma_write_serial()
    nested.update(ids=["known"], metadatas=[{"title": "NEW"}])
    assert cb.chroma_write_serial() - before == 1
    assert len(embed.calls) == 1
    assert collection.get(ids=["known"])["metadatas"] == [{"title": "NEW"}]
