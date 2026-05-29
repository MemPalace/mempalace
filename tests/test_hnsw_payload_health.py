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


def test_segment_health_rejects_zero_byte_link_lists_with_payload(tmp_path):
    """Regression #1457: real HNSW payload with empty link_lists.bin is corrupt."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"

    _write_segment(
        seg_dir,
        data_size=2_000,
        link_size=0,
        write_metadata=True,
    )

    assert not _segment_appears_healthy(str(seg_dir))


def test_quarantine_catches_zero_byte_link_lists_when_stale(tmp_path):
    """Regression #1457: stale segments with empty link_lists.bin are quarantined."""
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

    assert len(moved) == 1
    assert not seg_dir.exists()

    moved_path = Path(moved[0])
    assert moved_path.exists()
    assert moved_path.name.startswith("11111111-2222-3333-4444-555555555555.drift-")


# ── Fix B: deferred-persist (incremental mine) segments (#1579) ────────


def test_deferred_persist_segment_not_quarantined(tmp_path):
    """#1579: data_level0 present, link_lists empty, NO pickle → healthy deferred-persist.

    Small/incremental mines never trigger hnsw:sync_threshold, so
    index_metadata.pickle is never written. That is the NORMAL state,
    not corruption. _segment_appears_healthy must return True for it.
    """
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "data_level0.bin").write_bytes(b"\0" * 167_600)
    (seg_dir / "link_lists.bin").write_bytes(b"")
    # NO index_metadata.pickle written

    assert _segment_appears_healthy(str(seg_dir)), (
        "deferred-persist segment (data present, link_lists empty, no pickle) "
        "must be treated as healthy (fix #1579)"
    )

    # Also verify quarantine_stale_hnsw leaves it in place even when sqlite
    # mtime is arbitrarily newer than the HNSW files.
    palace = tmp_path / "palace"
    palace.mkdir()
    db_path = palace / "chroma.sqlite3"
    db_path.write_text("sqlite placeholder")

    seg_dir2 = palace / "22222222-3333-4444-5555-666666666666"
    seg_dir2.mkdir(parents=True, exist_ok=True)
    (seg_dir2 / "data_level0.bin").write_bytes(b"\0" * 167_600)
    (seg_dir2 / "link_lists.bin").write_bytes(b"")

    hnsw_time = 1_700_000_000
    sqlite_time = hnsw_time + 9_999
    os.utime(seg_dir2 / "data_level0.bin", (hnsw_time, hnsw_time))
    os.utime(db_path, (sqlite_time, sqlite_time))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=300)
    assert moved == [], (
        "deferred-persist segment must not be quarantined even when stale_seconds exceeded"
    )
    assert seg_dir2.exists()


def test_bloated_link_lists_still_quarantined(tmp_path):
    """#344 regression guard: bloated link_lists (ratio > max) must still quarantine.

    The deferred-persist fix must not loosen the #344 bloat detection path.
    link_lists >> data_level0 is always structurally corrupt regardless of
    whether pickle is present.
    """
    palace = tmp_path / "palace"
    palace.mkdir()

    db_path = palace / "chroma.sqlite3"
    db_path.write_text("sqlite placeholder")

    seg_dir = palace / "11111111-2222-3333-4444-555555555555"
    seg_dir.mkdir(parents=True, exist_ok=True)
    data_size = 1_000
    link_size = int(data_size * (_HNSW_LINK_TO_DATA_MAX_RATIO + 1))
    (seg_dir / "data_level0.bin").write_bytes(b"\0" * data_size)
    (seg_dir / "link_lists.bin").write_bytes(b"\0" * link_size)
    # No pickle — deferred-persist appearance, but link_lists is bloated

    same_time = 1_700_000_000
    os.utime(db_path, (same_time, same_time))
    os.utime(seg_dir / "data_level0.bin", (same_time, same_time))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=999_999)
    assert len(moved) == 1, "bloated link_lists must still be quarantined even without pickle"
    assert not seg_dir.exists()


def test_corrupt_pickle_still_quarantined(tmp_path):
    """#1579: pickle present but bad magic bytes → still quarantined.

    The deferred-persist fix applies ONLY when pickle is absent.
    A present-but-corrupt pickle is genuine corruption.
    """
    palace = tmp_path / "palace"
    palace.mkdir()

    db_path = palace / "chroma.sqlite3"
    db_path.write_text("sqlite placeholder")

    seg_dir = palace / "11111111-2222-3333-4444-555555555555"
    _write_segment(
        seg_dir,
        data_size=2_000,
        link_size=100,
        write_metadata=False,
    )
    # Write a pickle with invalid magic bytes
    (seg_dir / "index_metadata.pickle").write_bytes(b"\xff" + b"x" * 16 + b"\xff")

    hnsw_time = 1_700_000_000
    sqlite_time = hnsw_time + 1_000
    os.utime(seg_dir / "data_level0.bin", (hnsw_time, hnsw_time))
    os.utime(db_path, (sqlite_time, sqlite_time))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=300)
    assert len(moved) == 1, "corrupt pickle must still be quarantined"
    assert not seg_dir.exists()


def test_partial_flush_missing_pickle_nonempty_links_still_quarantined(tmp_path):
    """#1655 Gemini review: non-empty link_lists.bin + no pickle = interrupted partial flush.

    chromadb writes link_lists.bin before index_metadata.pickle. A segment with
    data above the floor, a non-empty (but not bloated) link_lists.bin, and no
    pickle is an interrupted partial flush — NOT the benign deferred-persist state.
    It must be quarantined.
    """
    palace = tmp_path / "palace"
    palace.mkdir()

    db_path = palace / "chroma.sqlite3"
    db_path.write_text("sqlite placeholder")

    seg_dir = palace / "11111111-2222-3333-4444-555555555555"
    seg_dir.mkdir(parents=True, exist_ok=True)
    # data above floor, link_lists non-empty but ratio << max (not a bloat hit)
    data_size = 2_000
    link_size = 200  # ratio = 0.1, well below _HNSW_LINK_TO_DATA_MAX_RATIO
    (seg_dir / "data_level0.bin").write_bytes(b"\0" * data_size)
    (seg_dir / "link_lists.bin").write_bytes(b"\0" * link_size)
    # NO index_metadata.pickle — simulates interrupted flush

    assert not _segment_appears_healthy(str(seg_dir)), (
        "partial-flush segment (non-empty link_lists, no pickle) must be unhealthy"
    )

    hnsw_time = 1_700_000_000
    sqlite_time = hnsw_time + 1_000
    os.utime(seg_dir / "data_level0.bin", (hnsw_time, hnsw_time))
    os.utime(db_path, (sqlite_time, sqlite_time))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=300)
    assert len(moved) == 1, "partial-flush segment must be quarantined"
    assert not seg_dir.exists()

    moved_path = Path(moved[0])
    assert moved_path.exists()
    assert moved_path.name.startswith("11111111-2222-3333-4444-555555555555.drift-")


def test_healthy_persisted_segment_unaffected(tmp_path):
    """#1579: data + non-zero link_lists + valid pickle → healthy, not quarantined."""
    palace = tmp_path / "palace"
    palace.mkdir()

    db_path = palace / "chroma.sqlite3"
    db_path.write_text("sqlite placeholder")

    seg_dir = palace / "11111111-2222-3333-4444-555555555555"
    _write_segment(
        seg_dir,
        data_size=2_000,
        link_size=500,
        write_metadata=True,
    )

    hnsw_time = 1_700_000_000
    sqlite_time = hnsw_time + 9_999
    os.utime(seg_dir / "data_level0.bin", (hnsw_time, hnsw_time))
    os.utime(db_path, (sqlite_time, sqlite_time))

    assert _segment_appears_healthy(str(seg_dir))

    moved = quarantine_stale_hnsw(str(palace), stale_seconds=300)
    assert moved == [], "healthy persisted segment must not be quarantined"
    assert seg_dir.exists()
