# MemPalace Hooks — Auto-Save for Terminal AI Tools

These hook scripts make MemPalace save automatically. No manual "save" commands needed.

This file covers the **Claude Code** and **Codex CLI** hooks that live
flat under `hooks/`. For the **Cursor IDE** hooks, see
[`hooks/cursor/README.md`](cursor/README.md) or the rendered docs at
[`website/guide/cursor-hooks.md`](../website/guide/cursor-hooks.md). The
two are additive and share the same `~/.mempalace/hook_state/`
directory.

If you are trying to protect existing Claude Code transcripts immediately,
use the short checklist first: [`website/guide/claude-code-retention.md`](../website/guide/claude-code-retention.md).
It covers hook wiring, JSONL backup, and one-time backfill.

The supported Claude Code install is the plugin in
[`.claude-plugin/`](../.claude-plugin/README.md) (`claude plugin install`).
`mempal_save_hook.sh`, `mempal_precompact_hook.sh`, and
`mempal_session_end_hook.sh` are legacy wrappers around
`mempalace hook run`, which owns counting, the per-file PID guard,
write-routing, and silent-save. They share `mempal_hook_run.sh` to pick
the interpreter, so keep the four scripts together.

## What They Do

| Hook | When It Fires | What Happens |
|------|--------------|-------------|
| **Save Hook** | Every 15 human messages | Auto-mines transcript (tool output included), then blocks the AI to save topics/decisions/quotes |
| **SessionEnd Hook** | Clean session exit | Backgrounds a final transcript mine (when a transcript exists) so short sessions aren't lost; returns immediately so teardown is never delayed. A lightweight diary checkpoint is written in the detached child. |
| **PreCompact Hook** | Right before context compaction | Auto-mines transcript, then emergency save — forces the AI to save EVERYTHING before losing context |

**Two-layer capture:** Hooks auto-mine the JSONL transcript directly into the palace (capturing raw tool output — Bash results, search findings, build errors). They also block the AI with a reason message telling it to save verbatim tool output and key context. Belt and suspenders — tool output gets stored even if the AI summarizes instead of quoting.

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

`SessionEnd` runs once on a clean exit and backgrounds its work, so it
returns instantly and stays within Claude Code's SessionEnd budget. Wired
through `settings.local.json` (above) the `timeout` can raise that budget;
the bundled plugin cannot, which is why the hook backgrounds rather than
mining in the foreground.

Make them executable:
```bash
chmod +x hooks/mempal_save_hook.sh hooks/mempal_session_end_hook.sh hooks/mempal_precompact_hook.sh hooks/mempal_hook_run.sh
```

## Install — Antigravity (Google)

The Antigravity integration lives in its own subdirectory because the
wire format (camelCase JSON, `injectSteps[]` output) and event names
(`Stop`, `PreInvocation`) are Antigravity-specific. Use the dedicated
installer:

```bash
bash hooks/antigravity/install.sh
```

This installs to `~/.gemini/config/plugins/mempalace/`, registers the
MCP server, ships the `mempalace` skill, and wires the Stop +
PreInvocation hooks. See [`hooks/antigravity/README.md`](antigravity/README.md)
for the full guide and [`hooks/antigravity/INVESTIGATION.md`](antigravity/INVESTIGATION.md)
for the source-of-truth audit of which Antigravity surfaces the
integration uses.

## Install — Codex CLI (OpenAI)

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

**Other harnesses:** the clean-exit save runs through the harness-agnostic
`mempalace hook run --hook session-end` entry point. This release wires it
for Claude Code. Antigravity exposes no dedicated session-end event (its
lifecycle hooks are PreToolUse/PostToolUse/PreInvocation/PostInvocation/Stop,
and MemPalace already saves there via `Stop`); Cursor and Codex can adopt the
same entry point as a follow-up wherever their own session-end event is available.

## Configuration

The legacy shell scripts are wrappers. Do not edit them to change save
behavior. `mempalace hook run` owns that:

- **Save interval** — `SAVE_INTERVAL` in `mempalace/hooks_cli.py` (15 human messages).
- **`MEMPAL_DIR`** — Optional project directory. When set in the environment, the Python hook mines it with `--mode projects` in addition to the transcript. The wrappers do not set this variable.
- **`MEMPAL_PYTHON`** — Interpreter used by every hook wrapper, legacy and plugin. Resolution (`mempal_hook_run.sh`): `$MEMPAL_PYTHON`, then `mempalace` on `PATH`, then `python3`, then `python`, taking the first that can import mempalace. When none can, the legacy hooks print `{}` and exit 0; the Claude Code and Codex plugin hooks exit 1 so the harness reports the failure.
- **`MEMPAL_VERBOSE`** — No longer read by the legacy Stop wrapper. To have the Stop hook block and ask the AI to save instead of saving silently, set `hooks.silent_save` to `false` in `~/.mempalace/config.json`.

### Disabling Auto-Save (Silent Mode)

To keep hooks installed but disable auto-save blocking entirely, set `hooks.auto_save` to `false` in your config:

**Option 1 — config file** (`~/.mempalace/config.json`):
```json
{
  "hooks": {
    "auto_save": false
  }
}
```

**Option 2 — environment variable:**
```bash
export MEMPALACE_HOOKS_AUTO_SAVE=false
```

When disabled, both the stop hook and precompact hook pass through without blocking. You can still save manually with `mempalace mine <dir> --mode convos`.

### mempalace CLI

The relevant commands are:

```bash
mempalace mine <dir>               # Mine all files in a directory
mempalace mine <dir> --mode convos # Mine conversation transcripts only
```

The legacy wrappers find `mempal_hook_run.sh` next to themselves, so they work wherever the repo lives as long as the scripts stay in one directory.

## How It Works (Technical)

`mempal_save_hook.sh` and `mempal_precompact_hook.sh` forward the harness
JSON on stdin to `mempalace hook run --hook stop` or `--hook precompact`
through `mempal_hook_run.sh`.
The Python command counts messages, mines the transcript file (not its
parent directory) behind a per-file PID guard, and applies write-routing.
Prefer the plugin in `.claude-plugin/`, which calls the same command.

## Debugging

Check the hook log:
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

## Known Limitations

**Hooks require session restart after install.** Claude Code loads hooks from `settings.json` at session start only. If you run `mempalace init` or manually edit hook config mid-session, the hooks won't fire until you restart Claude Code. This is a Claude Code limitation.

**`MEMPAL_PYTHON` override.** The legacy wrappers run `python -m mempalace hook run`. A GUI-launched harness on macOS inherits launchd's minimal `PATH`. Point `MEMPAL_PYTHON` at an interpreter that has mempalace installed:

```bash
export MEMPAL_PYTHON="$HOME/.venvs/mempalace/bin/python"
```

Resolution: `$MEMPAL_PYTHON`, then `mempalace` on `PATH`, then `python3`, then `python`, taking the first that can import mempalace. The Claude Code and Codex plugins resolve the same way: each ships a byte-identical copy of `mempal_hook_run.sh` in its own `hooks/` folder, and a test keeps the copies in step.

## Backfill Past Conversations

The hooks only capture conversations going forward. To mine **past** Claude Code sessions into your palace, run a one-time backfill:

```bash
mempalace mine ~/.claude/projects/ --mode convos
```

This scans all JSONL transcripts from previous sessions and files them into the `conversations` wing. On a typical developer machine with months of history, this can yield 50K–200K drawers.

For Codex CLI sessions:
```bash
mempalace mine ~/.codex/sessions/ --mode convos
```

This only needs to be done once — after that, the hooks auto-mine each session as you go.

## Cost

**Zero extra tokens.** The hooks notify the AI that saves happened in the background — the AI doesn't need to write anything in the chat. All filing is handled automatically. Previous versions asked the AI to write diary entries and drawer content in the chat window, which cost ~$1/session in retransmitted tokens.
