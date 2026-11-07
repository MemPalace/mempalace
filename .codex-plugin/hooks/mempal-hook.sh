#!/usr/bin/env bash
# MemPalace hook for the Codex plugin: `mempal-hook.sh <hook-name>`.
# `mempalace hook run` does the work; mempal_hook_run.sh next to this file
# picks the interpreter. A missing mempalace exits 1 so Codex reports it.
set -uo pipefail
HOOK_NAME="${1:?Usage: mempal-hook.sh <hook-name>}"
here="${BASH_SOURCE[0]%/*}"
[ "$here" = "${BASH_SOURCE[0]}" ] && here=.
exec "${BASH:-bash}" "$here/mempal_hook_run.sh" --on-missing=error --hook "$HOOK_NAME" --harness codex
