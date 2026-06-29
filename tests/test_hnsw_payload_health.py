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
