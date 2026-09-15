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
      allBins:
        - mempalace
    install:
      - id: mempalace-plugin
        kind: npm
        label: "Install opencode-mempalace-persistence plugin (community)"
        package: opencode-mempalace-persistence
---

# MemPalace — OpenCode Integration

> **Community-maintained plugin.** This integration uses `opencode-mempalace-persistence`, a community plugin not officially maintained by the MemPalace team. Source: [github.com/geco/opencode-mempalace-persistence](https://github.com/geco/opencode-mempalace-persistence).

MemPalace provides persistent memory for OpenCode. Every conversation is automatically saved to a local vector database — no cron, no cloud, no manual effort. The plugin can inject relevant memories directly into every prompt, and the model can record Knowledge Graph facts during conversation via MCP tools.

## How it works

1. **Memory injection**: On every user message, the plugin hooks into `experimental.chat.messages.transform` (OpenCode 1.14+) and injects the user's identity + relevant memories from MemPalace directly into the prompt
2. **Persistence**: After each response, the plugin captures the conversation turn and exports it
3. **Mining**: `mempalace mine --mode convos` runs asynchronously — UI is never blocked
4. **KG (mandatory)**: The model records/updates structured facts via `mempalace_kg_add` / `kg_query` / `kg_invalidate` as instructed by AGENTS.md

Both memory injection and persistence are handled by the plugin — no model discipline required.

## Architecture

```
User message
      ↓
experimental.chat.messages.transform hook
      ↓
Plugin injects identity + MemPalace search results
      ↓
Model sees context → responds
      ↓
chat.message + session.idle hooks
      ↓
Export conversation → flat /tmp/oc-sessions/
      ↓
mempalace mine (async) — single serialized call, --mode convos
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

> **OpenCode version note:** The `experimental.chat.messages.transform` hook used by this plugin is available in OpenCode 1.14+. It is a stable experimental API — the hook signature has not changed since introduction. If a breaking change occurs in a future OpenCode version, this doc will be updated.

### 4. Enable memory injection (recommended)

Create `~/.mempalace/plugin-config.json` — this tells the plugin to automatically inject your identity and relevant memories into every prompt:

```json
{
  "autoInjectContext": true
}
```

**Do NOT put this in `opencode.json`** — OpenCode's schema validation rejects unknown keys. The plugin reads its config from `~/.mempalace/plugin-config.json` instead.

When enabled, on every user message:
- **First message**: Injects your identity from `~/.mempalace/identity.txt`
- **Every message**: Runs `mempalace search` and injects relevant results

> **Performance note:** With auto-inject enabled, the plugin runs `mempalace search` before every message, and AGENTS.md adds `mempalace_kg_query` on top — two MCP calls per response. On slow hardware or large palaces this adds latency. The combined cost is typically under 500ms on a modern machine with a palace under 100MB.

### 5. Add memory instructions for the model

Create `~/.config/opencode/AGENTS.md` — since the plugin handles memory search, the model only needs to manage the Knowledge Graph:

```markdown
# Memory & Knowledge instructions

## CRITICAL: You MUST follow these steps BEFORE every response.

### Step 1 — Query Knowledge Graph
Call `mempalace_mempalace_kg_query` for entity "user". Then filter the returned facts — keep only those whose text contains keywords from the user's question, so irrelevant facts are excluded.

### Step 2 — Record Knowledge Graph facts

After responding, if you discovered any new facts during the conversation (decisions made, milestones reached, problems encountered, preferences expressed, emotional states), call `mempalace_mempalace_kg_add` to record them. Object must be 128 characters or fewer.

**This is mandatory** — record facts you are confident about. Prefer quality over quantity; noisy KG entries degrade retrieval over time.
```

### 6. Add your identity

Create `~/.mempalace/identity.txt` with a brief description of who you are:

```
I am [name], a [role]. I work with [technologies]. My main projects are [projects].
```

This is loaded automatically by the plugin — no need to add it to `instructions` in opencode.json.

## Alternative: Model-driven memory search

If you prefer the model to search MemPalace on its own (requires good model tool-use discipline), omit `autoInjectContext` or set it to `false` in `plugin-config.json`, and use the full AGENTS.md that instructs the model to call `mempalace_mempalace_search` before every response.

## Comparison

| Feature | Auto-inject (recommended) | Model-driven |
|---------|:-:|:-:|
| Memory search | Plugin injects automatically | Model calls `mempalace_search` |
| Identity | Plugin injects automatically | Via `instructions: ["identity.txt"]` |
| AGENTS.md needed | Minimal (KG only) | Full (search + KG) |
| Depends on model discipline | No | Yes |

## What gets saved

Every conversation turn is saved as a **drawer** in MemPalace. No forced categorization — MemPalace's own mining handles organization. The model records structured facts (decisions, milestones, preferences) during conversation via MCP tools.

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
