---
name: mempalace
description: "MemPalace — Local AI memory for OpenCode. Real-time conversation persistence, auto-categorization, and Knowledge Graph extraction. Zero cron, zero cloud."
version: 1.1.0
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
        label: "Install opencode-mempalace-persistence plugin"
        package: opencode-mempalace-persistence
---

# MemPalace — OpenCode Integration

MemPalace provides persistent memory for OpenCode via the `opencode-mempalace-persistence` plugin. Every conversation is automatically saved to a local vector database, categorized by wing type, and indexed in the Knowledge Graph — no cron, no cloud, no manual effort.

## How it works

1. **Plugin hooks**: The plugin listens to OpenCode's `chat.message` and `session.idle` events
2. **Turn detection**: Each complete user question + AI answer is captured as a single turn
3. **Categorization**: Content is analyzed and assigned to a wing (developer, creative, emotions, family, consciousness)
4. **Mining**: The turn is saved to MemPalace via `mempalace mine` (vector DB + extract)
5. **Knowledge Graph**: Structured facts (decisions, milestones, problems, preferences) are extracted automatically from the content

The mining runs asynchronously — state is saved immediately, and the actual vector indexing happens in the background. The user interface is never blocked.

## Architecture

```
OpenCode chat.message hook
        ↓
  Query DB for new messages
        ↓
  Categorize by wing
        ↓
  Export turn → tmp file
        ↓
  Save sync state (immediate)
        ↓
  mempalace mine (async) → MemPalace vector DB + KG SQLite
        ↓
  session.idle hook (fallback for last turn on shutdown)
```

## Setup

### 1. Install MemPalace

```bash
python3 -m pip install mempalace
# or
uv tool install mempalace
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

```bash
# Add to opencode.json:
{
  "plugins": ["opencode-mempalace-persistence"]
}
```

### 4. Add memory instructions for the model

Create `~/.config/opencode/AGENTS.md`:

```markdown
# Memory & Knowledge instructions

Before answering, search MemPalace using the MCP tools.

1. Call `mempalace_search` with the user's question as query.
2. Call `mempalace_kg_query` for entity "user" and filter by keywords.
3. Use relevant context in your response.
```

### 5. (Optional) Add your identity

Create `~/.mempalace/identity.txt` with a brief description of who you are. It will be loaded automatically at session start.

## What gets saved

Every conversation turn produces:

- A **drawer** in MemPalace with the full user question + AI response text
- Assignment to a **wing** based on content analysis (developer, creative, emotions, family, consciousness)
- Structured **Knowledge Graph** facts:
  - `decision`: choices made ("decided to use TypeScript")
  - `milestone`: completed tasks ("backend deploy finished")
  - `problem`: issues encountered ("chromadb ModuleNotFoundError")
  - `preference`: likes and dislikes ("prefer Svelte over React")
  - `emotional`: feelings ("frustrated with Docker compose")

## Benefits over cron-based sync

- **Real-time**: sync happens immediately after each response, not every N minutes
- **Delta-only**: only new messages are processed — no duplicates, no re-indexing
- **Async mining**: the UI is never blocked while MemPalace indexes content
- **Graceful shutdown**: `session.idle` hook catches the last turn before the user closes OpenCode
- **Zero configuration**: install the plugin, it works

## Links

- GitHub: https://github.com/geco/opencode-mempalace-persistence
- npm: `opencode-mempalace-persistence`
- awesome-opencode: https://github.com/awesome-opencode/awesome-opencode/pull/357

## License

[MIT](https://github.com/geco/opencode-mempalace-persistence/blob/main/LICENSE)
