"""Tests for `--limit` semantics — should bound NEW work, not WALK.

Regression for #1535: before the fix, `--limit N` truncated the file list
BEFORE `file_already_mined` was checked in `process_file`, so a mine on a
corpus where the first-N walk-order files were all already-mined produced
0 new drawers despite the budget being intact.

These tests exercise:
- A second mine with `--limit N` makes forward progress past the first
  N files (would loop on first-N already-mined under the old behavior).
- A full mine then `--limit N` re-mine produces 0 new drawers (everything
  is already-mined; expected behavior either way) and doesn't crash.
- `--limit` on a fresh empty palace processes up to N files normally.
- `dry_run=True` with `--limit` doesn't try to query the palace
  (palace may not exist yet in dry-run scenarios).
"""
import os
import shutil
import tempfile
from pathlib import Path

import chromadb
import yaml

from mempalace.miner import mine
from mempalace.palace import file_already_mined


def _make_corpus(root: Path, n: int) -> None:
    """Stand up a corpus of `n` small .md files plus a mempalace.yaml."""
    for i in range(n):
        path = root / f"doc_{i:03d}.md"
        # Content varies per file so chunking + drawer creation is realistic.
        path.write_text(
            f"# Document {i}\n\n"
            + ("This is a sample paragraph with enough text to chunk. " * 30),
            encoding="utf-8",
        )
    with open(root / "mempalace.yaml", "w") as f:
        yaml.dump(
            {
                "wing": "limit_test",
                "rooms": [{"name": "general", "description": "All"}],
            },
            f,
        )


def _drawer_count(palace_path: Path) -> int:
    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.get_collection("mempalace_drawers")
    return col.count()


def test_limit_skips_already_mined_makes_forward_progress():
    """Two mines with `--limit 3` on a 6-file corpus should cover all 6.

    Under the old behavior (truncate before skip), the second mine would
    see the same first-3 files in walk order — all already-mined — and
    produce 0 new drawers, never reaching files 4-6.
    """
    tmpdir = Path(tempfile.mkdtemp())
    try:
        corpus = tmpdir / "corpus"
        corpus.mkdir()
        _make_corpus(corpus, n=6)
        palace = tmpdir / "palace"

        # First mine: limit=3 → only 3 files mined.
        mine(str(corpus), str(palace), limit=3)
        after_first = _drawer_count(palace)
        assert after_first > 0, "first mine should produce drawers"

        # Second mine: limit=3 → should pick up the OTHER 3 files
        # (already-mined files are skipped BEFORE the limit is applied,
        # so the limit budget goes to new files).
        mine(str(corpus), str(palace), limit=3)
        after_second = _drawer_count(palace)
        assert after_second > after_first, (
            f"second mine made no progress: drawers {after_first} -> "
            f"{after_second}. Under the old --limit semantics, the second "
            f"mine would loop on the first-N already-mined files."
        )

        # All 6 files should now be marked as mined.
        client = chromadb.PersistentClient(path=str(palace))
        col = client.get_collection("mempalace_drawers")
        for i in range(6):
            path = corpus / f"doc_{i:03d}.md"
            assert file_already_mined(col, str(path), check_mtime=True), (
                f"file {path.name} should be mined after two --limit=3 runs"
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_limit_when_corpus_fully_mined_is_safe_noop():
    """A `--limit` mine against a fully-mined corpus should not crash and
    should not produce new drawers (everything is already-mined)."""
    tmpdir = Path(tempfile.mkdtemp())
    try:
        corpus = tmpdir / "corpus"
        corpus.mkdir()
        _make_corpus(corpus, n=3)
        palace = tmpdir / "palace"

        # Mine everything first.
        mine(str(corpus), str(palace))
        after_full = _drawer_count(palace)

        # Re-mine with --limit 10. Pre-filter drops everything; the slice
        # is empty; no new drawers; no crash.
        mine(str(corpus), str(palace), limit=10)
        after_relimit = _drawer_count(palace)

        assert after_relimit == after_full, (
            f"already-mined re-mine should be a no-op; saw {after_full} -> {after_relimit}"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_limit_on_fresh_palace_processes_up_to_n():
    """`--limit N` on a fresh palace processes up to N files (the
    pre-filter finds zero already-mined, so the slice gets the first N)."""
    tmpdir = Path(tempfile.mkdtemp())
    try:
        corpus = tmpdir / "corpus"
        corpus.mkdir()
        _make_corpus(corpus, n=10)
        palace = tmpdir / "palace"

        mine(str(corpus), str(palace), limit=4)

        client = chromadb.PersistentClient(path=str(palace))
        col = client.get_collection("mempalace_drawers")
        # Count distinct source_files in the palace.
        rows = col.get(where={"wing": "limit_test"}, include=["metadatas"])
        mined_files = {m["source_file"] for m in rows.get("metadatas", []) if m}
        assert len(mined_files) <= 4, (
            f"--limit 4 mined {len(mined_files)} distinct files; expected ≤ 4"
        )
        assert len(mined_files) > 0, "expected at least one file mined"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_dry_run_with_limit_does_not_touch_palace():
    """`dry_run=True` short-circuits the pre-filter (palace may not even
    exist yet in a dry-run check). Should not crash on a missing palace."""
    tmpdir = Path(tempfile.mkdtemp())
    try:
        corpus = tmpdir / "corpus"
        corpus.mkdir()
        _make_corpus(corpus, n=5)
        palace = tmpdir / "palace_that_does_not_exist"

        # Should not raise — dry_run skips palace I/O entirely.
        mine(str(corpus), str(palace), limit=2, dry_run=True)

        # Palace dir may or may not be created by other code paths, but
        # in either case, the call must complete without error.
        assert True  # reaching here is the assertion
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
