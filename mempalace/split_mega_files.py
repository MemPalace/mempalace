#!/usr/bin/env python3
"""
split_mega_files.py — Split concatenated transcript files into per-session files
=================================================================================

Scans a directory for .txt files that contain multiple Claude Code sessions
(identified by "Claude Code v" headers). Splits each into individual files
named with: date, time, people detected, and subject from first prompt.

Distinguishes true session starts from mid-session context restores
(which show "Ctrl+E to show X previous messages").

Output files are written to --output-dir (default: same dir as source).
Original files are renamed with .mega_backup extension (not deleted).

Usage:
    python3 split_mega_files.py                          # scan ~/Desktop/transcripts
    python3 split_mega_files.py --source ~/Desktop/transcripts  # explicit source
    python3 split_mega_files.py --dry-run                # show what would happen
    python3 split_mega_files.py --min-sessions 2         # only files with 2+ sessions

By: Ben, 2026-03-30
"""

import argparse
import json
import os
import re
from pathlib import Path

HOME = Path.home()
LUMI_DIR = Path(os.environ.get("MEMPALACE_SOURCE_DIR", str(HOME / "Desktop/transcripts")))

# People we know about (for name detection in content)
# Loaded from ~/.mempalace/known_names.json if it exists, otherwise generic fallback.
_KNOWN_NAMES_PATH = HOME / ".mempalace" / "known_names.json"
_FALLBACK_KNOWN_PEOPLE = ["Alice", "Ben", "Riley", "Max", "Sam", "Devon", "Jordan"]
_KNOWN_NAMES_CACHE = None


def _load_known_names_config(force_reload: bool = False):
    """Load and cache the optional known-names config file."""
    global _KNOWN_NAMES_CACHE

    if force_reload:
        _KNOWN_NAMES_CACHE = None

    if _KNOWN_NAMES_CACHE is not None:
        return _KNOWN_NAMES_CACHE

    if _KNOWN_NAMES_PATH.exists():
        try:
            _KNOWN_NAMES_CACHE = json.loads(_KNOWN_NAMES_PATH.read_text(encoding="utf-8"))
            return _KNOWN_NAMES_CACHE
        except (json.JSONDecodeError, OSError):
            pass

    _KNOWN_NAMES_CACHE = None
    return None


def _load_known_people() -> list:
    """Load known names from config file, falling back to a generic list."""
    data = _load_known_names_config()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("names", [])
    return list(_FALLBACK_KNOWN_PEOPLE)


KNOWN_PEOPLE = _load_known_people()


def _load_username_map() -> dict:
    """Load username-to-name mapping from config file."""
    data = _load_known_names_config()
    if isinstance(data, dict):
        return data.get("username_map", {})
    return {}


def is_true_session_start(lines, idx):
    """
    True session start: 'Claude Code v' header NOT followed by 'Ctrl+E'/'previous messages'
    within the next 6 lines (those are context restores, not new sessions).
    """
    nearby = "".join(lines[idx : idx + 6])
    return "Ctrl+E" not in nearby and "previous messages" not in nearby


def find_session_boundaries(lines):
    """Return list of line indices where true new sessions begin."""
    boundaries = []
    for i, line in enumerate(lines):
        if "Claude Code v" in line and is_true_session_start(lines, i):
            boundaries.append(i)
    return boundaries


def extract_timestamp(lines):
    """
    Find the first timestamp line: ⏺ H:MM AM/PM Weekday, Month DD, YYYY
    Returns (datetime_str, iso_str) or (None, None).
    """
    ts_pattern = re.compile(r"⏺\s+(\d{1,2}:\d{2}\s+[AP]M)\s+\w+,\s+(\w+)\s+(\d{1,2}),\s+(\d{4})")
    months = {
        "January": "01",
        "February": "02",
        "March": "03",
        "April": "04",
        "May": "05",
        "June": "06",
        "July": "07",
        "August": "08",
        "September": "09",
        "October": "10",
        "November": "11",
        "December": "12",
    }
    for line in lines[:50]:
        m = ts_pattern.search(line)
        if m:
            time_str, month, day, year = m.groups()
            mon = months.get(month, "00")
            day_z = day.zfill(2)
            time_safe = time_str.replace(":", "").replace(" ", "")
            iso = f"{year}-{mon}-{day_z}"
            human = f"{year}-{mon}-{day_z}_{time_safe}"
            return human, iso
    return None, None


def extract_people(lines):
    """
    Detect people mentioned as speakers or by name in first 100 lines.
    Returns sorted list of detected names.
    """
    found = set()
    text = "".join(lines[:100])

    # Speaker tags: "Alice:", "Ben:", etc.
    for person in KNOWN_PEOPLE:
        if re.search(rf"\b{person}\b", text, re.IGNORECASE):
            found.add(person)

    # Working directory username hint — map to known people if configured
    dir_match = re.search(r"/Users/(\w+)/", text)
    if dir_match:
        username = dir_match.group(1)
        # User can map usernames to names in ~/.mempalace/known_names.json
        # under a "username_map" key, e.g. {"username_map": {"jdoe": "John"}}
        username_map = _load_username_map()
        if username in username_map:
            found.add(username_map[username])

    return sorted(found)


def extract_subject(lines):
    """
    Find the first meaningful user prompt (> line that isn't a shell command).
    Returns cleaned, filename-safe subject string.
    """
    skip_patterns = re.compile(
        r"^(\.\/|cd |ls |python|bash|git |cat |source |export |claude|./activate)"
    )
    for line in lines:
        if line.startswith("> "):
            prompt = line[2:].strip()
            if prompt and not skip_patterns.match(prompt) and len(prompt) > 5:
                # Clean for filename
                subject = re.sub(r"[^\w\s-]", "", prompt)
                subject = re.sub(r"\s+", "-", subject.strip())
                return subject[:60]
    return "session"


def split_file(filepath, output_dir, dry_run=False):
    """
    Split a single mega-file into per-session files.
    Returns list of output paths written (or would be written if dry_run).
    """
    path = Path(filepath)
    max_size = 500 * 1024 * 1024  # 500 MB safety limit
    if path.stat().st_size > max_size:
        print(f"  SKIP: {path.name} exceeds {max_size // (1024 * 1024)} MB limit")
        return []
    lines = path.read_text(errors="replace").splitlines(keepends=True)

    boundaries = find_session_boundaries(lines)
    if len(boundaries) < 2:
        return []  # Not a mega-file

    # Add sentinel at end
    boundaries.append(len(lines))

    out_dir = Path(output_dir) if output_dir else path.parent
    written = []

    for i, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        chunk = lines[start:end]
        if len(chunk) < 10:
            continue  # Skip tiny fragments

        ts_human, ts_iso = extract_timestamp(chunk)
        people = extract_people(chunk)
        subject = extract_subject(chunk)

        # Build filename: SOURCESTEM__DATE_TIME_People_subject.txt
        # Source stem prefix prevents collisions when multiple mega-files
        # produce sessions with the same timestamp/people/subject.
        ts_part = ts_human or f"part{i + 1:02d}"
        people_part = "-".join(people[:3]) if people else "unknown"
        src_stem = re.sub(r"[^\w-]", "_", path.stem)[:40]
        name = f"{src_stem}__{ts_part}_{people_part}_{subject}.txt"
        # Sanitize
        name = re.sub(r"[^\w\.\-]", "_", name)
        name = re.sub(r"_+", "_", name)

        out_path = out_dir / name

        if dry_run:
            print(f"  [{i + 1}/{len(boundaries) - 1}] {name}  ({len(chunk)} lines)")
        else:
            out_path.write_text("".join(chunk), encoding="utf-8")
            print(f"  + {name}  ({len(chunk)} lines)")

        written.append(out_path)

    return written


def main():
    parser = argparse.ArgumentParser(
        description="Split concatenated transcript mega-files into per-session files"
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help="Source directory (default: MEMPALACE_SOURCE_DIR or ~/Desktop/transcripts)",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None, help="Output directory (default: same as source)"
    )
    parser.add_argument(
        "--min-sessions",
        type=int,
        default=2,
        help="Only split files with at least N sessions (default: 2)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen without writing files"
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Split a single specific file instead of scanning dir",
    )
    args = parser.parse_args()

    src_dir = Path(args.source).expanduser().resolve() if args.source else LUMI_DIR
    output_dir = args.output_dir or None  # None = same dir as file

    if args.file:
        files = [Path(args.file)]
    else:
        files = sorted(src_dir.glob("*.txt"))

    mega_files = []
    max_scan_size = 500 * 1024 * 1024  # 500 MB
    for f in files:
        if f.stat().st_size > max_scan_size:
            print(f"  SKIP: {f.name} exceeds {max_scan_size // (1024 * 1024)} MB limit")
            continue
        lines = f.read_text(errors="replace").splitlines(keepends=True)
        boundaries = find_session_boundaries(lines)
        if len(boundaries) >= args.min_sessions:
            mega_files.append((f, len(boundaries)))

    if not mega_files:
        print(f"No mega-files found in {src_dir} (min {args.min_sessions} sessions).")
        return

    print(f"\n{'=' * 60}")
    print(f"  Mega-file splitter — {'DRY RUN' if args.dry_run else 'SPLITTING'}")
    print(f"{'=' * 60}")
    print(f"  Source:      {src_dir}")
    print(f"  Output:      {output_dir or 'same dir as source'}")
    print(f"  Mega-files:  {len(mega_files)}")
    print(f"{'-' * 60}\n")

    total_written = 0
    for f, n_sessions in mega_files:
        print(f"  {f.name}  ({n_sessions} sessions, {f.stat().st_size // 1024}KB)")
        written = split_file(f, output_dir, dry_run=args.dry_run)
        total_written += len(written)

        if not args.dry_run and written:
            backup = f.with_suffix(".mega_backup")
            f.rename(backup)
            print(f"  -> Original renamed to {backup.name}\n")
        else:
            print()

    print(f"{'-' * 60}")
    if args.dry_run:
        print(f"  DRY RUN — would create {total_written} files from {len(mega_files)} mega-files")
    else:
        print(f"  Done — created {total_written} files from {len(mega_files)} mega-files")
    print()


if __name__ == "__main__":
    main()
