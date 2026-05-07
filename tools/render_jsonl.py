#!/usr/bin/env python3
"""render_jsonl.py — turn one Claude Code JSONL transcript into readable text.

Claude Code stores conversations at ~/.claude/projects/<proj>/<uuid>.jsonl and
Anthropic auto-deletes them after 30 days
(https://docs.claude.com/en/docs/claude-code/data-usage). This script renders a
JSONL into a clean .txt so you can keep / read / share it without the tooling.

Usage:
    python3 render_jsonl.py <input.jsonl> [output.txt]

Stdlib only. Python 3.9+. Read-only on the input.
"""

import json
import sys
from pathlib import Path


def extract_text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                t = (blk.get("text") or "").strip()
                if t:
                    parts.append(t)
        return "\n".join(parts)
    return ""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = Path(sys.argv[1])
    if not src.is_file():
        print(f"ERROR: not a file: {src}")
        sys.exit(1)
    out = open(sys.argv[2], "w", encoding="utf-8") if len(sys.argv) > 2 else sys.stdout

    turns, stamps = [], []
    for raw in src.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        role = obj.get("type") or (obj.get("message") or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        msg = obj.get("message") or obj
        text = extract_text(msg.get("content"))
        if not text:
            continue
        ts = obj.get("timestamp") or ""
        if ts:
            stamps.append(ts)
        turns.append((ts, role, text))

    header = [
        f"# Claude Code transcript: {src}",
        f"# Total turns: {len(turns)}",
        f"# Date range : {min(stamps) if stamps else 'n/a'}  ->  {max(stamps) if stamps else 'n/a'}",
        "#" + "-" * 70,
        "",
    ]
    out.write("\n".join(header))
    for ts, role, text in turns:
        out.write(f"\n[{ts}] {role.upper()}\n{text}\n\n{'-'*72}\n")
    if out is not sys.stdout:
        out.close()
        print(f"Wrote {len(turns)} turns to {sys.argv[2]}")


if __name__ == "__main__":
    main()
