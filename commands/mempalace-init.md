---
description: Set up MemPalace — install the package, initialize a palace, register the MCP server with Cursor, and verify everything works.
---

Invoke the `mempalace` skill from this plugin and run the `init` instructions, then follow them.

Concretely: run `mempalace instructions init` in a terminal, then carry out the steps it prints.

Cursor-specific extras after init:

1. The `mempalace-mcp` server is already auto-registered by this plugin — no manual `mcp.json` edit needed.
2. For automatic background saves and session-start memory recall, also run `hooks/cursor/install.sh --scope user` from a cloned MemPalace repo. See `website/guide/cursor-hooks.md` for the walkthrough.
