---
name: mempalace
description: "MemPalace — Local AI memory for OpenCode. Real-time conversation persistence via community plugin. Zero cron, zero cloud."
version: 3.3.5
homepage: https://github.com/MemPalace/mempalace
user-invocable: true
metadata:
  opencode:
    emoji: "\U0001F3DB"
    os:
      - darwin
      - linux
      - win32
    requires:
      anyBins:
        - mempalace
        - python3
    install:
      - id: mempalace-plugin
        kind: npm
        label: "Install opencode-mempalace-persistence plugin (community)"
        package: opencode-mempalace-persistence
---

# MemPalace — OpenCode Integration

> **Community-maintained plugin.** This integration uses `opencode-mempalace-persistence`, a community plugin not officially maintained by the MemPalace team. Source: [github.com/geco/opencode-mempalace-persistence](https://github.com/geco/opencode-mempalace-persistence).

MemPalace provides persistent memory for OpenCode. Every conversation is automatically saved to a local vector database — no cron, no cloud, no manual effort. The model can optionally record Knowledge Graph facts during conversation via MCP tools.

## How it works

1. **Plugin hooks**: The plugin listens to OpenCode's `chat.message` and `session.idle` events
2. **Turn detection**: Each complete user question + AI answer is captured as a single turn
3. **Export**: Sessions are exported flat (no forced categorization) to a temp directory
4. **Mining**: `mempalace mine --mode convos` runs asynchronously — UI is never blocked
5. **Memory search**: The model searches MemPalace via MCP before answering (guided by AGENTS.md)
6. **KG (optional)**: The model may record or query structured facts via `mempalace_kg_add` / `kg_query` / `kg_invalidate`

The mining runs asynchronously — state is saved immediately, and the actual vector indexing happens in the background.

## Architecture

```
OpenCode chat.message hook
        ↓
  Query DB for new messages (delta since last sync)
        ↓
  Export sessions → flat /tmp/oc-sessions/ (no wing subdirs)
        ↓
  Save sync state immediately
        ↓
  mempalace mine (async) — single serialized call, --mode convos
        ↓
  session.idle hook (fallback for last turn on shutdown)
```

## Setup

### 1. Install MemPalace (v3.3.5+)

```bash
uv tool install "mempalace>=3.3.5"
# or
pipx install "mempalace>=3.3.5"
```

### 2. Configure MCP server

Add to your `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "mempalace": {
      "type": "local",
      "command": ["mempalace-mcp"],
      "enabled": true
    }
  }
}
```

### 3. Install the persistence plugin

Add to your `~/.config/opencode/opencode.json`:

```json
{
  "plugins": ["opencode-mempalace-persistence"]
}
```

### 4. Add memory instructions for the model

Create `~/.config/opencode/AGENTS.md`:

```markdown
# Memory & Knowledge instructions

## CRITICAL: You MUST search MemPalace BEFORE every response.

1. Call `mempalace_mempalace_search` with the user's question as query.
2. Call `mempalace_mempalace_kg_query` for entity "user" to retrieve relevant facts.
3. Use relevant context in your response.

Knowledge Graph management (optional but recommended):
- `mempalace_mempalace_kg_add` for new facts (subject → predicate → object)
- `mempalace_mempalace_kg_invalidate` when facts change
```

### 5. (Optional) Add your identity

Create `~/.mempalace/identity.txt` with a brief description of who you are. It will be loaded automatically at session start.

## What gets saved

Every conversation turn is saved as a **drawer** in MemPalace. No forced categorization — MemPalace's own mining handles organization. The model can optionally record structured facts (decisions, milestones, preferences) during conversation via MCP tools.

## Benefits over cron-based sync

- **Real-time**: sync happens immediately after each response
- **Delta-only**: only new messages are processed — no duplicates
- **Async mining**: UI never blocked
- **Graceful shutdown**: `session.idle` hook catches the last turn
- **No hardcoded wings**: sessions are exported flat, compatible with any palace structure
- **Serialized mining**: single mine call prevents SQLite FTS5 index corruption

## Links

- Plugin GitHub: https://github.com/geco/opencode-mempalace-persistence
- npm: `opencode-mempalace-persistence`
- awesome-opencode: https://github.com/awesome-opencode/awesome-opencode/pull/357

## License

[MIT](https://github.com/geco/opencode-mempalace-persistence/blob/main/LICENSE)
