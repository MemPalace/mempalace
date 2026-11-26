#!/usr/bin/env python3
"""Verify that mined batch files are actually searchable in the palace.

Two checks, both fail-closed:

  1. Content integrity — sha256 of each claimed work copy still matches
     the batch snapshot. With verbatim staging the mined file is a byte
     copy, so this is exact on every platform (no text-mode newline
     translation ever touches the bytes).

  2. Searchability — for each mined file (or a sample), extract a
     snippet and query the palace through mempalace.searcher directly.
     The ``source_file`` filter pins the search to that exact mined
     path, so a hit proves this batch's file — not a same-named file
     from an earlier batch — is retrievable. No CLI flags required.

Usage:
    python tools/verify_mined.py --palace /path/to/palace \
        --manifest mined.manifest --mine-dir /work/mine \
        --snapshot .batch_snapshot --work-dir /work/orig [--all]
"""

import argparse
import hashlib
import sys
from pathlib import Path

_SNAPSHOT_SEP = "\x1f"
_SNIPPET_LEN = 120
_SNIPPET_MIN = 10


def _repo_root():
    return Path(__file__).resolve().parent.parent


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_snapshot(path):
    entries = {}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split(_SNAPSHOT_SEP)
        if len(parts) >= 4:
            entries[parts[0]] = parts[3]
    return entries


def load_manifest(path):
    """Mined manifest lines: orig_rel <US> mined_rel. Returns (origs, mined)."""
    origs, mined = [], []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split(_SNAPSHOT_SEP)
        if len(parts) == 2:
            origs.append(parts[0])
            mined.append(parts[1])
    return sorted(set(origs)), mined


def extract_snippet(path):
    """First meaningful content line, decoded for use as a search query."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    for line in data.decode("utf-8", errors="replace").splitlines():
        s = line.strip()
        if len(s) >= _SNIPPET_MIN:
            return s[:_SNIPPET_LEN]
    return None


def _searcher():
    sys.path.insert(0, str(_repo_root()))
    from mempalace.searcher import search_memories

    return search_memories


def check_hashes(orig_rels, snapshot, work_dir):
    """Every claimed original still matches its snapshot hash."""
    bad = []
    for rel in orig_rels:
        expected = snapshot.get(rel)
        if expected is None:
            continue
        try:
            actual = _sha256(Path(work_dir) / rel)
        except OSError:
            bad.append((rel, "missing work copy"))
            continue
        if actual != expected:
            bad.append((rel, "hash drift"))
    return bad


def check_searchable(mined_rels, mine_dir, palace_path, sample, search_fn):
    """Each mined file must return a hit under its own source_file."""
    targets = mined_rels[:sample] if sample and sample < len(mined_rels) else mined_rels
    failures = []
    for rel in targets:
        mined_path = Path(mine_dir) / rel
        snippet = extract_snippet(mined_path)
        if snippet is None:
            failures.append((rel, "no usable snippet"))
            continue
        try:
            result = search_fn(
                snippet,
                str(palace_path),
                source_file=str(mined_path.resolve()),
                n_results=1,
            )
        except Exception as e:  # noqa: BLE001 — any search error = unverified
            failures.append((rel, f"search error: {e}"))
            continue
        hits = result.get("results") or []
        if not hits:
            failures.append((rel, "no hit under own source_file"))
    return failures


def main(argv=None):
    p = argparse.ArgumentParser(description="Verify mined batch files are searchable.")
    p.add_argument("--palace", required=True, help="palace directory")
    p.add_argument("--manifest", required=True, help="mined manifest (orig<US>mined)")
    p.add_argument("--mine-dir", required=True, help="mine root the manifest paths resolve under")
    p.add_argument("--snapshot", required=True, help="batch snapshot for hash checks")
    p.add_argument("--work-dir", required=True, help="verified original work copies")
    p.add_argument("--sample", type=int, default=0, help="verify only N mined files")
    p.add_argument("--all", action="store_true", help="verify every mined file")
    args = p.parse_args(argv)

    orig_rels, mined_rels = load_manifest(args.manifest)
    if not mined_rels:
        print("verify: manifest lists no mined files", file=sys.stderr)
        return 1

    snapshot = load_snapshot(args.snapshot)
    bad_hashes = check_hashes(orig_rels, snapshot, args.work_dir)
    for rel, why in bad_hashes:
        print(f"verify: HASH FAIL {rel} ({why})", file=sys.stderr)

    sample = 0 if args.all else (args.sample or min(5, len(mined_rels)))
    failures = check_searchable(mined_rels, args.mine_dir, args.palace, sample, _searcher())
    for rel, why in failures:
        print(f"verify: SEARCH FAIL {rel} ({why})", file=sys.stderr)

    if bad_hashes or failures:
        print(
            f"verify: FAILED — {len(bad_hashes)} hash, {len(failures)} search",
            file=sys.stderr,
        )
        return 1
    print(f"verify: OK — {len(mined_rels)} mined file(s), sample {len(mined_rels[:sample])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
