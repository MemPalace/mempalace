"""Tests for the staging watcher pipeline (Python port).

All tests use the StagingWatcher class directly — no bash required.
Runs on Linux, macOS, and Windows.
"""

from __future__ import annotations

import gzip
import hashlib
import sys
from pathlib import Path

import pytest

# Make tools/ importable
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from staging_watcher import StagingWatcher  # noqa: E402
import verify_mined  # noqa: E402


def _make_watcher(tmp_path: Path, **kwargs) -> StagingWatcher:
    """Create a StagingWatcher with standard test directories."""
    staging = tmp_path / "staging"
    archive = tmp_path / "archive"
    log = tmp_path / "watcher.log"
    work = tmp_path / "batch_work"
    snapshot = tmp_path / ".batch_snapshot"
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


def _claim(watcher: StagingWatcher, entries: list[tuple[str, bytes]]) -> None:
    """Snapshot + work-copy setup for a batch of (rel, content) entries."""
    _write_snapshot(watcher, entries)
    watcher.batch_id = "testbatch"
    watcher.work_orig.mkdir(parents=True, exist_ok=True)
    for rel, content in entries:
        dst = watcher.work_orig / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(content)


def _mark_mined(watcher: StagingWatcher, rels: list[str]) -> None:
    """Record rels as mined in the batch manifest (orig<US>mined pairs)."""
    watcher.batch_dir.mkdir(parents=True, exist_ok=True)
    (watcher.batch_dir / "mined.manifest").write_text(
        "".join(f"{r}\x1f{r}\n" for r in rels), encoding="utf-8"
    )


def _mark_skipped(watcher: StagingWatcher, rels: list[str], reason: str = "filtered") -> None:
    watcher.batch_dir.mkdir(parents=True, exist_ok=True)
    (watcher.batch_dir / "skipped.manifest").write_text(
        "".join(f"{r}\x1f{reason}\n" for r in rels), encoding="utf-8"
    )


# ── VerifyMined tests (verify_mined.py with injected searcher) ──────────────


def _fake_search_ok(must_match: Path):
    """Search stub that returns a hit only for the matching source_file."""

    def _search(query, palace, source_file=None, n_results=5, **_kw):
        if source_file and Path(source_file) == must_match:
            return {"results": [{"source_file": source_file}]}
        return {"results": []}

    return _search


class TestVerifyMined:
    def _setup(self, tmp_path, content=b"hello world this is a stable snippet\n"):
        staging = tmp_path / "staging"
        mine = tmp_path / "mine"
        work = tmp_path / "work"
        for d in (staging, mine, work):
            d.mkdir()
        mined = mine / "batch1" / "a.md"
        mined.parent.mkdir(parents=True)
        mined.write_bytes(content)
        (work / "a.md").write_bytes(content)
        manifest = tmp_path / "mined.manifest"
        manifest.write_text("a.md\x1fbatch1/a.md\n", encoding="utf-8")
        snap = tmp_path / "snap"
        snap.write_text(f"a.md\x1f{len(content)}\x1f0\x1f{_sha256(content)}\n")
        return staging, mine, work, manifest, snap, mined

    def test_passes_when_hit_under_own_source_file(self, tmp_path):
        _, mine, work, manifest, snap, mined = self._setup(tmp_path)
        failures = verify_mined.check_searchable(
            ["batch1/a.md"], str(mine), tmp_path / "palace", 0, _fake_search_ok(mined.resolve())
        )
        assert failures == []

    def test_fails_when_no_hit(self, tmp_path):
        _, mine, work, manifest, snap, _ = self._setup(tmp_path)
        failures = verify_mined.check_searchable(
            ["batch1/a.md"],
            str(mine),
            tmp_path / "palace",
            0,
            lambda *a, **k: {"results": []},
        )
        assert len(failures) == 1

    def test_fails_when_hit_is_different_batch(self, tmp_path):
        """A same-named file from another batch must not satisfy verify."""
        _, mine, work, manifest, snap, mined = self._setup(tmp_path)
        other = mine / "batchOTHER" / "a.md"
        failures = verify_mined.check_searchable(
            ["batch1/a.md"], str(mine), tmp_path / "palace", 0, _fake_search_ok(other)
        )
        assert len(failures) == 1

    def test_fails_on_unusable_snippet(self, tmp_path):
        _, mine, work, manifest, snap, _ = self._setup(tmp_path, content=b"\n   \n  \n")
        failures = verify_mined.check_searchable(
            ["batch1/a.md"], str(mine), tmp_path / "palace", 0, _fake_search_ok(mine)
        )
        assert len(failures) == 1

    def test_fails_on_search_exception(self, tmp_path):
        _, mine, work, manifest, snap, _ = self._setup(tmp_path)

        def _boom(*a, **k):
            raise RuntimeError("palace unavailable")

        failures = verify_mined.check_searchable(
            ["batch1/a.md"], str(mine), tmp_path / "palace", 0, _boom
        )
        assert len(failures) == 1

    def test_hash_check_catches_drift(self, tmp_path):
        _, mine, work, manifest, snap, _ = self._setup(tmp_path)
        (work / "a.md").write_bytes(b"drifted\n")
        bad = verify_mined.check_hashes(["a.md"], verify_mined.load_snapshot(snap), str(work))
        assert len(bad) == 1

    def test_hash_check_matches_verbatim_copy(self, tmp_path):
        """Byte-exact staging makes hash checks portable (no LF/CRLF skew)."""
        _, mine, work, manifest, snap, _ = self._setup(tmp_path)
        bad = verify_mined.check_hashes(["a.md"], verify_mined.load_snapshot(snap), str(work))
        assert bad == []


# ── ArchiveFiles tests ─────────────────────────────────────────────────────


class TestArchiveFiles:
    def test_archive_preserves_subdirectory_paths(self, tmp_path):
        w = _make_watcher(tmp_path)
        _claim(w, [("projA/notes.md", b"project A notes\n"), ("projB/notes.md", b"B\n")])
        _mark_mined(w, ["projA/notes.md", "projB/notes.md"])
        assert w.archive_files()
        batch_dirs = [d for d in w.archive_dir.iterdir() if d.is_dir()]
        assert len(batch_dirs) == 1
        batch = batch_dirs[0]
        assert (batch / "projA" / "notes.md.gz").exists()
        assert (batch / "projB" / "notes.md.gz").exists()
        manifest = (batch / "MANIFEST.txt").read_text(encoding="utf-8")
        assert "file: projA/notes.md" in manifest
        assert "file: projB/notes.md" in manifest

    def test_archive_gzip_content_matches_original(self, tmp_path):
        w = _make_watcher(tmp_path)
        _claim(w, [("subdir/file.txt", b"preserve this text\n")])
        _mark_mined(w, ["subdir/file.txt"])
        assert w.archive_files()
        batch = [d for d in w.archive_dir.iterdir() if d.is_dir()][0]
        with gzip.open(batch / "subdir" / "file.txt.gz", "rt", encoding="utf-8") as f:
            assert f.read() == "preserve this text\n"

    def test_archive_only_mined_files(self, tmp_path):
        """Unmined files are never archived — archive means 'in the palace'."""
        w = _make_watcher(tmp_path)
        _claim(w, [("mined.md", b"mined\n"), ("skipped.jsonl", b"{}\n")])
        _mark_mined(w, ["mined.md"])
        _mark_skipped(w, ["skipped.jsonl"])
        assert w.archive_files()
        batch = [d for d in w.archive_dir.iterdir() if d.is_dir()][0]
        assert (batch / "mined.md.gz").exists()
        assert not (batch / "skipped.jsonl.gz").exists()

    def test_archive_empty_mined_set_is_not_failure(self, tmp_path):
        w = _make_watcher(tmp_path)
        _claim(w, [("only.jsonl", b"{}\n")])
        _mark_skipped(w, ["only.jsonl"])
        assert w.archive_files()  # nothing mined → nothing to archive → ok
        assert not [d for d in w.archive_dir.iterdir() if d.is_dir()]

    @pytest.mark.skipif(
        sys.platform == "win32", reason="chmod(0o444) does not prevent writes on Windows"
    )
    def test_archive_fails_when_archive_dir_unwritable(self, tmp_path):
        """Archive errors must be a cleanup gate — staging stays intact."""
        w = _make_watcher(tmp_path)
        _claim(w, [("file.txt", b"content\n")])
        _mark_mined(w, ["file.txt"])
        w.archive_dir.chmod(0o444)
        try:
            assert not w.archive_files()
        finally:
            w.archive_dir.chmod(0o755)
        assert (w.staging_dir / "file.txt").exists()


# ── PreprocessSubdirectories tests ─────────────────────────────────────────


class TestPreprocessSubdirectories:
    def test_preprocess_directory_preserves_subdirectories(self, tmp_path):
        import preprocess_staging as pp

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "projA").mkdir()
        (staging / "projB").mkdir()
        (staging / "projA" / "notes.md").write_bytes(b"# Project A\n\nSome content here.\n")
        (staging / "projB" / "notes.md").write_bytes(b"# Project B\n\nOther content here.\n")
        mined, _ = pp.preprocess_directory(str(staging), batch_id="b1")
        assert len(mined) == 2
        out = staging / ".mp_processed" / "b1"
        assert (out / "projA" / "notes.md").exists()
        assert (out / "projB" / "notes.md").exists()


# ── ProcessBatch tests ─────────────────────────────────────────────────────


class TestProcessBatch:
    def test_process_batch_retains_staging_when_mine_fails(self, tmp_path):
        """If the pipeline aborts, staging files must remain for retry."""
        w = _make_watcher(tmp_path, mempalace_bin="/nonexistent/mempalace")
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
        assert w.fingerprint_staging() == w.fingerprint_staging()


# ── BatchIsolation tests ───────────────────────────────────────────────────


class TestBatchIsolation:
    def test_batch_id_is_content_keyed(self, tmp_path):
        """Same file set → same batch id → same mine paths → idempotent
        retry; different content → different paths → no cross-batch purge."""
        w = _make_watcher(tmp_path)
        _write_snapshot(w, [("notes.md", b"v1 content\n")])
        snap1 = w.batch_snapshot.read_text()
        id1 = w._hash_stdin(snap1)[:16]
        _write_snapshot(w, [("notes.md", b"v2 content\n")])
        snap2 = w.batch_snapshot.read_text()
        id2 = w._hash_stdin(snap2)[:16]
        assert id1 != id2
        # Identical file set again → identical id (idempotent retry).
        _write_snapshot(w, [("notes.md", b"v1 content\n")])
        assert w._hash_stdin(w.batch_snapshot.read_text())[:16] == id1

    def test_archive_ignores_unmined_file(self, tmp_path):
        """A file that arrives after the snapshot is not archived."""
        w = _make_watcher(tmp_path)
        _claim(w, [("claimed.txt", b"claimed content\n")])
        _mark_mined(w, ["claimed.txt"])
        (w.staging_dir / "late.txt").write_bytes(b"late content\n")
        assert w.archive_files()
        batch = [d for d in w.archive_dir.iterdir() if d.is_dir()][0]
        assert (batch / "claimed.txt.gz").exists()
        assert not (batch / "late.txt.gz").exists()

    def test_archive_skips_drifted_work_copy(self, tmp_path):
        """A work copy that no longer matches the snapshot is not archived."""
        w = _make_watcher(tmp_path)
        _claim(w, [("file.txt", b"original content\n")])
        _mark_mined(w, ["file.txt"])
        (w.work_orig / "file.txt").write_bytes(b"modified content\n")
        assert w.archive_files()
        batch_dirs = [d for d in w.archive_dir.iterdir() if d.is_dir()]
        if batch_dirs:
            assert not (batch_dirs[0] / "file.txt.gz").exists()

    def test_clear_only_moves_mined_files(self, tmp_path):
        """clear_staging moves mined files to retired/, leaves the rest."""
        w = _make_watcher(tmp_path)
        _claim(w, [("claimed.txt", b"claimed content\n"), ("modified.txt", b"orig\n")])
        _mark_mined(w, ["claimed.txt"])
        (w.staging_dir / "modified.txt").write_bytes(b"changed\n")
        (w.staging_dir / "late.txt").write_bytes(b"late\n")
        w.clear_staging()
        assert not (w.staging_dir / "claimed.txt").exists()
        assert (w.retired_dir / "claimed.txt").exists()  # moved, not deleted
        assert (w.staging_dir / "late.txt").exists()  # kept (not in snapshot)
        assert (w.staging_dir / "modified.txt").exists()  # kept (changed)

    def test_clear_never_unlinks_replaced_file(self, tmp_path):
        """A live file that changed since claim is preserved, not deleted."""
        w = _make_watcher(tmp_path)
        _claim(w, [("file.txt", b"original\n")])
        _mark_mined(w, ["file.txt"])
        (w.staging_dir / "file.txt").write_bytes(b"NEW CONTENT\n")
        w.clear_staging()
        assert (w.staging_dir / "file.txt").read_bytes() == b"NEW CONTENT\n"


# ── Quarantine tests ───────────────────────────────────────────────────────


class TestQuarantine:
    def test_skipped_files_quarantined_not_deleted(self, tmp_path):
        w = _make_watcher(tmp_path)
        _claim(w, [("data.jsonl", b"{}\n")])
        _mark_skipped(w, ["data.jsonl"])
        w.quarantine_skipped()
        assert not (w.staging_dir / "data.jsonl").exists()
        quarantined = list((w.staging_dir / ".mp_quarantine").rglob("data.jsonl"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == b"{}\n"

    def test_quarantined_files_not_rescanned(self, tmp_path):
        w = _make_watcher(tmp_path)
        qdir = w.staging_dir / ".mp_quarantine" / "oldbatch"
        qdir.mkdir(parents=True)
        (qdir / "data.jsonl").write_bytes(b"{}\n")
        assert w.count_files() == 0

    def test_changed_file_not_quarantined(self, tmp_path):
        w = _make_watcher(tmp_path)
        _claim(w, [("data.jsonl", b"{}\n")])
        _mark_skipped(w, ["data.jsonl"])
        (w.staging_dir / "data.jsonl").write_bytes(b'{"new": true}\n')
        w.quarantine_skipped()
        assert (w.staging_dir / "data.jsonl").exists()  # new drop stays


# ── Regression tests for reviewer issues ────────────────────────────────────


class TestStaleVersionVerification:
    """Verification must prove the CURRENT source version was mined."""

    def test_snapshot_hash_detects_post_claim_drift(self, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        original = b"x = 1\ny = 2\nz = 3\n"
        (staging / "claimed.py").write_bytes(original)
        sha = _sha256(original)
        (staging / "claimed.py").write_bytes(b"x = 999\n")
        assert verify_mined._sha256(staging / "claimed.py") != sha


class TestBatchWorkOutsideWatchedTree:
    """Batch work dirs must not be inside the watched staging tree."""

    def test_count_files_excludes_batch_work(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "real.md").write_bytes(b"hello\n")
        (w.batch_work / "copy.md").parent.mkdir(parents=True, exist_ok=True)
        (w.batch_work / "copy.md").write_bytes(b"copy\n")
        assert w.count_files() == 1


class TestArchiveUsesImmutableWorkCopy:
    """Archive must contain the claimed bytes, not a replaced live file."""

    def test_archive_from_work_copy_not_staging(self, tmp_path):
        w = _make_watcher(tmp_path)
        original = b"original content\n"
        _claim(w, [("file.txt", original)])
        _mark_mined(w, ["file.txt"])
        # After claim, producer replaces the live file.
        (w.staging_dir / "file.txt").write_bytes(b"REPLACED\n")
        assert w.archive_files()
        batch = [d for d in w.archive_dir.iterdir() if d.is_dir()][0]
        with gzip.open(batch / "file.txt.gz", "rt", encoding="utf-8") as f:
            assert f.read() == "original content\n"


class TestPortableHashing:
    """fingerprint_staging must work without external sha256sum."""

    def test_fingerprint_stable_when_unchanged(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "a.txt").write_bytes(b"hello\n")
        (w.staging_dir / "b.txt").write_bytes(b"world\n")
        assert w.fingerprint_staging() == w.fingerprint_staging()

    def test_fingerprint_changes_when_file_modified(self, tmp_path):
        w = _make_watcher(tmp_path)
        (w.staging_dir / "a.txt").write_bytes(b"hello\n")
        fp1 = w.fingerprint_staging()
        (w.staging_dir / "a.txt").write_bytes(b"CHANGED\n")
        assert w.fingerprint_staging() != fp1
