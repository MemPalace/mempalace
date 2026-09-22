#!/usr/bin/env python3
"""Stage verified batch files for mining — verbatim, byte-exact.

MemPalace stores content verbatim, full stop. This tool never rewrites
file contents. Its only jobs are transport-level:

  * filter structurally unprocessable files (dotfiles, binary
    extensions, generated directories) out of the batch,
  * split files over --max-lines into numbered parts on line boundaries
    (bytes preserved; the unsplit original is not kept in the mine tree),
  * emit every mine-ready file under <out-dir>/<batch_id>/<rel> so each
    batch has a unique source path — the miner purges by source_file, and
    a shared output directory lets a later batch's same-named file delete
    an earlier batch's drawers,
  * write mined/skipped manifests so the watcher knows which staging
    files were actually mined (archived) vs unprocessable (quarantined).

Usage:
    python tools/preprocess_staging.py /path/to/staging [options]

Options:
    --work-dir DIR       read verified work copies instead of the staging
                         directory (batch mode; sha256-verified against
                         --batch-snapshot before use)
    --batch-snapshot F   unit-separator snapshot produced by the watcher
    --out-dir DIR        mine-ready output root; files land under
                         <out-dir>/<batch_id>/ (default:
                         <staging>/.mp_processed)
    --batch-id ID        batch identifier for output paths (default:
                         UTC timestamp + pid)
    --manifest F         write mined pairs: orig_rel <US> mined_rel
    --skip-manifest F    write skipped orig rel paths (one per line)
    --max-lines N        split files over N lines (default 4000; 0 disables)
    --dry-run            report only, do not write
"""

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

# Binary/generated file types that cannot be mined — rejected as input,
# never modified.
SKIP_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".ico",
    ".webp",
    ".mp3",
    ".mp4",
    ".wav",
    ".avi",
    ".mov",
    ".mkv",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".class",
    ".jar",
    ".war",
    ".o",
    ".a",
    ".lib",
    ".jsonl",
    ".csv",
}

# Directory components that mark vendored/generated trees — any file whose
# relative path contains one of these is rejected as input.
GENERATED_DIRS = {
    "node_modules",
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    "target",
    "out",
    ".next",
    ".nuxt",
    ".cache",
    "venv",
    ".venv",
    "env",
    "vendor",
    "third_party",
}

# Never-mined names — config/markers, not content.
SKIP_FILENAMES = {
    "mempalace.yaml",
    "mempal.yaml",
    ".DS_Store",
    "Thumbs.db",
    ".batch_snapshot",
    ".batch_manifest",
    ".mined",
    ".skipped",
}

_DEFAULT_MAX_LINES = 4000
_SNAPSHOT_SEP = "\x1f"


# ── Snapshot parsing ─────────────────────────────────────────────────────────


def load_snapshot(path):
    """Parse a unit-separator snapshot file into {rel: (size, mtime, sha256)}."""
    entries = {}
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if not line:
            continue
        parts = line.split(_SNAPSHOT_SEP)
        if len(parts) < 4:
            continue
        rel, size, mtime, sha = parts[0], parts[1], parts[2], parts[3]
        entries[rel] = (size, mtime, sha)
    return entries


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Filters ──────────────────────────────────────────────────────────────────


def should_skip(rel_path):
    """True when the relative path is structurally unprocessable.

    Purely positional checks — never content-based. A skipped file is
    left alone entirely (the watcher quarantines it, never rewrites it).
    """
    parts = Path(rel_path).parts
    name = parts[-1] if parts else rel_path
    if name in SKIP_FILENAMES:
        return True
    for part in parts:
        if part.startswith("."):
            return True
        if part in GENERATED_DIRS:
            return True
    return Path(name).suffix.lower() in SKIP_EXTENSIONS


# ── Splitting ────────────────────────────────────────────────────────────────


def split_name(name, index, total):
    """``notes.md`` + (1, 3) -> ``notes__part001_of_003.md``."""
    stem, dot, ext = name.rpartition(".")
    if not stem:
        stem, ext = name, ""
    return f"{stem}__part{index:03d}_of_{total:03d}{dot if ext else ''}{ext}"


def split_bytes(data, max_lines):
    """Split raw bytes on LF boundaries into ≤ max_lines parts.

    Byte-level: no decoding, no re-encoding, so non-UTF-8 and CRLF
    content round-trips exactly. Returns a list of byte strings; a file
    under the limit returns [data].
    """
    if max_lines <= 0:
        return [data]
    lines = data.split(b"\n")
    if len(lines) <= max_lines:
        return [data]
    return [b"\n".join(lines[i : i + max_lines]) for i in range(0, len(lines), max_lines)]


# ── Staging ──────────────────────────────────────────────────────────────────


def stage_file(src, rel, out_dir, max_lines):
    """Stage one file verbatim. Returns (mined_rels, skipped_reason).

    mined_rels are paths relative to out_dir (``<batch_id>/<rel>`` form,
    part-suffixed when split). skipped_reason is None on success.
    """
    data = src.read_bytes()
    if not data.strip():
        return [], "empty"

    parts = split_bytes(data, max_lines)
    mined_rels = []
    for i, chunk in enumerate(parts, 1):
        if len(parts) > 1:
            mined_rel = str(Path(rel).parent / split_name(Path(rel).name, i, len(parts)))
        else:
            mined_rel = rel
        dst = Path(out_dir) / mined_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(chunk)
        mined_rels.append(mined_rel)
    return mined_rels, None


def preprocess_directory(
    staging_dir,
    work_dir=None,
    out_dir=None,
    batch_id=None,
    snapshot=None,
    manifest_path=None,
    skip_manifest_path=None,
    max_lines=_DEFAULT_MAX_LINES,
    dry_run=False,
):
    """Stage every processable file in the batch. Returns (mined, skipped)."""
    staging_dir = Path(staging_dir)
    batch_id = batch_id or (
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}"
    )
    if out_dir is None:
        # Standalone mode: namespace by batch id so every run has a unique
        # source path. Batch mode passes --out-dir that already carries a
        # content-keyed batch directory.
        out_dir = staging_dir / ".mp_processed" / batch_id
    out_dir = Path(out_dir)

    snapshot_map = load_snapshot(snapshot) if snapshot else {}

    if work_dir is not None:
        work_dir = Path(work_dir)
        # Batch mode: the snapshot defines the file set; each rel resolves
        # to a verified work copy under work_dir.
        candidates = [rel for rel in snapshot_map]
        src_for = lambda rel: work_dir / rel  # noqa: E731
    else:
        # Standalone: scan the staging tree, dot/generated trees excluded.
        candidates = []
        for p in sorted(staging_dir.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(staging_dir).as_posix()
            if snapshot_map and rel not in snapshot_map:
                continue
            candidates.append(rel)
        src_for = lambda rel: staging_dir / rel  # noqa: E731

    mined_pairs = []  # (orig_rel, mined_rel)
    skipped = []  # (orig_rel, reason)

    for rel in sorted(set(candidates)):
        if should_skip(rel):
            skipped.append((rel, "filtered"))
            continue
        src = src_for(rel)
        try:
            actual = sha256_file(src)
        except OSError:
            skipped.append((rel, "unreadable"))
            continue
        if snapshot_map and rel in snapshot_map and actual != snapshot_map[rel][2]:
            # The copy we were handed does not match the claimed snapshot —
            # refuse to stage it rather than mine stale bytes.
            skipped.append((rel, "hash_mismatch"))
            continue

        if dry_run:
            mined_pairs.append((rel, rel))
            continue

        rel_mined, reason = stage_file(src, rel, out_dir, max_lines)
        if reason:
            skipped.append((rel, reason))
        else:
            mined_pairs.extend((rel, m) for m in rel_mined)

    if not dry_run:
        if manifest_path:
            Path(manifest_path).write_text(
                "".join(f"{o}{_SNAPSHOT_SEP}{m}\n" for o, m in mined_pairs),
                encoding="utf-8",
            )
        if skip_manifest_path:
            Path(skip_manifest_path).write_text(
                "".join(f"{rel}{_SNAPSHOT_SEP}{reason}\n" for rel, reason in skipped),
                encoding="utf-8",
            )

    return mined_pairs, skipped


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv=None):
    p = argparse.ArgumentParser(description="Stage batch files for mining — verbatim, byte-exact.")
    p.add_argument("staging_dir", help="staging directory (scan root in standalone mode)")
    p.add_argument("--work-dir", help="verified work copies to read instead of staging")
    p.add_argument("--batch-snapshot", help="unit-separator snapshot for hash verification")
    p.add_argument("--out-dir", help="output root; files land under <out-dir>/<batch_id>/")
    p.add_argument("--batch-id", help="batch identifier for output paths")
    p.add_argument("--manifest", help="write mined orig<US>mined pairs here")
    p.add_argument("--skip-manifest", help="write skipped orig rels here")
    p.add_argument("--max-lines", type=int, default=_DEFAULT_MAX_LINES)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    mined, skipped = preprocess_directory(
        args.staging_dir,
        work_dir=args.work_dir,
        out_dir=args.out_dir,
        batch_id=args.batch_id,
        snapshot=args.batch_snapshot,
        manifest_path=args.manifest,
        skip_manifest_path=args.skip_manifest,
        max_lines=args.max_lines,
        dry_run=args.dry_run,
    )
    print(f"staged {len(mined)} file(s) for mining; skipped {len(skipped)}")
    for rel, reason in skipped:
        print(f"  skip {rel} ({reason})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
