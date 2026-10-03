#!/usr/bin/env python3
"""Staging watcher — cross-platform Python port of staging_watcher.sh.

Watches a staging directory for new files and runs the full MemPalace
ingest pipeline:

    stage (verbatim) → mine → verify → compress → archive → clear

When files arrive and stabilize (no writes for DEBOUNCE_SECONDS):
  1. Claim an immutable batch snapshot and copy every claimed file into a
     private work dir, verifying sha256.
  2. Stage VERBATIM copies into a per-batch mine dir (content-keyed batch
     id — the only transformation is splitting files over MAX_LINES).
  3. Mine the batch dir into the palace.
  4. Verify mined files are searchable under their own source_file.
  5. Compress: run mempalace compress (AAAK dialect).
  6. Archive ONLY mined files: gzip verified work copies to archive/.
  7. Quarantine skipped files under staging/.mp_quarantine/<batch_id>/
     and clear mined files by moving them into the work dir — never
     unlinking a live staging path.

Usage:
    python staging_watcher.py /path/to/staging /path/to/palace

Or with environment variables:
    STAGING_DIR=/path/to/staging PALACE_PATH=/path/to/palace python staging_watcher.py

For testing, import functions directly:
    from tools.staging_watcher import count_files, archive_files, ...
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration (overridable via env vars or constructor args) ─────────────

_TOOLS_DIR = Path(__file__).resolve().parent

# Files excluded from batch processing.
_EXCLUDED_NAMES = {".DS_Store", "mempalace.yaml", ".batch_manifest", ".batch_snapshot"}
_EXCLUDED_SUFFIXES = {".tmp"}


class StagingWatcher:
    """Cross-platform staging watcher pipeline."""

    def __init__(
        self,
        staging_dir: str | Path | None = None,
        palace_path: str | Path | None = None,
        *,
        archive_dir: str | Path | None = None,
        mempalace_bin: str | None = None,
        python_bin: str | None = None,
        log_file: str | Path | None = None,
        debounce_seconds: int | None = None,
        min_files: int | None = None,
        max_lines: int | None = None,
        work_root: str | Path | None = None,
        batch_snapshot: str | Path | None = None,
        batch_work: str | Path | None = None,
    ):
        self.staging_dir = Path(
            staging_dir or os.environ.get("STAGING_DIR", "/tmp/mempalace-staging")
        ).resolve()
        self.palace_path = Path(
            palace_path or os.environ.get("PALACE_PATH", str(Path.home() / ".mempalace/palace"))
        )
        self.archive_dir = Path(
            archive_dir or os.environ.get("ARCHIVE_DIR", str(Path.home() / ".mempalace/archive"))
        )
        self.mine_agent = os.environ.get("MINE_AGENT", "staging-watcher")
        self.mempalace_bin = mempalace_bin or os.environ.get("MEMPALACE_BIN", "mempalace")
        self.python_bin = python_bin or os.environ.get("PYTHON_BIN", sys.executable)
        self.preprocess_script = _TOOLS_DIR / "preprocess_staging.py"
        self.verify_script = _TOOLS_DIR / "verify_mined.py"

        log_dir = Path(os.environ.get("LOG_DIR", str(Path.home() / ".mempalace/logs")))
        self.log_file = Path(
            log_file or os.environ.get("LOG_FILE", str(log_dir / "staging-watcher.log"))
        )

        self.debounce_seconds = int(
            debounce_seconds
            if debounce_seconds is not None
            else os.environ.get("DEBOUNCE_SECONDS", 30)
        )
        self.min_files = int(min_files if min_files is not None else os.environ.get("MIN_FILES", 1))
        self.max_lines = int(
            max_lines if max_lines is not None else os.environ.get("MAX_LINES", 4000)
        )

        wr = Path(
            work_root or os.environ.get("WORK_ROOT") or tempfile.mkdtemp(prefix="mempalace-batch-")
        )
        self.work_root = wr
        self.batch_snapshot = Path(
            batch_snapshot or os.environ.get("BATCH_SNAPSHOT") or wr / ".batch_snapshot"
        )
        # Per-batch dirs — claim_batch re-roots them under the batch dir
        # once the content-keyed batch id is known. A BATCH_WORK override
        # supplies the batch dir parent for tests.
        self.batch_id = ""
        _batch_dir_override = batch_work or os.environ.get("BATCH_WORK")
        self._batch_dir_overridden = _batch_dir_override is not None
        self.batch_dir = Path(_batch_dir_override) if _batch_dir_override else wr / "batch-pending"
        self.work_orig = self.batch_dir / "orig"
        self.batch_work = self.work_orig  # alias for legacy callers/tests
        self.mine_dir = self.batch_dir / "mine"
        self.retired_dir = self.batch_dir / "retired"

        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    # ── Logging ─────────────────────────────────────────────────────────────

    def log(self, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")

    # ── File helpers (cross-platform) ───────────────────────────────────────

    @staticmethod
    def file_size(path: Path) -> int:
        return path.stat().st_size

    @staticmethod
    def file_mtime(path: Path) -> int:
        return int(path.stat().st_mtime)

    @staticmethod
    def file_sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # ── Batch file discovery ────────────────────────────────────────────────

    def find_batch_files(self, target: Path | None = None) -> list[Path]:
        """List all files that should participate in the batch."""
        target = target or self.staging_dir
        results: list[Path] = []
        for p in sorted(target.rglob("*")):
            if not p.is_file():
                continue
            if p.name in _EXCLUDED_NAMES:
                continue
            if p.suffix in _EXCLUDED_SUFFIXES:
                continue
            # Skip dot-prefixed path components relative to the scan root —
            # covers .mp_quarantine, .mp_processed, .git, and any hidden
            # user dir. Ordinary dirs named "processed" are content.
            rel_parts = p.relative_to(target).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            results.append(p)
        return results

    def count_files(self) -> int:
        return len(self.find_batch_files())

    # ── Snapshot ───────────────────────────────────────────────────────────

    def snapshot_staging(self, target: Path | None = None) -> str:
        """Write a unit-separator-delimited snapshot to stdout.

        Columns: relative_path\\x1fsize\\x1fmtime\\x1fsha256
        """
        target = target or self.staging_dir
        lines: list[str] = []
        for f in self.find_batch_files(target):
            rel = f.relative_to(target).as_posix()
            size = self.file_size(f)
            mtime = self.file_mtime(f)
            sha = self.file_sha256(f)
            lines.append(f"{rel}\x1f{size}\x1f{mtime}\x1f{sha}")
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _hash_stdin(data: str) -> str:
        """Hash a string with SHA-256.  Always available (uses hashlib)."""
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def fingerprint_staging(self, target: Path | None = None) -> str:
        """Return a single hash representing the current staging tree state."""
        snapshot = self.snapshot_staging(target)
        return self._hash_stdin(snapshot)

    # ── Debounce ───────────────────────────────────────────────────────────

    def wait_for_stable(self) -> None:
        last_fp = ""
        stable = 0
        while stable < self.debounce_seconds:
            current_fp = self.fingerprint_staging()
            current_count = self.count_files()
            if current_fp == last_fp and current_count >= self.min_files:
                stable += 5
            else:
                stable = 0
                last_fp = current_fp
            time.sleep(5)

    # ── Snapshot line parsing ───────────────────────────────────────────────

    @staticmethod
    def parse_snapshot_line(line: str) -> tuple[str, str, str, str]:
        """Parse a unit-separator-delimited snapshot line."""
        parts = line.rstrip("\n").split("\x1f", 3)
        if len(parts) < 4:
            return "", "", "", ""
        return parts[0], parts[1], parts[2], parts[3]

    # ── Claim batch ────────────────────────────────────────────────────────

    def claim_batch(self) -> bool:
        snapshot = self.snapshot_staging()
        if not snapshot.strip():
            self.log("Claim FAILED: no files to batch")
            return False
        self.batch_snapshot.write_text(snapshot, encoding="utf-8")
        # Content-addressed batch id: retrying the same file set re-uses the
        # same mine paths, so a retry is an idempotent replace (the miner
        # purges by source_file), never a duplicate insert. A different file
        # set yields different paths, so cross-batch purge cannot happen.
        self.batch_id = self._hash_stdin(snapshot)[:16]
        # Retire the PREVIOUS batch dir now — its retired/ copy survived one
        # full batch cycle as post-hoc insurance; the archive holds the
        # canonical bytes.
        prev = getattr(self, "_prev_batch_dir", None)
        if prev is not None and Path(prev).exists():
            shutil.rmtree(prev, ignore_errors=True)
        if not self._batch_dir_overridden:
            self.batch_dir = self.work_root / f"batch-{self.batch_id}"
        self._prev_batch_dir = self.batch_dir
        self.work_orig = self.batch_dir / "orig"
        for d in (self.work_orig, self.mine_dir, self.retired_dir):
            d.mkdir(parents=True, exist_ok=True)
        # Copy ALL claimed files into the batch work dir with sha256 verification.
        for line in snapshot.strip().split("\n"):
            rel_path, _size, _mtime, expected_hash = self.parse_snapshot_line(line)
            if not rel_path or rel_path in _EXCLUDED_NAMES:
                continue
            src = self.staging_dir / rel_path
            if not src.exists():
                continue
            dst = self.work_orig / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied_hash = self.file_sha256(dst)
            if copied_hash != expected_hash:
                self.log(f"Claim SKIP: {rel_path} changed during claim (hash mismatch)")
                dst.unlink(missing_ok=True)
        count = len([line for line in snapshot.strip().split("\n") if line])
        self.log(f"Claimed batch {self.batch_id}: {count} files -> {self.batch_snapshot}")
        return True

    # ── Preprocess ─────────────────────────────────────────────────────────

    def preprocess_staging(self) -> bool:
        """Stage verified work copies into mine_dir — verbatim, split-only."""
        self.log(f"Staging batch {self.batch_id} for mining (verbatim; split >{self.max_lines})...")
        cmd = [
            self.python_bin,
            str(self.preprocess_script),
            str(self.staging_dir),
            "--work-dir",
            str(self.work_orig),
            "--batch-snapshot",
            str(self.batch_snapshot),
            "--out-dir",
            str(self.mine_dir),
            "--batch-id",
            self.batch_id or "batch",
            "--manifest",
            str(self.batch_dir / "mined.manifest"),
            "--skip-manifest",
            str(self.batch_dir / "skipped.manifest"),
            "--max-lines",
            str(self.max_lines),
        ]
        with self.log_file.open("a", encoding="utf-8") as logf:
            try:
                result = subprocess.run(cmd, stdout=logf, stderr=logf)
            except FileNotFoundError as e:
                self.log(f"Stage FAILED: {e}")
                return False
        if result.returncode == 0:
            mined_manifest = self.batch_dir / "mined.manifest"
            mined_count = (
                len(mined_manifest.read_text(encoding="utf-8").splitlines())
                if mined_manifest.exists()
                else 0
            )
            # Copy mempalace.yaml into the mine dir for wing routing (the
            # miner skips the file itself by name).
            yaml_src = self.staging_dir / "mempalace.yaml"
            if yaml_src.exists():
                shutil.copy2(yaml_src, self.mine_dir / "mempalace.yaml")
            self.log(f"Stage complete: {mined_count} mine-ready file(s)")
            return True
        self.log(f"Stage FAILED (exit {result.returncode})")
        return False

    # ── Mine ───────────────────────────────────────────────────────────────

    def mine_processed(self) -> bool:
        if not self.mine_dir.exists():
            self.log("Mine: no staged directory — skipping")
            return True
        file_count = len(
            [p for p in self.mine_dir.rglob("*") if p.is_file() and p.name != "mempalace.yaml"]
        )
        if file_count == 0:
            self.log("Mine: no staged files to mine — skipping")
            return True
        self.log(f"Mining {file_count} staged files...")
        cmd = [
            self.mempalace_bin,
            "--palace",
            str(self.palace_path),
            "mine",
            str(self.mine_dir),
            "--agent",
            self.mine_agent,
            "--max-chunks-per-file",
            "500",
        ]
        with self.log_file.open("a", encoding="utf-8") as logf:
            try:
                result = subprocess.run(cmd, stdout=logf, stderr=logf)
            except FileNotFoundError as e:
                self.log(f"Mine FAILED: {e}")
                return False
        if result.returncode == 0:
            self.log(f"Mine complete ({file_count} files)")
            return True
        self.log(f"Mine FAILED (exit {result.returncode})")
        return False

    # ── Mined-set helpers ──────────────────────────────────────────────────

    def mined_orig_rels(self) -> list[str]:
        """Original rel paths that produced mined output (manifest col 1,
        deduplicated — a split file appears once per part)."""
        manifest = self.batch_dir / "mined.manifest"
        if not manifest.exists() or manifest.stat().st_size == 0:
            return []
        rels = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            parts = line.split("\x1f")
            if parts and parts[0]:
                rels.append(parts[0])
        return sorted(set(rels))

    def skipped_rels(self) -> list[str]:
        """Rel paths that produced no mined output (skip manifest col 1)."""
        manifest = self.batch_dir / "skipped.manifest"
        if not manifest.exists() or manifest.stat().st_size == 0:
            return []
        rels = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            parts = line.split("\x1f")
            if parts and parts[0]:
                rels.append(parts[0])
        return rels

    # ── Verify ─────────────────────────────────────────────────────────────

    def verify_mined(self) -> bool:
        manifest = self.batch_dir / "mined.manifest"
        if not manifest.exists() or manifest.stat().st_size == 0:
            # Nothing was mined — nothing to verify. Quarantine still runs so
            # unprocessable files stop re-triggering the batch.
            self.log("Verify: manifest empty (nothing mined) — skipping search checks")
            return True
        self.log(f"Verify: checking searchability of mined files under {self.mine_dir}")
        cmd = [
            self.python_bin,
            str(self.verify_script),
            "--palace",
            str(self.palace_path),
            "--manifest",
            str(manifest),
            "--mine-dir",
            str(self.mine_dir),
            "--snapshot",
            str(self.batch_snapshot),
            "--work-dir",
            str(self.work_orig),
            "--all",
        ]
        with self.log_file.open("a", encoding="utf-8") as logf:
            result = subprocess.run(cmd, stdout=logf, stderr=logf)
        if result.returncode == 0:
            self.log("Verify: all mined files are searchable under this batch's source paths")
            return True
        self.log("Verify FAILED: one or more mined files are not searchable")
        return False

    # ── Compress ───────────────────────────────────────────────────────────

    def compress_palace(self) -> None:
        self.log("Compressing palace (AAAK dialect)...")
        cmd = [self.mempalace_bin, "--palace", str(self.palace_path), "compress"]
        with self.log_file.open("a", encoding="utf-8") as logf:
            result = subprocess.run(cmd, stdout=logf, stderr=logf)
        if result.returncode == 0:
            self.log("Compress complete")
        else:
            self.log(f"Compress FAILED (exit {result.returncode}) — continuing (non-fatal)")

    # ── Archive ────────────────────────────────────────────────────────────

    def archive_files(self) -> bool:
        batch_date = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        batch_archive = self.archive_dir / batch_date
        batch_archive_tmp = self.archive_dir / f".tmp.{batch_date}.{os.getpid()}"
        # Build into a temp directory and atomically rename on success.
        try:
            if batch_archive_tmp.exists():
                shutil.rmtree(batch_archive_tmp, ignore_errors=True)
            batch_archive_tmp.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as e:
            self.log(f"Archive FAILED: cannot create temp archive dir: {e}")
            return False

        manifest = batch_archive_tmp / "MANIFEST.txt"
        manifest_header = (
            f"# MemPalace Archive Manifest\n"
            f"# Batch: {batch_date}\n"
            f"# Batch ID: {self.batch_id}\n"
            f"# Mined: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"# Palace: {self.palace_path}\n"
            f"# Pipeline: stage -> mine -> verify -> compress -> gzip -> archive\n\n"
        )

        # Snapshot hash lookup for the archive integrity check.
        snapshot_hashes: dict[str, str] = {}
        if self.batch_snapshot.exists():
            for line in self.batch_snapshot.read_text(encoding="utf-8").splitlines():
                rel, _size, _mtime, sha = self.parse_snapshot_line(line)
                if rel:
                    snapshot_hashes[rel] = sha

        manifest_lines: list[str] = [manifest_header]
        file_count = 0

        # Archive ONLY files that were actually mined — skipped files are
        # never archived; the archive means "this content is in the palace".
        for rel_path in self.mined_orig_rels():
            # Archive the verified work copy — the live staging file may
            # have been replaced since the claim.
            file_path = self.work_orig / rel_path
            if not file_path.exists():
                self.log(f"Archive SKIP: {rel_path} has no verified work copy")
                continue

            current_size = str(self.file_size(file_path))
            current_hash = self.file_sha256(file_path)

            expected_hash = snapshot_hashes.get(rel_path, "")
            if expected_hash and current_hash != expected_hash:
                self.log(f"Archive SKIP: {rel_path} work copy no longer matches snapshot")
                continue

            archive_name = batch_archive_tmp / f"{rel_path}.gz"
            archive_name.parent.mkdir(parents=True, exist_ok=True)

            if archive_name.exists():
                self.log(f"Archive FAILED: duplicate path would overwrite {archive_name}")
                return False

            # Gzip the file using Python's gzip module (cross-platform).
            try:
                with file_path.open("rb") as src_f, gzip.open(archive_name, "wb") as gz_f:
                    shutil.copyfileobj(src_f, gz_f)
            except Exception as e:
                self.log(f"Archive FAILED: gzip error for {file_path}: {e}")
                return False

            # Validate the compressed file.
            try:
                with gzip.open(archive_name, "rb") as gz_f:
                    gz_f.read(1)
            except Exception as e:
                self.log(f"Archive FAILED: gzip validation failed for {archive_name}: {e}")
                return False

            manifest_lines.append(f"file: {rel_path}\n")
            manifest_lines.append(f"  sha256: {current_hash}\n")
            manifest_lines.append(f"  size: {current_size} bytes\n")
            manifest_lines.append(f"  archived: {rel_path}.gz\n\n")
            file_count += 1

        if file_count == 0:
            self.log("Archive: no mined files to archive")
            try:
                batch_archive_tmp.rmdir()
            except OSError:
                shutil.rmtree(batch_archive_tmp, ignore_errors=True)
            return True

        manifest.write_text("".join(manifest_lines), encoding="utf-8")
        if manifest.stat().st_size == 0:
            self.log("Archive FAILED: manifest is empty or missing")
            return False

        # Atomic rename.
        try:
            batch_archive_tmp.rename(batch_archive)
        except OSError as e:
            self.log(f"Archive FAILED: could not move temporary archive to {batch_archive}: {e}")
            return False

        self.log(f"Archived {file_count} files to {batch_archive}")
        return True

    # ── Quarantine ─────────────────────────────────────────────────────────

    def quarantine_skipped(self) -> None:
        """Move unprocessable files out of the scan set, preserving them
        under staging/.mp_quarantine/<batch_id>/ — moved, never deleted.
        A file that changed since the claim is a new drop and stays."""
        snapshot_hashes: dict[str, str] = {}
        if self.batch_snapshot.exists():
            for line in self.batch_snapshot.read_text(encoding="utf-8").splitlines():
                rel, _s, _m, sha = self.parse_snapshot_line(line)
                if rel:
                    snapshot_hashes[rel] = sha

        moved = 0
        for rel_path in self.skipped_rels():
            file_path = self.staging_dir / rel_path
            if not file_path.exists():
                continue
            expected_hash = snapshot_hashes.get(rel_path, "")
            if expected_hash and self.file_sha256(file_path) != expected_hash:
                self.log(f"Quarantine SKIP: {rel_path} changed since claim — leaving in staging")
                continue
            dst = self.staging_dir / ".mp_quarantine" / self.batch_id / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            file_path.rename(dst)
            moved += 1
        if moved:
            self.log(
                f"Quarantined {moved} unprocessable file(s) to "
                f"staging/.mp_quarantine/{self.batch_id}/"
            )

    # ── Clear staging ──────────────────────────────────────────────────────

    def clear_staging(self) -> bool:
        """Clear only files that were actually mined (now in the palace AND
        archived). Verify the live file still matches the snapshot, then
        MOVE it into the private work dir rather than unlinking a live
        staging path — rename is atomic wrt path replacement, so a producer
        that swapped the file mid-pipeline loses nothing."""
        snapshot_hashes: dict[str, str] = {}
        if self.batch_snapshot.exists():
            for line in self.batch_snapshot.read_text(encoding="utf-8").splitlines():
                rel, _s, _m, sha = self.parse_snapshot_line(line)
                if rel:
                    snapshot_hashes[rel] = sha

        for rel_path in self.mined_orig_rels():
            file_path = self.staging_dir / rel_path
            if not file_path.exists():
                continue
            expected_hash = snapshot_hashes.get(rel_path, "")
            if expected_hash and self.file_sha256(file_path) == expected_hash:
                dst = self.retired_dir / rel_path
                dst.parent.mkdir(parents=True, exist_ok=True)
                file_path.rename(dst)
            else:
                self.log(
                    f"Clear SKIP: {rel_path} changed since batch was claimed, leaving for next run"
                )

        # Remove empty directories left behind (but not the staging root,
        # and never the quarantine tree).
        for p in sorted(self.staging_dir.rglob("*"), reverse=True):
            if p.is_dir() and p != self.staging_dir:
                rel_parts = p.relative_to(self.staging_dir).parts
                if any(part.startswith(".") for part in rel_parts):
                    continue
                try:
                    p.rmdir()
                except OSError:
                    pass
        self.log("Staging cleared (ready for next batch)")
        return True

    # ── Process batch ──────────────────────────────────────────────────────

    def _cleanup_work(self) -> None:
        if self.batch_dir.exists():
            shutil.rmtree(self.batch_dir, ignore_errors=True)
        self.batch_snapshot.unlink(missing_ok=True)

    def process_batch(self) -> bool:
        self.log("=== Processing batch ===")
        if not self.claim_batch():
            self.log("ABORT: could not claim batch")
            return False
        if not self.preprocess_staging():
            self.log("ABORT: staging failed — files left for retry")
            self._cleanup_work()
            return False
        if not self.mine_processed():
            self.log("ABORT: mine failed — files left for retry")
            self._cleanup_work()
            return False
        if not self.verify_mined():
            self.log("ABORT: verify failed — files left for inspection")
            self._cleanup_work()
            return False
        self.compress_palace()
        if not self.archive_files():
            self.log("ABORT: archive failed — files left for inspection")
            self._cleanup_work()
            return False
        self.quarantine_skipped()
        self.clear_staging()
        self.log("=== Batch complete ===")
        return True

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        """Main watcher loop."""
        self.log("=== staging_watcher started ===")
        self.log(f"Watching: {self.staging_dir}")
        self.log(f"Archive: {self.archive_dir}")
        self.log(f"Palace: {self.palace_path}")
        self.log("Pipeline: stage -> mine -> verify -> compress -> gzip -> archive")
        self.log(f"Debounce: {self.debounce_seconds}s, Max lines: {self.max_lines}")
        while True:
            while self.count_files() < self.min_files:
                time.sleep(10)
            self.log("Files detected — waiting for stable period...")
            self.wait_for_stable()
            self.process_batch()
            time.sleep(5)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="MemPalace staging watcher")
    parser.add_argument("staging_dir", nargs="?", default=None)
    parser.add_argument("palace_path", nargs="?", default=None)
    args = parser.parse_args()
    watcher = StagingWatcher(
        staging_dir=args.staging_dir,
        palace_path=args.palace_path,
    )
    watcher.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
