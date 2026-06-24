# MemPalace - Codex CLI Plugin

Give your AI a persistent memory -- mine projects and conversations into a
searchable palace backed by ChromaDB, with 35 MCP tools, auto-save hooks, and
guided skills.

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

1. Verify the plugin is detected:

```bash
codex --plugins
```

1. Initialize your palace:

```bash
codex /init
```

### Git Install

1. Clone the MemPalace repository:

```bash
git clone https://github.com/MemPalace/mempalace.git
cd mempalace
```

1. Install the Python package so the `mempalace-mcp` script lands on
   your PATH (the bundled `plugin.json` invokes it by bare name):

```bash
uv tool install --editable .   # or: pip install -e .
```

   Plain `uv sync` is **not** enough here — it installs the scripts into
   `.venv/bin/`, which Codex will not find unless you activate the venv
   before launching Codex.

1. The `.codex-plugin` directory is already in the repo root. Codex CLI will
   detect it automatically when you run Codex from inside the repository.

2. Initialize your palace:

```bash
codex /init
```

## Available Skills

| Skill | Description |
| --- | --- |
| `/help` | Show available commands and usage tips |
| `/init` | Initialize a new memory palace |
| `/search` | Semantic search across all mined memories |
| `/mine` | Mine a project or conversation into your palace |
| `/status` | Show palace status, room counts, and health |

## Hooks

The plugin includes auto-save hooks that run on session stop (every 15
messages) and before context compaction, automatically preserving conversation
context into your palace.

Set the `MEMPAL_DIR` environment variable to a directory path to automatically
run `mempalace mine` on that directory during each save trigger.

## MCP Transport Diagnostics

If Codex reports `Transport closed` for `mcp__mempalace`, do not treat that as
an empty palace or empty wing. The in-thread MCP handle is unavailable, so call
the shell diagnostic instead:

```bash
mempalace mcp-health --json
```

`mcp_transport: "ok"` means a fresh MemPalace MCP server can answer over
stdio.
`mcp_transport: "transport_unavailable"` means the agent should use the CLI
fallback commands in the JSON payload, report a runtime MCP transport failure,
and retry MCP only after the host restarts or `mempalace_reconnect` is callable.

## Support

- Repository: <https://github.com/MemPalace/mempalace>
- Issues: <https://github.com/MemPalace/mempalace/issues>
