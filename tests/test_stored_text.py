"""NUL, lone surrogates, and escape-looking text round-trip unchanged."""

import pytest

from mempalace.backends.base import PalaceRef
from mempalace.stored_text import (
    FLAG,
    StoredTextError,
    decode_record,
    encode_filter,
    encode_record,
    prepare_stored_write,
)

PREFIX = "\x01mpesc:"
BS = chr(92)
ORIGINAL = "alpha\x00beta" + chr(0xD800) + "gamma " + BS + "0 " + BS + BS
# Already looks like an escape and holds no bad character.
LOOKALIKE = PREFIX + "path C:" + BS + "0dir " + BS + BS + "share"


@pytest.mark.parametrize("text", [ORIGINAL, LOOKALIKE, "plain", "", BS + "0"])
def test_encode_then_decode_returns_the_original(text):
    stored, meta = encode_record(text, {"note": text, "tags": [text, 3]})
    assert "\x00" not in stored
    stored.encode("utf-8")
    assert decode_record(stored, meta) == (text, {"note": text, "tags": [text, 3]})


def test_clean_records_are_stored_as_given():
    assert encode_record("plain", None) == ("plain", None)
    assert encode_record("plain", {"wing": "w"}) == ("plain", {"wing": "w"})


def test_an_unflagged_record_is_never_decoded():
    assert decode_record(LOOKALIKE, {"wing": "w"}) == (LOOKALIKE, {"wing": "w"})


def test_the_flag_is_hidden_and_never_taken_from_a_caller():
    stored, meta = encode_record(ORIGINAL, {"wing": "w"})
    assert meta[FLAG] == 1
    assert FLAG not in decode_record(stored, meta)[1]
    # A forged flag is dropped and the text encoded like any other write.
    forged_doc, forged_meta = encode_record(LOOKALIKE, {FLAG: 1, "note": "x\x00y"})
    assert "\x00" not in forged_meta["note"]
    assert decode_record(forged_doc, forged_meta) == (LOOKALIKE, {"note": "x\x00y"})
    assert encode_record("plain", {FLAG: "mine"}) == ("plain", {})


def test_a_decoded_copy_is_stored_again_unchanged():
    stored, meta = encode_record(ORIGINAL, {"note": LOOKALIKE})
    assert encode_record(*decode_record(stored, meta)) == (stored, meta)


def test_ids_and_metadata_keys_are_rejected_not_rewritten():
    for bad in ("id\x00", "id" + chr(0xD800), ""):
        with pytest.raises(StoredTextError):
            prepare_stored_write(["x"], [bad], [{}])
    with pytest.raises(StoredTextError):
        prepare_stored_write(["x"], ["ok"], [{"k\x00": "v"}])


def test_batch_without_metadata_flags_only_when_needed():
    assert prepare_stored_write(["a", "b"], ["1", "2"]) == (["a", "b"], ["1", "2"], None)
    docs, _, metas = prepare_stored_write(["a", "b\x00"], ["1", "2"])
    assert metas == [{FLAG: 1}, {FLAG: 1}]
    assert decode_record(docs[1], metas[1])[0] == "b\x00"
    assert prepare_stored_write(None, ["1"], [{"n": "x\x00"}])[0] is None


def _sqlite_exact(tmp_path):
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    return SQLiteExactBackend().get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="mempalace_drawers",
        create=True,
    )


def _chroma(tmp_path):
    from mempalace.backends.chroma import ChromaBackend

    return ChromaBackend().get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="mempalace_drawers",
        create=True,
    )


def _seed(col, vectors):
    kw = {"embeddings": [[0.1, 0.2, 0.3, 0.4], [0.4, 0.3, 0.2, 0.1]]} if vectors else {}
    col.add(
        documents=[ORIGINAL + " kubernetes rollout", LOOKALIKE + " kubernetes notes"],
        ids=["a", "b"],
        metadatas=[{"wing": "w", "note": "keep\x00me"}, {"wing": "w", "note": LOOKALIKE}],
        **kw,
    )


def _check_reads(col, vectors):
    want = {
        "a": (ORIGINAL + " kubernetes rollout", "keep\x00me"),
        "b": (LOOKALIKE + " kubernetes notes", LOOKALIKE),
    }
    got = col.get(ids=["a", "b"], include=["documents", "metadatas"])
    assert {i: (d, m["note"]) for i, d, m in zip(got.ids, got.documents, got.metadatas)} == want
    assert all(FLAG not in m for m in got.metadatas)
    # documents alone still decode
    assert col.get(ids=["a"], include=["documents"]).documents == [want["a"][0]]
    hits = col.lexical_search(query="kubernetes", n_results=5).hits
    assert {h.id: (h.document, h.metadata["note"]) for h in hits} == want
    q = {"query_embeddings": [[0.1, 0.2, 0.3, 0.4]]} if vectors else {"query_texts": ["notes"]}
    res = col.query(n_results=2, include=["documents"], **q)
    assert sorted(res.documents[0]) == sorted(d for d, _ in want.values())


def _check_updates(col, vectors):
    kw = {"embeddings": [[0.2, 0.2, 0.2, 0.2]]} if vectors else {}
    col.update(ids=["a"], documents=["x\x00y"], **kw)
    col.update(ids=["b"], metadatas=[{"extra": "s" + chr(0xD800)}])
    got = col.get(ids=["a", "b"], include=["documents", "metadatas"])
    assert got.documents == ["x\x00y", LOOKALIKE + " kubernetes notes"]
    assert got.metadatas[1]["extra"] == "s" + chr(0xD800)
    assert got.metadatas[1]["note"] == LOOKALIKE


def test_sqlite_exact_round_trips_every_read_and_update(tmp_path):
    col = _sqlite_exact(tmp_path)
    _seed(col, vectors=True)
    _check_reads(col, vectors=True)
    _check_updates(col, vectors=True)


def test_chroma_round_trips_every_read_and_update(tmp_path):
    col = _chroma(tmp_path)
    _seed(col, vectors=False)
    _check_reads(col, vectors=False)
    _check_updates(col, vectors=False)


def test_chroma_sqlite_readers_decode(tmp_path):
    from mempalace.backends.chroma import (
        sqlite_diary_rows,
        sqlite_documents_for_ids,
        sqlite_drawer_rows,
    )

    col = _chroma(tmp_path)
    col.add(
        documents=[ORIGINAL, "plain entry"],
        ids=["d1", "d2"],
        metadatas=[
            {"room": "diary", "agent": "a", "wing": "w", "filed_at": "2026-01-02", "t": LOOKALIKE},
            {"room": "diary", "agent": "a", "wing": "w", "filed_at": "2026-01-01"},
        ],
    )
    path = str(tmp_path)
    assert sqlite_documents_for_ids(path, "mempalace_drawers", ["d1", "d2"]) == {
        "d1": ORIGINAL,
        "d2": "plain entry",
    }
    direct, rows = sqlite_drawer_rows(path, "mempalace_drawers", drawer_id="d1")
    assert direct and rows[0][1] == ORIGINAL and rows[0][2]["t"] == LOOKALIKE
    assert FLAG not in rows[0][2]
    total, rows = sqlite_diary_rows(path, "mempalace_drawers", agent_name="a", limit=5)
    assert total == 2
    assert [row[1] for row in rows] == [ORIGINAL, "plain entry"]
    meta = col.get_recent(limit=1, include=["documents", "metadatas"])
    assert meta.documents == [ORIGINAL] and FLAG not in meta.metadatas[0]
    assert all(FLAG not in m for m in col.get_all_metadata())


SURROGATE_PATH = "/p/x" + chr(0xDCFF) + "y.txt"


def test_chroma_keyed_metadata_scans_decode(tmp_path):
    from mempalace.backends.chroma import sqlite_wing_source_counts
    from mempalace.palace import NORMALIZE_VERSION, prefetch_mined_set

    col = _chroma(tmp_path)
    col.add(
        documents=["one", "two"],
        ids=["k1", "k2"],
        metadatas=[
            {"note": "foo\x00bar", "wing": "w"},
            {
                "source_file": SURROGATE_PATH,
                "normalize_version": NORMALIZE_VERSION,
                "wing": "w",
            },
        ],
    )
    assert list(col.iter_metadata(["note"])) == [{"note": "foo\x00bar"}]
    assert SURROGATE_PATH in prefetch_mined_set(col)

    transcript = "/home/u/.claude/projects/p/s\x00.jsonl"
    col.add(documents=["t"], ids=["k3"], metadatas=[{"wing": "w", "source_file": transcript}])
    assert sqlite_wing_source_counts(str(tmp_path), "mempalace_drawers") == [("w", transcript, 1)]


def test_chroma_list_drawers_reader_decodes(tmp_path):
    from mempalace.backends.chroma import sqlite_list_id_metadata

    col = _chroma(tmp_path)
    col.add(
        documents=["one", "two"],
        ids=["l1", "l2"],
        metadatas=[{"wing": "w", "note": "foo\x00bar"}, {"wing": "v", "note": "plain"}],
    )
    ids, metas = sqlite_list_id_metadata(str(tmp_path), "mempalace_drawers", {"wing": "w"})
    assert ids == ["l1"]
    assert metas == [{"wing": "w", "note": "foo\x00bar"}]


@pytest.mark.parametrize("make", [_chroma, _sqlite_exact])
@pytest.mark.parametrize("value", ["a\x00b", "a" + chr(0xD800) + "b", LOOKALIKE])
def test_a_filter_matches_the_value_read_back(tmp_path, make, value):
    col = make(tmp_path)
    col.add(
        documents=["hit", "miss"],
        ids=["f1", "f2"],
        metadatas=[{"source_file": value}, {"source_file": "other"}],
        embeddings=[[0.1, 0.2, 0.3, 0.4], [0.4, 0.3, 0.2, 0.1]],
    )
    stored = col.get(ids=["f1"], include=["metadatas"]).metadatas[0]["source_file"]
    assert stored == value
    assert col.get(where={"source_file": stored}).ids == ["f1"]
    assert col.get(where={"source_file": {"$in": [stored]}}).ids == ["f1"]
    assert col.get(where={"source_file": {"$ne": stored}}).ids == ["f2"]


@pytest.mark.parametrize("value", ["a\x00b", "a" + chr(0xD800) + "b"])
def test_a_tuple_in_filter_matches_the_value_read_back(tmp_path, value):
    encoded = encode_filter({"$in": (value, "other")})["$in"]
    assert list(encoded) == encode_filter({"$in": [value, "other"]})["$in"]
    col = _chroma(tmp_path)
    col.add(
        documents=["rollout hit", "rollout miss"],
        ids=["t1", "t2"],
        metadatas=[{"wing": value}, {"wing": "elsewhere"}],
    )
    where = {"wing": {"$in": (value, "other")}}
    hits = col.lexical_search(query="rollout", n_results=5, where=where).hits
    assert [(h.id, h.metadata["wing"]) for h in hits] == [("t1", value)]
    assert col.get_recent(limit=5, where=where).ids == ["t1"]


def test_a_forged_flag_cannot_store_raw_text(tmp_path):
    import sqlite3

    col = _chroma(tmp_path)
    col.add(
        documents=[LOOKALIKE, "raw\x00nul"],
        ids=["g1", "g2"],
        metadatas=[{FLAG: 1}, {FLAG: 1}],
    )
    got = col.get(ids=["g1", "g2"], include=["documents", "metadatas"])
    assert got.documents == [LOOKALIKE, "raw\x00nul"]
    assert all(FLAG not in meta for meta in got.metadatas)
    conn = sqlite3.connect(str(tmp_path / "chroma.sqlite3"))
    try:
        stored = [row[0] for row in conn.execute("SELECT string_value FROM embedding_metadata")]
    finally:
        conn.close()
    assert not any(isinstance(value, str) and "\x00" in value for value in stored)


def test_sqlite_exact_full_text_finds_words_next_to_an_escape(tmp_path):
    col = _sqlite_exact(tmp_path)
    col.add(
        documents=["foo\x00bar", "baz" + chr(0xD800) + "qux"],
        ids=["x1", "x2"],
        embeddings=[[0.1, 0.2, 0.3, 0.4], [0.4, 0.3, 0.2, 0.1]],
    )
    assert [hit.document for hit in col.lexical_search(query="bar").hits] == ["foo\x00bar"]
    assert [hit.id for hit in col.lexical_search(query="qux").hits] == ["x2"]


def test_bm25_fallback_decodes(tmp_path):
    from mempalace.searcher import _bm25_only_via_sqlite

    _seed(_chroma(tmp_path), vectors=False)
    out = _bm25_only_via_sqlite(
        "kubernetes", str(tmp_path), collection_name="mempalace_drawers", n_results=5
    )
    assert sorted(r["text"] for r in out["results"]) == sorted(
        [ORIGINAL + " kubernetes rollout", LOOKALIKE + " kubernetes notes"]
    )
