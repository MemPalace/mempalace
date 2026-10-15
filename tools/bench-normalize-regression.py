#!/usr/bin/env python3
"""Measure the WorkBuddy reader: cross-format regression + injection census.

Two questions, for a PR that adds a new transcript format:

1. **Regression.** Does adding the reader change what every *other* format
   produces? The comparison is byte-for-byte against the same source tree with
   this patch absent, so a difference can only come from the patch. Point
   ``--upstream`` at a clean checkout of the target branch and ``--patched`` at
   the tree under review.

2. **Census.** What do the reader's strip functions actually remove, and do the
   injected blocks pair up? The baseline here is the *same* tree with the strip
   functions monkey-patched to identity, so "before" and "after" are the same
   parsing pass and the difference is exactly the stripping.

Usage::

    python tools/bench-normalize-regression.py \\
        --upstream <clean tree> --patched <tree under review> \\
        [--workbuddy-root DIR] [--claude-root DIR] [--pi-root DIR]

Only the roots that exist are measured. Nothing is written; JSON goes to
stdout. These corpora are live conversation stores, so counts drift between
runs: every file's input hash is recorded, and a file whose bytes changed
between the two passes is excluded from the comparison.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOTS = {
    "workbuddy": (str(Path.home() / ".workbuddy" / "projects"), None),
    "claude": (str(Path.home() / ".claude" / "projects"), None),
    "pi": (str(Path.home() / ".pi" / "agent" / "sessions"), "node_modules"),
}

_WORKER = r'''
import hashlib, json, re, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import mempalace.normalize as N

TAGS = [
    "system-reminder", "user_query",
    "cb_summary", "conversation_history_summary",
    "previous_user_message", "previous_assistant_message", "previous_tool_call",
]


def tag_counts(text):
    """Line-anchored open/close counts — the same rule the strippers use."""
    d = {}
    for t in TAGS:
        esc = re.escape(t)
        d[t] = {
            "o": len(re.findall(rf"(?m)^[ \t]*(?:> )?<{esc}(?:\s[^>]*)?>", text)),
            "c": len(re.findall(rf"</{esc}>", text)),
        }
    return d


def row_counts(raw):
    lines = rows = nest = top = both = 0
    for ln in raw.splitlines():
        lines += 1
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
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
        "lines": lines, "rows_parsed": rows,
        "nest_hits": nest, "top_hits": top, "both_hits": both,
    }


def read_without_strip(path):
    """Parsed text with the strip functions disabled (ablation baseline).

    Rebinds the module-level names the WorkBuddy parser looks up, so this is the
    ordinary parse with only the stripping removed. On an unpatched tree these
    attributes do not exist and the caller falls back to no baseline.
    """
    saved = (N._strip_summary_echo, N._strip_history_echo, N._peel_user_query,
             N.strip_noise)
    N._strip_summary_echo = lambda t: t
    N._strip_history_echo = lambda t: t
    N._peel_user_query = lambda t: t
    N.strip_noise = lambda t, tag_patterns=None: t
    try:
        return N.normalize(str(path)) or ""
    finally:
        (N._strip_summary_echo, N._strip_history_echo, N._peel_user_query,
         N.strip_noise) = saved


roots = json.loads(sys.argv[2])
out = {}
for name, (root, excl) in roots.items():
    if not root:
        continue
    base = Path(root)
    if not base.is_dir():
        out[name] = {"__skip__": "no such directory: " + root}
        continue
    files = sorted(base.rglob("*.jsonl"))
    if excl:
        files = [f for f in files if excl not in f.parts]
    d = {}
    for f in files:
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        key = str(f)
        rec = {"in_sha": hashlib.sha256(raw).hexdigest()}
        try:
            t = N.normalize(key) or ""
        except Exception as exc:
            rec["error"] = type(exc).__name__
            d[key] = rec
            continue
        rec["len"] = len(t)
        rec["sha"] = hashlib.sha256(t.encode("utf-8", "surrogatepass")).hexdigest()
        rec["parsed"] = t.startswith("> ") or "\n> " in t
        if name == "workbuddy":
            txt = raw.decode("utf-8", "replace")
            rec["out_tags"] = tag_counts(t)
            rec["out_len"] = len(t)
            rec["rows"] = row_counts(txt)
            try:
                pre = read_without_strip(f)
            except Exception:
                pre = None
            if pre:
                rec["pre_tags"] = tag_counts(pre)
                rec["pre_len"] = len(pre)
        d[key] = rec
    out[name] = d
print(json.dumps(out))
'''


def run_worker(tree: str, corpora: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER, tree, json.dumps(corpora)],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if proc.returncode != 0:
        raise SystemExit(f"worker failed for {tree}:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def census(patched: dict, keys: list) -> dict:
    """Pre-strip vs. final output, same tree and same parsing pass."""
    pre, out = {}, {}
    pre_len = out_len = files = 0
    for k in keys:
        pt, ot = patched[k].get("pre_tags"), patched[k].get("out_tags")
        if not (pt and ot):
            continue
        files += 1
        pre_len += patched[k].get("pre_len", 0)
        out_len += patched[k].get("out_len", 0)
        for m, v in pt.items():
            pre.setdefault(m, {"o": 0, "c": 0})
            pre[m]["o"] += v["o"]
            pre[m]["c"] += v["c"]
        for m, v in ot.items():
            out.setdefault(m, {"o": 0, "c": 0})
            out[m]["o"] += v["o"]
            out[m]["c"] += v["c"]
    return {
        "files": files,
        "pre_strip_tags": pre,
        "output_tags": out,
        "removed_tags": {
            m: {
                "o": pre[m]["o"] - out.get(m, {}).get("o", 0),
                "c": pre[m]["c"] - out.get(m, {}).get("c", 0),
            }
            for m in pre
        },
        "pre_strip_chars": pre_len,
        "output_chars": out_len,
        "size_change_pct": (round(100.0 * (pre_len - out_len) / pre_len, 2) if pre_len else None),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--upstream", required=True, help="clean checkout of the target branch (patch absent)"
    )
    ap.add_argument("--patched", required=True, help="tree under review")
    ap.add_argument("--workbuddy-root", default=DEFAULT_ROOTS["workbuddy"][0])
    ap.add_argument("--claude-root", default=DEFAULT_ROOTS["claude"][0])
    ap.add_argument("--pi-root", default=DEFAULT_ROOTS["pi"][0])
    args = ap.parse_args()

    corpora = {
        "workbuddy": (args.workbuddy_root, None),
        "claude": (args.claude_root, None),
        "pi": (args.pi_root, "node_modules"),
    }

    up, patched = run_worker(args.upstream, corpora), run_worker(args.patched, corpora)
    res = {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "upstream_tree": args.upstream,
        "patched_tree": args.patched,
        "scope": {
            name: {"root": root, "recursive": True, "exclude": excl}
            for name, (root, excl) in corpora.items()
        },
    }

    for name in corpora:
        a, b = up.get(name), patched.get(name)
        if not a or not b:
            continue
        if "__skip__" in a or "__skip__" in b:
            res[name] = {"skipped": a.get("__skip__") or b.get("__skip__")}
            continue
        keys = [k for k in sorted(set(a) & set(b)) if "sha" in a[k] and "sha" in b[k]]
        changed = [k for k in keys if a[k].get("in_sha") != b[k].get("in_sha")]
        stable = [k for k in keys if a[k].get("in_sha") == b[k].get("in_sha")]
        differing = [k for k in stable if a[k]["sha"] != b[k]["sha"]]
        res[name] = {
            "files_in_tree": len(set(a) | set(b)),
            "compared": len(stable),
            "excluded_changed_mid_run": len(changed),
            "byte_identical": len(stable) - len(differing),
            "differing": [
                {"file": k, "upstream_len": a[k].get("len"), "patched_len": b[k].get("len")}
                for k in differing
            ][:10],
            "parsed_upstream": sum(1 for k in stable if a[k].get("parsed")),
            "parsed_patched": sum(1 for k in stable if b[k].get("parsed")),
        }
        if name == "workbuddy":
            res[name]["row_census"] = {
                key: sum(b[k].get("rows", {}).get(key, 0) for k in stable)
                for key in ("lines", "rows_parsed", "nest_hits", "top_hits", "both_hits")
            }
            res[name]["strip_census"] = census(b, stable)

    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
