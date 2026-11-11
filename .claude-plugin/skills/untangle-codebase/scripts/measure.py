#!/usr/bin/env python3
"""Read-only baseline for a cleanup: largest files and repeated blocks.

Standard library only. Counts text files under a root (skipping VCS,
dependency and build directories) and reports the largest files plus the
most repeated windows of N consecutive non-blank lines. Duplicate detection
is exact after trimming whitespace, so treat hits as candidates to read.
"""

import argparse
import json
import os
import sys
from collections import defaultdict

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    "target",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
}
MAX_BYTES = 1_000_000
# Lines this short are mostly braces and keywords, not meaningful duplication.
MIN_LINE_CHARS = 4


def read_lines(path):
    """Return the file's lines, or None for unreadable or binary files."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, encoding="utf-8") as handle:
            return handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def iter_files(root):
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            path = os.path.join(current, name)
            lines = read_lines(path)
            if lines is not None:
                yield os.path.relpath(path, root), lines


def find_duplicates(files, window):
    """Map each repeated window of non-blank lines to the files that hold it."""
    seen = defaultdict(set)
    for rel, lines in files:
        kept = [line.strip() for line in lines if len(line.strip()) >= MIN_LINE_CHARS]
        for start in range(len(kept) - window + 1):
            seen[tuple(kept[start : start + window])].add(rel)
    repeated = {block: where for block, where in seen.items() if len(where) > 1}
    ranked = sorted(repeated.items(), key=lambda item: (-len(item[1]), item[0]))
    return [{"files": sorted(where), "preview": list(block[:2])} for block, where in ranked]


def measure(root, top, window):
    files = list(iter_files(root))
    largest = sorted(
        ((len(lines), rel) for rel, lines in files), key=lambda item: (-item[0], item[1])
    )
    duplicates = find_duplicates(files, window)
    return {
        "files_scanned": len(files),
        "largest_files": [{"lines": count, "path": rel} for count, rel in largest[:top]],
        "duplicate_blocks": len(duplicates),
        "duplicate_samples": duplicates[:top],
        "window_lines": window,
    }


def render(report):
    out = [f"files scanned: {report['files_scanned']}", "largest files:"]
    out += [f"  {item['lines']:>6}  {item['path']}" for item in report["largest_files"]]
    out.append(f"repeated {report['window_lines']}-line blocks: {report['duplicate_blocks']}")
    for sample in report["duplicate_samples"]:
        out.append("  in " + ", ".join(sample["files"]))
        out += [f"      {line}" for line in sample["preview"]]
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="directory to scan")
    parser.add_argument("--top", type=int, default=10, help="rows to show per section")
    parser.add_argument("--dup-lines", type=int, default=6, help="lines per duplicate window")
    parser.add_argument("--json", action="store_true", help="emit JSON for diffing")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        parser.error(f"not a directory: {args.root}")
    if args.top < 1 or args.dup_lines < 2:
        parser.error("--top must be >= 1 and --dup-lines >= 2")
    report = measure(args.root, args.top, args.dup_lines)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
