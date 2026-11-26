"""Drawers with the same text share one vector, whatever file they came from (#3056).

A forked, resumed or re-exported transcript has a new source_file, so its
drawer ids are new and the same-id reuse misses. The wrapper keys each vector
by what the model computes (``embed_key``) and copies a stored vector with
the same key instead of embedding again. Text and metadata stay per drawer.
"""

import shutil

import pytest

import mempalace.backends.embedding_wrapper as embedding_wrapper
import mempalace.embedding as embedding_mod
from mempalace.backends.base import EmbedderIdentity, GetResult
from mempalace.embed_key import EMBED_KEY_FIELD, embed_key, profile_identity
from mempalace.palace import get_collection

PROFILE = {
    "model": "test-hash-embedder",
    "archive_sha256": "0" * 64,
    "runtime": "test",
    "max_length": 256,
    "pooling": "mean",
    "normalization": "l2",
    "quantization": "float32",
    "dimension": 384,
}


@pytest.fixture
def profiled(monkeypatch):
    """Give the test embedder a profile, so its vectors are keyed and shared."""
    ef = embedding_mod.get_embedding_function()
    monkeypatch.setattr(type(ef), "embed_profile", lambda self: dict(PROFILE), raising=False)
    return ef


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


def _write_chat(path, turns):
    body = "\n\n".join(
        f"> question {i} " + "asked " * 30 + "\n" + f"answer {i} " + "said " * 60 for i in turns
    )
    path.write_text(body + "\n", encoding="utf-8")


def _rows(palace, source):
    got = get_collection(palace).get(
        where={"source_file": source}, include=["documents", "metadatas", "embeddings"]
    )
    return list(zip(got["ids"], got["documents"], got["metadatas"], got["embeddings"]))


def _use_sqlite_exact(palace, monkeypatch):
    from mempalace.backends.base import PalaceRef
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend = SQLiteExactBackend()
    backend.get_collection(
        palace=PalaceRef(id=palace, local_path=palace),
        collection_name="mempalace_drawers",
        create=True,
    )
    backend.close()
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact"])
def test_a_forked_transcript_embeds_nothing_for_the_chunks_it_shares(
    tmp_path, monkeypatch, profiled, embedded, backend
):
    from mempalace import convo_miner

    src = (tmp_path / "chats").resolve()
    src.mkdir()
    parent = src / "parent.txt"
    _write_chat(parent, range(4))
    palace = str(tmp_path / "palace")
    if backend == "sqlite_exact":
        _use_sqlite_exact(palace, monkeypatch)
    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")
    parent_rows = _rows(palace, str(parent))
    assert parent_rows
    shared = {doc for _, doc, _, _ in parent_rows}

    fork = src / "fork.txt"
    shutil.copyfile(parent, fork)
    embedded.clear()
    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")

    assert not shared.intersection(embedded)
    fork_rows = _rows(palace, str(fork))
    assert sorted(doc for _, doc, _, _ in fork_rows) == sorted(shared)
    parent_ids = {drawer_id for drawer_id, _, _, _ in parent_rows}
    vector_of = {doc: list(vector) for _, doc, _, vector in parent_rows}
    for drawer_id, doc, meta, vector in fork_rows:
        assert drawer_id not in parent_ids
        assert meta["source_file"] == str(fork)
        assert meta[EMBED_KEY_FIELD]
        assert list(vector) == pytest.approx(vector_of[doc])
    # The parent's drawers are still there, verbatim.
    assert {(i, d) for i, d, _, _ in _rows(palace, str(parent))} == {
        (i, d) for i, d, _, _ in parent_rows
    }


def test_reuse_is_off_on_a_backend_without_the_capability(
    tmp_path, monkeypatch, profiled, embedded
):
    import mempalace.palace as palace_pkg
    from mempalace import convo_miner
    from mempalace.backends.embedding_wrapper import EmbeddingCollection
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    class NoReuse(SQLiteExactBackend):
        capabilities = SQLiteExactBackend.capabilities - {"supports_embedding_reuse"}

    backend = NoReuse()
    monkeypatch.setattr(palace_pkg, "get_backend_for_palace", lambda *a, **k: backend)
    src = (tmp_path / "chats").resolve()
    src.mkdir()
    parent = src / "parent.txt"
    _write_chat(parent, range(3))
    palace = str(tmp_path / "palace")
    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")
    col = get_collection(palace)
    assert isinstance(col, EmbeddingCollection) and not col._content_reuse

    shutil.copyfile(parent, src / "fork.txt")
    embedded.clear()
    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")
    shared = {doc for _, doc, _, _ in _rows(palace, str(parent))}
    assert shared <= set(embedded)
    for _, _, meta, _ in _rows(palace, str(src / "fork.txt")):
        assert EMBED_KEY_FIELD not in meta


def _drawer_writes(monkeypatch):
    """Record every write call the wrapper receives."""
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    calls = []
    for name in ("add", "upsert", "update", "delete"):
        original = getattr(EmbeddingCollection, name)

        def spy(self, *args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(EmbeddingCollection, name, spy)
    return calls


def _touch(path):
    import os

    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def _metas(palace, source):
    return {drawer_id: meta for drawer_id, _, meta, _ in _rows(palace, source)}


def test_a_touched_keyed_transcript_remines_without_a_write(tmp_path, monkeypatch, profiled):
    from mempalace import convo_miner

    src = (tmp_path / "chats").resolve()
    src.mkdir()
    chat = src / "chat.txt"
    _write_chat(chat, range(3))
    palace = str(tmp_path / "palace")
    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")
    before = _metas(palace, str(chat))
    assert all(meta.get(EMBED_KEY_FIELD) for meta in before.values())

    _touch(chat)
    calls = _drawer_writes(monkeypatch)
    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")
    assert calls == []
    assert _metas(palace, str(chat)) == before


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact"])
def test_a_drawer_rewritten_with_its_own_vector_keeps_its_key(
    tmp_path, monkeypatch, profiled, embedded, backend
):
    from mempalace import convo_miner

    src = (tmp_path / "chats").resolve()
    src.mkdir()
    chat = src / "chat.txt"
    _write_chat(chat, range(3))
    palace = str(tmp_path / "palace")
    if backend == "sqlite_exact":
        _use_sqlite_exact(palace, monkeypatch)
    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")
    before = _metas(palace, str(chat))
    # A key the new filing no longer writes makes the re-file upsert each
    # drawer with its stored vector, since an update cannot remove a key.
    get_collection(palace).update(
        ids=list(before), metadatas=[{"dropped_key": "x"} for _ in before]
    )
    _touch(chat)
    embedded.clear()
    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")
    after = _metas(palace, str(chat))
    assert embedded == []
    for drawer_id, meta in after.items():
        assert "dropped_key" not in meta
        assert meta[EMBED_KEY_FIELD] == before[drawer_id][EMBED_KEY_FIELD]


def test_a_refile_without_a_source_record_keeps_the_key_of_an_unchanged_drawer(
    tmp_path, profiled, embedded
):
    from mempalace.palace import write_file_drawers

    palace = str(tmp_path / "palace")
    col = get_collection(palace, create=True)
    source = "/x/notes.txt"
    drawers = [
        (f"d{i}", f"note {i} " + "words " * 30, {"wing": "w", "room": "r", "source_file": source})
        for i in range(3)
    ]
    write_file_drawers(col, source, drawers, palace_path=palace)
    before = _metas(palace, source)
    embedded.clear()
    write_file_drawers(col, source, drawers, palace_path=palace)
    assert embedded == []
    for drawer_id, meta in _metas(palace, source).items():
        assert meta[EMBED_KEY_FIELD] == before[drawer_id][EMBED_KEY_FIELD]


def test_unkeyed_drawers_are_not_backfilled_with_a_key(tmp_path, monkeypatch):
    from mempalace import convo_miner

    src = (tmp_path / "chats").resolve()
    src.mkdir()
    chat = src / "chat.txt"
    _write_chat(chat, range(3))
    palace = str(tmp_path / "palace")
    # Filed by an embedder without a profile: the drawers carry no key.
    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")
    before = _metas(palace, str(chat))
    assert not any(EMBED_KEY_FIELD in meta for meta in before.values())

    ef = embedding_mod.get_embedding_function()
    monkeypatch.setattr(type(ef), "embed_profile", lambda self: dict(PROFILE), raising=False)
    _touch(chat)
    calls = _drawer_writes(monkeypatch)
    convo_miner.mine_convos(str(src), palace, wing="chats", extract_mode="exchange")
    assert calls == []
    assert _metas(palace, str(chat)) == before


# -- the wrapper, against an in-memory store ------------------------------------


class _Store:
    """Just enough of a backend collection for the wrapper's write paths."""

    upsert_merges_metadata = False

    def __init__(self, dimension=0):
        self.rows = {}
        self.lookups = []
        self.fail_lookup = False
        self.dimension = dimension

    def get_stored_embedder_identity(self):
        return EmbedderIdentity("test", self.dimension) if self.dimension else None

    def get(self, *, ids=None, where=None, include=None, **_):
        if where is not None:
            keys = where[EMBED_KEY_FIELD]["$in"]
            self.lookups.append(list(keys))
            if self.fail_lookup:
                raise RuntimeError("store unavailable")
            picked = [i for i, (_, m, _) in self.rows.items() if m.get(EMBED_KEY_FIELD) in keys]
        else:
            picked = [i for i in (ids or self.rows) if i in self.rows]
        return GetResult(
            ids=picked,
            documents=[self.rows[i][0] for i in picked],
            metadatas=[self.rows[i][1] for i in picked],
            embeddings=[self.rows[i][2] for i in picked],
        )

    def upsert(self, *, ids, documents, metadatas, embeddings):
        for i, doc, meta, vector in zip(ids, documents, metadatas, embeddings):
            self.rows[i] = (doc, dict(meta or {}), list(vector))

    add = upsert


def _key(text, profile=PROFILE):
    return embed_key(profile_identity(profile), "document", text)


def _wrapped(store):
    return embedding_wrapper.EmbeddingCollection(store, content_reuse=True)


def test_a_stored_vector_is_copied_by_key_and_the_drawer_keeps_its_own_text(profiled, embedded):
    store = _Store()
    col = _wrapped(store)
    col.upsert(ids=["a"], documents=["same words"], metadatas=[{"source_file": "/x/a.txt"}])
    embedded.clear()
    col.upsert(ids=["b"], documents=["same words"], metadatas=[{"source_file": "/x/b.txt"}])

    assert embedded == []
    assert store.rows["b"][0] == "same words"
    assert store.rows["b"][1]["source_file"] == "/x/b.txt"
    assert store.rows["b"][1][EMBED_KEY_FIELD] == _key("same words")
    assert store.rows["b"][2] == store.rows["a"][2]


def test_identical_texts_in_one_batch_are_embedded_once(profiled, embedded):
    store = _Store()
    _wrapped(store).upsert(
        ids=["a", "b", "c"], documents=["one", "one", "two"], metadatas=[{}, {}, {}]
    )
    assert sorted(embedded) == ["one", "two"]
    assert store.rows["a"][2] == store.rows["b"][2]


def test_a_hit_whose_key_does_not_match_its_text_is_not_reused(profiled, embedded):
    store = _Store()
    # A row carrying the key of "same words" over other text (say, its text
    # was later replaced with an explicit vector).
    store.rows["old"] = ("other words", {EMBED_KEY_FIELD: _key("same words")}, [1.0] * 384)
    _wrapped(store).upsert(ids=["b"], documents=["same words"], metadatas=[{}])
    assert embedded == ["same words"]
    assert store.rows["b"][2] != [1.0] * 384


def test_a_hit_with_another_dimension_is_not_reused(profiled, embedded):
    store = _Store()
    store.rows["old"] = ("same words", {EMBED_KEY_FIELD: _key("same words")}, [0.5] * 128)
    _wrapped(store).upsert(ids=["b"], documents=["same words"], metadatas=[{}])
    assert embedded == ["same words"]
    assert len(store.rows["b"][2]) == 384


def test_a_hit_with_a_non_finite_vector_is_not_reused(profiled, embedded):
    store = _Store()
    vector = [0.1] * 383 + [float("nan")]
    store.rows["old"] = ("same words", {EMBED_KEY_FIELD: _key("same words")}, vector)
    _wrapped(store).upsert(ids=["b"], documents=["same words"], metadatas=[{}])
    assert embedded == ["same words"]


def test_a_failed_lookup_falls_back_to_embedding(profiled, embedded):
    store = _Store()
    col = _wrapped(store)
    col.upsert(ids=["a"], documents=["same words"], metadatas=[{}])
    store.fail_lookup = True
    embedded.clear()
    col.upsert(ids=["b"], documents=["same words"], metadatas=[{}])
    assert store.lookups and embedded == ["same words"]
    assert store.rows["b"][1][EMBED_KEY_FIELD] == _key("same words")


def test_lookups_go_at_most_a_thousand_keys_at_a_time(profiled, embedded):
    store = _Store()
    texts = [f"text {i}" for i in range(2500)]
    _wrapped(store).upsert(
        ids=[f"id{i}" for i in range(2500)], documents=texts, metadatas=[{} for _ in texts]
    )
    assert [len(keys) for keys in store.lookups] == [1000, 1000, 500]
    assert len({key for keys in store.lookups for key in keys}) == 2500


def test_without_content_reuse_the_wrapper_neither_looks_up_nor_keys(profiled, embedded):
    store = _Store()
    col = embedding_wrapper.EmbeddingCollection(store)
    col.upsert(ids=["a"], documents=["same words"], metadatas=[{}])
    col.upsert(ids=["b"], documents=["same words"], metadatas=[{}])
    assert store.lookups == []
    assert embedded == ["same words", "same words"]
    assert EMBED_KEY_FIELD not in store.rows["b"][1]


def test_an_embedder_without_a_profile_gets_no_keys(embedded):
    store = _Store()
    col = _wrapped(store)
    col.upsert(ids=["a"], documents=["same words"], metadatas=[{}])
    col.upsert(ids=["b"], documents=["same words"], metadatas=[{}])
    assert store.lookups == []
    assert EMBED_KEY_FIELD not in store.rows["b"][1]


def test_a_text_update_restamps_the_key(profiled, embedded):
    store = _Store()
    store.update = lambda *, ids, documents=None, metadatas=None, embeddings=None: [
        store.rows.__setitem__(
            i, (doc, {**store.rows[i][1], **(meta or {})}, list(vec) if vec else store.rows[i][2])
        )
        for i, doc, meta, vec in zip(ids, documents, metadatas or [None] * len(ids), embeddings)
    ]
    col = _wrapped(store)
    col.upsert(ids=["a"], documents=["first"], metadatas=[{}])
    col.update(ids=["a"], documents=["second"])
    assert store.rows["a"][1][EMBED_KEY_FIELD] == _key("second")


# -- keys and profiles ----------------------------------------------------------


def test_a_profile_that_differs_in_any_one_field_gives_another_key():
    base = _key("words")
    for field, value in PROFILE.items():
        changed = {**PROFILE, field: f"{value}-other"}
        assert _key("words", changed) != base, field
    assert len(base) == 64


def test_the_key_covers_the_input_kind_and_the_exact_input():
    identity = profile_identity(PROFILE)
    assert embed_key(identity, "document", "words") != embed_key(identity, "query", "words")
    assert embed_key(identity, "document", "words") != embed_key(identity, "document", "words ")


def test_the_minilm_profile_names_what_decides_its_vectors():
    profile = embedding_mod._build_ef_class()().embed_profile()
    for field in ("archive_sha256", "max_length", "pooling", "normalization", "quantization"):
        assert profile[field], field
    assert profile["dimension"] == 384


def test_the_openai_compat_profile_includes_the_endpoint_model_and_url():
    def key(url, model):
        ef = embedding_mod.OpenAICompatEmbeddingFunction(base_url=url, model=model)
        return embed_key(profile_identity(ef.embed_profile()), "document", "words")

    base = key("http://localhost:1234/v1", "model-a")
    assert key("http://localhost:1234/v1", "model-b") != base
    assert key("http://localhost:5678/v1", "model-a") != base
    assert key("http://localhost:1234/v1", "model-a") == base


def test_a_titled_embedder_keys_the_title_so_forks_miss_there():
    from mempalace.embed_key import embedder_inputs
    from mempalace.embeddinggemma2 import EmbeddingGemma2EmbeddingFunction

    ef = EmbeddingGemma2EmbeddingFunction()
    inputs = embedder_inputs(
        ef, ["words", "words"], [{"source_file": "/x/a.txt"}, {"source_file": "/x/b.txt"}]
    )
    assert inputs[0] == ("document:titled", "title: a.txt | text: words")
    assert inputs[0] != inputs[1]
    assert embedder_inputs(ef, ["words"], [{}]) == [("document:prompt=Document", "words")]
    assert ef.embed_profile()["model"] == ef.identity


# -- search ---------------------------------------------------------------------


def test_identical_copies_fold_into_one_hit_and_top_k_stays_distinct(tmp_path, profiled):
    from mempalace.searcher import search_memories

    palace = str(tmp_path / "palace")
    col = get_collection(palace, create=True)
    copy = "the deploy key lives in the vault under ops, rotated every quarter " * 3
    others = [f"the deploy note {i} " + f"topic{i} " * 20 for i in range(4)]
    ids, docs, metas = [], [], []
    for n in range(6):
        ids.append(f"copy{n}")
        docs.append(copy)
        metas.append({"wing": "w", "room": "r", "source_file": f"/x/fork{n}.txt"})
    for i, text in enumerate(others):
        ids.append(f"other{i}")
        docs.append(text)
        metas.append({"wing": "w", "room": "r", "source_file": f"/x/other{i}.txt"})
    col.upsert(ids=ids, documents=docs, metadatas=metas)

    result = search_memories("deploy key vault", palace, n_results=3)
    hits = result["results"]
    texts = [hit["text"] for hit in hits]
    assert len(hits) == 3 and len(set(texts)) == 3
    folded = next(hit for hit in hits if hit["text"] == copy)
    assert len(folded["also_in"]) == 5
    shown = {folded["source_path"]} | {ref["source_path"] for ref in folded["also_in"]}
    assert shown == {f"/x/fork{n}.txt" for n in range(6)}
