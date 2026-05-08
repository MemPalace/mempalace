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
