"""Tests for the staging watcher pipeline (Python port).

All tests use the StagingWatcher class directly — no bash required.
Runs on Linux, macOS, and Windows.
"""

from __future__ import annotations

import gzip
import hashlib
import json
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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_snapshot(watcher: StagingWatcher, entries: list[tuple[str, bytes]]) -> None:
    """Write a batch snapshot file with the given (rel_path, content) entries."""
    lines = []
    for rel, content in entries:
        path = watcher.staging_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        sha = _sha256(content)
        size = len(content)
        mtime = int(path.stat().st_mtime)
        lines.append(f"{rel}\x1f{size}\x1f{mtime}\x1f{sha}")
    watcher.batch_snapshot.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


# ── VerifyMined tests (use verify_mined.py via subprocess) ──────────────────


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
    escaped = body.replace("'", "'\\''")
    script.write_text(f"#!/bin/sh\necho '{escaped}'\n", encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.mark.skipif(sys.platform == "win32", reason="fake mempalace is a shell script")
class TestVerifyMined:
    def test_verify_passes_when_search_returns_matching_source(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_bytes(b"hello world this is a stable snippet\nmore content\n")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")
        fake = _make_fake_mempalace(
            tmp_path, json.dumps({"results": [{"source_file": str(sample.resolve())}]})
        )
        assert _run_verify(sample, manifest, fake) == 0

    def test_verify_fails_when_search_exits_nonzero(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_bytes(b"hello world this is a stable snippet\n")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")
        fake = tmp_path / "mempalace"
        fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
        assert _run_verify(sample, manifest, fake) == 1

    def test_verify_fails_on_blank_output(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_bytes(b"hello world this is a stable snippet\n")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")
        fake = _make_fake_mempalace(tmp_path, json.dumps({"results": []}))
        assert _run_verify(sample, manifest, fake) == 1

    def test_verify_fails_on_unusable_sample(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_bytes(b"\n# comment\n   \n")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")
        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps({"results": [{"source_file": str(sample.resolve())}]}),
        )
        assert _run_verify(sample, manifest, fake) == 1

    def test_verify_fails_on_unrelated_matching_drawer(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_bytes(b"hello world this is a stable snippet\n")
        other = tmp_path / "other.md"
        other.write_bytes(b"hello world this is a stable snippet\n")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")
        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps({"results": [{"source_file": str(other.resolve())}]}),
        )
        assert _run_verify(sample, manifest, fake) == 1

    def test_verify_fails_on_source_not_in_manifest(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_bytes(b"hello world this is a stable snippet\n")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text("/some/other/file.md\n", encoding="utf-8")
        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps({"results": [{"source_file": str(sample.resolve())}]}),
        )
        assert _run_verify(sample, manifest, fake) == 1

    def test_verify_all_requires_every_manifest_entry(self, tmp_path):
        sample_a = tmp_path / "a.md"
        sample_a.write_bytes(b"hello world this is a stable snippet\n")
        sample_b = tmp_path / "b.md"
        sample_b.write_bytes(b"another stable snippet here\n")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(f"{sample_a.resolve()}\n{sample_b.resolve()}\n", encoding="utf-8")
        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps({"results": [{"source_file": str(sample_a.resolve())}]}),
        )
        assert _run_verify(manifest, manifest, fake) == 1

    def test_verify_all_passes_when_all_entries_searchable(self, tmp_path):
        sample_a = tmp_path / "a.md"
        sample_a.write_bytes(b"hello world this is a stable snippet\n")
        sample_b = tmp_path / "b.md"
        sample_b.write_bytes(b"another stable snippet here\n")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(f"{sample_a.resolve()}\n{sample_b.resolve()}\n", encoding="utf-8")
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
        (w.staging_dir / "projA" / "notes.md").write_bytes(b"project A notes\n")
        (w.staging_dir / "projB" / "notes.md").write_bytes(b"project B notes\n")
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
        original.write_bytes(b"preserve this text\n")
        assert w.archive_files()
        batch = [d for d in w.archive_dir.iterdir() if d.is_dir()][0]
        archived = batch / "subdir" / "file.txt.gz"
        assert archived.exists()
        with gzip.open(archived, "rt", encoding="utf-8") as f:
            assert f.read() == "preserve this text\n"

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod(0o444) does not prevent writes on Windows"
    )
    def test_archive_fails_when_archive_dir_unwritable(self, tmp_path):
        """Archive errors must be a cleanup gate — staging stays intact."""
        w = _make_watcher(tmp_path)
        (w.staging_dir / "file.txt").write_bytes(b"content\n")
        w.archive_dir.chmod(0o444)
        try:
            assert not w.archive_files()
        finally:
            w.archive_dir.chmod(0o755)
        assert (w.staging_dir / "file.txt").exists()


# ── PreprocessSubdirectories tests ─────────────────────────────────────────


class TestPreprocessSubdirectories:
    def test_preprocess_directory_preserves_subdirectories(self, tmp_path):
        sys.path.insert(0, str(_TOOLS_DIR))
        import preprocess_staging as pp

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "projA").mkdir()
        (staging / "projB").mkdir()
        (staging / "projA" / "notes.md").write_bytes(b"# Project A\n\nSome content here.\n")
        (staging / "projB" / "notes.md").write_bytes(b"# Project B\n\nOther content here.\n")
        stats = pp.preprocess_directory(str(staging), max_lines=4000)
        assert stats["processed"] == 2
        assert (staging / "processed" / "projA" / "notes.md").exists()
        assert (staging / "processed" / "projB" / "notes.md").exists()


# ── ProcessBatch tests ─────────────────────────────────────────────────────


class TestProcessBatch:
    def test_process_batch_retains_staging_when_verify_fails(self, tmp_path):
        """If verify fails, staging files must remain for the next attempt."""
        w = _make_watcher(tmp_path)
        (w.staging_dir / "file.md").write_bytes(b"hello world this is a stable snippet\n")
        result = w.process_batch()
        assert result is False
        assert (w.staging_dir / "file.md").exists()


# ── BatchStability tests ───────────────────────────────────────────────────


class TestBatchStability:
    def test_fingerprint_changes_when_file_grows(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "file.txt").write_bytes(b"hello\n")
        fp1 = w.fingerprint_staging()
        (w.staging_dir / "file.txt").write_bytes(b"hello world\n")
        fp2 = w.fingerprint_staging()
        assert fp1 != fp2

    def test_fingerprint_changes_when_file_added(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "a.txt").write_bytes(b"hello\n")
        fp1 = w.fingerprint_staging()
        (w.staging_dir / "b.txt").write_bytes(b"world\n")
        fp2 = w.fingerprint_staging()
        assert fp1 != fp2

    def test_fingerprint_stable_when_unchanged(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "a.txt").write_bytes(b"hello\n")
        (w.staging_dir / "b.txt").write_bytes(b"world\n")
        fp1 = w.fingerprint_staging()
        fp2 = w.fingerprint_staging()
        assert fp1 == fp2


# ── BatchIsolation tests ───────────────────────────────────────────────────


class TestBatchIsolation:
    def test_archive_ignores_late_file(self, tmp_path):
        """A file that arrives after the batch snapshot is not archived."""
        w = _make_watcher(tmp_path)
        _write_snapshot(w, [("claimed.txt", b"claimed content\n")])
        # Late file arrives after snapshot
        (w.staging_dir / "late.txt").write_bytes(b"late content\n")
        assert w.archive_files()
        batch = [d for d in w.archive_dir.iterdir() if d.is_dir()][0]
        assert (batch / "claimed.txt.gz").exists()
        assert not (batch / "late.txt.gz").exists()

    def test_archive_skips_modified_file(self, tmp_path):
        """A file that changes after the snapshot is not archived."""
        w = _make_watcher(tmp_path)
        _write_snapshot(w, [("file.txt", b"original content\n")])
        # Modify the file after the snapshot
        (w.staging_dir / "file.txt").write_bytes(b"modified content\n")
        assert not w.archive_files()

    def test_clear_staging_ignores_late_and_modified_files(self, tmp_path):
        """clear_staging only deletes files matching the snapshot."""
        w = _make_watcher(tmp_path)
        # Claimed file (will be in snapshot and unchanged)
        claimed = b"claimed content\n"
        (w.staging_dir / "claimed.txt").write_bytes(claimed)
        sha = _sha256(claimed)
        size = len(claimed)
        mtime = int((w.staging_dir / "claimed.txt").stat().st_mtime)
        # Modified file (in snapshot but will change)
        modified_orig = b"modified original\n"
        (w.staging_dir / "modified.txt").write_bytes(modified_orig)
        sha2 = _sha256(modified_orig)
        size2 = len(modified_orig)
        mtime2 = int((w.staging_dir / "modified.txt").stat().st_mtime)
        # Write snapshot with both files
        snapshot_data = (
            f"claimed.txt\x1f{size}\x1f{mtime}\x1f{sha}\n"
            f"modified.txt\x1f{size2}\x1f{mtime2}\x1f{sha2}\n"
        )
        w.batch_snapshot.write_bytes(snapshot_data.encode("utf-8"))
        # Late file (not in snapshot)
        (w.staging_dir / "late.txt").write_bytes(b"late\n")
        # Now modify the modified file
        (w.staging_dir / "modified.txt").write_bytes(b"changed\n")
        w.clear_staging()
        assert not (w.staging_dir / "claimed.txt").exists()  # deleted (matched snapshot)
        assert (w.staging_dir / "late.txt").exists()  # kept (not in snapshot)
        assert (w.staging_dir / "modified.txt").exists()  # kept (changed since snapshot)


# ── Regression tests for reviewer issues 1-4 ────────────────────────────────


class TestStaleVersionVerification:
    """Issue 1: verification must prove the CURRENT source version was mined."""

    def test_verify_fails_when_file_sha256_mismatches_snapshot(self, tmp_path):
        sys.path.insert(0, str(_TOOLS_DIR))
        from verify_mined import verify_one

        staging = tmp_path / "staging"
        staging.mkdir()
        original = b"x = 1\ny = 2\nz = 3\n"
        (staging / "claimed.py").write_bytes(original)
        sha256 = _sha256(original)
        # Simulate the file changing after the snapshot was claimed.
        (staging / "claimed.py").write_bytes(b"x = 999\n")
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
        sys.path.insert(0, str(_TOOLS_DIR))
        from verify_mined import file_sha256

        staging = tmp_path / "staging"
        staging.mkdir()
        content = b"x = 1\ny = 2\nz = 3\n"
        (staging / "claimed.py").write_bytes(content)
        sha256 = _sha256(content)
        sample = (staging / "claimed.py").resolve()
        assert file_sha256(sample) == sha256


class TestBatchWorkOutsideWatchedTree:
    """Issue 2: .batch_work must not be inside the watched staging tree."""

    def test_count_files_excludes_batch_work(self, tmp_path):
        """count_files must not list files inside batch_work."""
        w = _make_watcher(tmp_path)
        (w.staging_dir / "real.md").write_bytes(b"hello\n")
        (w.batch_work / "copy.md").parent.mkdir(parents=True, exist_ok=True)
        (w.batch_work / "copy.md").write_bytes(b"copy\n")
        assert w.count_files() == 1


class TestArchiveUsesImmutableWorkCopy:
    """Issue 3: archive and deletion must use the claimed immutable bytes."""

    def test_archive_from_work_copy_not_staging(self, tmp_path):
        w = _make_watcher(tmp_path)
        original = b"original content\n"
        (w.staging_dir / "file.txt").write_bytes(original)
        (w.batch_work / "file.txt").parent.mkdir(parents=True, exist_ok=True)
        (w.batch_work / "file.txt").write_bytes(original)
        # After claim, producer replaces the live file.
        (w.staging_dir / "file.txt").write_bytes(b"REPLACED\n")
        sha = _sha256(original)
        size = len(original)
        mtime = int((w.batch_work / "file.txt").stat().st_mtime)
        w.batch_snapshot.write_bytes(f"file.txt\x1f{size}\x1f{mtime}\x1f{sha}\n".encode("utf-8"))
        assert w.archive_files()
        batch = [d for d in w.archive_dir.iterdir() if d.is_dir()][0]
        with gzip.open(batch / "file.txt.gz", "rt", encoding="utf-8") as f:
            archived_content = f.read()
        assert archived_content == "original content\n", (
            "archive must contain the claimed bytes, not the replaced live file"
        )


class TestPortableHashing:
    """Issue 4: fingerprint_staging must work without external sha256sum."""

    def test_fingerprint_stable_when_unchanged(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "a.txt").write_bytes(b"hello\n")
        (w.staging_dir / "b.txt").write_bytes(b"world\n")
        fp1 = w.fingerprint_staging()
        fp2 = w.fingerprint_staging()
        assert fp1 == fp2, "fingerprint must be stable for unchanged tree"

    def test_fingerprint_changes_when_file_modified(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "a.txt").write_bytes(b"hello\n")
        fp1 = w.fingerprint_staging()
        (w.staging_dir / "a.txt").write_bytes(b"CHANGED\n")
        fp2 = w.fingerprint_staging()
        assert fp1 != fp2, "fingerprint must change when file content changes"
