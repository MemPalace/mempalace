# MemPalace - Codex CLI Plugin

Give your AI a persistent memory -- mine projects and conversations into a searchable palace backed by ChromaDB, with 35 MCP tools, auto-save hooks, and guided skills.

## Prerequisites

- Python 3.9+
- Codex CLI installed and configured
- `uv tool install mempalace` (recommended) or `pip install mempalace`

## Installation

### Local Repository Install

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

3. Add the local marketplace and install the plugin:

```bash
codex plugin marketplace add .
codex plugin add mempalace@mempalace
```

4. Verify the plugin and MCP server:

```bash
codex plugin list
codex mcp list
```

`codex mcp list` should show a local `mempalace` server whose command is `mempalace-mcp`.
Start a fresh Codex thread after installing so Codex loads the new skills and MCP tools.

## Available Skills

| Skill | Description |
|-------|-------------|
| `/help` | Show available commands and usage tips |
| `/init` | Initialize a new memory palace |
| `/search` | Semantic search across all mined memories |
| `/mine` | Mine a project or conversation into your palace |
| `/status` | Show palace status, room counts, and health |
| `mempalace-recall` | Search-before-answer protocol for natural questions like "do you remember", "what did we decide", and "where did we leave off" |

For normal work, users should not need to invoke `mempalace-recall`
manually. Codex should select it when a question is about past work,
prior decisions, people, projects, earlier sessions, or anything that
may already be filed in the palace.

## Hooks

The plugin includes auto-save hooks that run on session start, session stop, and before context compaction. They call `mempalace hook run` locally, mine the active transcript into the palace, and optionally mine `MEMPAL_DIR` as project context.

Set the `MEMPAL_DIR` environment variable to a directory path to automatically run `mempalace mine` on that directory during each save trigger.

## Support

- Repository: https://github.com/MemPalace/mempalace
- Issues: https://github.com/MemPalace/mempalace/issues
