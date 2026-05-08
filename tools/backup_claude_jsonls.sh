#!/usr/bin/env bash
# backup_claude_jsonls.sh
#
# Claude Code stores every conversation as a JSONL transcript at
#   ~/.claude/projects/<encoded-project>/<session-uuid>.jsonl
# Anthropic auto-deletes those files after 30 DAYS:
#   https://docs.claude.com/en/docs/claude-code/data-usage
#
# This script copies them, read-only, into ~/Documents/Claude_JSONL_Backup/
# so the 30-day clock no longer applies. Re-run any time — rsync is incremental.
# It NEVER deletes, modifies, or touches files inside ~/.claude/.

set -eu

SRC="${HOME}/.claude/projects/"
DST="${HOME}/Documents/Claude_JSONL_Backup/"

[ -d "$SRC" ] || { echo "ERROR: $SRC does not exist."; exit 1; }
mkdir -p "$DST"

echo "Backing up $SRC -> $DST"
rsync -a --times "$SRC" "$DST"

src_count=$(find "$SRC" -type f -name '*.jsonl' | wc -l | tr -d ' ')
dst_count=$(find "$DST" -type f -name '*.jsonl' | wc -l | tr -d ' ')
oldest=$(find "$DST" -type f -name '*.jsonl' -exec stat -f '%Sm %N' -t '%Y-%m-%d' {} \; 2>/dev/null \
        || find "$DST" -type f -name '*.jsonl' -printf '%TY-%Tm-%Td %p\n' 2>/dev/null)
oldest_date=$(echo "$oldest" | sort | head -n 1 | awk '{print $1}')
newest_date=$(echo "$oldest" | sort | tail -n 1 | awk '{print $1}')

echo "Source JSONL count : $src_count"
echo "Backup JSONL count : $dst_count"
echo "Oldest backup file : ${oldest_date:-n/a}"
echo "Newest backup file : ${newest_date:-n/a}"

if [ "$src_count" -ne "$dst_count" ]; then
  echo "FAIL: count mismatch ($src_count vs $dst_count)"; exit 2
fi
echo "OK: backup verified."
