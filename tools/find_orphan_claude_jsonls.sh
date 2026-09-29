#!/usr/bin/env bash
# find_orphan_claude_jsonls.sh — v3 (multi-line shape + verb-aware preview)
# -----------------------------------------------------------------------------
# Finds Claude Code conversation transcripts (.jsonl) that may have survived in
# backup/sync locations. Claude Code stores transcripts at
# ~/.claude/projects/<encoded>/<session>.jsonl and auto-deletes them locally
# after 30 days. If your machine syncs to iCloud, Dropbox, Google Drive,
# OneDrive, Time Machine, or you copied transcripts elsewhere manually, those
# copies still exist. This script finds them and shows a topic preview from
# the first substantive user message — strips leading filler interjections
# ("ok so", "oh", "well", "hey") so previews surface the actual content.
#
# Read-only. Safe to re-run.
# -----------------------------------------------------------------------------
set -eu

LOCATIONS=(
  "$HOME/Library/Mobile Documents" "$HOME/Dropbox" "$HOME/Google Drive"
  "$HOME/OneDrive" "$HOME/Documents" "$HOME/Desktop" "/Volumes"
)

TMP="$(mktemp)"; trap 'rm -f "$TMP" "$TMP.s"' EXIT

printf "Scanning backup locations" >&2
for loc in "${LOCATIONS[@]}"; do
  [ -d "$loc" ] || continue
  printf "." >&2
  while IFS= read -r -d '' f; do
    # Combined: shape detection (multi-line) + verb-aware topic preview
    if preview="$(python3 - "$f" 2>/dev/null <<'PYEOF'
import json, sys, re

# Single-word/short greetings — message gets skipped entirely if it is just one of these
GREETINGS = {'hi','hey','hello','thanks','thank you','ok','okay','yes','no',
             'sure','cool','great','good','done','yep','nope','perfect','copy'}

# Leading filler — interjections that get STRIPPED from the start of a message
# before the preview is taken. Iterative — handles "ok so well, then..." → "then..."
LEADING_FILLER = re.compile(
    r'^(?:ok(?:ay)?|so|oh|well|anyway|btw|hmm+|um+|uh+|hey|hi|hello|right|'
    r'yes|no|sure|cool|great|good|listen|look|wait|actually|alright|gotcha|'
    r'yeah|yep|nope|nah)\b[\s,!.?:;-]*',
    re.IGNORECASE
)

path = sys.argv[1]
shape_ok = False
preview = ""
try:
    with open(path, 'r', errors='replace') as fh:
        for i, line in enumerate(fh):
            if i >= 30: break
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict): continue
            # Shape check — accept if any line in first 30 has session fields
            if not shape_ok and 'sessionId' in d and 'timestamp' in d and 'message' in d:
                shape_ok = True
            # Preview — first user message after stripping leading filler
            if not preview:
                role = d.get('type', '') or d.get('message', {}).get('role', '')
                if role == 'user':
                    content = d.get('message', {}).get('content', '')
                    if isinstance(content, list):
                        text = ' '.join(
                            c.get('text', '') for c in content
                            if isinstance(c, dict) and c.get('type') == 'text'
                        )
                    elif isinstance(content, str):
                        text = content
                    else:
                        text = ''
                    text = re.sub(r'\s+', ' ', text).strip()
                    # Skip messages that are pure greetings
                    if text.lower() in GREETINGS:
                        continue
                    # Iteratively strip leading filler tokens until stable
                    prev_text = None
                    while prev_text != text:
                        prev_text = text
                        text = LEADING_FILLER.sub('', text).strip()
                    # Skip if what remains is too short
                    if len(text) < 20:
                        continue
                    preview = text[:80] + ('...' if len(text) > 80 else '')
            if shape_ok and preview: break
except Exception:
    pass
if shape_ok:
    print(preview if preview else "(no preview — first 30 lines were greetings or short)")
    sys.exit(0)
sys.exit(1)
PYEOF
)"; then
      mtime="$(stat -f '%Sm' -t '%Y-%m-%d' "$f" 2>/dev/null || stat -c '%y' "$f" 2>/dev/null | cut -d' ' -f1)"
      size="$(stat -f '%z' "$f" 2>/dev/null || stat -c '%s' "$f" 2>/dev/null)"
      printf '%s\t%s\t%s\t%s\n' "$mtime" "$size" "$f" "$preview" >>"$TMP"
    fi
  done < <(find "$loc" -type f -name '*.jsonl' -print0 2>/dev/null)
done
printf "\n" >&2

count=$(wc -l <"$TMP" | tr -d ' ')
if [ "$count" -eq 0 ]; then
  echo "No orphan Claude Code transcripts found in scanned backup locations."
  exit 0
fi
sort -k1,1 "$TMP" >"$TMP.s"
oldest="$(head -n 1 "$TMP.s" | cut -f1)"
newest="$(tail -n 1 "$TMP.s" | cut -f1)"
echo "Found $count orphan Claude Code transcript(s). Oldest: $oldest  Newest: $newest"
echo "----------------------------------------------------------------------"
awk -F'\t' '{ printf "%s  %10s  %s\n              \"%s\"\n\n", $1, $2, $3, $4 }' "$TMP.s"
