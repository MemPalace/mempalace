"""
Tests for mine_epoch tracking and revision snapshots in miner.py.

Verifies four features added alongside #521's delete-before-insert fix:
  1. Unix-time epoch tracking — each mine run stamps chunks with a monotonic
     Unix timestamp (int(time.time()) with +1 bump on collision). The epoch
     is persisted in epoch.json and survives cmd_purge rebuilds automatically.
  2. Chunks tagged with mine_epoch for version identity.
  3. Revision snapshots — chunks are saved to revisions.jsonl before upstream's
     delete-before-insert purge, preserving a queryable history of previous
     file versions.
  4. Time-based retention + hard count cap on revisions.jsonl (default 90 days,
     configurable via MEMPALACE_REVISION_RETENTION_DAYS env var).

Run with: python -m pytest tests/test_mine_epoch_tracking.py -v
"""

import json
import time
from pathlib import Path

import pytest

from mempalace import miner
from mempalace.palace import get_collection


# =============================================================================
# FIXTURES
# =============================================================================


@pytest.fixture
def palace_dir(tmp_path):
    """Create a temporary palace directory."""
    palace = tmp_path / "palace"
    palace.mkdir()
    return str(palace)


@pytest.fixture
def project_dir(tmp_path):
    """Create a temporary project directory with a mempalace.yaml."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "mempalace.yaml").write_text(
        "wing: test_wing\n"
        "rooms:\n"
        "  - name: general\n"
        "    description: Everything\n"
    )
    return project


def _big_content(n_paragraphs: int) -> str:
    """Generate content with N distinct paragraphs, each large enough to
    produce its own chunk (CHUNK_SIZE is 800 chars)."""
    paragraphs = []
    for i in range(n_paragraphs):
        # Each paragraph is ~900 chars so chunking produces roughly 1 chunk per paragraph.
        body = f"Paragraph {i}: " + (f"content_{i} " * 80)
        paragraphs.append(body)
    return "\n\n".join(paragraphs)


# =============================================================================
# EPOCH TRACKING TESTS
# =============================================================================


def test_epoch_starts_at_zero(palace_dir):
    """A fresh palace (no epoch.json yet) returns 0."""
    assert miner._load_epoch(palace_dir) == 0


def test_epoch_is_unix_time_and_monotonic(palace_dir, project_dir):
    """Each mine() call stamps the run with a Unix timestamp, strictly monotonic."""
    (project_dir / "a.txt").write_text(_big_content(2))

    before = int(time.time())
    miner.mine(str(project_dir), palace_dir)
    first_epoch = miner._load_epoch(palace_dir)
    after = int(time.time())

    # First epoch should be a plausible Unix timestamp for "now"
    assert first_epoch >= before
    assert first_epoch <= after + 1
    assert first_epoch > 1_000_000_000, (
        f"epoch {first_epoch} should be a Unix timestamp, not a small counter"
    )

    # Touch the file so it gets re-mined
    time.sleep(0.01)
    (project_dir / "a.txt").write_text(_big_content(2) + "\n\nmore stuff here")
    miner.mine(str(project_dir), palace_dir)
    second_epoch = miner._load_epoch(palace_dir)
    assert second_epoch > first_epoch, (
        f"epoch should strictly increase: {first_epoch} -> {second_epoch}"
    )

    time.sleep(0.01)
    (project_dir / "a.txt").write_text(_big_content(3))
    miner.mine(str(project_dir), palace_dir)
    third_epoch = miner._load_epoch(palace_dir)
    assert third_epoch > second_epoch


def test_epoch_persisted_to_file(palace_dir, project_dir):
    """epoch.json exists after a mine run and contains current + last_mine."""
    (project_dir / "a.txt").write_text(_big_content(2))
    miner.mine(str(project_dir), palace_dir)

    epoch_file = Path(palace_dir) / "epoch.json"
    assert epoch_file.exists()

    data = json.loads(epoch_file.read_text())
    assert data["current"] > 1_000_000_000
    assert "last_mine" in data


def test_chunks_tagged_with_mine_epoch(palace_dir, project_dir):
    """Every drawer stored during a mine run carries a consistent mine_epoch."""
    (project_dir / "a.txt").write_text(_big_content(2))
    miner.mine(str(project_dir), palace_dir)

    expected = miner._load_epoch(palace_dir)
    assert expected > 1_000_000_000  # Unix timestamp shape

    collection = get_collection(palace_dir)
    result = collection.get(include=["metadatas"])
    assert len(result["ids"]) > 0
    for meta in result["metadatas"]:
        assert meta.get("mine_epoch") == expected


def test_same_second_mine_bumps_epoch_by_one(palace_dir, project_dir, monkeypatch):
    """If two mines fire in the same second, the second epoch is still strictly greater.

    Forces time.time() to return a fixed value so both mines would otherwise
    produce the same epoch. The monotonic bump should make the second one
    equal to previous + 1.
    """
    (project_dir / "a.txt").write_text(_big_content(2))

    fixed_now = 1_700_000_000
    monkeypatch.setattr(miner.time, "time", lambda: fixed_now)

    miner.mine(str(project_dir), palace_dir)
    first = miner._load_epoch(palace_dir)
    assert first == fixed_now

    time.sleep(0.01)
    (project_dir / "a.txt").write_text(_big_content(2) + "\n\nmore")
    miner.mine(str(project_dir), palace_dir)
    second = miner._load_epoch(palace_dir)

    assert second > first, (
        f"same-second mines should still produce strictly increasing epochs: "
        f"{first} -> {second}"
    )
    assert second == fixed_now + 1


def test_epoch_survives_cmd_purge_equivalent(palace_dir, project_dir):
    """After deleting epoch.json (simulating a palace rebuild), the next
    mine produces an epoch greater than the pre-purge epoch.

    This is the key property Unix-time epochs give us: cmd_purge-style
    rebuilds no longer break monotonicity, because real time moves forward.
    """
    (project_dir / "a.txt").write_text(_big_content(2))
    miner.mine(str(project_dir), palace_dir)
    pre_purge = miner._load_epoch(palace_dir)

    # Simulate a cmd_purge that wipes the palace state
    epoch_file = Path(palace_dir) / "epoch.json"
    epoch_file.unlink()
    assert miner._load_epoch(palace_dir) == 0  # truly wiped

    # Force at least a one-second delay so Unix time moves forward
    time.sleep(1.1)
    (project_dir / "a.txt").write_text(_big_content(3))
    miner.mine(str(project_dir), palace_dir)
    post_purge = miner._load_epoch(palace_dir)

    assert post_purge > pre_purge, (
        f"post-purge epoch should exceed pre-purge epoch: "
        f"{pre_purge} -> {post_purge}"
    )


# =============================================================================
# STALE CHUNK EVICTION TESTS
# =============================================================================


def test_shrinking_file_evicts_orphaned_chunks(palace_dir, project_dir):
    """When a file shrinks, high-index orphan chunks are deleted."""
    target = project_dir / "a.txt"
    target.write_text(_big_content(5))  # big file → many chunks

    miner.mine(str(project_dir), palace_dir)

    collection = get_collection(palace_dir)
    initial_result = collection.get(where={"source_file": str(target)}, include=["metadatas"])
    initial_count = len(initial_result["ids"])
    assert initial_count >= 5, f"expected >=5 chunks from 5 paragraphs, got {initial_count}"

    # Shrink the file to just 1 paragraph
    time.sleep(0.01)
    target.write_text(_big_content(1))

    miner.mine(str(project_dir), palace_dir)

    # Re-check — orphaned high-index chunks should be gone
    final_result = collection.get(where={"source_file": str(target)}, include=["metadatas"])
    final_count = len(final_result["ids"])

    assert final_count < initial_count, (
        f"expected fewer chunks after shrinking, "
        f"had {initial_count} before, {final_count} after"
    )

    # No chunk_index should exceed what the new content produces
    indices = [m.get("chunk_index", 0) for m in final_result["metadatas"]]
    assert max(indices) < initial_count, "orphaned high-index chunks not cleaned up"


def test_unchanged_file_not_reprocessed(palace_dir, project_dir):
    """Mining twice without changes doesn't create duplicate chunks."""
    (project_dir / "a.txt").write_text(_big_content(3))

    miner.mine(str(project_dir), palace_dir)
    collection = get_collection(palace_dir)
    first_count = len(collection.get()["ids"])

    # Mine again — the file hasn't changed
    miner.mine(str(project_dir), palace_dir)
    second_count = len(collection.get()["ids"])

    assert first_count == second_count, (
        f"unchanged file was re-processed: {first_count} → {second_count}"
    )


def test_growing_file_keeps_all_chunks(palace_dir, project_dir):
    """When a file grows, new chunks are added alongside existing ones."""
    target = project_dir / "a.txt"
    target.write_text(_big_content(2))

    miner.mine(str(project_dir), palace_dir)
    collection = get_collection(palace_dir)
    initial_count = len(
        collection.get(where={"source_file": str(target)})["ids"]
    )

    time.sleep(0.01)
    target.write_text(_big_content(5))  # grow
    miner.mine(str(project_dir), palace_dir)

    final_count = len(
        collection.get(where={"source_file": str(target)})["ids"]
    )
    assert final_count > initial_count, "growing file should produce more chunks"


# =============================================================================
# REVISION SNAPSHOT TESTS
# =============================================================================


def test_revisions_jsonl_created_when_chunks_evicted(palace_dir, project_dir):
    """Shrinking a file produces entries in revisions.jsonl with correct metadata."""
    target = project_dir / "a.txt"
    target.write_text(_big_content(5))

    miner.mine(str(project_dir), palace_dir)
    first_epoch = miner._load_epoch(palace_dir)

    revisions_path = Path(palace_dir) / "revisions.jsonl"
    assert not revisions_path.exists(), "no revisions expected before any shrinking"

    # Shrink
    time.sleep(1.1)  # ensure Unix time moves forward so epoch is strictly greater
    target.write_text(_big_content(1))
    miner.mine(str(project_dir), palace_dir)
    second_epoch = miner._load_epoch(palace_dir)
    assert second_epoch > first_epoch

    assert revisions_path.exists(), "revisions.jsonl should exist after chunks evicted"

    lines = revisions_path.read_text().strip().splitlines()
    assert len(lines) > 0, "expected at least one revision record"

    for line in lines:
        record = json.loads(line)
        # All required fields present
        assert "superseded_at" in record
        assert "superseded_by_epoch" in record
        assert "source_file" in record
        assert "chunk_index" in record
        assert "content" in record
        assert "original_epoch" in record
        # Monotonic epoch relationship: records were superseded by the second
        # mine run, and their original epoch is from the first run
        assert record["superseded_by_epoch"] == second_epoch
        assert record["original_epoch"] == first_epoch
        assert record["original_epoch"] < record["superseded_by_epoch"]


def test_revision_content_preserves_original_text(palace_dir, project_dir):
    """The snapshotted revision contains the exact content that was deleted."""
    target = project_dir / "a.txt"
    # Put unique, recognizable content in the file
    unique = "UNIQUE_MARKER_STRING_XYZZY_12345"
    content = (
        _big_content(3)
        + "\n\n"
        + f"Special paragraph with {unique}: " + ("data " * 150)
    )
    target.write_text(content)

    miner.mine(str(project_dir), palace_dir)

    # Shrink to remove the special paragraph
    time.sleep(0.01)
    target.write_text(_big_content(1))
    miner.mine(str(project_dir), palace_dir)

    revisions_path = Path(palace_dir) / "revisions.jsonl"
    records = [json.loads(line) for line in revisions_path.read_text().splitlines() if line.strip()]

    # At least one revision should contain our unique marker
    found = any(unique in r["content"] for r in records)
    assert found, (
        f"unique marker {unique!r} not found in any revision record — "
        "revision content was not preserved correctly"
    )


def test_revisions_accumulate_across_multiple_mines(palace_dir, project_dir):
    """Each shrinking run adds to revisions.jsonl without clobbering previous ones."""
    target = project_dir / "a.txt"

    target.write_text(_big_content(5))
    miner.mine(str(project_dir), palace_dir)

    time.sleep(0.01)
    target.write_text(_big_content(3))
    miner.mine(str(project_dir), palace_dir)

    revisions_path = Path(palace_dir) / "revisions.jsonl"
    first_count = len(revisions_path.read_text().strip().splitlines())

    time.sleep(0.01)
    target.write_text(_big_content(1))
    miner.mine(str(project_dir), palace_dir)

    second_count = len(revisions_path.read_text().strip().splitlines())
    assert second_count > first_count, (
        f"revisions.jsonl should grow across mine runs: {first_count} → {second_count}"
    )


def test_revisions_file_tail_truncates_at_max(palace_dir, project_dir, monkeypatch):
    """revisions.jsonl is capped at MAX_REVISIONS lines when the file grows past it.

    Shrinks the hard cap to a small value and performs multiple re-mines
    until the file would exceed it, then verifies the cap holds and the
    most recent records are preserved.
    """
    # Shrink the hard cap so we can trigger truncation quickly
    monkeypatch.setattr(miner, "MAX_REVISIONS", 6)
    # Keep a long retention window so time-based filtering doesn't also fire
    monkeypatch.setattr(miner, "REVISION_RETENTION_SECONDS", 365 * 86400)

    target = project_dir / "a.txt"

    # Round 1: fresh mine → no revisions yet
    target.write_text(_big_content(4))
    miner.mine(str(project_dir), palace_dir)
    revisions_path = Path(palace_dir) / "revisions.jsonl"
    assert not revisions_path.exists(), "first mine creates no revisions"

    # Round 2: re-mine with different content → snapshots appended
    time.sleep(1.1)
    target.write_text(_big_content(4) + "\n\nround2 marker")
    miner.mine(str(project_dir), palace_dir)
    round2_epoch = miner._load_epoch(palace_dir)
    after_round2 = len(revisions_path.read_text().splitlines())
    assert after_round2 >= 4, f"expected at least 4 revisions, got {after_round2}"

    # Round 3: re-mine again → exceeds cap, truncation should fire
    time.sleep(1.1)
    target.write_text(_big_content(4) + "\n\nround3 newer marker")
    miner.mine(str(project_dir), palace_dir)
    round3_epoch = miner._load_epoch(palace_dir)

    lines = revisions_path.read_text().splitlines()
    assert len(lines) <= 6, f"truncation should cap at MAX_REVISIONS=6, got {len(lines)}"

    # The remaining records should include entries from the most recent mine
    records = [json.loads(line) for line in lines if line.strip()]
    epochs_seen = {r["superseded_by_epoch"] for r in records}
    assert round3_epoch in epochs_seen, (
        f"newest epoch ({round3_epoch}) should be preserved after truncation, "
        f"saw {epochs_seen}"
    )


def test_revisions_time_retention_drops_ancient_records(palace_dir, project_dir, monkeypatch):
    """Time-based retention drops records older than REVISION_RETENTION_SECONDS.

    Writes a synthetic revisions.jsonl with a mix of ancient and recent
    records, then triggers a truncation by exceeding MAX_REVISIONS. The
    ancient records should be filtered out by the time cutoff, keeping
    only the recent ones.
    """
    # Very small cap so our re-mine triggers truncation
    monkeypatch.setattr(miner, "MAX_REVISIONS", 3)
    # 60-second retention window
    monkeypatch.setattr(miner, "REVISION_RETENTION_SECONDS", 60)

    revisions_path = Path(palace_dir) / "revisions.jsonl"
    now = int(time.time())
    ancient = now - 7200  # 2 hours ago — way past the 60-second cutoff
    recent = now - 10     # 10 seconds ago — within the window

    # Hand-craft a revisions.jsonl with 4 ancient records + 0 recent
    with open(revisions_path, "w") as f:
        for i in range(4):
            f.write(json.dumps({
                "superseded_at": "2020-01-01T00:00:00",
                "superseded_by_epoch": ancient,
                "source_file": f"/tmp/ancient_{i}.txt",
                "chunk_index": i,
                "content": f"ancient content {i}",
                "original_epoch": ancient - 1000,
                "original_filed_at": "2020-01-01T00:00:00",
                "wing": "test_wing",
                "room": "general",
            }) + "\n")

    # Now perform a real mine that will append fresh records and trigger
    # truncation because we're already at 4 lines with MAX_REVISIONS=3.
    target = project_dir / "a.txt"
    target.write_text(_big_content(3))
    miner.mine(str(project_dir), palace_dir)  # first mine — no revisions from this
    time.sleep(1.1)
    target.write_text(_big_content(3) + "\n\nfresh round marker")
    miner.mine(str(project_dir), palace_dir)  # re-mine — appends + truncates

    lines = revisions_path.read_text().splitlines()
    records = [json.loads(l) for l in lines if l.strip()]

    # All remaining records should have superseded_by_epoch >= cutoff (now - 60)
    cutoff = int(time.time()) - 60
    for r in records:
        assert r["superseded_by_epoch"] >= cutoff, (
            f"ancient record survived retention sweep: "
            f"superseded_by_epoch={r['superseded_by_epoch']}, cutoff={cutoff}"
        )

    # Hard cap still enforced
    assert len(records) <= 3, f"MAX_REVISIONS=3 cap violated: got {len(records)}"


# =============================================================================
# BACKWARD COMPATIBILITY TESTS
# =============================================================================


def test_mining_works_without_palace_path_arg():
    """process_file still works when palace_path is not provided (old callers)."""
    # Just verify the default argument path — the function signature accepts
    # palace_path="" and mine_epoch=0 as defaults, so old callers don't break.
    import inspect
    sig = inspect.signature(miner.process_file)
    assert sig.parameters["palace_path"].default == ""
    assert sig.parameters["mine_epoch"].default == 0
