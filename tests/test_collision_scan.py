"""Tests for mempalace.collision_scan — pre-mining defense against
drawer_id collisions.

The scan runs immediately before batched chromadb upserts and aborts the
mine with an actionable error if any proposed drawer_id appears more than
once in the union of (incoming-vs-incoming) and (incoming-vs-existing).

Under the v2 hash recipe these collisions are vanishingly rare in practice
— SHA-256 truncated to 24 hex chars makes accidental collision ~2^-96.
The scan's real value is (a) catching upstream bugs that emit duplicate
(source_file, chunk_index) pairs in the same batch, and (b) surfacing the
astronomical-but-possible SHA-256 collision with a clear error instead of
a silent overwrite at the ChromaDB upsert.
"""

from __future__ import annotations

from typing import Optional

import pytest

from mempalace.collision_scan import CollisionError, assert_no_collisions


class _MockGet:
    """Stand-in for ChromaDB's get() result. Real ChromaDB returns a
    dict-like with ``ids`` and ``metadatas`` keys; we mirror that shape."""

    def __init__(self, ids: list[str], metadatas: list[dict]):
        self._payload = {"ids": ids, "metadatas": metadatas}

    def __getitem__(self, key):
        return self._payload[key]

    def get(self, key, default=None):
        return self._payload.get(key, default)


class _MockCollection:
    """Stand-in for a ChromaDB collection. Stores a fixed mapping of
    drawer_id → metadata for the test to model 'existing palace state'.
    ``get(ids=[...])`` returns only the rows whose ids are in storage.
    """

    def __init__(self, existing: Optional[dict[str, dict]] = None):
        self._existing = existing or {}

    def get(self, ids=None, include=None, **kwargs):
        ids = ids or []
        rows = [(did, self._existing[did]) for did in ids if did in self._existing]
        return _MockGet(
            ids=[did for did, _ in rows],
            metadatas=[meta for _, meta in rows],
        )


# ── Happy path: no collisions ────────────────────────────────────────


def test_assert_no_collisions_passes_for_clean_batch():
    """Distinct incoming ids + no overlap with existing = no error."""
    proposed = [
        ("drawer_a", {"source_file": "/file_a.md", "chunk_index": 0}),
        ("drawer_b", {"source_file": "/file_a.md", "chunk_index": 1}),
        ("drawer_c", {"source_file": "/file_b.md", "chunk_index": 0}),
    ]
    col = _MockCollection()
    assert_no_collisions(proposed, col) is None


def test_assert_no_collisions_passes_for_clean_batch_with_existing_drawers():
    """Existing drawers with DIFFERENT ids than incoming = no error.
    The scan only fires when an id appears more than once across the
    union of incoming + existing."""
    proposed = [
        ("drawer_new_1", {"source_file": "/new.md", "chunk_index": 0}),
    ]
    col = _MockCollection(
        existing={
            "drawer_old_1": {"source_file": "/old.md", "chunk_index": 0},
            "drawer_old_2": {"source_file": "/old.md", "chunk_index": 1},
        }
    )
    assert_no_collisions(proposed, col) is None


def test_assert_no_collisions_treats_idempotent_re_mine_as_clean():
    """If incoming drawer_id matches an existing id AND the
    (source_file, chunk_index) metadata also matches, that's a normal
    re-mine of the same chunk — NOT a collision. The scan must let it
    pass; otherwise re-mining a clean palace would always raise."""
    proposed = [
        ("drawer_same", {"source_file": "/file.md", "chunk_index": 5}),
    ]
    col = _MockCollection(
        existing={
            "drawer_same": {"source_file": "/file.md", "chunk_index": 5},
        }
    )
    assert_no_collisions(proposed, col) is None


# ── Incoming-vs-incoming collisions ──────────────────────────────────


def test_assert_no_collisions_raises_on_incoming_duplicate_with_different_metadata():
    """Two incoming chunks producing the same drawer_id with DIFFERENT
    (source_file, chunk_index) pairs = an upstream bug or astronomical
    SHA-256 hash collision. Either way, abort the mine."""
    proposed = [
        ("drawer_X", {"source_file": "/file_a.md", "chunk_index": 0}),
        ("drawer_X", {"source_file": "/file_b.md", "chunk_index": 1}),
    ]
    col = _MockCollection()
    with pytest.raises(CollisionError) as exc_info:
        assert_no_collisions(proposed, col)
    # Error message names the colliding (source_file, chunk_index) pairs
    msg = str(exc_info.value)
    assert "drawer_X" in msg
    assert "/file_a.md" in msg
    assert "/file_b.md" in msg


def test_assert_no_collisions_passes_on_incoming_duplicate_with_same_metadata():
    """Two incoming chunks with the SAME (source_file, chunk_index)
    producing the same drawer_id = a duplicate chunk in the batch (still
    an upstream bug, but the downstream collision damage is zero since
    they'd write identical content). The scan does not fire on this
    case because it's not the collision shape v2 was designed to
    catch."""
    proposed = [
        ("drawer_X", {"source_file": "/file.md", "chunk_index": 5}),
        ("drawer_X", {"source_file": "/file.md", "chunk_index": 5}),
    ]
    col = _MockCollection()
    assert_no_collisions(proposed, col) is None


# ── Incoming-vs-existing collisions ──────────────────────────────────


def test_assert_no_collisions_raises_on_incoming_matching_existing_with_different_metadata():
    """Incoming chunk produces the same drawer_id as an existing drawer
    whose stored (source_file, chunk_index) DIFFERS = SHA-256 collision
    or recipe-version skew. Abort the mine so the upsert doesn't
    silently overwrite the existing row."""
    proposed = [
        ("drawer_Y", {"source_file": "/incoming.md", "chunk_index": 3}),
    ]
    col = _MockCollection(
        existing={
            "drawer_Y": {"source_file": "/existing.md", "chunk_index": 7},
        }
    )
    with pytest.raises(CollisionError) as exc_info:
        assert_no_collisions(proposed, col)
    msg = str(exc_info.value)
    assert "drawer_Y" in msg
    assert "/incoming.md" in msg
    assert "/existing.md" in msg


# ── Error message quality ────────────────────────────────────────────


def test_collision_error_lists_all_collisions_not_just_first():
    """If a batch contains multiple distinct collisions, the error
    surfaces ALL of them — a user fixing one and re-running shouldn't
    rediscover the next one from scratch."""
    proposed = [
        ("drawer_A", {"source_file": "/f1.md", "chunk_index": 0}),
        ("drawer_A", {"source_file": "/f2.md", "chunk_index": 0}),  # collision 1
        ("drawer_B", {"source_file": "/f3.md", "chunk_index": 0}),
        ("drawer_B", {"source_file": "/f4.md", "chunk_index": 0}),  # collision 2
    ]
    col = _MockCollection()
    with pytest.raises(CollisionError) as exc_info:
        assert_no_collisions(proposed, col)
    msg = str(exc_info.value)
    assert "drawer_A" in msg
    assert "drawer_B" in msg
    assert "/f1.md" in msg
    assert "/f2.md" in msg
    assert "/f3.md" in msg
    assert "/f4.md" in msg


# ── Edge cases ───────────────────────────────────────────────────────


def test_assert_no_collisions_passes_for_empty_batch():
    """An empty mining batch is trivially collision-free — the scan
    must not raise on len(proposed) == 0 so callers don't have to guard
    the call site."""
    assert_no_collisions([], _MockCollection()) is None


def test_assert_no_collisions_handles_metadata_without_chunk_index():
    """Some drawer types (e.g. diary entries, sentinels) don't carry
    chunk_index. The scan should compare whatever metadata is present
    without crashing on missing keys."""
    proposed = [
        ("drawer_diary_1", {"source_file": "/diary.md"}),
    ]
    col = _MockCollection(
        existing={
            "drawer_diary_1": {"source_file": "/diary_other.md"},
        }
    )
    with pytest.raises(CollisionError):
        assert_no_collisions(proposed, col)


def test_assert_no_collisions_tolerates_chromadb_get_failure():
    """ChromaDB's get() can raise on transient backend errors. The
    scan should NOT swallow those — the caller's broad-except can decide
    whether to abort the mine or proceed. The scan's contract is
    'either confirms no collisions, or raises'. Hiding backend errors
    behind a False is the silent-failure shape this PR was meant to
    eliminate."""

    class _RaisingCollection:
        def get(self, **kwargs):
            raise RuntimeError("chromadb is sad")

    proposed = [("drawer_X", {"source_file": "/f.md", "chunk_index": 0})]
    with pytest.raises(RuntimeError, match="chromadb is sad"):
        assert_no_collisions(proposed, _RaisingCollection())
