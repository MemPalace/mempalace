# MemPalace Cursor Plugin

A Cursor IDE plugin that gives your agent a persistent memory system. Auto-registers the `mempalace-mcp` server (19 MCP tools), ships 5 slash commands, and provides one model-invocable skill that guides the agent through setup, mining, and search.

> Hooks (auto-save + session-start memory recall) are shipped separately under `hooks/cursor/` so the plugin is safe to install in any Cursor workspace without touching the agent loop. See [Hooks](#hooks-optional) below.

## Prerequisites

- Python 3.9+
- Cursor 1.7+ (plugin manifest schema requires it)

## Installation

### Local clone (recommended while not in the marketplace yet)

Symlink (or copy) this repository into Cursor's local plugins folder:

```bash
ln -s /path/to/mempalace ~/.cursor/plugins/local/mempalace
```

Then in Cursor: <kbd>Cmd</kbd>-<kbd>Shift</kbd>-<kbd>P</kbd> → **Developer: Reload Window**.

### Marketplace

Once published, install via the Cursor marketplace panel and select `mempalace`. Required-plugin distribution from a team marketplace is also supported.

## Post-Install Setup

After installing the plugin, run the `init` command in a Cursor chat:

```
/mempalace-init
```

(Or just say "use the mempalace skill" — Cursor will model-invoke the bundled skill.)

This installs the `mempalace` package via `uv tool` or `pip`, initializes a palace under `~/.mempalace/`, and verifies the MCP server is reachable.

## Available Slash Commands

| Command             | Description                                                                       |
|---------------------|-----------------------------------------------------------------------------------|
| `/mempalace-help`   | Show available tools, skills, CLI commands, hooks, and architecture               |
| `/mempalace-init`   | Set up MemPalace — install, configure, onboard                                    |
| `/mempalace-search` | Search your memories across the palace using semantic search                      |
| `/mempalace-mine`   | Mine projects and conversations into the palace                                   |
| `/mempalace-status` | Show palace overview — wings, rooms, drawer counts                                |

> Cursor commands are global, not plugin-namespaced — that's why each slug is prefixed with `mempalace-` rather than appearing as `/help`, `/init`, etc. This keeps them collision-free with built-in or other-plugin commands.

## MCP Server

This plugin ships `mcp.json` at the plugin root, so Cursor auto-loads the `mempalace-mcp` server on plugin install:

```json
{
  "mempalace": {
    "command": "mempalace-mcp"
  }
}
```

All 19 MemPalace MCP tools (`mempalace_search`, `mempalace_add_drawer`, `mempalace_diary_write`, `mempalace_check_duplicate`, `mempalace_diary_read`, …) become available to the agent immediately. No manual `~/.cursor/mcp.json` edit required.

If the server doesn't appear, confirm `mempalace-mcp` is on the user `$PATH`:

```bash
command -v mempalace-mcp
```

If it isn't, run `/init` (or `mempalace install` from a terminal) — `mempalace-mcp` is installed alongside the `mempalace` package.

## Hooks (optional)

Cursor's hooks system is configured separately from plugins (in `~/.cursor/hooks.json` or `.cursor/hooks.json`), so this plugin does **not** wire hooks itself. The MemPalace repository ships three Cursor-native hooks under [`hooks/cursor/`](../hooks/cursor/) that you install with one command.

User scope — writes `~/.cursor/hooks.json`, applies to every Cursor workspace (recommended):

```bash
hooks/cursor/install.sh --scope user --variant full
```

Project scope — writes `.cursor/hooks.json` under the current project only:

```bash
hooks/cursor/install.sh --scope project --variant full
```

What you get:

| Hook event     | What it does                                                                                          |
|----------------|-------------------------------------------------------------------------------------------------------|
| `sessionStart` | Injects an `additional_context` recap of relevant memories scoped to the workspace wing               |
| `stop`         | Counts agent turns; every N turns, emits a `followup_message` instructing a memory checkpoint         |
| `preCompact`   | Synchronously mines the transcript before compaction, drops a marker so the next `stop` saves a diary |

Full details: [`website/guide/cursor-hooks.md`](../website/guide/cursor-hooks.md) and [`hooks/cursor/README.md`](../hooks/cursor/README.md).

## Uninstall

Remove the local plugin symlink:

```bash
rm ~/.cursor/plugins/local/mempalace
```

Then in Cursor: <kbd>Cmd</kbd>-<kbd>Shift</kbd>-<kbd>P</kbd> → **Developer: Reload Window**.

If you also installed the hooks, remove them (leaves any unrelated hooks in `hooks.json` untouched):

```bash
hooks/cursor/install.sh --scope user --uninstall
```

## Full Documentation

See the main [README](../README.md) for complete documentation, architecture details, and advanced usage.
