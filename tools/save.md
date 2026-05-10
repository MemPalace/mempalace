---
description: Save the current Claude Code session into MemPalace. Idempotent — won't dupe.
---

# /save

Save the current Claude Code session into MemPalace. Run this when you
want a checkpoint. Safe to run repeatedly — drawer IDs are content-hashed
so re-running on the same session overwrites in place, no duplicates.

Behavior:

1. Find the current session's JSONL transcript path (Claude Code passes
   it via the conversation context — look for `~/.claude/projects/` paths).
2. Run via bash:

   ```
   mempalace mine "<TRANSCRIPT_PATH>" --mode convos --wing claude_imports
   ```

3. If the user supplied an argument after `/save`, use it as the wing name
   instead of `claude_imports` (e.g. `/save my_research` →
   `--wing my_research`).
4. Report back: how many drawers were filed, into which wing/room.

Requires `mempalace` to be installed (`uv tool install mempalace` recommended, or `pip install mempalace`).
