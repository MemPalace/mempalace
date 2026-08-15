"""Tests for the staging watcher pipeline: verify, archive, and preprocess."""

import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _write_snapshot(staging: Path, rels: list[str]) -> Path:
    """Create a unit-separator batch snapshot for the given relative paths."""
    snapshot = staging / ".batch_snapshot"
    lines: list[str] = []
    for rel in rels:
        p = staging / rel
        content = p.read_bytes()
        size = len(content)
        mtime = int(p.stat().st_mtime)
        h = hashlib.sha256(content).hexdigest()
        lines.append(f"{rel}\x1f{size}\x1f{mtime}\x1f{h}\n")
    snapshot.write_text("".join(lines), encoding="utf-8")
    return snapshot


def _run_bash_function(func: str, env: dict, staging: Path, check: bool = False) -> subprocess.CompletedProcess:
    """Source staging_watcher.sh and call a single function."""
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    full_env = os.environ.copy()
    full_env.update(env)
    full_env.setdefault("STAGING_DIR", str(staging))
    full_env.setdefault("LOG_FILE", str(staging.parent / "watcher.log"))
    full_env.setdefault("STAGING_WATCHER_TEST_MODE", "1")
    return subprocess.run(
        ["bash", "-c", f"source '{tools_dir / 'staging_watcher.sh'}' && {func}"],
        env=full_env,
        capture_output=True,
        text=True,
        check=check,
    )

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


class TestProcessBatch:
    """End-to-end batch regressions (fatkobra review)."""

    def test_process_batch_retains_staging_when_verify_fails(self, tmp_path):
        """If one file mines and another is skipped, staging must not be cleared."""
        staging = tmp_path / "staging"
        staging.mkdir()
        archive = tmp_path / "archive"
        palace = tmp_path / "palace"
        log = tmp_path / "watcher.log"

        (staging / "good.md").write_text(
            "hello world this is the good file\n", encoding="utf-8"
        )
        (staging / "bad.md").write_text(
            "this is the bad file that exceeds chunk cap\n", encoding="utf-8"
        )
        archive.mkdir()
        palace.mkdir()

        fake_mempalace = tmp_path / "mempalace"
        fake_mempalace.write_text(
            "#!/usr/bin/env sh\n"
            "for arg in \"$@\"; do\n"
            '  if [ "$arg" = "mine" ] || [ "$arg" = "compress" ]; then\n'
            "    exit 0\n"
            "  fi\n"
            "done\n"
            "source=\"\"\n"
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "--source-file" ]; then source="$2"; fi\n'
            "  shift\n"
            "done\n"
            'if [ "$(basename "$source")" = "good.md" ]; then\n'
            '  echo \'{"results": [{"source_file": "\'"$source"\'"}]}\'\n'
            "else\n"
            '  echo \'{"results": []}\'\n'
            "fi\n",
            encoding="utf-8",
        )
        fake_mempalace.chmod(0o755)

        env = os.environ.copy()
        env["STAGING_DIR"] = str(staging)
        env["ARCHIVE_DIR"] = str(archive)
        env["PALACE_PATH"] = str(palace)
        env["LOG_FILE"] = str(log)
        env["STAGING_WATCHER_TEST_MODE"] = "1"
        env["MEMPALACE_BIN"] = str(fake_mempalace)

        result = subprocess.run(
            ["bash", "-c", f"source '{_TOOLS_DIR / 'staging_watcher.sh'}' && process_batch"],
            env=env,
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0, result.stderr

        # Staging originals must still be present (cleanup was gated by verify).
        assert (staging / "good.md").exists()
        assert (staging / "bad.md").exists()

        # No final archive directory should have been created.
        final_batches = [d for d in archive.iterdir() if d.is_dir() and not d.name.startswith(".")]
        assert len(final_batches) == 0


class TestBatchStability:
    """Regressions for content-based debounce (fatkobra review)."""

    def test_fingerprint_changes_when_file_grows(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        f = staging / "growing.md"
        f.write_text("hello\n", encoding="utf-8")

        before = _run_bash_function(
            f"fingerprint_staging '{staging}'",
            {"STAGING_DIR": str(staging)},
            staging,
        )
        assert before.returncode == 0

        f.write_text("hello\nworld\n", encoding="utf-8")

        after = _run_bash_function(
            f"fingerprint_staging '{staging}'",
            {"STAGING_DIR": str(staging)},
            staging,
        )
        assert after.returncode == 0
        assert before.stdout.strip() != after.stdout.strip()

    def test_fingerprint_changes_when_file_added(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "a.md").write_text("hello\n", encoding="utf-8")

        before = _run_bash_function(
            f"fingerprint_staging '{staging}'",
            {"STAGING_DIR": str(staging)},
            staging,
        )

        (staging / "b.md").write_text("world\n", encoding="utf-8")

        after = _run_bash_function(
            f"fingerprint_staging '{staging}'",
            {"STAGING_DIR": str(staging)},
            staging,
        )
        assert before.stdout.strip() != after.stdout.strip()

    def test_fingerprint_stable_when_unchanged(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "a.md").write_text("hello\n", encoding="utf-8")

        first = _run_bash_function(
            f"fingerprint_staging '{staging}'",
            {"STAGING_DIR": str(staging)},
            staging,
        )
        second = _run_bash_function(
            f"fingerprint_staging '{staging}'",
            {"STAGING_DIR": str(staging)},
            staging,
        )
        assert first.stdout.strip() == second.stdout.strip()


class TestBatchIsolation:
    """Regressions for immutable batch claim (fatkobra review)."""

    def test_archive_ignores_late_file(self, tmp_path):
        staging = tmp_path / "staging"
        archive = tmp_path / "archive"
        staging.mkdir()
        archive.mkdir()

        (staging / "claimed.md").write_text("claimed\n", encoding="utf-8")
        _write_snapshot(staging, ["claimed.md"])

        # Late file arrives after the batch was claimed.
        (staging / "late.md").write_text("late\n", encoding="utf-8")

        _run_bash_function("archive_files", {"ARCHIVE_DIR": str(archive)}, staging, check=True)

        batch = [d for d in archive.iterdir() if d.is_dir()][0]
        assert (batch / "claimed.md.gz").exists()
        assert not (batch / "late.md.gz").exists()
        assert (staging / "late.md").exists()

    def test_archive_skips_modified_file(self, tmp_path):
        staging = tmp_path / "staging"
        archive = tmp_path / "archive"
        staging.mkdir()
        archive.mkdir()

        (staging / "claimed.md").write_text("claimed\n", encoding="utf-8")
        _write_snapshot(staging, ["claimed.md"])
        # Modify the claimed file before archive runs.
        (staging / "claimed.md").write_text("claimed\nmodified\n", encoding="utf-8")

        result = _run_bash_function("archive_files", {"ARCHIVE_DIR": str(archive)}, staging)

        assert result.returncode != 0
        # No final archive should be created.
        final_batches = [d for d in archive.iterdir() if d.is_dir() and not d.name.startswith(".")]
        assert len(final_batches) == 0
        # The modified file must remain in staging.
        assert (staging / "claimed.md").exists()

    def test_clear_staging_ignores_late_and_modified_files(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()

        (staging / "claimed.md").write_text("claimed\n", encoding="utf-8")
        _write_snapshot(staging, ["claimed.md"])

        # Late file arrives; claimed file is modified.
        (staging / "late.md").write_text("late\n", encoding="utf-8")
        time.sleep(0.1)
        (staging / "claimed.md").write_text("claimed\nmodified\n", encoding="utf-8")

        _run_bash_function("clear_staging", {}, staging, check=True)

        # Late file should remain, modified claimed file should remain.
        assert (staging / "late.md").exists()
        assert (staging / "claimed.md").exists()
        # processed/ and metadata should be gone.
        assert not (staging / "processed").exists()
        assert not (staging / ".batch_snapshot").exists()
        assert not (staging / ".batch_manifest").exists()
