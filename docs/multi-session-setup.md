# Multi-Session / Multi-Project Setup Guide

This guide shows how to configure MemPalace for teams or power users who run multiple AI assistants simultaneously.

## Use Case

You run multiple AI sessions:
- Claude Code for daily coding
- Codex for architectural work
- Gemini for research
- Qwen for specialized tasks

You want **all sessions to share the same memory palace** so knowledge accumulates across tools.

## Architecture

```
~/.mempalace/palace/  (single shared palace)
         │
         ├── Claude session (reads/writes)
         ├── Codex session (reads/writes)  
         ├── Gemini session (reads/writes)
         └── Qwen session (reads/writes)
```

## Setup Steps

### 1. Initialize Your Palace

```bash
mempalace init ~/projects/your-main-project
```

This creates `~/.mempalace/palace/` with your initial wing/room structure.

### 2. Configure Each AI Session

#### Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/.mempalace/src/hooks/mempal_save_hook.sh",
            "timeout": 30
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/.mempalace/src/hooks/mempal_precompact_hook.sh",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

#### Codex (tmux-based)

Create `~/projects/codex-project/.mcp.json`:

```json
{
  "mcpServers": {
    "mempalace": {
      "type": "stdio",
      "command": "python3",
      "args": ["-m", "mempalace.mcp_server"],
      "env": {
        "PYTHONPATH": "/path/to/.mempalace/src"
      }
    }
  }
}
```

#### Gemini CLI

Add to `~/.gemini/settings.json`:

```json
{
  "hooks": {
    "AfterAgent": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/.mempalace/src/hooks/mempal_save_hook_throttled.sh",
            "timeout": 30000
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/.mempalace/src/hooks/mempal_precompact_hook.sh",
            "timeout": 30000
          }
        ]
      }
    ]
  },
  "mcpServers": {
    "mempalace": {
      "type": "stdio",
      "command": "python3",
      "args": ["-m", "mempalace.mcp_server"],
      "env": {
        "PYTHONPATH": "/path/to/.mempalace/src"
      }
    }
  }
}
```

### 3. Verify Shared Memory

From any session, search:

```bash
mempalace search "what did we discuss about auth"
```

Results include conversations from **all** AI sessions.

## Benefits

### Cross-Session Learning

- Claude decides on database schema → Codex remembers it hours later
- Gemini researches a library → Qwen can reference the findings
- No context loss when switching tools

### Unified Knowledge Base

Single source of truth for:
- Project decisions
- Team preferences  
- Debugging history
- Architecture patterns

### Scalable Setup

Add more AI assistants without reconfiguring:
- Just point to the same `~/.mempalace/palace/`
- Hooks ensure automatic saves
- MCP server provides unified API

## Troubleshooting

### Hook Not Firing

Check hook permissions:

```bash
chmod +x /path/to/.mempalace/src/hooks/mempal_save_hook.sh
```

### Multiple Palaces Created

Ensure all sessions use the **same** `palace_path`:

```bash
grep -r "palace_path" ~/.mempalace/config.json
```

Should point to one location (e.g., `~/.mempalace/palace/`).

## Advanced: Per-Project Wings

For large ecosystems, organize by project:

```bash
# Mine each project into its own wing
mempalace mine ~/projects/project-alpha/ --wing project-alpha
mempalace mine ~/projects/project-beta/ --wing project-beta

# Search within a specific project
mempalace search "auth migration" --wing project-alpha
```

## Comparison

| Setup | Single Session | Multi-Session (This Guide) |
|-------|---------------|---------------------------|
| Palace count | 1 per session | **1 shared palace** |
| Knowledge sharing | None | **Full cross-session** |
| Hook config | Per-session | **Universal hooks** |
| Best for | Personal use | **Teams, power users** |

---

## See Also

- [Official MemPalace Docs](https://github.com/MemPalace/mempalace)
- [AAAK Dialect Guide](https://github.com/MemPalace/mempalace/blob/main/docs/AAAK.md)
- [Knowledge Graph](https://github.com/MemPalace/mempalace/blob/main/docs/knowledge_graph.md)
