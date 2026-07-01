# Auto-Save Hooks

Hooks for Claude Code and Codex automatically capture memory during work. No manual "save" command is needed for the transcript path.

## What They Do

| Hook | When It Fires | What Happens |
|------|--------------|-------------|
| **Stop** | Every 15 human messages | Mines the active transcript into the palace, then asks the agent to save any extra high-value context through MCP tools |
| **SessionEnd** | Clean Claude Code exit | Backgrounds one final transcript mine so short sessions are not lost |
| **PreCompact** | Right before context compaction | Mines the transcript before compaction and asks the agent to preserve critical context |

The hooks use a two-layer capture model: MemPalace mines the raw transcript locally, while the agent can add targeted drawers for decisions, quotes, or other context it still has in-window. `MEMPAL_DIR` is optional and additive; it mines project files in addition to the conversation transcript.

## Install — Claude Code

The Claude plugin installs these hooks automatically. For manual wiring, add this to `.claude/settings.local.json`:

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/hooks/mempal_save_hook.sh",
        "timeout": 30
      }]
    }],
    "SessionEnd": [{
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/hooks/mempal_session_end_hook.sh",
        "timeout": 10
      }]
    }],
    "PreCompact": [{
      "hooks": [{
        "type": "command",
        "command": "/absolute/path/to/hooks/mempal_precompact_hook.sh",
        "timeout": 30
      }]
    }]
  }
}
```

## Install — Codex CLI

The Codex plugin ships hook wrappers. For manual wiring, add this to `.codex/hooks.json`:

```json
{
  "Stop": [{
    "type": "command",
    "command": "/absolute/path/to/hooks/mempal_save_hook.sh",
    "timeout": 30
  }],
  "PreCompact": [{
    "type": "command",
    "command": "/absolute/path/to/hooks/mempal_precompact_hook.sh",
    "timeout": 30
  }]
}
```

## Configuration

Use the CLI for persistent settings:

```bash
mempalace config set palace-path ~/.mempalace/palaces/shared-agent-brain
mempalace config set hooks.auto-save true
mempalace config set hooks.silent-save true
mempalace config show
```

Environment overrides:

- **`MEMPAL_DIR`** — Optional project directory to mine alongside the active transcript.
- **`MEMPAL_PYTHON`** — Python interpreter for standalone shell hook scripts.
- **`MEMPALACE_PYTHON`** — Python interpreter used by `mempalace hook run` for background mining.
- **`MEMPALACE_HOOKS_AUTO_SAVE=false`** — Disable hook auto-save without uninstalling hooks.

## Backfill

Hooks capture new sessions going forward. To import past sessions once:

```bash
mempalace mine ~/.claude/projects/ --mode convos
mempalace mine ~/.codex/sessions/ --mode convos
```

## Cost

Core mining is local. No telemetry or external service is used for hook ingestion.
