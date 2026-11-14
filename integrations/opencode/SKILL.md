---
name: mempalace
description: "MemPalace — Local AI memory for OpenCode. Real-time conversation persistence via community plugin. Zero cron, zero cloud."
version: 2.2.0
homepage: https://github.com/MemPalace/mempalace
user-invocable: true
metadata:
  opencode:
    emoji: "\U0001F3DB"
    os:
      - darwin
      - linux
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

1. **Memory injection**: On every user message, the plugin hooks into `experimental.chat.messages.transform` and injects the user's identity + relevant memories from MemPalace directly into the prompt
2. **AI checkpoints**: Every ~15 messages (configurable `saveInterval`) the model files topics, decisions and quotes via MemPalace MCP tools (diary + knowledge graph); a pre-compaction emergency save files everything before context loss
3. **Persistence**: Completed turns are exported per project wing and mined asynchronously — UI is never blocked. Mines run on `session.idle` / exit / startup, never mid-reply snapshots
4. **KG**: The model records structured facts via `mempalace_mempalace_kg_add` / `mempalace_mempalace_kg_supersede` / `mempalace_mempalace_kg_invalidate` when something new emerges

Both memory injection and persistence are handled by the plugin — no model discipline required.

## Relationship to the built-in OpenCode source adapter

MemPalace is adding a first-party OpenCode source adapter (`mempalace.sources.opencode`, see #1484 — once it merges) for historical ingest of existing sessions. This plugin is the complementary real-time layer: it captures turns as they happen (`chat.message` + `session.idle` + exit hooks), injects live recall into prompts, and files AI checkpoints and pre-compaction emergency saves via MCP tools. Use the source adapter to backfill history, this plugin to never lose the present.

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
session.idle / exit / startup triggers
      ↓
Export completed turns → private ~/.mempalace/oc-sessions/ (0700)
      ↓
mempalace mine --mode convos (default exchange: verbatim pairs)
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
  "plugin": ["opencode-mempalace-persistence@2.2.0"]
}
```

> **OpenCode version note:** The `experimental.chat.messages.transform` hook used by this plugin is available in OpenCode 1.14+. It is a stable experimental API — the hook signature has not changed since introduction. If a breaking change occurs in a future OpenCode version, this doc will be updated.

### 4. Enable memory injection (recommended)

Create `~/.mempalace/plugin-config.json` — this tells the plugin to automatically inject your identity and relevant memories into every prompt:

```json
{
  "autoInjectContext": true,
  "saveInterval": 15
}
```

**Do NOT put this in `opencode.json`** — OpenCode's schema validation rejects unknown keys. The plugin reads its config from `~/.mempalace/plugin-config.json` instead.

When enabled, on every user message:
- **First message**: Injects your identity from `~/.mempalace/identity.txt`
- **Every message**: Runs `mempalace search` and injects relevant results

> **Performance note:** With auto-inject enabled, the plugin runs `mempalace search` before every message (extra recall only when the model itself searches via MCP tools). Expect latency on slow hardware or large palaces — notably, a cold `mempalace search` (first embedding load) can take ~20s; warm queries are fast.

### 5. Add memory instructions for the model

Create `~/.config/opencode/AGENTS.md` — since the plugin handles memory search, the model only needs to manage the Knowledge Graph:

```markdown
# Memory & Knowledge instructions

## Recall (usually already covered)

The plugin auto-injects identity + relevant memories into every prompt.
Only search MemPalace yourself (`mempalace_mempalace_search`) when the question is about past work, decisions, people, or projects AND the injected context has nothing — quote results verbatim, never paraphrase. Full protocol: `integrations/shared/recall-protocol.md`.

## Record facts (after responding, only when something new emerged)

- Durable outcomes: `mempalace_mempalace_add_drawer`.
- New KG facts: `mempalace_mempalace_kg_add` (128 chars or fewer).
- Changed single-valued fact: `mempalace_mempalace_kg_supersede`.
- Ended fact: `mempalace_mempalace_kg_invalidate`.

Record facts you are confident about. Prefer quality over quantity. Don't file secrets or tokens.
```

> Do NOT overwrite an existing `~/.config/opencode/AGENTS.md` — append the block above (it may already contain shared-brain rules).

### 6. Add your identity

Create `~/.mempalace/identity.txt` with a brief description of who you are:

```
I am [name], a [role]. I work with [technologies]. My main projects are [projects].
```

This is loaded automatically by the plugin — no need to add it to `instructions` in opencode.json.

## Alternative: Model-driven memory search

If you prefer the model to search MemPalace on its own (requires good model tool-use discipline), omit `autoInjectContext` or set it to `false` in `plugin-config.json`, and follow the bundled `mempalace-recall` skill protocol (question-driven search, same as the official MemPalace skill).

## Comparison

| Feature | Auto-inject (recommended) | Model-driven |
|---------|:-:|:-:|
| Memory search | Plugin injects automatically | Model calls `mempalace_mempalace_search` (skill protocol) |
| Identity | Plugin injects automatically | Via `instructions: ["identity.txt"]` |
| AGENTS.md needed | Record-only (recall auto-injected) | Conditional search + record |
| Depends on model discipline | No | Yes |

## What gets saved

Every completed turn is saved as **drawers** in MemPalace (`--mode convos`, default `exchange` extraction: one drawer per exchange pair, verbatim, no paraphrasing). Exports are grouped one wing per project. Only finished replies are exported — in-flight text is revisited by the next sync. The model records structured facts and session diaries during conversation, at checkpoints, and before compaction via MCP tools.

## Benefits over cron-based sync

- **Incremental**: mines run on idle / exit / startup — a failed compaction costs a summary, never memory
- **Delta-only**: only new messages are processed — no duplicates
- **Async mining**: UI never blocked
- **Crash-safe**: synchronous exit save with bounded budget; startup mine catches leftovers
- **Per-project wings**: sessions grouped by project, never leaking across projects
- **Serialized mining**: single mine call prevents SQLite FTS5 index corruption

## Windows note

Windows is currently untested: the plugin resolves the `mempalace` binary from `PATH` (override with the `MEMPALACE_BIN` env var) instead of assuming Unix paths, but no Windows run has been verified. Reports and fixes welcome.

## Links

- Plugin GitHub: https://github.com/geco/opencode-mempalace-persistence
- npm: `opencode-mempalace-persistence`
- awesome-opencode: https://github.com/awesome-opencode/awesome-opencode/pull/730

## License

[MIT](https://github.com/geco/opencode-mempalace-persistence/blob/main/LICENSE)
