#!/usr/bin/env python3
"""Preprocess files before MemPalace mining for denser, cleaner drawers.

Strips boilerplate that wastes chunks:
- License headers (SPDX, MIT, Apache, BSD, GPL blocks)
- System prompts and IDE metadata (Claude Code, Cursor, Copilot wrappers)
- Tool confirmation noise ("Todos have been modified", "Successfully wrote")
- File-view/ref-tag XML from agent transcripts
- Excessive blank lines and consecutive duplicate lines

Then splits files exceeding a line limit into numbered parts so each
stays under the miner's chunk budget.  Output goes to a ``processed/``
subdirectory alongside ``mempalace.yaml`` so the miner picks up wing
routing from the staging directory.

Usage::

    python3 tools/preprocess_staging.py <staging_dir> [--max-lines 4000] [--dry-run]

Intended to run as a step in a staging watcher pipeline::

    preprocess → mine → verify → compress → gzip → archive
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MAX_LINES_DEFAULT = 4000

# ── Whole-block patterns to strip entirely ──────────────────────────────────

STRIP_BLOCK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"<system_info>.*?</system_info>", re.DOTALL),
    re.compile(r'<rules type="always-on">.*?</rules>', re.DOTALL),
    re.compile(r"<available_skills>.*?</available_skills>", re.DOTALL),
    re.compile(r"<additional_metadata>.*?</additional_metadata>", re.DOTALL),
    re.compile(r"<truncation_notice>.*?</truncation_notice>", re.DOTALL),
    re.compile(r"<system_guidance>.*?</system_guidance>", re.DOTALL),
    re.compile(
        r"<subagent_completion_notification>.*?</subagent_completion_notification>",
        re.DOTALL,
    ),
]

# ── Individual lines to strip ───────────────────────────────────────────────

STRIP_LINE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^#\s*Messages in this part:"),
    re.compile(r"^#\s*Split from:"),
    re.compile(r"^#\s*Part \d+"),
    re.compile(r"^NOTE:.*Open files and cursor"),
    re.compile(r"^The current state of the user.s IDE"),
    re.compile(r"^Other open documents:"),
    re.compile(r"^Only use this information"),
]

# ── Tool result patterns to strip (entire line) ─────────────────────────────

STRIP_TOOL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Todos have been modified successfully"),
    re.compile(r"Current todo list:"),
    re.compile(r"Successfully wrote \d+ bytes"),
    re.compile(r"Successfully edited"),
    re.compile(r"Tool execution was rejected"),
    re.compile(r"No output produced after"),
    re.compile(r"Stopped waiting for output"),
    re.compile(r"Command running in background"),
    re.compile(r"Output from command in shell"),
    re.compile(r"<file-view\s+path="),
    re.compile(r"<ref_file\s"),
    re.compile(r"<ref_snippet\s"),
]

# ── License header patterns ─────────────────────────────────────────────────

LICENSE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"SPDX-License-Identifier", re.IGNORECASE),
    re.compile(r"Licensed under (the |)Apache License", re.IGNORECASE),
    re.compile(r"Licensed under (the |)MIT License", re.IGNORECASE),
    re.compile(r"Copyright \(c\) \d{4}", re.IGNORECASE),
    re.compile(r"Permission is hereby granted, free of charge", re.IGNORECASE),
    re.compile(r"Redistribution and use in source and binary forms", re.IGNORECASE),
    re.compile(r"This file is part of", re.IGNORECASE),
    re.compile(r"Licensed under the Apache License, Version 2\.0", re.IGNORECASE),
    re.compile(r"you may not use this file except in compliance", re.IGNORECASE),
]

BLOCK_COMMENT_START = re.compile(r"^\s*/\*")
BLOCK_COMMENT_END = re.compile(r"\*/\s*$")

# ── File filtering ──────────────────────────────────────────────────────────

PROCESSABLE_EXTENSIONS = {
    ".txt",
    ".py",
    ".rs",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".sol",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".md",
    ".mdx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sql",
    ".sh",
    ".bash",
    ".astro",
    ".css",
    ".scss",
    ".html",
    ".xml",
    ".env",
    ".cfg",
    ".ini",
    ".conf",
}

SKIP_FILES = {
    ".DS_Store",
    "mempalace.yaml",
    "mempal.yaml",
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    ".dockerignore",
    ".vercelignore",
    ".nvmrc",
    ".python-version",
    ".node-version",
}

SKIP_EXTENSIONS = {
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite3",
    ".sqlite",
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".o",
    ".a",
    ".lib",
    ".wasm",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".bz2",
    ".7z",
    ".rar",
    ".mp4",
    ".mp3",
    ".wav",
    ".avi",
    ".mov",
    ".lock",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
}

SKIP_DIR_PARTS = frozenset({"node_modules", "target", "build", "dist", ".next", "__pycache__"})


def should_skip(filepath: Path) -> bool:
    """Return True if *filepath* should not be processed."""
    name = filepath.name
    if name in SKIP_FILES or name.startswith("."):
        return True
    if filepath.suffix.lower() in SKIP_EXTENSIONS:
        return True
    if any(p in SKIP_DIR_PARTS for p in filepath.parts):
        return True
    return False


def strip_block_patterns(text: str) -> str:
    """Strip whole-block boilerplate patterns from *text*."""
    for p in STRIP_BLOCK_PATTERNS:
        text = p.sub("", text)
    return text


def strip_license_headers(text: str) -> str:
    """Remove license header blocks from *text*."""
    lines = text.split("\n")
    result: list[str] = []
    in_block_comment = False
    in_license = False
    license_line_count = 0

    for line in lines:
        if BLOCK_COMMENT_START.match(line):
            in_block_comment = True
            # Check if this line or any we've seen starts a license block
            if any(p.search(line) for p in LICENSE_PATTERNS):
                in_license = True
                license_line_count = 0
                continue
        if in_block_comment:
            if in_license:
                if BLOCK_COMMENT_END.search(line):
                    in_block_comment = False
                    in_license = False
                license_line_count += 1
                if license_line_count > 30:
                    in_license = False
                    in_block_comment = False
                    result.append(line)
                continue
            else:
                # Check if a later line in the block matches license patterns
                if any(p.search(line) for p in LICENSE_PATTERNS):
                    in_license = True
                    license_line_count = 0
                    continue
                result.append(line)
                if BLOCK_COMMENT_END.search(line):
                    in_block_comment = False
                continue

        if any(p.search(line) for p in LICENSE_PATTERNS):
            continue

        result.append(line)

    return "\n".join(result)


def strip_tool_noise(text: str) -> str:
    """Strip tool confirmation and noise lines from *text*."""
    lines = text.split("\n")
    result: list[str] = []
    for line in lines:
        if any(p.search(line) for p in STRIP_TOOL_PATTERNS):
            continue
        if any(p.search(line) for p in STRIP_LINE_PATTERNS):
            continue
        result.append(line)
    return "\n".join(result)


def dedup_consecutive(text: str) -> str:
    """Remove consecutive duplicate lines from *text*."""
    lines = text.split("\n")
    result: list[str] = []
    prev: str | None = None
    for line in lines:
        if line == prev and line.strip():
            continue
        result.append(line)
        prev = line
    return "\n".join(result)


def strip_excessive_blank_lines(text: str) -> str:
    """Collapse 3+ consecutive blank lines to 2 in *text*."""
    lines = text.split("\n")
    result: list[str] = []
    blank_count = 0
    for line in lines:
        if not line.strip():
            blank_count += 1
            if blank_count <= 2:
                result.append(line)
        else:
            blank_count = 0
            result.append(line)
    return "\n".join(result)


def process_content(text: str) -> str:
    """Apply all cleaning steps to *text*."""
    text = strip_block_patterns(text)
    text = strip_license_headers(text)
    text = strip_tool_noise(text)
    text = dedup_consecutive(text)
    text = strip_excessive_blank_lines(text)
    return text


def split_file(filepath: Path, content: str, max_lines: int, output_dir: Path) -> list[Path]:
    """Split *content* into chunks if it exceeds *max_lines*.

    Returns a list of output file paths.  When no split is needed a
    single file is written.
    """
    lines = content.split("\n")

    if len(lines) <= max_lines:
        out_path = output_dir / filepath.name
        out_path.write_text(content, encoding="utf-8")
        return [out_path]

    base_name = filepath.stem
    ext = filepath.suffix
    total_parts = (len(lines) + max_lines - 1) // max_lines
    output_files: list[Path] = []

    for i in range(0, len(lines), max_lines):
        chunk = lines[i : i + max_lines]
        part_num = i // max_lines + 1
        out_name = f"{base_name}_part{part_num:03d}_of_{total_parts:03d}{ext}"
        out_path = output_dir / out_name
        out_path.write_text("\n".join(chunk), encoding="utf-8")
        output_files.append(out_path)

    return output_files


def preprocess_file(
    filepath: Path,
    output_dir: Path,
    max_lines: int,
    dry_run: bool = False,
) -> list[Path]:
    """Process a single file.  Returns list of output file paths."""
    if should_skip(filepath):
        return []

    ext = filepath.suffix.lower()
    if ext not in PROCESSABLE_EXTENSIONS and ext != "":
        return []

    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    if len(content.strip()) < 10:
        return []

    cleaned = process_content(content)

    if len(cleaned.strip()) < 10:
        return []

    if dry_run:
        original_lines = len(content.split("\n"))
        cleaned_lines = len(cleaned.split("\n"))
        print(f"  {filepath.name}: {original_lines} -> {cleaned_lines} lines")
        return []

    return split_file(filepath, cleaned, max_lines, output_dir)


def preprocess_directory(staging_dir: str, max_lines: int, dry_run: bool = False) -> dict[str, int]:
    """Preprocess all files in *staging_dir*.

    Writes cleaned/split files to ``staging_dir/processed/``.
    Returns a stats dict.
    """
    staging = Path(staging_dir)
    processed_dir = staging / "processed"

    if not dry_run:
        processed_dir.mkdir(exist_ok=True)
        for f in processed_dir.iterdir():
            if f.is_file():
                f.unlink()

    stats = {
        "total_files": 0,
        "processed": 0,
        "skipped": 0,
        "split": 0,
        "output_files": 0,
        "errors": 0,
    }

    for filepath in sorted(staging.rglob("*")):
        if not filepath.is_file():
            continue
        if filepath.parent.name == "processed":
            continue
        if filepath.name == "mempalace.yaml":
            continue

        stats["total_files"] += 1

        if should_skip(filepath):
            stats["skipped"] += 1
            continue

        try:
            outputs = preprocess_file(filepath, processed_dir, max_lines, dry_run)
            if outputs:
                stats["processed"] += 1
                stats["output_files"] += len(outputs)
                if len(outputs) > 1:
                    stats["split"] += 1
            elif not dry_run:
                stats["skipped"] += 1
        except Exception as e:
            stats["errors"] += 1
            print(f"  ERROR processing {filepath.name}: {e}", file=sys.stderr)

    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preprocess files for MemPalace mining")
    parser.add_argument("staging_dir", help="Staging directory to process")
    parser.add_argument(
        "--max-lines",
        type=int,
        default=MAX_LINES_DEFAULT,
        help=f"Max lines per file (default: {MAX_LINES_DEFAULT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be processed without writing",
    )
    args = parser.parse_args()

    stats = preprocess_directory(args.staging_dir, args.max_lines, args.dry_run)

    print("\nPreprocessing complete:")
    print(f"  Total files scanned: {stats['total_files']}")
    print(f"  Files processed:     {stats['processed']}")
    print(f"  Files split:         {stats['split']}")
    print(f"  Files skipped:       {stats['skipped']}")
    print(f"  Output files:        {stats['output_files']}")
    print(f"  Errors:              {stats['errors']}")
