#!/usr/bin/env python3
"""Measure the WorkBuddy hook behaviour: wing collapse + human-turn census.

Two questions, for a PR that adds a harness whose ``cwd`` is not a stable
project root:

1. **Wing collapse.** How many distinct wings does
   ``_wing_from_transcript_path()`` mint for one corpus, with and without this
   patch? Point ``--upstream`` at a clean checkout of the target branch and
   ``--patched`` at the tree under review. Both trees resolve the *same*
   transcript paths, so a difference can only come from the change.

2. **Human-turn census.** How many turns does ``_count_human_messages()`` count
   in that corpus, per tree? Zero on the unpatched tree means the stop hook
   never reaches ``SAVE_INTERVAL`` and silently never saves. The row census
   below also reports how many rows match the nested-``message`` shape, the
   top-level-``role`` shape, and **both** — a non-zero "both" would mean the two
   branches can double-count the same turn.

Usage::

    python tools/bench-workbuddy-hooks.py \\
        --upstream <clean tree> --patched <tree under review> \\
        [--workbuddy-root DIR]

Nothing is written; JSON goes to stdout. The script needs a corpus of ``.jsonl``
transcripts (defaults to the WorkBuddy store, overridable with
``--workbuddy-root``) and two trees that can be imported — a reviewer without a
WorkBuddy install can point ``--workbuddy-root`` at any directory of transcripts
to exercise the two trees against a corpus of their own. One boundary: the wing
census feeds each file's real path to ``_wing_from_transcript_path()``, so the
fold only triggers when the corpus path contains ``/.workbuddy/projects/``; a
corpus parked elsewhere reports a zero wing delta on both trees, while the
human-turn census (content-based) reproduces anywhere. These corpora are live
conversation stores, so counts drift between runs: every file's input hash is
recorded, and a file whose bytes changed between the two passes is excluded from
the comparison.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = str(Path.home() / ".workbuddy" / "projects")

_WORKER = r"""
import hashlib, json, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import mempalace.hooks_cli as H

root = Path(sys.argv[2])
out = {}
for f in sorted(root.rglob("*.jsonl")):
    key = str(f)
    try:
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
    except OSError:
        continue
    try:
        wing = H._wing_from_transcript_path(key)
    except Exception as exc:
        wing = "<ERROR " + type(exc).__name__ + ">"
    try:
        human = H._count_human_messages(key)
    except Exception:
        human = -1
    out[key] = {"sha": sha, "wing": wing, "human_messages": human}
print(json.dumps(out))
"""


def run_worker(tree: str, root: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER, tree, root],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        raise SystemExit(f"worker failed for {tree}:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def row_census(root: Path) -> dict:
    """Raw-JSONL census: which shape does each row carry? Tree-independent."""
    lines = rows = nest = top = both = 0
    for f in sorted(root.rglob("*.jsonl")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            lines += 1
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            rows += 1
            m = e.get("message", {})
            n = isinstance(m, dict) and m.get("role") == "user"
            t = e.get("role") == "user"
            nest += int(n)
            top += int(t)
            both += int(n and t)
    return {
        "lines": lines,
        "rows_parsed": rows,
        "nest_hits": nest,
        "top_hits": top,
        "both_hits": both,
    }


def wing_census(records: dict, keys: list) -> dict:
    wings: dict[str, int] = {}
    for k in keys:
        w = records[k]["wing"]
        wings[w] = wings.get(w, 0) + 1
    return {
        "distinct_wings": len(wings),
        "top_wings": [
            {"wing": w, "files": n} for w, n in sorted(wings.items(), key=lambda kv: -kv[1])[:8]
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--upstream", required=True, help="clean checkout of the target branch (patch absent)"
    )
    ap.add_argument("--patched", required=True, help="tree under review")
    ap.add_argument("--workbuddy-root", default=DEFAULT_ROOT)
    args = ap.parse_args()

    root = Path(args.workbuddy_root)
    if not root.is_dir():
        raise SystemExit(
            f"corpus directory not found: {root}\n"
            "Point --workbuddy-root at any directory of .jsonl transcripts (the "
            "WorkBuddy store is only the default); the wing fold reproduces only "
            "when the path contains /.workbuddy/projects/."
        )
    files = sorted(root.rglob("*.jsonl"))

    up = run_worker(args.upstream, args.workbuddy_root)
    patched = run_worker(args.patched, args.workbuddy_root)

    keys = [str(f) for f in files if str(f) in up and str(f) in patched]
    changed = [k for k in keys if up[k]["sha"] != patched[k]["sha"]]
    stable = [k for k in keys if up[k]["sha"] == patched[k]["sha"]]

    before = wing_census(up, stable)
    after = wing_census(patched, stable)
    human_before = sum(max(up[k]["human_messages"], 0) for k in stable)
    human_after = sum(max(patched[k]["human_messages"], 0) for k in stable)

    res = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "upstream_tree": args.upstream,
        "patched_tree": args.patched,
        "corpus_root": args.workbuddy_root,
        "files_in_corpus": len(files),
        "compared": len(stable),
        "excluded_changed_mid_run": len(changed),
        "rows": row_census(root),
        "before": {**before, "human_messages_counted": human_before},
        "after": {**after, "human_messages_counted": human_after},
    }
    print(json.dumps(res, ensure_ascii=False, indent=2))

    n = len(stable)
    rows = res["rows"]

    def _plural(count: int) -> str:
        return f"{count} wing" if count == 1 else f"{count} wings"

    print()
    print(
        f"before (develop):  {n} transcripts \u2192 {_plural(before['distinct_wings'])}"
        f" | {human_before} human turns counted"
    )
    print(
        f"after  (this PR):  {n} transcripts \u2192 {_plural(after['distinct_wings'])}"
        f" | {human_after} human turns counted"
    )
    print(
        f"row census:        {rows['rows_parsed']} rows"
        f" | nested-message {rows['nest_hits']} | top-level role {rows['top_hits']}"
        f" | both {rows['both_hits']}"
    )


if __name__ == "__main__":
    main()
