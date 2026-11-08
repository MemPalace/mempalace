#!/bin/bash
# MemPalace PreCompact hook for the Claude Code plugin. `mempalace hook run`
# does the work; mempal_hook_run.sh next to this file picks the interpreter. A
# missing mempalace exits 1 so Claude Code reports the hook as failed.
here="${BASH_SOURCE[0]%/*}"
[ "$here" = "${BASH_SOURCE[0]}" ] && here=.
exec "${BASH:-bash}" "$here/mempal_hook_run.sh" --on-missing=error --hook precompact --harness claude-code
