# MemPalace - Codex CLI Plugin

Give your AI a persistent memory -- mine projects and conversations into a searchable palace backed by ChromaDB, with 44 MCP tools and guided skills.

## Prerequisites

- Python 3.9+
- Codex CLI installed and configured
- `uv tool install mempalace` (recommended) or `pip install mempalace`

## Installation

### Local Install

1. Copy or symlink the `.codex-plugin` directory into your project root:

```bash
cp -r .codex-plugin /path/to/your/project/.codex-plugin
```

2. Verify the plugin is detected:

```bash
codex --plugins
```

3. Initialize your palace:

```bash
codex /init
```

### Git Install

1. Clone the MemPalace repository:

```bash
git clone https://github.com/MemPalace/mempalace.git
cd mempalace
```

2. Install the Python package so the `mempalace-mcp` script lands on
   your PATH (the bundled `plugin.json` invokes it by bare name):

```bash
uv tool install --editable .   # or: pip install -e .
```

   Plain `uv sync` is **not** enough here — it installs the scripts into
   `.venv/bin/`, which Codex will not find unless you activate the venv
   before launching Codex.

3. The `.codex-plugin` directory is already in the repo root. Codex CLI will detect it automatically when you run Codex from inside the repository.

4. Initialize your palace:

```bash
codex /init
```

## Available Skills

| Skill | Description |
|-------|-------------|
| `/help` | Show available commands and usage tips |
| `/init` | Initialize a new memory palace |
| `/search` | Semantic search across all mined memories |
| `/mine` | Mine a project or conversation into your palace |
| `/status` | Show palace status, room counts, and health |

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
