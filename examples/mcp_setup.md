# MCP Integration — Claude Code

## Setup

Run the MCP server:

```bash
mempalace-mcp
```

Or add it to Claude Code:

```bash
claude mcp add mempalace -- mempalace-mcp
```

## Available Tools

The server exposes the full MemPalace MCP toolset. Common entry points include:

- **mempalace_status** — palace stats (wings, rooms, drawer counts)
- **mempalace_search** — semantic search across all memories
- **mempalace_list_wings** — list all projects in the palace

## Usage in Claude Code

Once configured, Claude Code can search your memories directly during conversations.
