#!/bin/bash
# MEMPALACE SESSION-END HOOK — Final save on clean exit
#
# Thin wrapper around the Python hook dispatcher so the clean-exit path
# reuses the same save/mining logic as the first-class CLI hook.

HOOK_HARNESS="${MEMPALACE_HOOK_HARNESS:-claude-code}"

exec mempalace hook run --hook session-end --harness "$HOOK_HARNESS"
