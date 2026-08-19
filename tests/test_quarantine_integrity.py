"""Tests for the deep integrity gate added to quarantine_stale_hnsw.

Verifies that a segment which passes the surface sniff-test
(_segment_appears_healthy returns True) is still quarantined when the
deep integrity checks reveal corruption, and that the flush-lag pass is
preserved when both checks are clean.
"""

import os
import time

import mempalace.backends.chroma as chroma_mod
from mempalace.backends.chroma import quarantine_stale_hnsw


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_segment(
    palace_dir: str,
    seg_name: str,
    *,
    data_size: int = 2048,
    stale_seconds: float = 400.0,
) -> str:
    """Build a minimal fake HNSW segment that passes _segment_appears_healthy.

    Creates:
    * data_level0.bin  — ``data_size`` bytes of null payload (> 1024 threshold)
    * link_lists.bin   — 8 non-zero bytes (non-empty, small ratio to data)
    * index_metadata.pickle — valid magic bytes (0x80 ... 0x2e) with enough
                              padding to pass the 16-byte size floor

    Sets data_level0.bin mtime ``stale_seconds`` in the past relative to
    chroma.sqlite3's mtime so the mtime gap check fires.

    Returns the full path to the segment directory.
    """
    seg_dir = os.path.join(palace_dir, seg_name)
    os.makedirs(seg_dir, exist_ok=True)

    # data_level0.bin — oversized enough to pass the missing-metadata floor
    data_path = os.path.join(seg_dir, "data_level0.bin")
    with open(data_path, "wb") as f:
        f.write(b"\x00" * data_size)

    # link_lists.bin — small relative to data so ratio check passes
    link_path = os.path.join(seg_dir, "link_lists.bin")
    with open(link_path, "wb") as f:
        f.write(b"\x01" * 8)

    # index_metadata.pickle — magic bytes only (no real structure needed for
    # _segment_appears_healthy's sniff-test: byte0==0x80, last-byte==0x2e,
    # total size >= 16 bytes).
    meta_path = os.path.join(seg_dir, "index_metadata.pickle")
    payload = b"\x80" + b"\x00" * 30 + b"\x2e"
    with open(meta_path, "wb") as f:
        f.write(payload)

    # Stamp chroma.sqlite3 as recent, data_level0.bin as stale.
    now = time.time()
    db_path = os.path.join(palace_dir, "chroma.sqlite3")
    # Touch chroma.sqlite3 with current time (already exists from caller, or
    # created here).
    with open(db_path, "ab"):
        pass
    os.utime(db_path, (now, now))
    old_time = now - stale_seconds - 10  # comfortably past the threshold
    os.utime(data_path, (old_time, old_time))

    return seg_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_corrupt_sqlite_is_quarantined_not_flushlag(tmp_path, monkeypatch):
    """A segment that passes the sniff-test is quarantined when sqlite_integrity_errors
    returns a non-empty list — i.e. SQLite corruption takes precedence over flush-lag.
    """
    palace_dir = str(tmp_path)
    seg_name = "aabbccdd-0000-1111-2222-333333333333"
    seg_dir = _make_fake_segment(palace_dir, seg_name, stale_seconds=400.0)

    # Monkeypatch the integrity-check shim so it reports corruption.
    monkeypatch.setattr(chroma_mod, "_sqlite_integrity_errors", lambda _path: ["malformed"])

    # Count helpers: return matching counts so only the SQLite signal fires.
    monkeypatch.setattr(chroma_mod, "_hnsw_element_count", lambda _palace, _seg: 100)
    monkeypatch.setattr(
        chroma_mod, "_sqlite_embedding_count_for_segment", lambda _palace, _seg: 100
    )

    moved = quarantine_stale_hnsw(palace_dir, stale_seconds=300.0)

    # The segment must have been renamed aside (quarantined).
    assert len(moved) == 1, f"Expected one quarantined segment, got: {moved}"
    assert not os.path.isdir(seg_dir), "Original segment dir should not exist after quarantine"
    assert os.path.isdir(moved[0]), "Quarantined rename target should exist on disk"
    assert ".drift-" in moved[0], "Quarantine rename should include .drift- marker"


def test_clean_store_keeps_flushlag_pass(tmp_path, monkeypatch):
    """A segment that passes the sniff-test AND the deep integrity gate is left
    in place — existing flush-lag behaviour is preserved.
    """
    palace_dir = str(tmp_path)
    seg_name = "aabbccdd-0000-1111-2222-444444444444"
    seg_dir = _make_fake_segment(palace_dir, seg_name, stale_seconds=400.0)

    # Both integrity signals are clean.
    monkeypatch.setattr(chroma_mod, "_sqlite_integrity_errors", lambda _path: [])
    # Counts match — no divergence.
    monkeypatch.setattr(chroma_mod, "_hnsw_element_count", lambda _palace, _seg: 50)
    monkeypatch.setattr(chroma_mod, "_sqlite_embedding_count_for_segment", lambda _palace, _seg: 50)

    moved = quarantine_stale_hnsw(palace_dir, stale_seconds=300.0)

    # Segment must NOT have been quarantined.
    assert moved == [], f"Expected no quarantined segments, got: {moved}"
    assert os.path.isdir(seg_dir), "Segment dir should still exist — flush-lag pass preserved"


def test_count_divergence_triggers_quarantine(tmp_path, monkeypatch):
    """A segment with clean SQLite but severely diverged HNSW/SQLite counts is
    quarantined — count divergence alone is sufficient to reject the flush-lag pass.
    """
    palace_dir = str(tmp_path)
    seg_name = "aabbccdd-0000-1111-2222-555555555555"
    seg_dir = _make_fake_segment(palace_dir, seg_name, stale_seconds=400.0)

    monkeypatch.setattr(chroma_mod, "_sqlite_integrity_errors", lambda _path: [])
    # HNSW count is tiny, SQLite count is huge — far beyond any tolerance.
    monkeypatch.setattr(chroma_mod, "_hnsw_element_count", lambda _palace, _seg: 100)
    monkeypatch.setattr(
        chroma_mod, "_sqlite_embedding_count_for_segment", lambda _palace, _seg: 200_000
    )

    moved = quarantine_stale_hnsw(palace_dir, stale_seconds=300.0)

    assert len(moved) == 1, f"Expected one quarantined segment, got: {moved}"
    assert not os.path.isdir(seg_dir)


def test_unknown_counts_do_not_block_flushlag(tmp_path, monkeypatch):
    """When count helpers return None (e.g. fresh segment, no pickle yet), the
    count divergence check is inconclusive and must NOT block the flush-lag pass.
    The segment should be left in place provided SQLite is clean.
    """
    palace_dir = str(tmp_path)
    seg_name = "aabbccdd-0000-1111-2222-666666666666"
    seg_dir = _make_fake_segment(palace_dir, seg_name, stale_seconds=400.0)

    monkeypatch.setattr(chroma_mod, "_sqlite_integrity_errors", lambda _path: [])
    # Both counts unknown — no divergence signal possible.
    monkeypatch.setattr(chroma_mod, "_hnsw_element_count", lambda _palace, _seg: None)
    monkeypatch.setattr(
        chroma_mod, "_sqlite_embedding_count_for_segment", lambda _palace, _seg: None
    )

    moved = quarantine_stale_hnsw(palace_dir, stale_seconds=300.0)

    assert moved == [], f"Unknown counts should not trigger quarantine, got: {moved}"
    assert os.path.isdir(seg_dir)


def test_unreadable_sqlite_does_not_trigger_quarantine(tmp_path):
    """Regression: when chroma.sqlite3 cannot be opened (placeholder file,
    locked, mid-checkpoint, or genuinely not SQLite), the integrity probe
    is "unknown" — not "corruption". The shim must filter the couldn't-open
    signal out so the flush-lag pass is preserved.

    Locks down the fix for a false-positive quarantine on #1716's all-layer-0
    regression fixture (whose ``chroma.sqlite3`` is a placeholder string,
    not a real SQLite file). Before the fix, the shim passed
    ``"PRAGMA quick_check failed: file is not a database"`` straight through
    to the quarantine gate, which then quarantined a healthy segment on
    every CI run.

    Uses a real placeholder file (no monkeypatching) so the full chain is
    exercised: real ``repair.sqlite_integrity_errors`` returns the
    couldn't-open shape, the shim filters it, the quarantine gate stays
    open.
    """
    palace_dir = str(tmp_path)
    seg_name = "aabbccdd-0000-1111-2222-777777777777"
    seg_dir = _make_fake_segment(palace_dir, seg_name, stale_seconds=400.0)

    # Overwrite chroma.sqlite3 with a placeholder — NOT real SQLite.
    # _make_fake_segment already touched the file; replace with junk content.
    db_path = os.path.join(palace_dir, "chroma.sqlite3")
    with open(db_path, "w") as f:
        f.write("sqlite placeholder")
    # Re-stamp mtime so the gap check still fires (write may have changed it).
    now = time.time()
    os.utime(db_path, (now, now))

    moved = quarantine_stale_hnsw(palace_dir, stale_seconds=300.0)

    # Couldn't-open is NOT corruption — leave the segment alone.
    assert moved == [], f"Unreadable SQLite should not trigger quarantine, got: {moved}"
    assert os.path.isdir(seg_dir), "Segment should be left in place — flush-lag pass preserved"
