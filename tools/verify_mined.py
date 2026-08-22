#!/usr/bin/env python3
"""Verify a mined sample is searchable and belongs to the current batch.

Called by staging_watcher.sh after ``mempalace mine`` to fail closed on:
  - search command errors
  - blank/empty search output
  - unusable sample (cannot extract a stable snippet)
  - search hits that point outside the current batch manifest

The script requires a machine-readable JSON response from
``mempalace search --json`` and checks the returned ``source_file`` against
the batch manifest before destructive cleanup may proceed.
"""

import json
import re
import subprocess
import sys
from pathlib import Path


def extract_snippet(sample_file: Path) -> str:
    """Extract a stable, unique-ish snippet from the first lines of a file."""
    try:
        text = sample_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

    # Same logic as staging_watcher.sh: first 20 non-blank, non-comment lines
    for line in text.split("\n")[:20]:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Remove punctuation/cruft and collapse whitespace
        snippet = re.sub(r"[^a-zA-Z0-9 ]", " ", line)
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if len(snippet) >= 10:
            return snippet
    return ""


def load_manifest(manifest_file: Path) -> set[str]:
    """Load the batch manifest as a set of processed file paths."""
    paths = set()
    if not manifest_file.exists():
        return paths
    for line in manifest_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            paths.add(line)
    return paths


def run_search(mempalace_bin: str, palace_path: str, query: str, source_file: str) -> dict:
    """Run a machine-readable mempalace search scoped to a single source file."""
    cmd = [
        mempalace_bin,
        "--palace",
        palace_path,
        "search",
        query,
        "--source-file",
        source_file,
        "--json",
        "--results",
        "5",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return json.loads(result.stdout)


def verify_one(
    palace_path: str,
    sample_file: Path,
    manifest: set[str],
    mempalace_bin: str,
) -> bool:
    """Return True if *sample_file* is searchable and its hits are in *manifest*."""
    snippet = extract_snippet(sample_file)
    if not snippet:
        print(
            f"verify_mined: could not extract a usable snippet from {sample_file}", file=sys.stderr
        )
        return False

    if str(sample_file) not in manifest:
        print(f"verify_mined: {sample_file} not in batch manifest", file=sys.stderr)
        return False

    try:
        data = run_search(mempalace_bin, palace_path, snippet, str(sample_file))
    except subprocess.CalledProcessError as e:
        print(f"verify_mined: search failed with exit {e.returncode}", file=sys.stderr)
        return False
    except json.JSONDecodeError as e:
        print(f"verify_mined: could not parse search JSON: {e}", file=sys.stderr)
        return False

    results = data.get("results", [])
    if not results:
        print(f"verify_mined: search returned no results for {sample_file}", file=sys.stderr)
        return False

    for hit in results:
        hit_source = hit.get("source_file", "")
        if hit_source and hit_source in manifest:
            if Path(hit_source).resolve() == sample_file:
                return True

    print(
        f"verify_mined: search hit source_file not in batch manifest for {sample_file}",
        file=sys.stderr,
    )
    return False


def main() -> int:
    if len(sys.argv) < 4:
        print(
            "usage: verify_mined.py <palace_path> <sample_file|manifest_file> <manifest_file> [mempalace_bin]",
            file=sys.stderr,
        )
        return 2

    palace_path = sys.argv[1]
    sample_arg = Path(sys.argv[2])
    manifest_file = Path(sys.argv[3])
    mempalace_bin = sys.argv[4] if len(sys.argv) > 4 else "mempalace"

    manifest = load_manifest(manifest_file)
    if not manifest:
        print("verify_mined: manifest is empty or missing", file=sys.stderr)
        return 1

    # If the second argument is the manifest itself, verify every entry.
    if sample_arg.resolve() == manifest_file.resolve():
        failures = 0
        for item in sorted(manifest):
            sample_file = Path(item).resolve()
            if not sample_file.exists():
                print(f"verify_mined: manifest entry missing on disk: {item}", file=sys.stderr)
                failures += 1
                continue
            if not verify_one(palace_path, sample_file, manifest, mempalace_bin):
                failures += 1
        if failures:
            print(f"verify_mined: {failures} manifest entries failed verification", file=sys.stderr)
            return 1
        return 0

    # Backward-compatible single-sample verification.
    sample_file = sample_arg.resolve()
    if verify_one(palace_path, sample_file, manifest, mempalace_bin):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
