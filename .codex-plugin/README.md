# MemPalace - Codex CLI Plugin

Give your AI a persistent memory -- mine projects and conversations into a searchable palace backed by ChromaDB, with 44 MCP tools and guided skills.

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
| `$mempalace:mempalace` | Install, configure, and operate MemPalace, including a private local palace, a shared-brain hub, or a client joining an existing hub |
| `$mempalace:mempalace-recall` | Recall protocol for MemPalace — search the palace before answering about past work, people, projects, or prior decisions |
| `$mempalace:mempalace-task` | Create, hand off, claim, execute, and close agent tasks through the MemPalace logstream |

### Skill Commands

The main `$mempalace:mempalace` skill can be invoked with five different subcommands. `$mempalace <command>` can be used as a short form invocation. 

| Command | Description |
|---------| ------------|
| `$mempalace help` | Show available commands and usage tips |
| `$mempalace init` | Initialize a new memory palace |
| `$mempalace search` | Semantic search across all mined memories |
| `$mempalace mine` | Mine a project or conversation into your palace |
| `$mempalace status` | Show palace status, room counts, and health |

## Capturing conversations

Codex's plugin manifest supports skills and MCP servers, but not lifecycle
hooks — so this plugin cannot auto-save turns as they happen. Capture works
by mining the session transcripts Codex already writes to disk:

```bash
mempalace mine ~/.codex
```

or `/mine` from inside Codex. Mining is incremental — re-running it picks up
new sessions without duplicating what is already filed. Both the legacy
(`user_message`/`agent_message`) and current (`item_completed`, Codex
>= 0.149) transcript formats are supported.

A `hooks.json` and hook scripts ship in this directory for the day Codex's
plugin schema gains lifecycle hooks; today no supported manifest field can
reference them, and they are not active after installation.

## Support

- Repository: https://github.com/MemPalace/mempalace
- Issues: https://github.com/MemPalace/mempalace/issues
