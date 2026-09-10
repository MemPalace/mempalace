#!/usr/bin/env python3
"""Staging watcher — cross-platform Python port of staging_watcher.sh.

Watches a staging directory for new files and runs the full MemPalace
ingest pipeline:

    preprocess → mine → verify → compress → gzip → archive

When files arrive and stabilize (no writes for DEBOUNCE_SECONDS):
  1. Claim an immutable batch snapshot.
  2. Preprocess only the claimed files (strip boilerplate, split >4000 lines).
  3. Mine processed files into the palace.
  4. Verify a sample of mined content is searchable.
  5. Compress: run mempalace compress (AAAK dialect).
  6. Gzip original files from the batch snapshot and move to archive/.
  7. Write manifest with checksums + file list.
  8. Clear only the claimed batch from staging (ready for next batch).

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
            archive_dir or os.environ.get("ARCHIVE_DIR", str(self.staging_dir.parent / "archive"))
        )
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
        self.batch_work = Path(batch_work or os.environ.get("BATCH_WORK") or wr / ".batch_work")

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
            if "processed" in p.parts:
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
        # Create the private work directory outside the watched tree.
        if self.batch_work.exists():
            shutil.rmtree(self.batch_work, ignore_errors=True)
        self.batch_work.mkdir(parents=True, exist_ok=True)
        # Copy ALL claimed files into BATCH_WORK with sha256 verification.
        for line in snapshot.strip().split("\n"):
            rel_path, _size, _mtime, expected_hash = self.parse_snapshot_line(line)
            if not rel_path or rel_path in _EXCLUDED_NAMES:
                continue
            src = self.staging_dir / rel_path
            if not src.exists():
                continue
            dst = self.batch_work / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied_hash = self.file_sha256(dst)
            if copied_hash != expected_hash:
                self.log(f"Claim SKIP: {rel_path} changed during claim (hash mismatch)")
                dst.unlink(missing_ok=True)
        count = len([line for line in snapshot.strip().split("\n") if line])
        self.log(f"Claimed batch: {count} files -> {self.batch_snapshot}")
        return True

    # ── Preprocess ─────────────────────────────────────────────────────────

    def preprocess_staging(self) -> bool:
        file_count = self.count_files()
        self.log(
            f"Preprocessing {file_count} files (strip boilerplate, split >{self.max_lines} lines)..."
        )
        cmd = [
            self.python_bin,
            str(self.preprocess_script),
            str(self.staging_dir),
            "--max-lines",
            str(self.max_lines),
        ]
        if self.batch_snapshot.exists() and self.batch_snapshot.stat().st_size > 0:
            cmd.extend(
                ["--batch-snapshot", str(self.batch_snapshot), "--work-dir", str(self.batch_work)]
            )
        with self.log_file.open("a", encoding="utf-8") as logf:
            try:
                result = subprocess.run(cmd, stdout=logf, stderr=logf)
            except FileNotFoundError as e:
                self.log(f"Preprocess FAILED: {e}")
                return False
        if result.returncode == 0:
            processed_dir = self.staging_dir / "processed"
            processed_count = (
                len(
                    [
                        p
                        for p in processed_dir.rglob("*")
                        if p.is_file() and p.name != "mempalace.yaml"
                    ]
                )
                if processed_dir.exists()
                else 0
            )
            # Copy mempalace.yaml into processed/ for wing routing
            yaml_src = self.staging_dir / "mempalace.yaml"
            if yaml_src.exists():
                shutil.copy2(yaml_src, processed_dir / "mempalace.yaml")
            self.log(f"Preprocess complete: {processed_count} output files")
            return True
        self.log(f"Preprocess FAILED (exit {result.returncode})")
        return False

    # ── Mine ───────────────────────────────────────────────────────────────

    def mine_processed(self) -> bool:
        processed_dir = self.staging_dir / "processed"
        if not processed_dir.exists():
            self.log("Mine: no processed directory — skipping")
            return True
        file_count = len(
            [p for p in processed_dir.rglob("*") if p.is_file() and p.name != "mempalace.yaml"]
        )
        if file_count == 0:
            self.log("Mine: no processed files to mine — skipping")
            return True
        self.log(f"Mining {file_count} processed files...")
        cmd = [
            self.mempalace_bin,
            "--palace",
            str(self.palace_path),
            "mine",
            str(processed_dir),
            "--agent",
            "devin",
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

    # ── Build manifest ─────────────────────────────────────────────────────

    def build_batch_manifest(self) -> None:
        processed_dir = self.staging_dir / "processed"
        manifest = self.staging_dir / ".batch_manifest"
        files: list[str] = []
        if processed_dir.exists():
            for p in sorted(processed_dir.rglob("*")):
                if p.is_file() and p.name != "mempalace.yaml":
                    files.append(str(p))
        manifest.write_text("\n".join(files) + ("\n" if files else ""), encoding="utf-8")
        self.log(f"Built batch manifest with {len(files)} processed files")

    # ── Verify ─────────────────────────────────────────────────────────────

    def verify_mined(self) -> bool:
        manifest = self.staging_dir / ".batch_manifest"
        if not manifest.exists() or manifest.stat().st_size == 0:
            self.log("Verify FAILED: batch manifest is empty or missing")
            return False
        self.log(f"Verify: checking searchability of all processed files against {manifest}")
        cmd = [
            self.python_bin,
            str(self.verify_script),
            str(self.palace_path),
            str(manifest),
            str(manifest),
            str(self.mempalace_bin),
        ]
        if self.batch_snapshot.exists() and self.batch_snapshot.stat().st_size > 0:
            cmd.extend(["--snapshot", str(self.batch_snapshot)])
        with self.log_file.open("a", encoding="utf-8") as logf:
            result = subprocess.run(cmd, stdout=logf, stderr=logf)
        if result.returncode == 0:
            self.log("Verify: all processed files are searchable and in the current batch")
            return True
        self.log("Verify FAILED: one or more processed files are not searchable")
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
            f"# Mined: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"# Palace: {self.palace_path}\n"
            f"# Pipeline: preprocess -> mine -> verify -> compress -> gzip -> archive\n\n"
        )

        # Use the claimed batch snapshot when available.
        snapshot_file = self.batch_snapshot
        if not snapshot_file.exists() or snapshot_file.stat().st_size == 0:
            snapshot_file = self.staging_dir / ".batch_snapshot"
            if not snapshot_file.exists() or snapshot_file.stat().st_size == 0:
                snapshot_file.write_text(self.snapshot_staging(), encoding="utf-8")

        manifest_lines: list[str] = [manifest_header]
        file_count = 0

        for line in snapshot_file.read_text(encoding="utf-8").splitlines():
            rel_path, expected_size, expected_mtime, expected_hash = self.parse_snapshot_line(line)
            if not rel_path or rel_path in _EXCLUDED_NAMES:
                continue
            # Use the immutable work copy from claim_batch when available;
            # fall back to the live staging tree for direct function testing.
            file_path = self.batch_work / rel_path
            if not file_path.exists():
                file_path = self.staging_dir / rel_path
            if not file_path.exists():
                self.log(f"Archive SKIP: {rel_path} no longer exists")
                continue

            current_size = str(self.file_size(file_path))
            current_mtime = str(self.file_mtime(file_path))
            current_hash = self.file_sha256(file_path)

            if (
                current_size != expected_size
                or current_mtime != expected_mtime
                or current_hash != expected_hash
            ):
                self.log(
                    f"Archive SKIP: {rel_path} changed since batch was claimed (size/mtime/hash)"
                )
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
            self.log("Archive FAILED: no files to archive")
            return False

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

    # ── Clear staging ──────────────────────────────────────────────────────

    def clear_staging(self) -> bool:
        """Delete only the files listed in the batch snapshot."""
        snapshot_file = self.batch_snapshot
        if not snapshot_file.exists() or snapshot_file.stat().st_size == 0:
            snapshot_file = self.staging_dir / ".batch_snapshot"

        if snapshot_file.exists() and snapshot_file.stat().st_size > 0:
            for line in snapshot_file.read_text(encoding="utf-8").splitlines():
                rel_path, expected_size, expected_mtime, expected_hash = self.parse_snapshot_line(
                    line
                )
                if not rel_path or rel_path in _EXCLUDED_NAMES:
                    continue
                file_path = self.staging_dir / rel_path
                if not file_path.exists():
                    continue
                current_size = str(self.file_size(file_path))
                current_mtime = str(self.file_mtime(file_path))
                current_hash = self.file_sha256(file_path)
                if (
                    current_size == expected_size
                    and current_mtime == expected_mtime
                    and current_hash == expected_hash
                ):
                    file_path.unlink()
                else:
                    self.log(
                        f"Clear SKIP: {rel_path} changed since batch was claimed, leaving for next run"
                    )

        # Clean up processed directory and batch metadata.
        processed_dir = self.staging_dir / "processed"
        if processed_dir.exists():
            shutil.rmtree(processed_dir, ignore_errors=True)
        staging_snapshot = self.staging_dir / ".batch_snapshot"
        staging_manifest = self.staging_dir / ".batch_manifest"
        staging_snapshot.unlink(missing_ok=True)
        staging_manifest.unlink(missing_ok=True)
        # Remove empty directories (but not the staging root).
        for p in sorted(self.staging_dir.rglob("*"), reverse=True):
            if p.is_dir() and p != self.staging_dir:
                try:
                    p.rmdir()
                except OSError:
                    pass
        # Clean up the private work directory outside the watched tree.
        if self.batch_work.exists():
            shutil.rmtree(self.batch_work, ignore_errors=True)
        self.batch_snapshot.unlink(missing_ok=True)
        self.log("Staging cleared (ready for next batch)")
        return True

    # ── Process batch ──────────────────────────────────────────────────────

    def _cleanup_work(self) -> None:
        if self.batch_work.exists():
            shutil.rmtree(self.batch_work, ignore_errors=True)
        self.batch_snapshot.unlink(missing_ok=True)

    def process_batch(self) -> bool:
        self.log("=== Processing batch ===")
        if not self.claim_batch():
            self.log("ABORT: could not claim batch")
            return False
        if not self.preprocess_staging():
            self.log("ABORT: preprocess failed — files left for retry")
            self._cleanup_work()
            return False
        if not self.mine_processed():
            self.log("ABORT: mine failed — files left for retry")
            self._cleanup_work()
            return False
        self.build_batch_manifest()
        if not self.verify_mined():
            self.log("ABORT: verify failed — files left for inspection")
            self._cleanup_work()
            return False
        self.compress_palace()
        if not self.archive_files():
            self.log("ABORT: archive failed — files left for inspection")
            self._cleanup_work()
            return False
        if not self.clear_staging():
            self.log("ABORT: clear_staging failed — archive is at but staging may be dirty")
            self._cleanup_work()
            return False
        self.log("=== Batch complete ===")
        return True

    # ── Main loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        """Main watcher loop."""
        self.log("=== staging_watcher started ===")
        self.log(f"Watching: {self.staging_dir}")
        self.log(f"Archive: {self.archive_dir}")
        self.log(f"Palace: {self.palace_path}")
        self.log("Pipeline: preprocess -> mine -> verify -> compress -> gzip -> archive")
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
