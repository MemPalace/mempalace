# Auto-Save Hooks

Two hooks for Claude Code and Codex that automatically save memories during work. No manual "save" commands needed.

## What They Do

| Hook | When It Fires | What Happens |
|------|--------------|-------------|
| **Save Hook** | Every 15 human messages | Blocks the AI, tells it to save key topics/decisions/quotes to the palace |
| **PreCompact Hook** | Right before context compaction | Emergency save — forces the AI to save everything before losing context |

The AI does the actual filing — it knows the conversation context, so it classifies memories into the right wings/halls/closets. The hooks just tell it **when** to save.

## Install — Claude Code

### Via Plugin (Recommended)

If you installed MemPalace via the Claude Code plugin marketplace, **hooks are registered automatically** — no configuration needed. Verify they're active with the `/hooks` command inside Claude Code.

> **Do not** also add hooks manually to `settings.json` when the plugin is installed — this causes both the Stop and PreCompact hooks to fire twice per event.

### Without Plugin (Manual)

If you installed MemPalace with `pip install` or `uv tool install` but are **not** using the Claude Code plugin, add the following to `~/.claude/settings.json` (user-wide) or `.claude/settings.local.json` (project-scoped):

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "mempalace hook run --hook stop --harness claude-code",
        "timeout": 30
      }]
    }],
    "PreCompact": [{
      "hooks": [{
        "type": "command",
        "command": "mempalace hook run --hook precompact --harness claude-code",
        "timeout": 30
      }]
    }]
  }
}
```

## Install — Codex CLI

Add to `.codex/hooks.json`:

```json
{
  "Stop": [{
    "type": "command",
    "command": "mempalace hook run --hook stop --harness codex",
    "timeout": 30
  }],
  "PreCompact": [{
    "type": "command",
    "command": "mempalace hook run --hook precompact --harness codex",
    "timeout": 30
  }]
}
```

## Configuration

- **`SAVE_INTERVAL`** — How many messages between saves (default: `15`). Lower = more frequent, higher = less interruption.
- **`STATE_DIR`** — Where hook state is stored (defaults to `~/.mempalace/hook_state/`)
- **`MEMPAL_DIR`** — Optional. Set to a conversations directory to auto-run `mempalace mine` on each save trigger.

## How It Works

### Save Hook (Stop event)

```
User sends message → AI responds → Stop hook fires
                                          ↓
                                  Count human messages in transcript
                                          ↓
                            ┌── < 15 since last save → let AI stop
                            │
                            └── ≥ 15 since last save → block + save
                                                            ↓
                                                    AI saves to palace
                                                            ↓
                                                    AI stops (flag set)
```

The `stop_hook_active` flag prevents infinite loops.

### PreCompact Hook

```
Context window full → PreCompact fires → ALWAYS blocks → AI saves → Compaction proceeds
```

No counting needed — compaction always warrants a save.

## Debugging

```bash
cat ~/.mempalace/hook_state/hook.log
```

Example output:
```
[14:30:15] Session abc123: 12 exchanges, 12 since last save
[14:35:22] Session abc123: 15 exchanges, 15 since last save
[14:35:22] TRIGGERING SAVE at exchange 15
[14:40:01] Session abc123: 18 exchanges, 3 since last save
```

## Cost

**Zero extra tokens.** The hooks are bash scripts that run locally. They don't call any API. The only "cost" is a few seconds of the AI organizing memories at each checkpoint.
