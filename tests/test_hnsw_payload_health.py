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


# --- quarantine must clear the segment's max_seq_id watermark (#2428) ---


def _palace_with_watermark(tmp_path, seg_id, seq_id=798, *, extra_rows=()):
    """A palace whose sqlite carries a real ``max_seq_id`` row for ``seg_id``."""
    import sqlite3

    palace = tmp_path / "palace"
    palace.mkdir()
    db_path = palace / "chroma.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE max_seq_id (segment_id TEXT PRIMARY KEY, seq_id INTEGER)")
        conn.execute("INSERT INTO max_seq_id VALUES (?, ?)", (seg_id, seq_id))
        for other_id, other_seq in extra_rows:
            conn.execute("INSERT INTO max_seq_id VALUES (?, ?)", (other_id, other_seq))
    return palace, db_path


def _watermarks(db_path):
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        return dict(conn.execute("SELECT segment_id, seq_id FROM max_seq_id").fetchall())


def test_quarantine_clears_the_quarantined_segment_watermark(tmp_path):
    """Renaming the segment without clearing its watermark truncates the rebuild.

    Chroma reads a surviving ``max_seq_id`` as "already synced through N" and
    replays nothing below it into the new, empty index, so the rebuild the
    quarantine exists to force produces a permanently short index while
    ``collection.count()`` still reports the full total.
    """
    seg_id = "11111111-2222-3333-4444-555555555555"
    metadata_seg = "99999999-8888-7777-6666-555555555555"
    palace, db_path = _palace_with_watermark(tmp_path, seg_id, extra_rows=((metadata_seg, 981),))
    _write_segment(
        palace / seg_id,
        data_size=100,
        link_size=int(100 * (_HNSW_LINK_TO_DATA_MAX_RATIO + 1)),
    )
    same_time = 1_700_000_000
    os.utime(db_path, (same_time, same_time))
    os.utime(palace / seg_id / "data_level0.bin", (same_time, same_time))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=999_999)

    assert len(moved) == 1
    # The quarantined segment's row is gone; the sibling metadata segment's is not.
    assert _watermarks(db_path) == {metadata_seg: 981}


def test_a_segment_left_in_place_keeps_its_watermark(tmp_path):
    """Clearing the watermark of a live segment would replay the whole queue."""
    seg_id = "11111111-2222-3333-4444-555555555555"
    palace, db_path = _palace_with_watermark(tmp_path, seg_id)
    _write_segment(palace / seg_id, data_size=100, link_size=100)
    same_time = 1_700_000_000
    os.utime(db_path, (same_time, same_time))
    os.utime(palace / seg_id / "data_level0.bin", (same_time, same_time))

    assert quarantine_stale_hnsw(str(palace), stale_seconds=999_999) == []
    assert _watermarks(db_path) == {seg_id: 798}


def test_the_metadata_quarantine_path_clears_the_watermark_too(tmp_path):
    """Both quarantine paths rename a segment, so both must clear its watermark."""
    from mempalace.backends.chroma import quarantine_invalid_hnsw_metadata

    seg_id = "11111111-2222-3333-4444-555555555555"
    palace, db_path = _palace_with_watermark(tmp_path, seg_id)
    state = _state(labels=100, total=120)
    state["label_to_id"] = {i: f"WRONG-{i}" for i in range(100)}
    _write_pickled_segment(palace / seg_id, state)

    assert len(quarantine_invalid_hnsw_metadata(str(palace))) == 1
    assert _watermarks(db_path) == {}


def test_quarantine_survives_a_palace_whose_sqlite_is_unreadable(tmp_path):
    """The rename is the safety action; the watermark is best effort."""
    from mempalace.backends.chroma import _clear_segment_watermark

    db_path = tmp_path / "chroma.sqlite3"
    db_path.write_text("sqlite placeholder")

    assert _clear_segment_watermark(str(db_path), "11111111-2222-3333-4444-555555555555") is False


def test_the_watermark_connection_is_closed(tmp_path, monkeypatch):
    """Quarantine runs before `PersistentClient` opens the palace.

    An open Python sqlite3 connection against a ChromaDB 1.5.x WAL-mode
    database leaves state that segfaults that call, which is why
    `_fix_blob_seq_ids` and the collection-type migration both close theirs
    explicitly. A bare `with sqlite3.connect(...)` commits but does not close.
    """
    import sqlite3

    from mempalace.backends import chroma

    seg_id = "11111111-2222-3333-4444-555555555555"
    _, db_path = _palace_with_watermark(tmp_path, seg_id)
    closed: list[bool] = []
    real_connect = sqlite3.connect

    class TrackingConnection:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, *args, **kwargs):
            return self._inner.execute(*args, **kwargs)

        def commit(self):
            return self._inner.commit()

        def close(self):
            closed.append(True)
            return self._inner.close()

    monkeypatch.setattr(
        chroma.sqlite3, "connect", lambda *a, **kw: TrackingConnection(real_connect(*a, **kw))
    )

    assert chroma._clear_segment_watermark(str(db_path), seg_id) is True
    assert closed == [True], "the connection outlived the call"
