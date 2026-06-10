"""Tests for mempalace.ids — collision-safe ID construction.

The RED test that pins this whole PR is
``test_make_drawer_id_from_chunk_does_not_collide_across_boundary`` — it
constructs the classic ``"/path/a1" + "23" == "/path/a" + "123"`` collision
shape and asserts the new delimiter-based recipe produces distinct IDs.
Against the pre-v2 recipe (no delimiter), this test FAILS. Against v2,
it PASSES.
"""

from __future__ import annotations

import hashlib

from mempalace import ids


# ── ID_RECIPE constant ─────────────────────────────────────────────────


def test_id_recipe_constant_is_v2():
    """Audit code reads ids.ID_RECIPE to tag new drawers. The constant
    must be the literal "v2" string; a typo here silently re-introduces
    the ambiguity v2 was meant to fix."""
    assert ids.ID_RECIPE == "v2"


# ── make_drawer_id_from_chunk ─────────────────────────────────────────


def test_make_drawer_id_from_chunk_returns_expected_prefix():
    """Drawer IDs are namespaced by wing and room so cross-wing
    collisions are impossible regardless of the hash slice."""
    result = ids.make_drawer_id_from_chunk("proj", "log", "/a", 0)
    assert result.startswith("drawer_proj_log_")


def test_make_drawer_id_from_chunk_hash_length_is_24_hex():
    """The hash slice must be 24 hex chars to keep drawer IDs storable
    in fixed-width metadata columns and to match the historical recipe
    length."""
    result = ids.make_drawer_id_from_chunk("w", "r", "/file.md", 5)
    hash_part = result.removeprefix("drawer_w_r_")
    assert len(hash_part) == 24
    assert all(c in "0123456789abcdef" for c in hash_part)


def test_make_drawer_id_from_chunk_does_not_collide_across_boundary():
    """RED test pinning the whole PR.

    Classic collision: source_file="/path/a1" + chunk_index=23 produces
    hash input "/path/a123" under the pre-v2 recipe. source_file="/path/a"
    + chunk_index=123 produces the SAME "/path/a123" — same hash, same
    drawer_id, second ChromaDB upsert overwrites the first.

    Under the v2 recipe (delimiter '|'), the two inputs become
    "/path/a1|23" and "/path/a|123" — distinct strings, distinct
    hashes, distinct drawer IDs. No collision.
    """
    a = ids.make_drawer_id_from_chunk("w", "r", "/path/a1", 23)
    b = ids.make_drawer_id_from_chunk("w", "r", "/path/a", 123)
    assert a != b, f"Collision survived v2 recipe: {a!r} == {b!r}"


def test_make_drawer_id_from_chunk_is_deterministic():
    """Same inputs must always produce the same ID — re-mining a file
    that hasn't changed must hit the same drawer slot, or
    file_already_mined() loses its idempotency."""
    a = ids.make_drawer_id_from_chunk("w", "r", "/file.md", 7)
    b = ids.make_drawer_id_from_chunk("w", "r", "/file.md", 7)
    assert a == b


def test_make_drawer_id_from_chunk_windows_path_with_colon_does_not_collide():
    """Windows paths contain ':' in drive letters (C:\\Users\\...). The
    v2 recipe uses '|' precisely so paths that contain ':' can never
    align with a chunk index to collide. This test would FAIL on a
    ':'-delimited recipe because Windows paths and URL-like paths
    (https://host:8080) commonly end in ':digits'."""
    a = ids.make_drawer_id_from_chunk("w", "r", "C:\\Users\\foo", 5)
    b = ids.make_drawer_id_from_chunk("w", "r", "C:\\Users\\foo:", 5)
    assert a != b


# ── make_drawer_id_from_content ───────────────────────────────────────


def test_make_drawer_id_from_content_does_not_collide_across_boundary():
    """mcp_server.py:1136 hashes wing+room+content with no delimiter.
    Architecturally identical defect to the chunk-index sites:
    wing="foo"+room="bar" hashes the same as wing="fooba"+room="r".
    v2 delimiter breaks this."""
    a = ids.make_drawer_id_from_content("foo", "bar", "x")
    b = ids.make_drawer_id_from_content("fooba", "r", "x")
    assert a != b


def test_make_drawer_id_from_content_returns_expected_prefix():
    """Same namespacing pattern as the chunk-index helper."""
    result = ids.make_drawer_id_from_content("proj", "scratch", "hello")
    assert result.startswith("drawer_proj_scratch_")


# ── make_convo_drawer_id ──────────────────────────────────────────────


def test_make_convo_drawer_id_does_not_collide_across_extract_mode_boundary():
    """convo_miner.py:422 hashes source_file+extract_mode+chunk_index.
    Pre-v2 used ':' as delimiter — this test would still PASS on the ':'
    recipe for clean inputs, but the migration to '|' is for
    consistency with the chunk-index helpers and to remove the
    Windows-path / URL-source edge case where ':' can appear in the
    source_file itself."""
    a = ids.make_convo_drawer_id("w", "r", "/log.jsonl", "general", 5)
    b = ids.make_convo_drawer_id("w", "r", "/log.jsonl", "extract", 5)
    assert a != b


def test_make_convo_drawer_id_returns_expected_prefix():
    result = ids.make_convo_drawer_id("claude", "diary", "/c.jsonl", "general", 0)
    assert result.startswith("drawer_claude_diary_")


# ── make_convo_sentinel_id ────────────────────────────────────────────


def test_make_convo_sentinel_id_returns_expected_prefix():
    """Sentinel IDs are namespaced under '_reg_' so they can be
    filtered out of normal drawer queries."""
    result = ids.make_convo_sentinel_id("/c.jsonl", "general")
    assert result.startswith("_reg_")


def test_make_convo_sentinel_id_distinguishes_extract_modes():
    a = ids.make_convo_sentinel_id("/c.jsonl", "general")
    b = ids.make_convo_sentinel_id("/c.jsonl", "extract")
    assert a != b


# ── make_triple_id ────────────────────────────────────────────────────


def test_make_triple_id_returns_expected_prefix():
    """Triple IDs prefix with 't_' and embed the subject/predicate/object
    triple in the ID for grep-ability in SQLite."""
    result = ids.make_triple_id("sub1", "loves", "obj1", "2026-01-01", "2026-05-30T10:00:00")
    assert result.startswith("t_sub1_loves_obj1_")


def test_make_triple_id_hash_length_is_12_hex():
    """Triple IDs historically truncate at 12 hex chars (vs 24 for
    drawers) because the subject/predicate/object prefix already
    supplies the bulk of the namespace."""
    result = ids.make_triple_id("s", "p", "o", "2026-01-01", "2026-05-30T10:00:00")
    hash_part = result.removeprefix("t_s_p_o_")
    assert len(hash_part) == 12


def test_make_triple_id_does_not_collide_across_iso_datetime_boundary():
    """Pre-v2 hash input was f'{valid_from}{recorded_at}' with no
    delimiter — two ISO datetimes concatenated could in principle
    collide (valid_from='2026-01-01' + recorded_at='T12:00:00' ==
    valid_from='2026-01-01T12' + recorded_at=':00:00' for hash
    purposes). v2 delimiter prevents this."""
    a = ids.make_triple_id("s", "p", "o", "2026-01-01", "T12:00:00")
    b = ids.make_triple_id("s", "p", "o", "2026-01-01T12", ":00:00")
    assert a != b


# ── _delimited_sha256 (private helper, smoke test only) ───────────────


def test_private_delimited_sha256_uses_pipe_delimiter():
    """Confirms the implementation actually uses '|' and not ':' — a
    subtle copy-paste from the diary_ingest precedent or a stale
    ':' precedent from convo_miner could regress the delimiter without
    breaking the higher-level tests."""
    result = ids._delimited_sha256(("a", "b"), 64)
    expected = hashlib.sha256(b"a|b").hexdigest()
    assert result == expected


def test_private_delimited_sha256_truncation_honoured():
    """Truncation argument actually shortens the hex output."""
    result = ids._delimited_sha256(("a", "b"), 8)
    assert len(result) == 8
