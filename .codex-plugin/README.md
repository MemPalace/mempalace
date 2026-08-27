# MemPalace - Codex CLI Plugin

Give your AI a persistent memory -- mine projects and conversations into a searchable palace backed by ChromaDB, with 44 MCP tools, auto-save hooks, and guided skills.

## Prerequisites

- Python 3.9+
- Codex CLI installed and configured
- `uv tool install mempalace` (recommended) or `pip install mempalace`

## Installation

1. Add the repo to the Codex marketplaces:

```bash
codex plugin marketplace add MemPalace/mempalace
```

2. Install the plugin:

```bash
codex plugin add mempalace@mempalace
```

3. Initialize your palace in the Codex TUI:

```bash
codex
> $mempalace:mempalace init
```

## Available Skills

| Skill | Description |
|-------|-------------|
| `$mempalace:help` | Show available commands and usage tips |
| `$mempalace:init` | Initialize a new memory palace |
| `$mempalace:search` | Semantic search across all mined memories |
| `$mempalace:mine` | Mine a project or conversation into your palace |
| `$mempalace:status` | Show palace status, room counts, and health |

## Hooks

The plugin includes auto-save hooks that run on session stop (every 15 messages) and before context compaction, automatically preserving conversation context into your palace.

Set the `MEMPAL_DIR` environment variable to a directory path to automatically run `mempalace mine` on that directory during each save trigger.

## Support

- Repository: https://github.com/MemPalace/mempalace
- Issues: https://github.com/MemPalace/mempalace/issues
