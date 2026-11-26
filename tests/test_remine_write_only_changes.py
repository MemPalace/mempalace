"""A re-file with a source record writes only the drawers that changed (#3055).

These run on a real Chroma palace and count the collection's write calls,
because the keys a backend adds on its own (``last_modified``, ...) are what
would turn an unchanged re-file into a spurious metadata write.
"""

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
    assert all("content_sha256" not in meta for _, meta in rows.values())
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


def test_legacy_rows_refile_with_no_write_when_unchanged(palace, monkeypatch):
    # Rows filed before the record, every row stamped with the per-row group.
    collection = get_collection(palace)
    legacy = _drawers(["one", "two"])
    collection.upsert(
        ids=[i for i, _, _ in legacy],
        documents=[d for _, d, _ in legacy],
        metadatas=[{**m, "chunk_total": 2} for _, _, m in legacy],
    )
    spy = _WriteSpy(monkeypatch, get_collection(palace))

    _file(palace, _drawers(["one", "two"], "2:2:2"))

    assert spy.names() == []
    assert SourceStateStore(palace).get(_stream()).complete
    assert all("content_sha256" not in m for _, m in _rows(palace).values())


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


def test_a_redo_after_a_crash_finishes_the_pending_marker_on_unchanged_drawers(palace, monkeypatch):
    _file(palace, _drawers(["one", "two", "three"]))

    def fail(self, *args, **kwargs):
        raise RuntimeError("killed before the stale delete")

    with monkeypatch.context() as patch:
        patch.setattr(type(get_collection(palace)), "delete", fail)
        with pytest.raises(StaleDrawerPurgeFailed):
            _file(palace, _drawers(["ONE", "two"], "2:2:2"))
    assert _rows(palace)["drawer_0"][1]["chunk_total"] == 3  # the pending marker

    _file(palace, _drawers(["ONE", "two"], "2:2:2"))

    rows = _rows(palace)
    assert set(rows) == {"drawer_0", "drawer_1"}
    assert rows["drawer_0"][1]["chunk_total"] == 2


def test_an_unchanged_drawer_with_an_unusable_vector_is_re_embedded(palace, monkeypatch):
    _file(palace, _drawers(["one", "two"]))
    spy = _WriteSpy(monkeypatch, get_collection(palace))
    import mempalace.embedding as embedding

    # Vectors of another size, as ``set-embedder --force`` leaves behind.
    monkeypatch.setattr(embedding, "probe_dimension", lambda *a, **k: 7)
    filed = _file(palace, _drawers(["one", "two"], "2:2:2"))

    assert set(spy.ids("upsert")) == {"drawer_0", "drawer_1"}
    assert filed == 2 and filed.unchanged  # same text: closets and summaries still fit


def test_a_prune_only_convo_remine_still_recomputes_hallways(convo, monkeypatch):
    from mempalace import convo_miner

    doc, palace, mine = convo
    doc.write_text(_convo_text(range(5)), encoding="utf-8")
    mine()
    doc.write_text(_convo_text(range(3)), encoding="utf-8")

    hallways = []
    monkeypatch.setattr(
        convo_miner,
        "_compute_hallways_for_wing_safe",
        lambda wing, collection, filed, config=None: hallways.append(filed),
    )
    mine()

    assert hallways and hallways[0] > 0


def test_a_failing_dimension_probe_is_paid_once_per_filing_not_once_per_batch(palace, monkeypatch):
    import mempalace.embedding as embedding
    import mempalace.palace as palace_pkg

    monkeypatch.setattr(palace_pkg, "DRAWER_UPSERT_BATCH_SIZE", 2)
    texts = [f"text {i}" for i in range(7)]
    _file(palace, _drawers(texts))

    # A down embedder: probe_dimension returns 0 and caches nothing, so every
    # call would pay another embedder round trip.
    probes = []
    monkeypatch.setattr(embedding, "probe_dimension", lambda *a, **k: probes.append(1) or 0)
    filed = _file(palace, _drawers(texts, "2:2:2"))

    assert filed == 0
    assert len(probes) == 1


# -- crash windows around the source record --------------------------------------


def test_a_crash_between_the_drawer_writes_and_the_record_leaves_it_pending_then_finishes(
    palace, monkeypatch
):
    import sqlite3

    from mempalace.palace import SourceStateUnavailable

    _file(palace, _drawers(["one", "two"]))

    def crash(self, stream, **fields):
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(SourceStateStore, "mark_complete", crash)
        with pytest.raises(SourceStateUnavailable):
            _file(palace, _drawers(["one", "two", "three"], "2:2:2"))

    # The new drawer is durable, but the source must not read as mined.
    assert _rows(palace)["drawer_2"][0] == "three"
    assert SourceStateStore(palace).get(_stream()).state == "pending"
    assert not file_already_mined(
        get_collection(palace),
        SOURCE,
        extract_mode="exchange",
        wing=WING,
        source_state=SourceStateStore(palace),
    )

    spy = _WriteSpy(monkeypatch, get_collection(palace))
    filed = _file(palace, _drawers(["one", "two", "three"], "2:2:2"))

    # The redo finds every drawer already written and only completes the record.
    assert [name for name in spy.names() if name != "update"] == []
    assert filed == 0
    head = SourceStateStore(palace).get(_stream())
    assert head.complete and head.chunk_total == 3 and head.source_fingerprint == "2:2:2"
    assert {doc for doc, _ in _rows(palace).values()} == {"one", "two", "three"}


def test_a_record_that_cannot_go_pending_stops_before_any_drawer_write(palace, monkeypatch):
    import sqlite3

    from mempalace.palace import SourceStateUnavailable

    _file(palace, _drawers(["one", "two"]))
    spy = _WriteSpy(monkeypatch, get_collection(palace))

    def locked(self, stream, **fields):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SourceStateStore, "mark_pending", locked)
    with pytest.raises(SourceStateUnavailable):
        _file(palace, _drawers(["ONE", "three"], "2:2:2"))

    assert spy.names() == []
    assert {doc for doc, _ in _rows(palace).values()} == {"one", "two"}


def test_text_that_cannot_be_journaled_is_not_replaced(palace, monkeypatch):
    import mempalace.revisions as revisions
    from mempalace.palace import PriorTextArchiveFailed

    _file(palace, _drawers(["one", "two"]))
    spy = _WriteSpy(monkeypatch, get_collection(palace))

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(revisions, "append_drawer_revisions", full_disk)
    with pytest.raises(PriorTextArchiveFailed):
        _file(palace, _drawers(["one", "TWO", "three"], "2:2:2"))

    assert spy.names() == []
    assert {i: doc for i, (doc, _) in _rows(palace).items()} == {
        "drawer_0": "one",
        "drawer_1": "two",
    }
    assert SourceStateStore(palace).get(_stream()).state == "pending"


# -- older binaries on the same palace ---------------------------------------------


def _real_source(tmp_path, text):
    import os

    from mempalace.palace import source_fingerprint

    path = tmp_path / "session.jsonl"
    path.write_text(text, encoding="utf-8")
    return str(path), source_fingerprint(os.stat(path))


def _file_at(palace, path, texts, fingerprint, store):
    drawers = [
        (f"drawer_{i}", text, _meta(i, fingerprint, "2026-01-01T00:00:00Z", source_file=path))
        for i, text in enumerate(texts)
    ]
    return write_file_drawers(
        get_collection(palace),
        path,
        drawers,
        extract_mode="exchange",
        owner="convo",
        palace_path=palace,
        prune_wing=WING,
        source_state=SourceStateStore(palace) if store else None,
    )


def _mined(palace, path, store):
    return file_already_mined(
        get_collection(palace),
        path,
        extract_mode="exchange",
        check_source_fingerprint=True,
        wing=WING,
        source_state=SourceStateStore(palace) if store else None,
    )


def test_an_older_binary_re_mines_after_a_new_one_appended_and_nothing_is_lost(tmp_path):
    palace = str(tmp_path / "palace")
    path, first = _real_source(tmp_path, "a\n")
    _file_at(palace, path, ["one", "two"], first, store=True)
    assert _mined(palace, path, store=False)  # an older binary reads the first filing

    import os

    with open(path, "a", encoding="utf-8") as handle:
        handle.write("b\n")
    os.utime(path, (1, 2_000_000_000))
    from mempalace.palace import source_fingerprint

    second = source_fingerprint(os.stat(path))
    _file_at(palace, path, ["one", "two", "three"], second, store=True)

    # The new binary skips it; an older one, reading only per-row stamps,
    # sees an incomplete set for the current snapshot and re-mines.
    assert _mined(palace, path, store=True)
    assert not _mined(palace, path, store=False)

    # The older binary's re-mine rewrites every row; nothing is lost.
    _file_at(palace, path, ["one", "two", "three"], second, store=False)
    assert _mined(palace, path, store=False)
    assert _mined(palace, path, store=True)
    rows = get_collection(palace).get(where={"source_file": path}, include=["documents"])
    assert sorted(rows["documents"]) == ["one", "three", "two"]


def test_a_new_binary_re_mines_a_source_an_older_one_filed_after_it(tmp_path, monkeypatch):
    import os

    from mempalace.palace import source_fingerprint

    palace = str(tmp_path / "palace")
    path, first = _real_source(tmp_path, "a\n")
    _file_at(palace, path, ["one", "two"], first, store=True)

    # An older binary files a grown source; the new record still names the
    # old snapshot.
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("b\n")
    os.utime(path, (1, 2_000_000_000))
    second = source_fingerprint(os.stat(path))
    _file_at(palace, path, ["one", "two", "three"], second, store=False)

    assert not _mined(palace, path, store=True)
    spy = _WriteSpy(monkeypatch, get_collection(palace))
    filed = _file_at(palace, path, ["one", "two", "three"], second, store=True)

    # The older binary already wrote everything: the new one only restamps.
    assert filed == 0 and "upsert" not in spy.names()
    assert _mined(palace, path, store=True)
    rows = get_collection(palace).get(where={"source_file": path}, include=["documents"])
    assert sorted(rows["documents"]) == ["one", "three", "two"]


# -- a non-Chroma backend, end to end -------------------------------------------


def test_a_touched_transcript_on_sqlite_exact_remines_without_a_write(convo, monkeypatch):
    from mempalace.backends.base import PalaceRef
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    doc, palace, mine = convo
    backend = SQLiteExactBackend()
    backend.get_collection(
        palace=PalaceRef(id=palace, local_path=palace),
        collection_name="mempalace_drawers",
        create=True,
    )
    backend.close()
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")

    doc.write_text(_convo_text(range(3)), encoding="utf-8")
    mine()
    import os

    assert not os.path.exists(os.path.join(palace, "chroma.sqlite3"))
    collection = get_collection(palace)
    before = collection.get(where={"source_file": str(doc)}, include=["documents"])

    _bump_mtime(doc)
    spy = _WriteSpy(monkeypatch, collection)
    mine()
    assert spy.names() == []

    doc.write_text(_convo_text(range(5)), encoding="utf-8")
    mine()
    after = get_collection(palace).get(where={"source_file": str(doc)}, include=["documents"])
    old = dict(zip(before["ids"], before["documents"]))
    new = dict(zip(after["ids"], after["documents"]))
    assert set(spy.ids("upsert")) == set(new) - set(old)
    assert {i: new[i] for i in old} == old


def test_a_dead_backend_during_the_vector_check_stops_the_refile(palace, monkeypatch):
    from mempalace.backends.base import BackendError

    _file(palace, _drawers(["one", "two"]))
    collection = get_collection(palace)
    real_get = type(collection).get

    def get(self, *args, **kwargs):
        if "embeddings" in (kwargs.get("include") or []):
            raise BackendError("connection reset")
        return real_get(self, *args, **kwargs)

    monkeypatch.setattr(type(collection), "get", get)
    with pytest.raises(BackendError):
        _file(palace, _drawers(["one", "two", "three"], "2:2:2"))
    assert SourceStateStore(palace).get(_stream()).state == "pending"
