"""Tests for the staging watcher pipeline: verify, archive, and preprocess."""

import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

# tools/ is not a package
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


def _run_verify(sample: Path, manifest: Path, fake_mempalace: Path) -> int:
    """Run verify_mined.py and return its exit code."""
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
    """Regressions for staging_watcher verification (fatkobra review)."""

    def test_verify_passes_when_search_returns_matching_source(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_text("hello world this is a stable snippet\nmore content\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")

        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps({"results": [{"source_file": str(sample.resolve())}]}),
        )
        assert _run_verify(sample, manifest, fake) == 0

    def test_verify_fails_when_search_exits_nonzero(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_text("hello world this is a stable snippet\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")

        fake = tmp_path / "mempalace"
        fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
        assert _run_verify(sample, manifest, fake) == 1

    def test_verify_fails_on_blank_output(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_text("hello world this is a stable snippet\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")

        fake = _make_fake_mempalace(tmp_path, json.dumps({"results": []}))
        assert _run_verify(sample, manifest, fake) == 1

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

    def test_verify_fails_on_unrelated_matching_drawer(self, tmp_path):
        sample = tmp_path / "sample.md"
        sample.write_text("hello world this is a stable snippet\n", encoding="utf-8")
        manifest = tmp_path / "manifest.txt"
        manifest.write_text(str(sample.resolve()) + "\n", encoding="utf-8")

        # Search returns a hit from an older, unrelated source that is not the
        # sample file and not in the batch manifest.
        fake = _make_fake_mempalace(
            tmp_path,
            json.dumps({"results": [{"source_file": "/some/older/batch/notes.md"}]}),
        )
        assert _run_verify(sample, manifest, fake) == 1

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

    def test_verify_all_requires_every_manifest_entry(self, tmp_path):
        """Complete-manifest verification fails if any entry is not searchable."""
        good = tmp_path / "good.md"
        bad = tmp_path / "bad.md"
        good.write_text("hello world this is good\n", encoding="utf-8")
        bad.write_text("this is also a stable snippet for the bad file\n", encoding="utf-8")

        manifest = tmp_path / "manifest.txt"
        manifest.write_text(
            str(good.resolve()) + "\n" + str(bad.resolve()) + "\n",
            encoding="utf-8",
        )

        # Fake returns a hit only for `good`; `bad` gets an empty result.
        fake = tmp_path / "mempalace"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, json\n"
            "args = sys.argv\n"
            "source = args[args.index('--source-file') + 1] if '--source-file' in args else ''\n"
            f"good = '{str(good.resolve())}'\n"
            "if source == good:\n"
            "    print(json.dumps({'results': [{'source_file': good}]}))\n"
            "else:\n"
            "    print(json.dumps({'results': []}))\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)

        # Passing the manifest as both sample and manifest enables --all mode.
        assert _run_verify(manifest, manifest, fake) == 1

    def test_verify_all_passes_when_all_entries_searchable(self, tmp_path):
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text("hello world this is file a\n", encoding="utf-8")
        b.write_text("hello world this is file b\n", encoding="utf-8")

        manifest = tmp_path / "manifest.txt"
        manifest.write_text(
            str(a.resolve()) + "\n" + str(b.resolve()) + "\n",
            encoding="utf-8",
        )

        # Fake returns a hit for whichever source-file was requested.
        fake = tmp_path / "mempalace"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, json\n"
            "args = sys.argv\n"
            "source = args[args.index('--source-file') + 1] if '--source-file' in args else ''\n"
            "print(json.dumps({'results': [{'source_file': source}]}))\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)

        assert _run_verify(manifest, manifest, fake) == 0


class TestArchiveFiles:
    """Regressions for archive name collisions (mvalentsev review)."""

    def test_archive_preserves_subdirectory_paths(self, tmp_path):
        staging = tmp_path / "staging"
        archive = tmp_path / "archive"
        log = tmp_path / "watcher.log"
        (staging / "projA").mkdir(parents=True)
        (staging / "projB").mkdir(parents=True)
        (staging / "projA" / "notes.md").write_text("project A notes\n", encoding="utf-8")
        (staging / "projB" / "notes.md").write_text("project B notes\n", encoding="utf-8")

        env = os.environ.copy()
        env["STAGING_DIR"] = str(staging)
        env["ARCHIVE_DIR"] = str(archive)
        env["LOG_FILE"] = str(log)
        env["STAGING_WATCHER_TEST_MODE"] = "1"

        result = subprocess.run(
            ["bash", "-c", f"source '{_TOOLS_DIR / 'staging_watcher.sh'}' && archive_files"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

        batch_dirs = [d for d in archive.iterdir() if d.is_dir()]
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
        staging = tmp_path / "staging"
        archive = tmp_path / "archive"
        log = tmp_path / "watcher.log"
        (staging / "subdir").mkdir(parents=True)
        original = staging / "subdir" / "file.txt"
        original.write_text("preserve this text\n", encoding="utf-8")

        env = os.environ.copy()
        env["STAGING_DIR"] = str(staging)
        env["ARCHIVE_DIR"] = str(archive)
        env["LOG_FILE"] = str(log)
        env["STAGING_WATCHER_TEST_MODE"] = "1"

        subprocess.run(
            ["bash", "-c", f"source '{_TOOLS_DIR / 'staging_watcher.sh'}' && archive_files"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )

        batch = [d for d in archive.iterdir() if d.is_dir()][0]
        archived = batch / "subdir" / "file.txt.gz"
        assert archived.exists()
        with gzip.open(archived, "rt", encoding="utf-8") as f:
            assert f.read() == "preserve this text\n"

    def test_archive_fails_when_archive_dir_unwritable(self, tmp_path):
        """Archive errors must be a cleanup gate — staging stays intact."""
        staging = tmp_path / "staging"
        staging.mkdir()
        archive = tmp_path / "archive"
        log = tmp_path / "watcher.log"
        original = staging / "file.txt"
        original.write_text("keep me\n", encoding="utf-8")

        # Make the archive directory unwritable (parent is still writable).
        archive.mkdir()
        archive.chmod(0o000)

        env = os.environ.copy()
        env["STAGING_DIR"] = str(staging)
        env["ARCHIVE_DIR"] = str(archive)
        env["LOG_FILE"] = str(log)
        env["STAGING_WATCHER_TEST_MODE"] = "1"

        result = subprocess.run(
            ["bash", "-c", f"source '{_TOOLS_DIR / 'staging_watcher.sh'}' && archive_files"],
            env=env,
            capture_output=True,
            text=True,
        )

        archive.chmod(0o755)

        assert result.returncode != 0
        # Staging original must not have been removed by this function.
        assert original.exists()
        # No final archive directory should be left at the top level.
        final_batches = [d for d in archive.iterdir() if d.is_dir() and not d.name.startswith(".")]
        assert len(final_batches) == 0


class TestPreprocessSubdirectories:
    """Ensure preprocessing does not flatten subdirectories (root cause of archive collisions)."""

    def test_preprocess_directory_preserves_subdirectories(self, tmp_path):
        sys.path.insert(0, str(_TOOLS_DIR))
        try:
            import preprocess_staging as pp

            staging = tmp_path / "staging"
            (staging / "projA").mkdir(parents=True)
            (staging / "projB").mkdir(parents=True)
            (staging / "projA" / "notes.md").write_text("project A notes\n", encoding="utf-8")
            (staging / "projB" / "notes.md").write_text("project B notes\n", encoding="utf-8")

            pp.preprocess_directory(str(staging), max_lines=4000)

            assert (staging / "processed" / "projA" / "notes.md").exists()
            assert (staging / "processed" / "projB" / "notes.md").exists()
            assert (staging / "processed" / "projA" / "notes.md").read_text() == "project A notes\n"
            assert (staging / "processed" / "projB" / "notes.md").read_text() == "project B notes\n"
        finally:
            sys.path.pop(0)
