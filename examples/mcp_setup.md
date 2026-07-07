# MCP Integration — Claude Code

## Setup

Choose a client path that fits your workflow:

```bash
mempalace-mcp
```

Then register it where needed:

```bash
claude mcp add mempalace -- mempalace-mcp
```

## Available Tools

The server exposes the full MemPalace MCP toolset. For memory-aware assistants, these are the high-value calls:

- **mempalace_status** — protocol + AAAK dialect bootstrap.
- **mempalace_search** — scoped semantic recall.
- **mempalace_kg_query** — time-bounded relationship checks.
- **mempalace_get_drawer** — full-text verification of one fact.
- **mempalace_diary_write/read** — persistent agent memory continuity.

## Usage in Claude Code

Once configured, Claude Code can search your memories directly during conversations.

## Recommended recall pattern

For each new turn:

1. Call `mempalace_status` once at startup.
2. Search first (`mempalace_search`) before answering about people, projects, or prior decisions.
3. If a statement is time-sensitive, call `mempalace_kg_query` with `as_of` before asserting it.
4. End the session with a `mempalace_diary_write` entry (AAAK format preferred).

That pattern keeps context fresh without guessing and preserves continuity across sessions.
