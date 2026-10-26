import os
from pathlib import Path

from mempalace.backends.chroma import (
    _HNSW_LINK_TO_DATA_MAX_RATIO,
    _hnsw_link_to_data_ratio,
    _segment_appears_healthy,
    quarantine_stale_hnsw,
)


def _write_segment(
    seg_dir: Path,
    *,
    data_size: int = 100,
    link_size: int = 100,
    write_metadata: bool = True,
) -> None:
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "data_level0.bin").write_bytes(b"\0" * data_size)
    (seg_dir / "link_lists.bin").write_bytes(b"\0" * link_size)

    if write_metadata:
        # Enough bytes to pass the existing pickle envelope sniff-test:
        # starts with pickle protocol marker 0x80 and ends with STOP 0x2e.
        (seg_dir / "index_metadata.pickle").write_bytes(b"\x80" + b"x" * 16 + b"\x2e")


def test_hnsw_link_to_data_ratio_reports_payload_size_ratio(tmp_path):
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    _write_segment(seg_dir, data_size=100, link_size=250)

    assert _hnsw_link_to_data_ratio(str(seg_dir)) == 2.5


def test_segment_health_rejects_exploded_link_lists_even_with_valid_pickle(tmp_path):
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    _write_segment(
        seg_dir,
        data_size=100,
        link_size=int(100 * (_HNSW_LINK_TO_DATA_MAX_RATIO + 1)),
        write_metadata=True,
    )

    assert not _segment_appears_healthy(str(seg_dir))


def test_segment_health_keeps_reasonable_payload_with_valid_pickle(tmp_path):
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    _write_segment(
        seg_dir,
        data_size=100,
        link_size=int(100 * _HNSW_LINK_TO_DATA_MAX_RATIO),
        write_metadata=True,
    )

    assert _segment_appears_healthy(str(seg_dir))


def test_quarantine_catches_link_bloat_without_mtime_drift(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()

    db_path = palace / "chroma.sqlite3"
    db_path.write_text("sqlite placeholder")

    seg_dir = palace / "11111111-2222-3333-4444-555555555555"
    _write_segment(
        seg_dir,
        data_size=100,
        link_size=int(100 * (_HNSW_LINK_TO_DATA_MAX_RATIO + 1)),
        write_metadata=True,
    )

    # Make sqlite and HNSW mtimes identical. The old mtime-only gate would
    # skip this segment even though the payload is structurally corrupt.
    same_time = 1_700_000_000
    os.utime(db_path, (same_time, same_time))
    os.utime(seg_dir / "data_level0.bin", (same_time, same_time))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=999_999)

    assert len(moved) == 1
    assert not seg_dir.exists()

    moved_path = Path(moved[0])
    assert moved_path.exists()
    assert moved_path.name.startswith("11111111-2222-3333-4444-555555555555.drift-")


def test_quarantine_leaves_reasonable_payload_in_place(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()

    db_path = palace / "chroma.sqlite3"
    db_path.write_text("sqlite placeholder")

    seg_dir = palace / "11111111-2222-3333-4444-555555555555"
    _write_segment(
        seg_dir,
        data_size=100,
        link_size=100,
        write_metadata=True,
    )

    same_time = 1_700_000_000
    os.utime(db_path, (same_time, same_time))
    os.utime(seg_dir / "data_level0.bin", (same_time, same_time))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=999_999)

    assert moved == []
    assert seg_dir.exists()


def test_segment_health_accepts_zero_byte_link_lists_with_valid_pickle(tmp_path):
    """Regression #1716: an all-layer-0 HNSW index serializes an empty
    link_lists.bin (level-0 links live in data_level0.bin). With a complete
    index_metadata.pickle the persist finished, so the segment is healthy —
    not the partial-flush corruption a missing marker would imply."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"

    _write_segment(
        seg_dir,
        data_size=2_000,
        link_size=0,
        write_metadata=True,
    )

    assert _segment_appears_healthy(str(seg_dir))


def test_segment_health_rejects_zero_byte_link_lists_with_truncated_pickle(tmp_path):
    """An empty link_lists.bin with real payload but a truncated metadata
    envelope is a partial flush, not an all-layer-0 index — still rejected.
    The completion marker (intact pickle), not link_lists content, is what
    distinguishes the two."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"

    _write_segment(
        seg_dir,
        data_size=2_000,
        link_size=0,
        write_metadata=False,
    )
    # Persist started (0x80 head) but never wrote the STOP terminator.
    (seg_dir / "index_metadata.pickle").write_bytes(b"\x80" + b"x" * 20)

    assert not _segment_appears_healthy(str(seg_dir))


def test_quarantine_leaves_zero_byte_link_lists_with_valid_pickle(tmp_path):
    """Regression #1716: a stale all-layer-0 segment with a complete pickle is
    left in place. Quarantining it spawned the self-perpetuating loop — repair
    rebuilds the byte-identical empty-link_lists shape and it re-quarantines."""
    palace = tmp_path / "palace"
    palace.mkdir()

    db_path = palace / "chroma.sqlite3"
    db_path.write_text("sqlite placeholder")

    seg_dir = palace / "11111111-2222-3333-4444-555555555555"
    _write_segment(
        seg_dir,
        data_size=2_000,
        link_size=0,
        write_metadata=True,
    )

    hnsw_time = 1_700_000_000
    sqlite_time = hnsw_time + 1_000
    os.utime(seg_dir / "data_level0.bin", (hnsw_time, hnsw_time))
    os.utime(db_path, (sqlite_time, sqlite_time))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=300)

    assert moved == []
    assert seg_dir.exists()


# ── Regression cover for the #1710 cumulative-counter fix ─────────────
#
# _missing_dimensionality_appears_recoverable already requires
# total_elements_added >= len(id_to_label) rather than ==, which is what
# #1710 corrected: total_elements_added is hnswlib's cumulative add
# counter and includes elements later deleted or replaced, so it is
# legitimately greater on any palace that has removed a drawer.
#
# That fix landed without tests. These lock the behaviour in from both
# sides, because the failure it prevents is expensive and silent: a
# healthy index (183,009 consistent label pairs, 315 MB of vectors) gets
# renamed to .corrupt over a recoverable missing dimensionality, Chroma
# creates an empty replacement, and vector search quietly degrades.


def _write_pickled_segment(seg_dir: Path, state: dict, *, payload: int = 4096) -> None:
    import pickle

    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "data_level0.bin").write_bytes(b"\0" * payload)
    (seg_dir / "link_lists.bin").write_bytes(b"\0" * (payload // 8))
    with open(seg_dir / "index_metadata.pickle", "wb") as f:
        pickle.dump(state, f, pickle.HIGHEST_PROTOCOL)


def _state(*, labels: int, total: int, dimensionality=None) -> dict:
    return {
        "dimensionality": dimensionality,
        "total_elements_added": total,
        "max_seq_id": None,
        "id_to_label": {f"d-{i}": i for i in range(labels)},
        "label_to_id": {i: f"d-{i}" for i in range(labels)},
        "id_to_seq_id": {},
    }


def test_missing_dimensionality_recoverable_when_total_exceeds_labels(tmp_path):
    """Deletions make total_elements_added > label count. Still recoverable."""
    from mempalace.backends.chroma import _missing_dimensionality_appears_recoverable

    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    state = _state(labels=183_009, total=188_220)
    _write_pickled_segment(seg_dir, state)

    assert _missing_dimensionality_appears_recoverable(state, state["id_to_label"], str(seg_dir))


def test_missing_dimensionality_not_recoverable_when_total_below_labels(tmp_path):
    """total < labels is genuinely impossible - stay unrecoverable."""
    from mempalace.backends.chroma import _missing_dimensionality_appears_recoverable

    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    state = _state(labels=100, total=40)
    _write_pickled_segment(seg_dir, state)

    assert not _missing_dimensionality_appears_recoverable(
        state, state["id_to_label"], str(seg_dir)
    )


def test_quarantine_spares_healthy_index_with_cumulative_total(tmp_path):
    """End-to-end: a post-deletion segment must NOT be quarantined."""
    from mempalace.backends.chroma import quarantine_invalid_hnsw_metadata

    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    _write_pickled_segment(seg_dir, _state(labels=183_009, total=188_220))

    assert quarantine_invalid_hnsw_metadata(str(tmp_path)) == []
    assert seg_dir.is_dir(), "healthy index was quarantined"


def test_quarantine_still_catches_inconsistent_label_maps(tmp_path):
    """Accepting a cumulative total must not spare a truly broken index."""
    from mempalace.backends.chroma import quarantine_invalid_hnsw_metadata

    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    state = _state(labels=100, total=120)
    state["label_to_id"] = {i: f"WRONG-{i}" for i in range(100)}
    _write_pickled_segment(seg_dir, state)

    moved = quarantine_invalid_hnsw_metadata(str(tmp_path))
    assert len(moved) == 1
    assert not seg_dir.is_dir()


# --- quarantine must say when the WAL cannot replay what it renamed (#2510) ---


def _palace_with_purged_queue(tmp_path, *, total=8, purge_below=5):
    """A real chromadb palace whose embeddings_queue has been purged, as 1.5.x does.

    Every handle is released before returning. ``del client`` is enough on
    POSIX, where an open file does not stop its directory from being renamed,
    but not on Windows: a caller that quarantines the segment directory gets
    ``PermissionError: [WinError 5]`` from ``os.rename`` and measures the
    failure path instead of the code under test. Use the same close sequence
    the repair path uses, and close the sqlite connection too, since
    ``with sqlite3.connect(...)`` commits but does not close.
    """
    import gc
    import sqlite3
    from contextlib import closing

    import chromadb

    from mempalace.backends.chroma import _clear_chroma_system_cache, _close_client

    palace = tmp_path / "palace"
    client = chromadb.PersistentClient(path=str(palace))
    collection = client.get_or_create_collection("mempalace_drawers")
    collection.add(
        ids=[f"id{i}" for i in range(total)],
        embeddings=[[float(i), 0.0, 1.0] for i in range(total)],
        documents=[f"doc {i}" for i in range(total)],
    )
    del collection
    _close_client(client)
    del client
    _clear_chroma_system_cache()
    gc.collect()

    db_path = palace / "chroma.sqlite3"
    with closing(sqlite3.connect(db_path)) as conn:
        if purge_below is not None:
            conn.execute("DELETE FROM embeddings_queue WHERE seq_id < ?", (purge_below,))
            conn.commit()
        segment_id = conn.execute(
            "SELECT id FROM segments WHERE type LIKE '%hnsw%' OR scope = 'VECTOR' LIMIT 1"
        ).fetchone()[0]
    return palace, str(db_path), segment_id


def test_a_purged_queue_reports_what_it_cannot_replay(tmp_path):
    """The rebuild replays from the queue, and chromadb purges it.

    Everything written below the purge watermark is absent from the rebuilt
    index while its metadata row survives, which is why the count and an
    unfiltered search both keep looking healthy.
    """
    from mempalace.backends.chroma import _wal_unreplayable_count

    _, db_path, segment_id = _palace_with_purged_queue(tmp_path, total=8, purge_below=5)

    assert _wal_unreplayable_count(db_path, segment_id) == 4


def test_an_intact_queue_reports_nothing_lost(tmp_path):
    from mempalace.backends.chroma import _wal_unreplayable_count

    _, db_path, segment_id = _palace_with_purged_queue(tmp_path, total=8, purge_below=None)

    assert _wal_unreplayable_count(db_path, segment_id) == 0


def test_an_unmeasurable_palace_reports_none_not_zero(tmp_path):
    """A failure must not read as "nothing was lost"."""
    from mempalace.backends.chroma import _wal_unreplayable_count

    db_path = tmp_path / "chroma.sqlite3"
    db_path.write_text("sqlite placeholder")

    assert _wal_unreplayable_count(str(db_path), "11111111-2222-3333-4444-555555555555") is None


def test_quarantine_reports_the_unreplayable_remainder(tmp_path, caplog):
    """The wiring, not just the measurement: the rename must say what it costs."""
    import logging

    palace, db_path, segment_id = _palace_with_purged_queue(tmp_path, total=8, purge_below=5)
    seg_dir = palace / segment_id
    _write_segment(
        seg_dir,
        data_size=100,
        link_size=int(100 * (_HNSW_LINK_TO_DATA_MAX_RATIO + 1)),
    )
    same_time = 1_700_000_000
    os.utime(db_path, (same_time, same_time))
    os.utime(seg_dir / "data_level0.bin", (same_time, same_time))

    with caplog.at_level(logging.ERROR, logger="mempalace.backends.chroma"):
        moved = quarantine_stale_hnsw(str(palace), stale_seconds=999_999)

    assert len(moved) == 1, [record.getMessage() for record in caplog.records]
    assert any(
        "can no longer replay" in record.getMessage() and "4 embedding" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_blob_seq_ids_are_not_counted_as_unreplayable(tmp_path):
    """0.6.x wrote seq_id as a BLOB, and SQLite orders every blob after every integer.

    Counting those rows would report an intact queue as a total loss.
    """
    import sqlite3

    from mempalace.backends.chroma import _wal_unreplayable_count

    _, db_path, segment_id = _palace_with_purged_queue(tmp_path, total=8, purge_below=None)
    assert _wal_unreplayable_count(db_path, segment_id) == 0

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT segment_id, embedding_id, created_at FROM embeddings LIMIT 1"
        ).fetchone()
        conn.execute(
            "INSERT INTO embeddings (segment_id, embedding_id, seq_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (row[0], "legacy-blob-row", (12345).to_bytes(8, "big"), row[2]),
        )

    assert _wal_unreplayable_count(db_path, segment_id) == 0
