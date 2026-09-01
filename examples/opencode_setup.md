# OpenCode + Oh-My-OpenAgent Integration Guide

This guide explains how to set up MemPalace as a persistent memory layer for [OpenCode](https://opencode.ai) with the [oh-my-openagent](https://github.com/oh-my-opencode/oh-my-openagent) plugin.

## Prerequisites

- Python 3.9+ (3.12 recommended for ChromaDB compatibility)
- [uv](https://docs.astral.sh/uv/) or pip
- [OpenCode](https://opencode.ai) installed and configured
- [oh-my-openagent](https://github.com/oh-my-opencode/oh-my-openagent) plugin active

## 1. Install MemPalace

```bash
# Recommended: install via uv (isolated tool environment)
uv tool install mempalace

# Fallback: pip
pip install mempalace
```

## 2. Initialize Your Project

MemPalace requires per-project initialization before mining. This creates `mempalace.yaml` in the project directory and updates `~/.mempalace/config.json` globally.

```bash
# Run from your project root (or pass the path)
mempalace init /path/to/your/project
```

*Note: `mempalace status` will report "No palace found" until you run `mempalace mine` — this is expected.*

### Identity Configuration (Optional but Recommended)

Define your role and projects by editing files in `~/.mempalace/`:

- **`~/.mempalace/identity.txt`**: Plain text description of your role and focus.
- **`~/.mempalace/wing_config.json`**: JSON mapping projects and name variants to "Wings".

## 3. Connect to OpenCode (MCP)

Add MemPalace to your `~/.config/opencode/opencode.json`:

```bash
# Find your uv tools directory
uv tool dir
# Example output: /home/youruser/.local/share/uv/tools
```

Edit `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["oh-my-openagent@latest"],
  "mcp": {
    "mempalace": {
      "type": "local",
      "command": ["<UV_TOOL_DIR>/mempalace/bin/python", "-m", "mempalace.mcp_server"],
      "environment": { "PYTHONUNBUFFERED": "1" },
      "enabled": true,
      "timeout": 10000
    }
  }
}
```

Replace `$(uv tool dir)` with the actual path from `uv tool dir`. For example:
```
~/.local/share/uv/tools/mempalace/bin/python
```

*Note: oh-my-openagent inherits all MCPs from `opencode.json` automatically — no separate plugin config needed.*

## 4. Mine Your Projects

Run the miner to ingest code and documentation into your palace. `mempalace init` must be run first for each project.

```bash
# Mine a single project
mempalace mine /path/to/your/project

# Mine multiple projects
for dir in ~/projects/*/; do
  mempalace init "$dir"
  mempalace mine "$dir"
done
```

## 5. Memory Protocol (AGENTS.md)

Add a memory section to your project's `AGENTS.md` so agents know when and how to use MemPalace:

```markdown
## Memory (MemPalace)

- Search before starting: `mempalace_search` for related past work, decisions, patterns
- Save after significant work: `mempalace_add_drawer` with wing, room, and verbatim content
- Query entity relationships: `mempalace_kg_query` for linked concepts and decisions
```

## 6. Verify

Start OpenCode and confirm MemPalace is connected:

```bash
# In an OpenCode session, check MCP status
/mcp list
# Expected: mempalace  CONNECTED  19 tools
```

If not connected, verify the python path is correct:
```bash
ls $(uv tool dir)/mempalace/bin/python
```

## 7. Usage

Once connected, OpenCode agents automatically have access to 19 MemPalace tools. Example queries that trigger memory lookups:

- *"What decisions did we make about the auth module?"* → `mempalace_search`
- *"Save what we decided about the API structure"* → `mempalace_add_drawer`
- *"What patterns have we used for error handling?"* → `mempalace_search`

Agents using oh-my-openagent will call these tools proactively based on skill instructions — no explicit prompting required.
