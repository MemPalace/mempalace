from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import mempalace.backends.chroma as chroma


def _make_palace_db(path: Path) -> None:
    """Create a chromadb 1.5.x-shaped ``chroma.sqlite3`` with watermark rows."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE max_seq_id (segment_id TEXT PRIMARY KEY, seq_id BLOB NOT NULL)"
        )
        conn.execute(
            "INSERT INTO max_seq_id VALUES (?, ?)",
            ("3f1c4a2e-9b7d-4a3e-8c5f-1a2b3c4d5e6f", b"\x11\x11\x00\x00\x00\x01\x23"),
        )
        conn.execute(
            "INSERT INTO max_seq_id VALUES (?, ?)",
            ("ffffffff-0000-1111-2222-333344445555", b"\x11\x11\x00\x00\x00\x01\x24"),
        )
        conn.commit()
    finally:
        conn.close()


def _watermark_rows(path: Path) -> list[tuple[str, bytes]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT segment_id, seq_id FROM max_seq_id").fetchall()
    finally:
        conn.close()


def test_clear_segment_watermark_deletes_only_target_row(tmp_path: Path) -> None:
    db_path = tmp_path / "chroma.sqlite3"
    _make_palace_db(db_path)

    chroma._clear_segment_watermark(str(db_path), "3f1c4a2e-9b7d-4a3e-8c5f-1a2b3c4d5e6f")

    remaining = _watermark_rows(db_path)
    assert remaining == [("ffffffff-0000-1111-2222-333344445555", b"\x11\x11\x00\x00\x00\x01\x24")]


def test_clear_segment_watermark_unknown_segment_is_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "chroma.sqlite3"
    _make_palace_db(db_path)

    chroma._clear_segment_watermark(str(db_path), "no-such-segment")

    assert len(_watermark_rows(db_path)) == 2


def test_clear_segment_watermark_missing_table_is_silent(tmp_path: Path) -> None:
    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    # Must not raise — older chromadb versions may lack the table entirely.
    chroma._clear_segment_watermark(str(db_path), "some-segment")


def test_clear_segment_watermark_missing_db_is_noop_and_creates_nothing(tmp_path: Path) -> None:
    """A palace without ``chroma.sqlite3`` must not get an empty file created.

    ``quarantine_invalid_hnsw_metadata`` can run before Chroma has created the
    database; opening the writer would create a 0-byte file the quarantine
    never asked for.
    """
    db_path = tmp_path / "chroma.sqlite3"
    assert not db_path.exists()

    chroma._clear_segment_watermark(str(db_path), "some-segment")

    assert not db_path.exists()


def test_quarantine_stale_hnsw_clears_watermark(tmp_path: Path) -> None:
    """End-to-end quarantine: renaming the segment drops its watermark row."""
    db_path = tmp_path / "chroma.sqlite3"
    _make_palace_db(db_path)

    seg_id = "3f1c4a2e-9b7d-4a3e-8c5f-1a2b3c4d5e6f"
    seg_dir = tmp_path / seg_id
    seg_dir.mkdir()
    (seg_dir / "data_level0.bin").write_bytes(b"\x00" * 8)
    (seg_dir / "link_lists.bin").write_bytes(b"\x00" * 8)
    # Make the segment look stale vs. the sqlite mtime so the drift check fires.
    old = time.time() - 10 * 86400
    os.utime(seg_dir / "data_level0.bin", (old, old))

    moved = chroma.quarantine_stale_hnsw(str(tmp_path), stale_seconds=0)

    assert len(moved) == 1
    assert seg_id in os.path.basename(moved[0])
    remaining = _watermark_rows(db_path)
    assert remaining == [("ffffffff-0000-1111-2222-333344445555", b"\x11\x11\x00\x00\x00\x01\x24")]
