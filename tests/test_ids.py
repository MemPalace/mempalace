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


def test_id_recipe_constant_is_v4():
    """Audit code reads ids.ID_RECIPE to tag new drawers. Post write-flip the
    active recipe is content-pure v4 — write paths mint drawer_<hash(content)>
    so the same content anywhere in the mesh is the same drawer. A typo here
    silently mixes recipes."""
    assert ids.ID_RECIPE == "v4"


# ── write-flip: make_drawer_id_for_write under the active recipe ────────────


def test_write_id_is_content_pure_under_v4_and_matches_migration():
    """The write-flip's load-bearing property: under v4 every write path mints
    the SAME id the migration produced for the same content — drawer_<hash>,
    independent of wing/room/source/chunk_index/extract_mode. Without this, a
    re-mine of a migrated source would mint a different id and duplicate instead
    of resolving on peers."""
    content = "a decision was made about the replicated palace"
    migration_id = ids.make_drawer_id_content_pure(content)

    # Every write path (varying non-content inputs) lands on the same v4 id.
    assert (
        ids.make_drawer_id_for_write(content, wing="w", room="r")  # MCP add_drawer
        == ids.make_drawer_id_for_write(  # project/format miner
            content, wing="x", room="y", source_file="/a", chunk_index=3
        )
        == ids.make_drawer_id_for_write(  # conversation miner
            content, wing="p", room="q", source_file="/b", extract_mode="general", chunk_index=9
        )
        == migration_id
    )
    # And it is genuinely content-pure form.
    suffix = migration_id.removeprefix("drawer_")
    assert len(suffix) == 32 and all(c in "0123456789abcdef" for c in suffix)


def test_write_id_v3_fallback_preserves_per_path_recipe(monkeypatch):
    """Flip is one switch: with ID_RECIPE forced back to v3, each write path
    falls back to its historical recipe (proving the gate, and rollback)."""
    monkeypatch.setattr(ids, "ID_RECIPE", "v3")
    c = "some content"
    assert ids.make_drawer_id_for_write(c, wing="w", room="r") == ids.make_drawer_id_from_content(
        "w", "r", c
    )
    assert ids.make_drawer_id_for_write(
        c, wing="w", room="r", source_file="/f", chunk_index=2
    ) == ids.make_drawer_id_from_chunk("w", "r", "/f", 2)
    assert ids.make_drawer_id_for_write(
        c, wing="w", room="r", source_file="/f", extract_mode="general", chunk_index=2
    ) == ids.make_convo_drawer_id("w", "r", "/f", "general", 2)


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


def test_private_delimited_sha256_uses_length_prefixing():
    result = ids._delimited_sha256(("a", "b"), 64)
    expected = hashlib.sha256(b"1:a1:b").hexdigest()
    assert result == expected

    # These tuples collapse to the same raw pipe-joined string:
    # "a|b|c|d". The v3 length-prefixed recipe must keep them distinct.
    left = ids._delimited_sha256(("a", "b|c", "d"), 64)
    right = ids._delimited_sha256(("a|b", "c", "d"), 64)
    assert left != right


def test_private_delimited_sha256_truncation_honoured():
    """Truncation argument actually shortens the hex output."""
    result = ids._delimited_sha256(("a", "b"), 8)
    assert len(result) == 8
