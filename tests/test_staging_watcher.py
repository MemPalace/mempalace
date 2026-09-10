"""Tests for the staging watcher pipeline (Python port).

All tests use the StagingWatcher class directly — no bash required.
Runs on Linux, macOS, and Windows.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

# Make tools/ importable
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from staging_watcher import StagingWatcher  # noqa: E402


def _make_watcher(tmp_path: Path, **kwargs) -> StagingWatcher:
    """Create a StagingWatcher with standard test directories."""
    staging = tmp_path / "staging"
    archive = tmp_path / "archive"
    log = tmp_path / "watcher.log"
    work = tmp_path / "batch_work"
    snapshot = work / ".batch_snapshot"
    staging.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    return StagingWatcher(
        staging_dir=staging,
        palace_path=tmp_path / "palace",
        archive_dir=archive,
        log_file=log,
        batch_work=work,
        batch_snapshot=snapshot,
        work_root=work,
        **kwargs,
    )


# ── VerifyMined tests (still use verify_mined.py via subprocess) ────────────


def _run_verify(sample: Path, manifest: Path, fake_mempalace: Path) -> int:
    """Run verify_mined.py and return its exit code."""
    import subprocess

    return subprocess.run(
        [
            sys.executable,
            str(_TOOLS_DIR / "verify_mined.py"),
            "/tmp/palace",
            str(sample),
            str(manifest),
            str(fake_mempalace),
        ],
        capture_output=True,
    ).returncode


def _make_fake_mempalace(tmp_path: Path, body: str) -> Path:
    """Create a fake mempalace binary that prints *body* for any search call."""
    script = tmp_path / "mempalace"
    script.write_text(
        f"#!/bin/sh\necho '{body.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


class TestVerifyMined:
    @pytest.mark.skipif(sys.platform == "win32", reason="fake mempalace is a shell script")
    def test_verify_passes_when_search_returns_matching_source(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_text("hello world this is a stable snippet\nmore content\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")
        fake = _make_fake_mempalace(
            tmp_path, json.dumps({"results": [{"source_file": str(sample.resolve())}]})
        )
        assert _run_verify(sample, manifest, fake) == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="fake mempalace is a shell script")
    def test_verify_fails_when_search_exits_nonzero(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_text("hello world this is a stable snippet\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")
        fake = tmp_path / "mempalace"
        fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
        assert _run_verify(sample, manifest, fake) == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="fake mempalace is a shell script")
    def test_verify_fails_on_blank_output(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_text("hello world this is a stable snippet\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")
        fake = _make_fake_mempalace(tmp_path, json.dumps({"results": []}))
        assert _run_verify(sample, manifest, fake) == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="fake mempalace is a shell script")
    def test_verify_fails_on_unusable_sample(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_text("\n# comment\n   \n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")
        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps({"results": [{"source_file": str(sample.resolve())}]}),
        )
        assert _run_verify(sample, manifest, fake) == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="fake mempalace is a shell script")
    def test_verify_fails_on_unrelated_matching_drawer(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_text("hello world this is a stable snippet\n", encoding="utf-8")
        other = tmp_path / "other.md"
        other.write_text("hello world this is a stable snippet\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")
        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps({"results": [{"source_file": str(other.resolve())}]}),
        )
        assert _run_verify(sample, manifest, fake) == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="fake mempalace is a shell script")
    def test_verify_fails_on_source_not_in_manifest(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_text("hello world this is a stable snippet\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text("/some/other/file.md\n", encoding="utf-8")
        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps({"results": [{"source_file": str(sample.resolve())}]}),
        )
        assert _run_verify(sample, manifest, fake) == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="fake mempalace is a shell script")
    def test_verify_all_requires_every_manifest_entry(self, tmp_path):
        sample_a = tmp_path / "a.md"
        sample_a.write_text("hello world this is a stable snippet\n", encoding="utf-8")
        sample_b = tmp_path / "b.md"
        sample_b.write_text("another stable snippet here\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(f"{sample_a.resolve()}\n{sample_b.resolve()}\n", encoding="utf-8")
        # Only returns results for sample_a, not sample_b
        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps({"results": [{"source_file": str(sample_a.resolve())}]}),
        )
        assert _run_verify(manifest, manifest, fake) == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="fake mempalace is a shell script")
    def test_verify_all_passes_when_all_entries_searchable(self, tmp_path):
        sample_a = tmp_path / "a.md"
        sample_a.write_text("hello world this is a stable snippet\n", encoding="utf-8")
        sample_b = tmp_path / "b.md"
        sample_b.write_text("another stable snippet here\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(f"{sample_a.resolve()}\n{sample_b.resolve()}\n", encoding="utf-8")
        # Returns results for both
        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps(
                {
                    "results": [
                        {"source_file": str(sample_a.resolve())},
                        {"source_file": str(sample_b.resolve())},
                    ]
                }
            ),
        )
        assert _run_verify(manifest, manifest, fake) == 0


# ── ArchiveFiles tests ─────────────────────────────────────────────────────


class TestArchiveFiles:
    def test_archive_preserves_subdirectory_paths(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "projA").mkdir(parents=True)
        (w.staging_dir / "projB").mkdir(parents=True)
        (w.staging_dir / "projA" / "notes.md").write_text("project A notes\n", encoding="utf-8")
        (w.staging_dir / "projB" / "notes.md").write_text("project B notes\n", encoding="utf-8")
        assert w.archive_files()
        batch_dirs = [d for d in w.archive_dir.iterdir() if d.is_dir()]
        assert len(batch_dirs) == 1
        batch = batch_dirs[0]
        assert (batch / "projA" / "notes.md.gz").exists()
        assert (batch / "projB" / "notes.md.gz").exists()
        manifest = (batch / "MANIFEST.txt").read_text(encoding="utf-8")
        assert "file: projA/notes.md" in manifest
        assert "file: projB/notes.md" in manifest
        assert "archived: projA/notes.md.gz" in manifest
        assert "archived: projB/notes.md.gz" in manifest

    def test_archive_gzip_content_matches_original(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "subdir").mkdir(parents=True)
        original = w.staging_dir / "subdir" / "file.txt"
        original.write_text("preserve this text\n", encoding="utf-8")
        assert w.archive_files()
        batch = [d for d in w.archive_dir.iterdir() if d.is_dir()][0]
        archived = batch / "subdir" / "file.txt.gz"
        assert archived.exists()
        with gzip.open(archived, "rt", encoding="utf-8") as f:
            assert f.read() == "preserve this text\n"

    def test_archive_fails_when_archive_dir_unwritable(self, tmp_path):
        """Archive errors must be a cleanup gate — staging stays intact."""
        w = _make_watcher(tmp_path)
        (w.staging_dir / "file.txt").write_text("content\n", encoding="utf-8")
        # Make archive dir unwritable
        w.archive_dir.chmod(0o444)
        try:
            assert not w.archive_files()
        finally:
            w.archive_dir.chmod(0o755)
        # Staging file must still exist
        assert (w.staging_dir / "file.txt").exists()


# ── PreprocessSubdirectories tests ─────────────────────────────────────────


class TestPreprocessSubdirectories:
    def test_preprocess_directory_preserves_subdirectories(self, tmp_path):
        import sys

        sys.path.insert(0, str(_TOOLS_DIR))
        import preprocess_staging as pp

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "projA").mkdir()
        (staging / "projB").mkdir()
        (staging / "projA" / "notes.md").write_text(
            "# Project A\n\nSome content here.\n", encoding="utf-8"
        )
        (staging / "projB" / "notes.md").write_text(
            "# Project B\n\nOther content here.\n", encoding="utf-8"
        )
        stats = pp.preprocess_directory(str(staging), max_lines=4000)
        assert stats["processed"] == 2
        assert (staging / "processed" / "projA" / "notes.md").exists()
        assert (staging / "processed" / "projB" / "notes.md").exists()


# ── ProcessBatch tests ─────────────────────────────────────────────────────


class TestProcessBatch:
    def test_process_batch_retains_staging_when_verify_fails(self, tmp_path):
        """If verify fails, staging files must remain for the next attempt."""
        w = _make_watcher(tmp_path)
        (w.staging_dir / "file.md").write_text(
            "hello world this is a stable snippet\n", encoding="utf-8"
        )
        # verify_mined will fail because there's no real palace to search
        # But first we need preprocess + mine to "succeed"
        # Since mine calls mempalace which doesn't exist in test, it will fail.
        # The key assertion is that staging is NOT cleared on failure.
        result = w.process_batch()
        # process_batch returns False on any pipeline failure
        assert result is False
        # Staging file must still exist (not cleared)
        assert (w.staging_dir / "file.md").exists()


# ── BatchStability tests ───────────────────────────────────────────────────


class TestBatchStability:
    def test_fingerprint_changes_when_file_grows(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "file.txt").write_text("hello\n", encoding="utf-8")
        fp1 = w.fingerprint_staging()
        (w.staging_dir / "file.txt").write_text("hello world\n", encoding="utf-8")
        fp2 = w.fingerprint_staging()
        assert fp1 != fp2

    def test_fingerprint_changes_when_file_added(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "a.txt").write_text("hello\n", encoding="utf-8")
        fp1 = w.fingerprint_staging()
        (w.staging_dir / "b.txt").write_text("world\n", encoding="utf-8")
        fp2 = w.fingerprint_staging()
        assert fp1 != fp2

    def test_fingerprint_stable_when_unchanged(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "a.txt").write_text("hello\n", encoding="utf-8")
        (w.staging_dir / "b.txt").write_text("world\n", encoding="utf-8")
        fp1 = w.fingerprint_staging()
        fp2 = w.fingerprint_staging()
        assert fp1 == fp2


# ── BatchIsolation tests ───────────────────────────────────────────────────


class TestBatchIsolation:
    def test_archive_ignores_late_file(self, tmp_path):
        """A file that arrives after the batch snapshot is not archived."""
        w = _make_watcher(tmp_path)
        (w.staging_dir / "claimed.txt").write_text("claimed content\n", encoding="utf-8")
        # Manually create a snapshot with only the claimed file
        content = "claimed content\n"
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        size = len(content.encode("utf-8"))
        mtime = int(os.path.getmtime(w.staging_dir / "claimed.txt"))
        w.batch_snapshot.write_text(
            f"claimed.txt\x1f{size}\x1f{mtime}\x1f{sha}\n", encoding="utf-8"
        )
        # Late file arrives after snapshot
        (w.staging_dir / "late.txt").write_text("late content\n", encoding="utf-8")
        assert w.archive_files()
        batch = [d for d in w.archive_dir.iterdir() if d.is_dir()][0]
        assert (batch / "claimed.txt.gz").exists()
        assert not (batch / "late.txt.gz").exists()

    def test_archive_skips_modified_file(self, tmp_path):
        """A file that changes after the snapshot is not archived."""
        w = _make_watcher(tmp_path)
        original = "original content\n"
        (w.staging_dir / "file.txt").write_text(original, encoding="utf-8")
        sha = hashlib.sha256(original.encode("utf-8")).hexdigest()
        size = len(original.encode("utf-8"))
        mtime = int(os.path.getmtime(w.staging_dir / "file.txt"))
        w.batch_snapshot.write_text(f"file.txt\x1f{size}\x1f{mtime}\x1f{sha}\n", encoding="utf-8")
        # Modify the file after the snapshot
        (w.staging_dir / "file.txt").write_text("modified content\n", encoding="utf-8")
        assert not w.archive_files()

    def test_clear_staging_ignores_late_and_modified_files(self, tmp_path):
        """clear_staging only deletes files matching the snapshot."""
        w = _make_watcher(tmp_path)
        # Claimed file
        claimed = "claimed content\n"
        (w.staging_dir / "claimed.txt").write_text(claimed, encoding="utf-8")
        sha = hashlib.sha256(claimed.encode("utf-8")).hexdigest()
        size = len(claimed.encode("utf-8"))
        mtime = int(os.path.getmtime(w.staging_dir / "claimed.txt"))
        w.batch_snapshot.write_text(
            f"claimed.txt\x1f{size}\x1f{mtime}\x1f{sha}\n", encoding="utf-8"
        )
        # Late file (not in snapshot)
        (w.staging_dir / "late.txt").write_text("late\n", encoding="utf-8")
        # Modified file (in snapshot but changed)
        modified_orig = "modified original\n"
        (w.staging_dir / "modified.txt").write_text(modified_orig, encoding="utf-8")
        sha2 = hashlib.sha256(modified_orig.encode("utf-8")).hexdigest()
        size2 = len(modified_orig.encode("utf-8"))
        mtime2 = int(os.path.getmtime(w.staging_dir / "modified.txt"))
        # Append to the snapshot
        with w.batch_snapshot.open("a", encoding="utf-8") as f:
            f.write(f"modified.txt\x1f{size2}\x1f{mtime2}\x1f{sha2}\n")
        # Now modify the file
        (w.staging_dir / "modified.txt").write_text("changed\n", encoding="utf-8")
        w.clear_staging()
        assert not (w.staging_dir / "claimed.txt").exists()  # deleted (matched snapshot)
        assert (w.staging_dir / "late.txt").exists()  # kept (not in snapshot)
        assert (w.staging_dir / "modified.txt").exists()  # kept (changed since snapshot)


# ── Regression tests for fatkobra review issues 1-4 ──────────────────────────


class TestStaleVersionVerification:
    """Issue 1: verification must prove the CURRENT source version was mined."""

    def test_verify_fails_when_file_sha256_mismatches_snapshot(self, tmp_path):
        import sys

        sys.path.insert(0, str(_TOOLS_DIR))
        from verify_mined import verify_one

        staging = tmp_path / "staging"
        staging.mkdir()
        original = "x = 1\ny = 2\nz = 3\n"
        (staging / "claimed.py").write_text(original, encoding="utf-8")
        sha256 = hashlib.sha256(original.encode("utf-8")).hexdigest()
        # Simulate the file changing after the snapshot was claimed.
        (staging / "claimed.py").write_text("x = 999\n", encoding="utf-8")
        manifest = {str((staging / "claimed.py").resolve())}
        result = verify_one(
            "/fake/palace",
            (staging / "claimed.py").resolve(),
            manifest,
            "mempalace",
            expected_sha256=sha256,
        )
        assert result is False, "verify must fail when sha256 mismatches"

    def test_verify_passes_when_file_sha256_matches_snapshot(self, tmp_path):
        import sys

        sys.path.insert(0, str(_TOOLS_DIR))
        from verify_mined import file_sha256

        staging = tmp_path / "staging"
        staging.mkdir()
        content = "x = 1\ny = 2\nz = 3\n"
        (staging / "claimed.py").write_text(content, encoding="utf-8")
        sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        sample = (staging / "claimed.py").resolve()
        assert file_sha256(sample) == sha256


class TestBatchWorkOutsideWatchedTree:
    """Issue 2: .batch_work must not be inside the watched staging tree."""

    def test_count_files_excludes_batch_work(self, tmp_path):
        """count_files must not list files inside batch_work."""
        w = _make_watcher(tmp_path)
        (w.staging_dir / "real.md").write_text("hello\n", encoding="utf-8")
        # Simulate a work directory OUTSIDE the staging tree.
        (w.batch_work / "copy.md").parent.mkdir(parents=True, exist_ok=True)
        (w.batch_work / "copy.md").write_text("copy\n", encoding="utf-8")
        assert w.count_files() == 1


class TestArchiveUsesImmutableWorkCopy:
    """Issue 3: archive and deletion must use the claimed immutable bytes."""

    def test_archive_from_work_copy_not_staging(self, tmp_path):
        w = _make_watcher(tmp_path)
        original = "original content\n"
        (w.staging_dir / "file.txt").write_text(original, encoding="utf-8")
        (w.batch_work / "file.txt").parent.mkdir(parents=True, exist_ok=True)
        (w.batch_work / "file.txt").write_text(original, encoding="utf-8")
        # After claim, producer replaces the live file.
        (w.staging_dir / "file.txt").write_text("REPLACED\n", encoding="utf-8")
        sha = hashlib.sha256(original.encode("utf-8")).hexdigest()
        size = len(original.encode("utf-8"))
        mtime = int(os.path.getmtime(w.batch_work / "file.txt"))
        w.batch_snapshot.write_text(f"file.txt\x1f{size}\x1f{mtime}\x1f{sha}\n", encoding="utf-8")
        assert w.archive_files()
        batch = [d for d in w.archive_dir.iterdir() if d.is_dir()][0]
        with gzip.open(batch / "file.txt.gz", "rt", encoding="utf-8") as f:
            archived_content = f.read()
        assert archived_content == original, (
            "archive must contain the claimed bytes, not the replaced live file"
        )


class TestPortableHashing:
    """Issue 4: fingerprint_staging must work without external sha256sum."""

    def test_fingerprint_stable_when_unchanged(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "a.txt").write_text("hello\n", encoding="utf-8")
        (w.staging_dir / "b.txt").write_text("world\n", encoding="utf-8")
        fp1 = w.fingerprint_staging()
        fp2 = w.fingerprint_staging()
        assert fp1 == fp2, "fingerprint must be stable for unchanged tree"

    def test_fingerprint_changes_when_file_modified(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "a.txt").write_text("hello\n", encoding="utf-8")
        fp1 = w.fingerprint_staging()
        (w.staging_dir / "a.txt").write_text("CHANGED\n", encoding="utf-8")
        fp2 = w.fingerprint_staging()
        assert fp1 != fp2, "fingerprint must change when file content changes"
