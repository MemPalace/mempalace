# MemPalace Claude Code Plugin

A Claude Code plugin that gives your AI a persistent memory system. Mine projects and conversations into a searchable palace backed by ChromaDB, with 19 MCP tools, auto-save hooks, and 5 guided skills.

## Prerequisites

- Python 3.9+

## Installation

### Claude Code Marketplace

```bash
claude plugin marketplace add MemPalace/mempalace
claude plugin install --scope user mempalace
```

### Local Clone

```bash
claude plugin add /path/to/mempalace
```

## Post-Install Setup

After installing the plugin, run the init command to complete setup (installs the `mempalace` package via `uv tool` or `pip`, configures MCP, etc.):

```
/mempalace:init
```

## Available Slash Commands

| Command | Description |
|---------|-------------|
| `/mempalace:help` | Show available tools, skills, and architecture |
| `/mempalace:init` | Set up MemPalace -- install, configure MCP, onboard |
| `/mempalace:search` | Search your memories across the palace |
| `/mempalace:mine` | Mine projects and conversations into the palace |
| `/mempalace:status` | Show palace overview -- wings, rooms, drawer counts |

## Hooks

MemPalace registers two hooks that run automatically:

- **Stop** -- Saves conversation context every 15 messages.
- **PreCompact** -- Preserves important memories before context compaction.

Set the `MEMPAL_DIR` environment variable to a directory path to automatically run `mempalace mine` on that directory during each save trigger.

## MCP Server

The plugin automatically configures a local MCP server with 19 tools for storing, searching, and managing memories. No manual MCP setup is required -- `/mempalace:init` handles everything.

## Full Documentation

See the main [README](../README.md) for complete documentation, architecture details, and advanced usage.
