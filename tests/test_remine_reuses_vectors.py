"""A version-bump re-mine embeds only the text that changed.

Bumping NORMALIZE_VERSION makes every file read as not mined, so the next
mine re-files all of them. Drawers whose text comes out the same keep the
vector already stored, so that pass costs reads, not embeddings.
"""

import pytest

import mempalace.backends.embedding_wrapper as embedding_wrapper
import mempalace.embedding as embedding_mod
from mempalace.palace import (
    NORMALIZE_VERSION,
    _reusable_vector,
    file_already_mined,
    get_aaak_collection,
    get_closets_collection,
    get_collection,
)


def _write_chat(path, turns):
    body = "\n\n".join(f"> question {i} " + "asked " * 30 + "\n" + "answer " * 60 for i in turns)
    path.write_text(body + "\n", encoding="utf-8")


@pytest.fixture
def embedded(monkeypatch):
    texts = []
    real = embedding_wrapper._embed_texts

    def counting(batch, **kwargs):
        if not kwargs.get("query"):
            texts.extend(batch)
        return real(batch, **kwargs)

    monkeypatch.setattr(embedding_wrapper, "_embed_texts", counting)
    return texts


def _mine(src, palace):
    from mempalace import convo_miner

    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")


def _age_stamps(palace, **extra):
    """Mark every drawer as filed by the previous NORMALIZE_VERSION."""
    col = get_collection(palace)
    got = col.get(include=["metadatas"])
    col.update(
        ids=got["ids"],
        metadatas=[
            {**meta, "normalize_version": NORMALIZE_VERSION - 1, **extra}
            for meta in got["metadatas"]
        ],
    )


def _palace_rows(palace):
    got = get_collection(palace).get(include=["documents", "metadatas", "embeddings"])
    return {
        drawer_id: (doc, meta, list(vector))
        for drawer_id, doc, meta, vector in zip(
            got["ids"], got["documents"], got["metadatas"], got["embeddings"]
        )
    }


def test_version_bump_remine_of_unchanged_conversations_embeds_nothing(tmp_path, embedded):
    src = (tmp_path / "chats").resolve()
    src.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        _write_chat(src / name, range(3))
    palace = str(tmp_path / "palace")
    _mine(src, palace)
    first = _palace_rows(palace)
    assert first and len(embedded) == len(first)

    _age_stamps(palace)
    for name in ("a.txt", "b.txt", "c.txt"):
        assert not file_already_mined(
            get_collection(palace), str(src / name), check_mtime=True, extract_mode="exchange"
        )
    embedded.clear()
    _mine(src, palace)

    assert embedded == []
    second = _palace_rows(palace)
    assert set(second) == set(first)
    for drawer_id, (doc, meta, vector) in second.items():
        assert doc == first[drawer_id][0]
        assert vector == pytest.approx(first[drawer_id][2])
        assert meta["normalize_version"] == NORMALIZE_VERSION
    for name in ("a.txt", "b.txt", "c.txt"):
        assert file_already_mined(
            get_collection(palace), str(src / name), check_mtime=True, extract_mode="exchange"
        )


def test_version_bump_remine_embeds_only_the_chunks_that_changed(tmp_path, embedded):
    src = (tmp_path / "chats").resolve()
    src.mkdir()
    chat = src / "chat.txt"
    _write_chat(chat, range(3))
    palace = str(tmp_path / "palace")
    _mine(src, palace)
    before = _palace_rows(palace)

    _age_stamps(palace)
    _write_chat(chat, range(4))
    embedded.clear()
    _mine(src, palace)

    after = _palace_rows(palace)
    changed = [
        doc for drawer_id, (doc, _, _) in after.items() if before.get(drawer_id, (None,))[0] != doc
    ]
    assert changed
    assert sorted(embedded) == sorted(changed)
    assert len(embedded) < len(after)


def test_reused_vector_still_drops_a_key_the_new_metadata_lacks(tmp_path, embedded):
    src = (tmp_path / "chats").resolve()
    src.mkdir()
    _write_chat(src / "chat.txt", range(2))
    palace = str(tmp_path / "palace")
    _mine(src, palace)

    _age_stamps(palace, stale_key="left over")
    embedded.clear()
    _mine(src, palace)

    assert embedded == []
    for _, meta, _ in _palace_rows(palace).values():
        assert "stale_key" not in meta


def _paragraphs(texts):
    return "\n\n".join(f"{text} " + "verbatim words " * 40 for text in texts)


def _mine_project(src, palace):
    from mempalace import miner

    miner.mine(str(src), palace, wing_override="proj", agent="test")


def _project_with_llm_closet_and_summaries(tmp_path, texts):
    src = (tmp_path / "proj").resolve()
    src.mkdir()
    doc = src / "notes.md"
    doc.write_text(_paragraphs(texts), encoding="utf-8")
    palace = str(tmp_path / "palace")
    _mine_project(src, palace)
    drawer_ids = get_collection(palace).get(where={"source_file": str(doc)})["ids"]
    assert len(drawer_ids) == len(texts)
    get_closets_collection(palace).upsert(
        ids=["closet_llm_notes"],
        documents=["deploy plan|alice|→" + drawer_ids[0]],
        metadatas=[{"source_file": str(doc), "generated_by": "llm:test-model"}],
    )
    get_aaak_collection(palace).upsert(
        ids=drawer_ids,
        documents=[f"0:SUMMARY|{i}" for i in range(len(drawer_ids))],
        metadatas=[{"source_file": str(doc), "kind": "aaak"} for _ in drawer_ids],
    )
    return src, doc, palace, drawer_ids


def test_bump_remine_of_an_unchanged_project_file_keeps_its_llm_closet_and_summaries(
    tmp_path, embedded
):
    src, doc, palace, drawer_ids = _project_with_llm_closet_and_summaries(
        tmp_path, ["alpha", "beta", "gamma"]
    )
    _age_stamps(palace)
    embedded.clear()
    _mine_project(src, palace)

    assert embedded == []
    closets = get_closets_collection(palace).get(where={"source_file": str(doc)})
    assert "closet_llm_notes" in closets["ids"]
    assert sorted(get_aaak_collection(palace).get()["ids"]) == sorted(drawer_ids)
    assert file_already_mined(get_collection(palace), str(doc), check_mtime=True)


def test_bump_remine_drops_only_the_summary_of_the_chunk_that_changed(tmp_path, embedded):
    src, doc, palace, drawer_ids = _project_with_llm_closet_and_summaries(
        tmp_path, ["alpha", "beta", "gamma"]
    )
    got = get_collection(palace).get(ids=drawer_ids, include=["documents"])
    before = dict(zip(got["ids"], got["documents"]))
    _age_stamps(palace)
    doc.write_text(_paragraphs(["alpha", "BETA CHANGED", "gamma"]), encoding="utf-8")
    embedded.clear()
    _mine_project(src, palace)

    after = get_collection(palace).get(ids=drawer_ids, include=["documents"])
    changed = [
        drawer_id
        for drawer_id, text in zip(after["ids"], after["documents"])
        if before.get(drawer_id) != text
    ]
    assert len(changed) == 1
    drawer_texts = set(after["documents"])
    assert [text for text in embedded if text in drawer_texts] == [
        text for drawer_id, text in zip(after["ids"], after["documents"]) if drawer_id in changed
    ]
    kept = get_aaak_collection(palace).get()["ids"]
    assert sorted(kept) == sorted(set(drawer_ids) - set(changed))


def test_a_stored_vector_of_another_dimension_is_embedded_again(tmp_path, embedded, monkeypatch):
    src = (tmp_path / "chats").resolve()
    src.mkdir()
    _write_chat(src / "chat.txt", range(2))
    palace = str(tmp_path / "palace")
    _mine(src, palace)
    count = len(embedded)

    _age_stamps(palace)
    monkeypatch.setattr(embedding_mod, "probe_dimension", lambda *a, **k: 7)
    embedded.clear()
    _mine(src, palace)

    assert len(embedded) == count


def _record_gets(monkeypatch, *, fail_vector_reads=False):
    """Record every include list a mine passes to a drawer read."""
    from chromadb.errors import InternalError

    includes = []
    real = embedding_wrapper.EmbeddingCollection.get

    def recording(self, *args, **kwargs):
        include = list(kwargs.get("include") or [])
        includes.append(include)
        if fail_vector_reads and "embeddings" in include:
            raise InternalError(
                "Error executing plan: Internal error: "
                "Error creating hnsw segment reader: Nothing found on disk"
            )
        return real(self, *args, **kwargs)

    monkeypatch.setattr(embedding_wrapper.EmbeddingCollection, "get", recording)
    return includes


def test_a_first_mine_reads_no_stored_vectors(tmp_path, embedded, monkeypatch):
    # Chroma persists HNSW in the background; a vector read that lands
    # mid-persist on a young segment fails, so a first mine must not make one.
    src = (tmp_path / "chats").resolve()
    src.mkdir()
    for name in ("a.txt", "b.txt"):
        _write_chat(src / name, range(3))
    includes = _record_gets(monkeypatch)
    _mine(src, str(tmp_path / "palace"))
    assert includes
    assert not [include for include in includes if "embeddings" in include]


def test_a_failed_vector_read_embeds_again_instead_of_failing_the_mine(
    tmp_path, embedded, monkeypatch
):
    src = (tmp_path / "chats").resolve()
    src.mkdir()
    _write_chat(src / "a.txt", range(3))
    palace = str(tmp_path / "palace")
    _mine(src, palace)
    first = _palace_rows(palace)

    _age_stamps(palace)
    embedded.clear()
    includes = _record_gets(monkeypatch, fail_vector_reads=True)
    _mine(src, palace)
    monkeypatch.undo()

    assert [include for include in includes if "embeddings" in include]
    assert len(embedded) == len(first)
    second = _palace_rows(palace)
    assert {drawer_id: row[0] for drawer_id, row in second.items()} == {
        drawer_id: row[0] for drawer_id, row in first.items()
    }
    assert file_already_mined(
        get_collection(palace), str(src / "a.txt"), check_mtime=True, extract_mode="exchange"
    )


def test_reusable_vector_needs_the_current_dimension():
    assert _reusable_vector([0.1, 0.2], 2)
    assert not _reusable_vector([], 2)
    assert not _reusable_vector(None, 2)
    assert not _reusable_vector([0.1, 0.2, 0.3], 2)
    assert not _reusable_vector([0.1, 0.2], 0)
