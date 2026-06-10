"""Tests for HNSW binary header sanity checking (#1595).

The binary header is a secondary validation layer that detects when
the hnswlib file is corrupt (e.g., by chroma-core/chroma#4460's
type-confusion bug). When max_elements or cur_element_count land in
the trillions, the segment must be rejected as corrupt.
"""

from __future__ import annotations

import os
import pickle
import sqlite3
import struct
from pathlib import Path

from mempalace.backends.chroma import (
    _SANE_ELEMENT_CAP,
    _read_hnsw_binary_header,
    _segment_appears_healthy,
    hnsw_capacity_status,
)


COLLECTION = "mempalace_drawers"


# ── Fixtures ──────────────────────────────────────────────────────────


def _write_binary_header(seg_dir: Path, max_elements: int, cur_element_count: int) -> None:
    """Write a minimal hnswlib binary header to data_level0.bin.

    hnswlib header layout (64-bit):
    - bytes 0..7: offsetLevel0
    - bytes 8..15: max_elements
    - bytes 16..23: cur_element_count
    - rest: padding/other fields
    """
    seg_dir.mkdir(parents=True, exist_ok=True)
    header = struct.pack(
        "<QQQ",
        0,  # offsetLevel0
        max_elements,
        cur_element_count,
    )
    # Pad to at least 48 bytes as the real hnswlib header requires.
    header = header + b"\x00" * (48 - len(header))
    (seg_dir / "data_level0.bin").write_bytes(header)


def _write_segment_with_header(
    seg_dir: Path,
    *,
    max_elements: int = 1000,
    cur_element_count: int = 950,
    write_payload: bool = True,
    write_metadata: bool = True,
) -> None:
    """Write a complete HNSW segment with binary header and optional payload."""
    seg_dir.mkdir(parents=True, exist_ok=True)

    # Binary header
    _write_binary_header(seg_dir, max_elements, cur_element_count)

    # Payload files
    if write_payload:
        (seg_dir / "link_lists.bin").write_bytes(b"\x00" * 100)

    # Metadata pickle
    if write_metadata:
        (seg_dir / "index_metadata.pickle").write_bytes(b"\x80" + b"x" * 16 + b"\x2e")


def _seed_chroma_db_for_capacity_test(
    palace: str,
    sqlite_count: int,
    segment_id: str,
) -> None:
    """Create a minimal chroma.sqlite3 for hnsw_capacity_status tests."""
    db_path = os.path.join(palace, "chroma.sqlite3")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
            CREATE TABLE collection_metadata (
                collection_id TEXT REFERENCES collections(id) ON DELETE CASCADE,
                key TEXT NOT NULL,
                str_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER,
                PRIMARY KEY (collection_id, key)
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                scope TEXT NOT NULL
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL,
                seq_id BLOB NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        col_id = "col-test"
        meta_seg = "seg-meta"
        conn.execute("INSERT INTO collections (id, name) VALUES (?, ?)", (col_id, COLLECTION))
        conn.execute(
            "INSERT INTO segments (id, collection, scope) VALUES (?, ?, 'VECTOR')",
            (segment_id, col_id),
        )
        conn.execute(
            "INSERT INTO segments (id, collection, scope) VALUES (?, ?, 'METADATA')",
            (meta_seg, col_id),
        )
        for i in range(sqlite_count):
            conn.execute(
                """INSERT INTO embeddings (id, segment_id, embedding_id, seq_id)
                   VALUES (?, ?, ?, ?)""",
                (i + 1, segment_id, f"d-{i}", b"\x00\x00\x00\x00\x00\x00\x00\x01"),
            )
        conn.commit()
    finally:
        conn.close()


def _write_pickle(palace: str, segment_id: str, hnsw_count: int) -> None:
    """Write an index_metadata.pickle matching chromadb 1.5.x's shape."""
    seg_dir = os.path.join(palace, segment_id)
    os.makedirs(seg_dir, exist_ok=True)
    pickle_path = os.path.join(seg_dir, "index_metadata.pickle")
    state = {
        "dimensionality": 384,
        "total_elements_added": hnsw_count,
        "max_seq_id": None,
        "id_to_label": {f"d-{i}": i for i in range(hnsw_count)},
        "label_to_id": {i: f"d-{i}" for i in range(hnsw_count)},
        "id_to_seq_id": {},
    }
    with open(pickle_path, "wb") as f:
        pickle.dump(state, f, pickle.HIGHEST_PROTOCOL)


# ── _read_hnsw_binary_header ──────────────────────────────────────────


def test_read_hnsw_binary_header_parses_valid_header(tmp_path):
    """Verify parsing of a valid hnswlib binary header."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    _write_binary_header(seg_dir, max_elements=1024, cur_element_count=768)

    hdr = _read_hnsw_binary_header(str(seg_dir))

    assert hdr is not None
    assert hdr["max_elements"] == 1024
    assert hdr["cur_element_count"] == 768
    assert hdr["offset_level0"] == 0


def test_read_hnsw_binary_header_returns_none_on_missing_file(tmp_path):
    """Absent data_level0.bin returns None."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    seg_dir.mkdir()

    hdr = _read_hnsw_binary_header(str(seg_dir))

    assert hdr is None


def test_read_hnsw_binary_header_returns_none_on_truncated_file(tmp_path):
    """File < 24 bytes (minimum header size) returns None."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    seg_dir.mkdir()
    (seg_dir / "data_level0.bin").write_bytes(b"\x00" * 20)

    hdr = _read_hnsw_binary_header(str(seg_dir))

    assert hdr is None


def test_read_hnsw_binary_header_handles_corrupt_values(tmp_path):
    """Type-confusion corruption (trillions) is still read correctly."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    seg_dir.mkdir()

    # Simulate the type-confusion bug: a 64-bit float interpreted as int64
    # can land in the trillions. E.g., 1e18 is easily representable.
    corrupt_max = 2**52  # ~4.5e15, in the trillions
    corrupt_cur = 2**50  # ~1e15, also in the trillions

    header = struct.pack("<QQQ", 0, corrupt_max, corrupt_cur)
    header = header + b"\x00" * (48 - len(header))
    (seg_dir / "data_level0.bin").write_bytes(header)

    hdr = _read_hnsw_binary_header(str(seg_dir))

    assert hdr is not None
    assert hdr["max_elements"] == corrupt_max
    assert hdr["cur_element_count"] == corrupt_cur


# ── _segment_appears_healthy integration ──────────────────────────────


def test_corrupt_header_fails_segment_health(tmp_path):
    """Segment with trillion-element header fails _segment_appears_healthy."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    _write_segment_with_header(
        seg_dir,
        max_elements=_SANE_ELEMENT_CAP + 1,
        cur_element_count=100,
        write_payload=True,
        write_metadata=True,
    )

    assert not _segment_appears_healthy(str(seg_dir))


def test_corrupt_header_fails_health_on_cur_element_count(tmp_path):
    """Segment with corrupt cur_element_count (but sane max) fails health."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    _write_segment_with_header(
        seg_dir,
        max_elements=1000,
        cur_element_count=_SANE_ELEMENT_CAP + 1,
        write_payload=True,
        write_metadata=True,
    )

    assert not _segment_appears_healthy(str(seg_dir))


def test_sane_header_passes_segment_health(tmp_path):
    """Normal header values pass health check."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    _write_segment_with_header(
        seg_dir,
        max_elements=16384,
        cur_element_count=12000,
        write_payload=True,
        write_metadata=True,
    )

    assert _segment_appears_healthy(str(seg_dir))


def test_segment_health_missing_data_level0_still_checks_other_fields(tmp_path):
    """Segment without data_level0.bin still passes if payload/metadata are OK."""
    seg_dir = tmp_path / "11111111-2222-3333-4444-555555555555"
    seg_dir.mkdir()
    (seg_dir / "link_lists.bin").write_bytes(b"\x00" * 100)
    (seg_dir / "index_metadata.pickle").write_bytes(b"\x80" + b"x" * 16 + b"\x2e")

    # No data_level0.bin, but _read_hnsw_binary_header returns None,
    # which doesn't trip the corrupt check.
    assert _segment_appears_healthy(str(seg_dir))


# ── hnsw_capacity_status integration ───────────────────────────────────


def test_hnsw_capacity_status_detects_corrupt_binary_header(tmp_path):
    """hnsw_capacity_status returns status='corrupt' when binary header is astronomical."""
    seg = "seg-corrupt"
    _seed_chroma_db_for_capacity_test(str(tmp_path), sqlite_count=1000, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=950)

    # Overwrite the pickle's segment dir with corrupt binary header.
    seg_dir = tmp_path / seg
    _write_binary_header(
        seg_dir,
        max_elements=_SANE_ELEMENT_CAP + 1,
        cur_element_count=100,
    )

    info = hnsw_capacity_status(str(tmp_path), COLLECTION)

    assert info["status"] == "corrupt"
    assert info["diverged"] is True
    assert "astronomical values" in info["message"]
    assert "type-confusion" in info["message"]
    assert "chroma-core/chroma#4460" in info["message"]
    assert "mempalace repair" in info["message"]
    assert info["binary_max_elements"] == _SANE_ELEMENT_CAP + 1


def test_hnsw_capacity_status_detects_corrupt_cur_element_count(tmp_path):
    """Corrupt cur_element_count also triggers corruption detection."""
    seg = "seg-corrupt-cur"
    _seed_chroma_db_for_capacity_test(str(tmp_path), sqlite_count=1000, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=950)

    seg_dir = tmp_path / seg
    _write_binary_header(
        seg_dir,
        max_elements=1024,
        cur_element_count=_SANE_ELEMENT_CAP + 1,
    )

    info = hnsw_capacity_status(str(tmp_path), COLLECTION)

    assert info["status"] == "corrupt"
    assert info["diverged"] is True
    assert info["binary_cur_elements"] == _SANE_ELEMENT_CAP + 1


def test_hnsw_capacity_status_normal_header_does_not_flag_ok_status(tmp_path):
    """Normal binary header values don't override the divergence check."""
    seg = "seg-normal"
    _seed_chroma_db_for_capacity_test(str(tmp_path), sqlite_count=1000, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=950)

    seg_dir = tmp_path / seg
    _write_binary_header(
        seg_dir,
        max_elements=2048,
        cur_element_count=950,
    )

    info = hnsw_capacity_status(str(tmp_path), COLLECTION)

    assert info["status"] == "ok"
    assert info["diverged"] is False
    assert info["binary_max_elements"] == 2048
    assert info["binary_cur_elements"] == 950


def test_hnsw_capacity_status_includes_binary_fields_when_present(tmp_path):
    """Binary header fields are always populated when data_level0.bin exists."""
    seg = "seg-with-header"
    _seed_chroma_db_for_capacity_test(str(tmp_path), sqlite_count=100, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=95)

    seg_dir = tmp_path / seg
    _write_binary_header(seg_dir, max_elements=256, cur_element_count=95)

    info = hnsw_capacity_status(str(tmp_path), COLLECTION)

    assert "binary_max_elements" in info
    assert "binary_cur_elements" in info
    assert info["binary_max_elements"] == 256
    assert info["binary_cur_elements"] == 95


def test_hnsw_capacity_status_ok_when_no_binary_header(tmp_path):
    """Missing data_level0.bin doesn't fail capacity check (backward compat)."""
    seg = "seg-no-bin"
    _seed_chroma_db_for_capacity_test(str(tmp_path), sqlite_count=100, segment_id=seg)
    _write_pickle(str(tmp_path), seg, hnsw_count=95)

    # Don't write any binary header file.
    info = hnsw_capacity_status(str(tmp_path), COLLECTION)

    assert info["status"] == "ok"
    assert info["diverged"] is False
    assert "binary_max_elements" not in info
    assert "binary_cur_elements" not in info
