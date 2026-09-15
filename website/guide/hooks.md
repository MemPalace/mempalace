# Auto-Save Hooks

Two hooks for Claude Code and Codex that automatically save memories during work. No manual "save" commands needed.

## What They Do

| Hook | When It Fires | What Happens |
|------|--------------|-------------|
| **Save Hook** | Every 15 human messages | Files a diary checkpoint and mines the session transcript into the palace |
| **PreCompact Hook** | Right before context compaction | Mines the transcript before compaction discards it |

Both run silently by default (`hooks.silent_save`, since v3.3.0): the hooks file
the memories themselves and spend no tokens in the chat window. Setting
`silent_save` to `false` restores the legacy behaviour, where the hook blocks
instead and asks the AI to file via MCP tools.

## Install — Claude Code

Add to `.claude/settings.local.json`:

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

Make them executable:
```bash
chmod +x hooks/mempal_save_hook.sh hooks/mempal_precompact_hook.sh
```

## Install — Codex CLI

Add to `.codex/hooks.json`:

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

Edit `mempal_save_hook.sh` to change:

- **`SAVE_INTERVAL=15`** — How many messages between saves. Lower = more frequent, higher = less interruption.
- **`STATE_DIR`** — Where hook state is stored (defaults to `~/.mempalace/hook_state/`)
- **`MEMPAL_DIR`** — Optional. Set to a conversations directory to auto-run `mempalace mine` on each save trigger.

Two settings in `~/.mempalace/config.json` control what gets captured:

```json
{
  "hooks": {
    "auto_save": true,
    "mine_transcript": false
  }
}
```

- **`auto_save`** (default `true`) — the master switch. `false` makes all hooks
  pass straight through; nothing is captured.
- **`mine_transcript`** (default `true`) — whether to file the whole session
  transcript verbatim, tool output included. Turn it off to keep continuity
  checkpoints without archiving every command's output; MemPalace never
  summarizes or redacts, so whatever a command printed is stored as printed.

Environment equivalents (`MEMPALACE_HOOKS_AUTO_SAVE`,
`MEMPALACE_HOOKS_MINE_TRANSCRIPT`) win over the config file. Full reference:
[`hooks/README.md`](https://github.com/MemPalace/mempalace/blob/develop/hooks/README.md).

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
