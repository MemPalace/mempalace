"""Previously independent repair probes retained as synthetic regression cases."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import chromadb
import pytest
from chromadb.config import Settings
from mempalace import embedding
from mempalace.backends import chroma as cb
from mempalace.backends import embedding_wrapper as ew
from mempalace.backends._inproc_sqlite import release
from mempalace.config import MempalaceConfig
from mempalace.rooms import load_pending_apply, pending_apply_path

_REAL_EMBED_TEXTS = ew._embed_texts


@pytest.fixture
def collection(tmp_path):
    client = chromadb.PersistentClient(
        path=str(tmp_path), settings=Settings(anonymized_telemetry=False)
    )
    col = client.create_collection("independent_repairs", embedding_function=None)
    yield col
    client.close()
    release(str(tmp_path / "chroma.sqlite3"))


class Embedder:
    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook

    def embed_documents(self, input, metadatas=None):
        metadatas = metadatas if metadatas is not None else [{} for _ in input]
        self.calls.append((list(input), [dict(m or {}) for m in metadatas]))
        if self.hook:
            self.hook()
        return [[float(len((m or {}).get("title") or "")), 1.0] for m in metadatas]


def wrapper(monkeypatch, collection, hook=None):
    monkeypatch.setattr(ew, "_embed_texts", _REAL_EMBED_TEXTS)
    embed = Embedder(hook)
    monkeypatch.setattr(ew, "_get_document_embedder", lambda: embed)
    # Exercise the configured embedder on both the original and repaired upsert.
    monkeypatch.setattr(embedding, "configured_embedding_function", lambda: embed)
    return ew.EmbeddingCollection(cb.ChromaCollection(collection)), embed


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [{"room": "elsewhere"}],
        [{"title": None}],
        [{"title": "Changed 🦋"}],
        [{"source_file": "/new/path.md"}],
    ],
)
def test_actual_chroma_upsert_effective_titles(monkeypatch, collection, metadata):
    collection.add(
        ids=["known"],
        documents=["original"],
        metadatas=[{"title": "Stored 🦋", "source_file": "/old/path.md"}],
        embeddings=[[1.0, 0.0]],
    )
    wrapped, embed = wrapper(monkeypatch, collection)
    wrapped.upsert(ids=["known"], documents=["replacement"], metadatas=metadata)
    actual = collection.get(ids=["known"], include=["metadatas"])["metadatas"][0]
    assert embed.calls[0][1][0] == actual


def test_real_chroma_mixed_input_order(monkeypatch, collection):
    collection.add(
        ids=["z", "a"],
        documents=["zbody", "abody"],
        metadatas=[{"title": "Zed"}, {"title": "Ay"}],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
    )
    wrapped, embed = wrapper(monkeypatch, collection)
    wrapped.upsert(
        ids=["z", "new", "a"],
        documents=["zz", "nn", "aa"],
        metadatas=[{"room": "z"}, {"room": "n"}, {"room": "a"}],
    )
    actual = collection.get(ids=["z", "new", "a"], include=["metadatas"])
    actual_by_id = dict(zip(actual["ids"], actual["metadatas"]))
    assert embed.calls[0][1] == [actual_by_id[i] for i in ["z", "new", "a"]]


@pytest.mark.parametrize("media_type", ["image", "audio", "video"])
def test_mixed_media_is_rejected_before_any_write(monkeypatch, collection, media_type):
    collection.add(
        ids=["asset", "text"],
        documents=["assetdesc", "before"],
        metadatas=[{"media_type": media_type, "title": "Asset"}, {"title": "Text"}],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
    )
    wrapped, embed = wrapper(monkeypatch, collection)
    with pytest.raises(ValueError, match="native media"):
        wrapped.upsert(
            ids=["new", "text", "asset"],
            documents=["newbody", "textbody", "newdesc"],
            metadatas=[{"room": "n"}, {"room": "t"}, {"room": "m"}],
        )
    after = collection.get(include=["documents"])
    assert dict(zip(after["ids"], after["documents"])) == {"asset": "assetdesc", "text": "before"}
    assert embed.calls == []


def test_metadata_update_during_embedding_does_not_publish_wrong_vector(monkeypatch, collection):
    collection.add(
        ids=["known"], documents=["before"], metadatas=[{"title": "OLD"}], embeddings=[[1.0, 0.0]]
    )
    wrapped, embed = wrapper(
        monkeypatch,
        collection,
        hook=lambda: cb.ChromaCollection(collection).update(
            ids=["known"], metadatas=[{"title": "NEW TITLE"}]
        ),
    )
    try:
        wrapped.upsert(ids=["known"], documents=["after"], metadatas=[{"room": "next"}])
    except ValueError:
        after = collection.get(ids=["known"], include=["documents", "metadatas", "embeddings"])
        assert after["documents"] == ["before"]
        assert after["metadatas"] == [{"title": "NEW TITLE"}]
        assert after["embeddings"].tolist() == [[1.0, 0.0]]
        return  # Refusal preserves the newer metadata and the prior document/vector.
    after = collection.get(ids=["known"], include=["metadatas", "embeddings"])
    assert embed.calls[-1][1][0]["title"] == after["metadatas"][0]["title"], {
        "embedded": embed.calls,
        "saved": after["metadatas"],
        "vectors": after["embeddings"].tolist(),
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"closets": [], "closets": [["s","a","b"]]}',
        '{"inputs":{"wing":"a","wing":"b"},"closets":[]}',
        '{"closet_moves":{"a":[],"\\u0061":[]},"closets":[]}',
    ],
)
def test_duplicate_json_members_every_depth_rejected(tmp_path, raw):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    path = Path(pending_apply_path(cfg, "w"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)
    with pytest.raises(ValueError, match="duplicate member"):
        load_pending_apply(cfg, "w")
    assert path.read_text() == raw


def synthetic_db(tmp_path):
    db = tmp_path / "chroma.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.executescript("""PRAGMA journal_mode=WAL;
        CREATE TABLE collections(id TEXT,name TEXT);
        CREATE TABLE segments(id TEXT,collection TEXT,scope TEXT);
        CREATE TABLE max_seq_id(segment_id TEXT,seq_id INTEGER);
        CREATE TABLE embeddings(id INTEGER,embedding_id TEXT,segment_id TEXT);
        CREATE TABLE embeddings_queue(seq_id INTEGER,topic TEXT);
        CREATE TABLE embedding_metadata(id INTEGER,key TEXT,string_value TEXT,int_value INTEGER,float_value REAL,bool_value INTEGER);
        INSERT INTO collections VALUES('col','independent_repairs');
        INSERT INTO segments VALUES('seg','col','METADATA');
        INSERT INTO max_seq_id VALUES('seg',1);
        INSERT INTO embeddings VALUES(1,'doc','seg');
        INSERT INTO embedding_metadata(id,key,string_value) VALUES(1,'wing','original');
        """)
    return db


@pytest.mark.parametrize("reader", ["list", "stream"])
def test_readiness_and_result_share_snapshot(monkeypatch, tmp_path, reader):
    db = synthetic_db(tmp_path)
    original = cb._sqlite_ready_metadata_segment

    def readiness(conn, name):
        ready = original(conn, name)
        with sqlite3.connect(db) as writer:
            writer.execute("UPDATE embedding_metadata SET string_value='changed' WHERE key='wing'")
        return ready

    monkeypatch.setattr(cb, "_sqlite_ready_metadata_segment", readiness)
    try:
        if reader == "list":
            actual = cb.sqlite_list_id_metadata(str(tmp_path), "independent_repairs")[1]
        else:
            actual = list(
                cb.ChromaCollection(
                    SimpleNamespace(name="independent_repairs"), palace_path=str(tmp_path)
                ).iter_metadata()
            )
        assert actual == [{"wing": "original"}], actual
        with sqlite3.connect(db) as check:
            assert (
                check.execute("SELECT string_value FROM embedding_metadata").fetchone()[0]
                == "changed"
            )
    finally:
        release(str(db))


@pytest.mark.parametrize("reader", ["list", "stream"])
def test_pending_foreign_collection_does_not_hide_ready_collection(tmp_path, reader):
    db = synthetic_db(tmp_path)
    with sqlite3.connect(db) as writer:
        writer.execute("INSERT INTO embeddings_queue VALUES(500, 'persistent/default/foreign')")
    try:
        if reader == "list":
            actual = cb.sqlite_list_id_metadata(str(tmp_path), "independent_repairs")[1]
        else:
            actual = list(
                cb.ChromaCollection(
                    SimpleNamespace(name="independent_repairs"), palace_path=str(tmp_path)
                ).iter_metadata()
            )
        assert actual == [{"wing": "original"}]
    finally:
        release(str(db))


@pytest.mark.parametrize("watermark", ["not-a-sequence", -1, b"\x11\x11xxxxxx", b"bad", 1.5])
@pytest.mark.parametrize("reader", ["list", "stream"])
def test_invalid_watermark_refuses_fast_read(tmp_path, watermark, reader):
    db = synthetic_db(tmp_path)
    with sqlite3.connect(db) as writer:
        writer.execute("UPDATE max_seq_id SET seq_id=?", (watermark,))
    try:
        if reader == "list":
            assert cb.sqlite_list_id_metadata(str(tmp_path), "independent_repairs") is None
        else:
            stream = cb.ChromaCollection(
                SimpleNamespace(name="independent_repairs"), palace_path=str(tmp_path)
            ).iter_metadata()
            with pytest.raises(sqlite3.Error):
                list(stream)
    finally:
        release(str(db))


def test_native_media_explicit_vectors_still_work(monkeypatch, collection):
    collection.add(
        ids=["asset"],
        documents=["old description"],
        metadatas=[{"media_type": "image", "title": "Original"}],
        embeddings=[[1.0, 0.0]],
    )
    monkeypatch.setattr(
        ew,
        "_get_document_embedder",
        lambda: pytest.fail("explicit vectors must not load an embedder"),
    )
    ew.EmbeddingCollection(cb.ChromaCollection(collection)).upsert(
        ids=["asset"],
        documents=["new description"],
        metadatas=[{"title": "Changed"}],
        embeddings=[[0.0, 1.0]],
    )
    result = collection.get(ids=["asset"], include=["documents", "metadatas", "embeddings"])
    assert result["documents"] == ["new description"]
    assert result["metadatas"] == [{"media_type": "image", "title": "Changed"}]
    assert result["embeddings"].tolist() == [[0.0, 1.0]]


def test_list_reserved_metadata_policy_is_case_sensitive(collection, tmp_path):
    metadata = {
        "wing": "alpha",
        "chroma:secret": "hide",
        "Chroma:public": "keep",
        "chroma": "ordinary-key",
    }
    collection.add(ids=["probe"], documents=["text"], metadatas=[metadata], embeddings=[[1.0, 0.0]])
    sdk = collection.get(ids=["probe"], include=["metadatas"])["metadatas"]
    assert cb.sqlite_list_id_metadata(str(tmp_path), "independent_repairs")[1] == sdk
