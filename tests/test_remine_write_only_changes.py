"""A re-file with a source record writes only the drawers that changed (#3055).

These run on a real Chroma palace and count the collection's write calls,
because the keys a backend adds on its own (``last_modified``, ...) are what
would turn an unchanged re-file into a spurious metadata write.
"""

import hashlib
import json

import pytest

from mempalace.palace import (
    NORMALIZE_VERSION,
    StaleDrawerPurgeFailed,
    file_already_mined,
    get_collection,
    write_file_drawers,
)
from mempalace.palace.source_state import SourceStateStore, source_stream

SOURCE = "/chats/session.jsonl"
WING = "chats"


class _WriteSpy:
    """Count every write the collection class receives."""

    METHODS = ("add", "upsert", "update", "delete")

    def __init__(self, monkeypatch, collection):
        self.calls = []
        cls = type(collection)
        for name in self.METHODS:
            original = getattr(cls, name)

            def spy(inner_self, *args, _name=name, _original=original, **kwargs):
                self.calls.append((_name, kwargs))
                return _original(inner_self, *args, **kwargs)

            monkeypatch.setattr(cls, name, spy)

    def names(self):
        return [name for name, _ in self.calls]

    def ids(self, name):
        return [i for call, kwargs in self.calls if call == name for i in kwargs["ids"]]

    def reset(self):
        self.calls.clear()


def _meta(index, fingerprint, filed_at, **extra):
    meta = {
        "wing": WING,
        "room": "general",
        "source_file": SOURCE,
        "chunk_index": index,
        "filed_at": filed_at,
        "authored_at": filed_at,
        "ingest_mode": "convos",
        "extract_mode": "exchange",
        "normalize_version": NORMALIZE_VERSION,
        "convo_chunker_version": 2,
        "source_fingerprint": fingerprint,
        "source_mtime": 100.0,
        "entities": "",
    }
    meta.update(extra)
    return meta


def _drawers(texts, fingerprint="1:1:1", filed_at="2026-01-01T00:00:00Z", **extra):
    return [
        (f"drawer_{i}", text, _meta(i, fingerprint, filed_at, **extra))
        for i, text in enumerate(texts)
    ]


@pytest.fixture
def palace(tmp_path):
    return str(tmp_path / "palace")


def _file(palace, drawers, store=True, collection=None):
    return write_file_drawers(
        collection if collection is not None else get_collection(palace),
        SOURCE,
        drawers,
        extract_mode="exchange",
        owner="convo",
        palace_path=palace,
        prune_wing=WING,
        source_state=SourceStateStore(palace) if store else None,
    )


def _stream():
    return source_stream(SOURCE, owner="convo", extract_mode="exchange", wing=WING)


def _rows(palace):
    got = get_collection(palace).get(
        where={"source_file": SOURCE}, include=["documents", "metadatas"]
    )
    return {i: (doc, meta) for i, doc, meta in zip(got["ids"], got["documents"], got["metadatas"])}


def test_a_first_filing_writes_every_drawer_and_completes_the_record(palace):
    filed = _file(palace, _drawers(["one", "two", "three"]))

    assert filed == 3
    head = SourceStateStore(palace).get(_stream())
    assert head.complete and head.chunk_total == 3 and head.source_fingerprint == "1:1:1"
    rows = _rows(palace)
    assert rows["drawer_1"][1]["content_sha256"] == hashlib.sha256(b"two").hexdigest()
    # Old binaries still read a complete per-row group from the rows written.
    assert {meta["chunk_total"] for _, meta in rows.values()} == {3}


def test_an_unchanged_refile_writes_nothing_to_the_collection(palace, monkeypatch):
    _file(palace, _drawers(["one", "two", "three"]))
    spy = _WriteSpy(monkeypatch, get_collection(palace))

    # A touched file: new fingerprint, new filed_at/authored_at, same text.
    filed = _file(palace, _drawers(["one", "two", "three"], "2:2:2", "2026-02-02T00:00:00Z"))

    assert spy.names() == []
    assert filed == 0 and filed.unchanged
    head = SourceStateStore(palace).get(_stream())
    assert head.complete and head.source_fingerprint == "2:2:2" and head.chunk_total == 3


def test_an_append_writes_only_the_new_drawers(palace, monkeypatch):
    _file(palace, _drawers(["one", "two"]))
    spy = _WriteSpy(monkeypatch, get_collection(palace))

    filed = _file(
        palace, _drawers(["one", "two", "three", "four"], "2:2:2", "2026-02-02T00:00:00Z")
    )

    assert filed == 2
    assert set(spy.ids("upsert")) == {"drawer_2", "drawer_3"}
    assert set(spy.ids("update")) <= {"drawer_2", "drawer_3"}
    assert spy.ids("delete") == []
    rows = _rows(palace)
    # The unchanged drawers keep what they were filed with.
    assert rows["drawer_0"][1]["filed_at"] == "2026-01-01T00:00:00Z"
    assert rows["drawer_0"][1]["source_fingerprint"] == "1:1:1"
    assert rows["drawer_3"][1]["chunk_total"] == 4
    head = SourceStateStore(palace).get(_stream())
    assert head.complete and head.chunk_total == 4 and head.source_fingerprint == "2:2:2"


def test_changed_text_is_journaled_before_it_is_replaced(palace, monkeypatch):
    _file(palace, _drawers(["one", "two"]))
    spy = _WriteSpy(monkeypatch, get_collection(palace))

    _file(palace, _drawers(["one", "TWO"], "2:2:2"))

    assert "drawer_1" in spy.ids("upsert") and "drawer_0" not in spy.ids("upsert")
    journal = [
        json.loads(line)
        for line in open(f"{palace}/drawer_revisions.jsonl", encoding="utf-8")
        if line.strip()
    ]
    assert [(r["drawer_id"], r["content"]) for r in journal] == [("drawer_1", "two")]
    assert _rows(palace)["drawer_1"][0] == "TWO"


def test_changed_metadata_is_an_update_of_only_the_changed_keys(palace, monkeypatch):
    _file(palace, _drawers(["one", "two"]))
    spy = _WriteSpy(monkeypatch, get_collection(palace))

    changed = _drawers(["one", "two"], "2:2:2")
    changed[1][2]["entities"] = "Alice"
    filed = _file(palace, changed)

    assert filed == 1
    assert "upsert" not in spy.names() and "add" not in spy.names()
    first_update = next(kwargs for name, kwargs in spy.calls if name == "update")
    assert first_update["ids"] == ["drawer_1"]
    assert "documents" not in first_update and "embeddings" not in first_update
    sent = first_update["metadatas"][0]
    assert sent["entities"] == "Alice"
    assert "wing" not in sent and "chunk_index" not in sent
    assert _rows(palace)["drawer_1"][1]["entities"] == "Alice"


def test_a_dropped_metadata_key_is_removed(palace):
    _file(palace, _drawers(["one"], hall="hall_facts"))

    _file(palace, _drawers(["one"], "2:2:2"))

    assert "hall" not in _rows(palace)["drawer_0"][1]


def test_stale_drawers_are_journaled_and_pruned(palace):
    _file(palace, _drawers(["one", "two", "three"]))

    filed = _file(palace, _drawers(["one"], "2:2:2"))

    assert filed == 0 and not filed.unchanged
    assert set(_rows(palace)) == {"drawer_0"}
    assert SourceStateStore(palace).get(_stream()).chunk_total == 1


def test_a_crash_before_the_record_completes_leaves_the_source_pending(palace, monkeypatch):
    _file(palace, _drawers(["one", "two"]))

    def fail(self, *args, **kwargs):
        raise RuntimeError("killed before the stale delete")

    with monkeypatch.context() as patch:
        patch.setattr(type(get_collection(palace)), "delete", fail)
        with pytest.raises(StaleDrawerPurgeFailed):
            _file(palace, _drawers(["uno"], "2:2:2"))

    head = SourceStateStore(palace).get(_stream())
    assert head.state == "pending"
    assert not file_already_mined(
        get_collection(palace),
        SOURCE,
        extract_mode="exchange",
        wing=WING,
        source_state=SourceStateStore(palace),
    )

    _file(palace, _drawers(["uno"], "2:2:2"))
    assert SourceStateStore(palace).get(_stream()).complete


def test_legacy_rows_get_their_hash_by_a_metadata_update(palace, monkeypatch):
    # Rows filed before the record: no content_sha256, every row stamped.
    collection = get_collection(palace)
    legacy = _drawers(["one", "two"])
    collection.upsert(
        ids=[i for i, _, _ in legacy],
        documents=[d for _, d, _ in legacy],
        metadatas=[{**m, "chunk_total": 2} for _, _, m in legacy],
    )
    spy = _WriteSpy(monkeypatch, get_collection(palace))

    _file(palace, _drawers(["one", "two"], "2:2:2"))

    assert "upsert" not in spy.names()
    assert {m["content_sha256"] for _, m in _rows(palace).values()} == {
        hashlib.sha256(b"one").hexdigest(),
        hashlib.sha256(b"two").hexdigest(),
    }
    spy.reset()
    _file(palace, _drawers(["one", "two"], "3:3:3"))
    assert spy.names() == []


# -- the miners, end to end ----------------------------------------------------


def _bump_mtime(path):
    import os

    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def _convo_text(turns):
    return (
        "\n\n".join(f"> question {i} " + "asked " * 30 + "\n" + "answer " * 60 for i in turns)
        + "\n"
    )


@pytest.fixture
def convo(tmp_path):
    from mempalace import convo_miner

    src = (tmp_path / "chats").resolve()
    src.mkdir()
    doc = src / "chat.txt"
    palace = str(tmp_path / "palace")

    def mine():
        convo_miner.mine_convos(str(src), palace, wing="chats")

    return doc, palace, mine


def test_a_touched_transcript_remines_without_a_single_palace_write(convo, monkeypatch):
    from mempalace import convo_miner
    from mempalace.palace import palace_write_serial

    doc, palace, mine = convo
    doc.write_text(_convo_text(range(4)), encoding="utf-8")
    mine()
    _bump_mtime(doc)

    spy = _WriteSpy(monkeypatch, get_collection(palace))
    hallways = []
    real_hallways = convo_miner._compute_hallways_for_wing_safe
    monkeypatch.setattr(
        convo_miner,
        "_compute_hallways_for_wing_safe",
        lambda wing, collection, filed, config=None: (
            hallways.append(filed) or real_hallways(wing, collection, filed, config=config)
        ),
    )
    serial = palace_write_serial()
    mine()

    assert spy.names() == []
    assert palace_write_serial() == serial
    assert hallways == [0]


def test_the_bulk_skip_fires_after_a_record_mode_remine(convo, monkeypatch):
    from mempalace import convo_miner

    doc, palace, mine = convo
    doc.write_text(_convo_text(range(3)), encoding="utf-8")
    mine()
    doc.write_text(_convo_text(range(5)), encoding="utf-8")
    mine()

    checked = []
    real_check = convo_miner.file_already_mined
    monkeypatch.setattr(
        convo_miner,
        "file_already_mined",
        lambda *a, **k: checked.append(a[1]) or real_check(*a, **k),
    )
    mine()

    # Skipped from the prefetched record, without the lock-held recheck.
    assert checked == []


def test_an_appended_transcript_writes_only_its_new_exchanges(convo, monkeypatch):
    doc, palace, mine = convo
    doc.write_text(_convo_text(range(4)), encoding="utf-8")
    mine()
    before = {i for i in get_collection(palace).get(where={"source_file": str(doc)})["ids"]}

    doc.write_text(_convo_text(range(6)), encoding="utf-8")
    spy = _WriteSpy(monkeypatch, get_collection(palace))
    mine()

    after = set(get_collection(palace).get(where={"source_file": str(doc)})["ids"])
    new = after - before
    assert new
    assert set(spy.ids("upsert")) == new
    assert set(spy.ids("update")) <= new
    assert spy.ids("delete") == []


def test_a_touched_project_file_remines_without_a_write_and_then_prefetch_skips(
    tmp_path, monkeypatch
):
    from mempalace import miner

    src = (tmp_path / "proj").resolve()
    src.mkdir()
    doc = src / "notes.md"
    doc.write_text(
        "\n\n".join(f"paragraph {i}. " + "verbatim words " * 40 for i in range(4)),
        encoding="utf-8",
    )
    palace = str(tmp_path / "palace")
    miner.mine(str(src), palace, wing_override="proj", agent="test")
    _bump_mtime(doc)

    spy = _WriteSpy(monkeypatch, get_collection(palace))
    miner.mine(str(src), palace, wing_override="proj", agent="test")
    assert spy.names() == []

    checked = []
    real_check = miner.file_already_mined
    monkeypatch.setattr(
        miner, "file_already_mined", lambda *a, **k: checked.append(a[1]) or real_check(*a, **k)
    )
    miner.mine(str(src), palace, wing_override="proj", agent="test")
    assert checked == []


def test_a_reader_without_the_record_re_mines_rather_than_trusting_stale_stamps(palace):
    # An older binary reads only the per-row stamps.
    _file(palace, _drawers(["one", "two"]))
    _file(palace, _drawers(["one", "two", "three"], "2:2:2"))

    collection = get_collection(palace)
    rows = collection.get(where={"source_file": SOURCE}, include=["metadatas"])["metadatas"]
    groups = {}
    for meta in rows:
        groups.setdefault(meta["source_fingerprint"], []).append(meta["chunk_total"])
    # The current snapshot's group is short of its chunk_total: "not mined".
    assert len(groups["2:2:2"]) < max(groups["2:2:2"])


def test_legacy_rows_are_backfilled_into_a_record_on_first_read(palace):
    collection = get_collection(palace)
    legacy = _drawers(["one", "two"])
    collection.upsert(
        ids=[i for i, _, _ in legacy],
        documents=[d for _, d, _ in legacy],
        metadatas=[{**m, "chunk_total": 2} for _, _, m in legacy],
    )
    store = SourceStateStore(palace)

    import mempalace.palace as palace_pkg

    mined = palace_pkg.prefetch_mined_set(
        collection,
        extract_mode="exchange",
        source_fingerprints=True,
        wing=WING,
        source_state=store,
    )

    assert mined == {SOURCE: "1:1:1"}
    head = store.get(_stream())
    assert head.complete and head.chunk_total == 2 and head.source_fingerprint == "1:1:1"
    # Idempotent, and the record now decides.
    palace_pkg.prefetch_mined_set(
        collection, extract_mode="exchange", source_fingerprints=True, wing=WING, source_state=store
    )
    assert store.get(_stream()) == head


def test_drawers_deleted_behind_the_record_read_as_not_mined(palace, monkeypatch):
    _file(palace, _drawers(["one", "two"]))
    collection = get_collection(palace)
    collection.delete(ids=["drawer_1"])

    import mempalace.palace as palace_pkg

    mined = palace_pkg.prefetch_mined_set(
        collection,
        extract_mode="exchange",
        source_fingerprints=True,
        wing=WING,
        source_state=SourceStateStore(palace),
    )
    assert SOURCE not in mined


def test_a_replacing_backend_appends_and_drops_keys_the_same_way(palace):
    collection = get_collection(palace, create=True, backend="sqlite_exact")
    _file(palace, _drawers(["one", "two"], hall="hall_facts"), collection=collection)

    changed = _drawers(["one", "two", "three"], "2:2:2")
    changed[0][2]["hall"] = "hall_facts"
    filed = _file(palace, changed, collection=collection)

    got = collection.get(where={"source_file": SOURCE}, include=["metadatas"])
    metas = dict(zip(got["ids"], got["metadatas"]))
    assert filed == 2  # drawer_2 added, drawer_1 lost its hall
    assert metas["drawer_0"]["hall"] == "hall_facts"
    assert "hall" not in metas["drawer_1"]
    assert metas["drawer_2"]["chunk_total"] == 3
    assert file_already_mined(
        collection,
        SOURCE,
        extract_mode="exchange",
        wing=WING,
        source_state=SourceStateStore(palace),
    )
