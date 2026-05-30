#!/bin/bash
# MemPalace PreCompact Hook
# This hook MUST allow compaction to proceed. The previous implementation
# routed through `mempalace hook run --hook precompact --harness claude-code`
# which always returned a "block" decision, preventing both `/compact` and
# auto-compact from working as documented.
# Session saving is unaffected — it happens via the Stop hook (separate path).
echo '{"decision": "allow"}'
