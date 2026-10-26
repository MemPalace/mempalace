#!/bin/bash
# Legacy Claude Code PreCompact hook. `mempalace hook run` does the mining;
# mempal_hook_run.sh picks the interpreter. The supported install is the
# Claude Code plugin (.claude-plugin/); see hooks/README.md.
exec bash "$(dirname "$0")/mempal_hook_run.sh" --hook precompact --harness claude-code
