# Auto-Save Hooks

Hooks for Claude Code, Codex, and GitHub Copilot CLI that automatically save memories during work. No manual "save" commands needed.

## What They Do

| Hook | When It Fires | What Happens |
|------|--------------|-------------|
| **Save Hook** | Every 15 human messages | Blocks the AI, tells it to save key topics/decisions/quotes to the palace |
| **PreCompact Hook** | Right before context compaction | Emergency save — forces the AI to save everything before losing context |

The AI does the actual filing — it knows the conversation context, so it classifies memories into the right wings/halls/closets. The hooks just tell it **when** to save.

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

## Install — GitHub Copilot CLI

Add a repo-scoped hook file such as `.github/hooks/mempalace.json`:

```json
{
  "version": 1,
  "hooks": {
    "agentStop": [
      {
        "type": "command",
        "bash": "mempalace hook run --hook stop --harness copilot-cli",
        "powershell": "$payload = [Console]::In.ReadToEnd(); $payload | & 'mempalace.exe' hook run --hook stop --harness copilot-cli",
        "timeoutSec": 30
      }
    ],
    "preCompact": [
      {
        "type": "command",
        "bash": "mempalace hook run --hook precompact --harness copilot-cli",
        "powershell": "$payload = [Console]::In.ReadToEnd(); $payload | & 'mempalace.exe' hook run --hook precompact --harness copilot-cli",
        "timeoutSec": 30
      }
    ]
  }
}
```

On Windows, the `powershell` command must explicitly forward stdin. Copilot CLI writes the hook JSON to the PowerShell process's stdin, but PowerShell's native-command invocation does not reliably pass that original process stdin through to a child executable. Without the bridge, `mempalace.exe` may receive empty stdin and the hook will log `Session unknown: 0 exchanges`.

The `ReadToEnd()` bridge is intentionally explicit:

```powershell
$payload = [Console]::In.ReadToEnd(); $payload | & 'mempalace.exe' hook run --hook stop --harness copilot-cli
```

It reads the complete hook JSON payload from PowerShell's process stdin, then pipes that payload into `mempalace.exe`, where MemPalace can parse `sessionId`, `transcriptPath`, and `cwd`.

Alternatives exist, but are less predictable for this use case:

- `$Input | & 'mempalace.exe' ...` uses PowerShell's pipeline input enumerator, which can line-buffer/objectize input and is less clear than reading the raw JSON payload.
- `cmd /c mempalace.exe ...` may behave more like a transparent stdin pass-through, but adds another shell layer and `cmd.exe` quoting/escaping rules.
- A separate `.ps1` wrapper can hide the long command, but it still needs the same stdin-forwarding step internally.

For Bash, no bridge is needed because Bash normally leaves its stdin connected to the child command:

```bash
mempalace hook run --hook stop --harness copilot-cli
```

If `mempalace.exe` is not on `PATH`, use its absolute path:

```json
{
  "version": 1,
  "hooks": {
    "agentStop": [
      {
        "type": "command",
        "bash": "mempalace hook run --hook stop --harness copilot-cli",
        "powershell": "$payload = [Console]::In.ReadToEnd(); $payload | & 'C:\\Users\\you\\.local\\bin\\mempalace.exe' hook run --hook stop --harness copilot-cli",
        "timeoutSec": 30
      }
    ],
    "preCompact": [
      {
        "type": "command",
        "bash": "mempalace hook run --hook precompact --harness copilot-cli",
        "powershell": "$payload = [Console]::In.ReadToEnd(); $payload | & 'C:\\Users\\you\\.local\\bin\\mempalace.exe' hook run --hook precompact --harness copilot-cli",
        "timeoutSec": 30
      }
    ]
  }
}
```

Copilot CLI stores local session event streams under:

```powershell
$HOME\.copilot\session-state
```

To mine them manually:

```powershell
mempalace mine $HOME\.copilot\session-state --mode convos --wing sessions
```

Or from a session-state directory:

```powershell
cd $HOME\.copilot\session-state
mempalace mine . --mode convos --wing sessions
```

## Configuration

Edit `mempal_save_hook.sh` to change:

- **`SAVE_INTERVAL=15`** — How many messages between saves. Lower = more frequent, higher = less interruption.
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

The `stop_hook_active` flag prevents infinite loops in Claude Code. Copilot CLI does not expose that flag, so MemPalace uses silent direct saves for Copilot stop hooks instead of returning a blocking decision.

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
