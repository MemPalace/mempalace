"""The per-source record: one row per source stream, pending until complete."""

import os

from mempalace.palace.source_state import (
    SOURCE_STATE_FILENAME,
    SourceStateStore,
    source_stream,
)


def _stream(path="/notes/a.md", **kwargs):
    return source_stream(path, **kwargs)


def test_a_missing_store_reads_empty_and_a_read_only_store_never_creates_the_file(tmp_path):
    store = SourceStateStore(str(tmp_path), read_only=True)

    assert store.get(_stream(owner="project")) is None
    assert store.get_many(owner="project") == {}
    assert not (tmp_path / SOURCE_STATE_FILENAME).exists()


def test_pending_then_complete_round_trips_every_field(tmp_path):
    store = SourceStateStore(str(tmp_path))
    stream = _stream(owner="convo", extract_mode="exchange", wing="chats")

    store.mark_pending(stream, source_fingerprint="1:2:3", source_mtime=12.5)
    head = store.get(stream)
    assert head.state == "pending" and not head.complete

    store.mark_complete(
        stream,
        chunk_total=7,
        source_fingerprint="1:2:3",
        source_mtime=12.5,
        normalize_version=3,
        chunker_version=2,
    )
    head = store.get(stream)
    assert head.complete
    assert (head.chunk_total, head.source_fingerprint, head.source_mtime) == (7, "1:2:3", 12.5)
    assert (head.normalize_version, head.chunker_version) == (3, 2)


def test_streams_of_one_path_are_independent(tmp_path):
    store = SourceStateStore(str(tmp_path))
    exchange = _stream(owner="convo", extract_mode="exchange", wing="chats")
    general = _stream(owner="convo", extract_mode="general", wing="chats")
    other_wing = _stream(owner="convo", extract_mode="exchange", wing="elsewhere")
    project = _stream(owner="project")

    store.mark_complete(exchange, chunk_total=3, normalize_version=3)

    assert store.get(exchange).complete
    assert store.get(general) is None
    assert store.get(other_wing) is None
    assert store.get(project) is None


def test_a_convo_reader_without_an_owner_finds_the_convo_writers_stream(tmp_path):
    # The convo miner's mined-set checks pass no owner; its writer passes "convo".
    assert source_stream("/c.txt", extract_mode="exchange", wing="w") == source_stream(
        "/c.txt", owner="convo", extract_mode="exchange", wing="w"
    )
    assert source_stream("/f.pdf", extract_mode="format") == source_stream(
        "/f.pdf", owner="format", extract_mode="format"
    )


def test_get_many_returns_only_the_requested_scope(tmp_path):
    store = SourceStateStore(str(tmp_path))
    for name in ("a", "b"):
        store.mark_complete(
            _stream(f"/{name}.txt", owner="convo", extract_mode="exchange", wing="w"),
            chunk_total=1,
        )
    store.mark_complete(
        _stream("/c.txt", owner="convo", extract_mode="general", wing="w"), chunk_total=1
    )

    heads = store.get_many(owner="convo", extract_mode="exchange", wing="w")
    assert set(heads) == {"/a.txt", "/b.txt"}
    assert set(
        store.get_many(owner="convo", extract_mode="exchange", wing="w", source_files=["/a.txt"])
    ) == {"/a.txt"}


def test_backfill_never_overwrites_a_record_a_writer_made(tmp_path):
    store = SourceStateStore(str(tmp_path))
    stream = _stream(owner="project")
    store.mark_pending(stream, source_mtime=1.0)

    store.backfill([(stream, {"chunk_total": 4, "source_mtime": 1.0, "normalize_version": 3})])
    assert store.get(stream).state == "pending"

    fresh = _stream("/notes/b.md", owner="project")
    store.backfill([(fresh, {"chunk_total": 4, "source_mtime": 2.0, "normalize_version": 3})])
    store.backfill([(fresh, {"chunk_total": 9, "source_mtime": 9.0, "normalize_version": 3})])
    head = store.get(fresh)
    assert head.complete and head.chunk_total == 4 and head.source_mtime == 2.0


def test_a_read_only_store_does_not_backfill(tmp_path):
    SourceStateStore(str(tmp_path)).mark_complete(_stream("/x", owner="project"), chunk_total=1)
    reader = SourceStateStore(str(tmp_path), read_only=True)

    reader.backfill([(_stream("/y", owner="project"), {"chunk_total": 1})])

    assert reader.get(_stream("/y", owner="project")) is None


def test_the_store_file_is_private(tmp_path):
    SourceStateStore(str(tmp_path)).mark_pending(_stream(owner="project"))
    if os.name == "posix":
        assert (tmp_path / SOURCE_STATE_FILENAME).stat().st_mode & 0o077 == 0
