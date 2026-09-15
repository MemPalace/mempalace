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
chmod +x hooks/mempal_save_hook.sh hooks/mempal_session_end_hook.sh hooks/mempal_precompact_hook.sh
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

Edit `mempal_save_hook.sh` to change:

- **`SAVE_INTERVAL=15`** — How many human messages between saves. Lower = more frequent saves, higher = less interruption.
- **`STATE_DIR`** — Where hook state is stored (defaults to `~/.mempalace/hook_state/`)
- **`MEMPAL_DIR`** — Optional **project directory** (code, notes, docs) to also mine on each save trigger, with `--mode projects`. The hook ALWAYS mines the active conversation transcript automatically with `--mode convos` — `MEMPAL_DIR` is purely additive, never an override. Leave blank if you don't want to ingest project files.
- **`MEMPALACE_PYTHON`** — Optional env var. Python interpreter with mempalace + chromadb installed. Auto-detects: `MEMPALACE_PYTHON` env var → repo `venv/bin/python3` → system `python3`. Set this if your venv is in a non-standard location.

### The two capture layers have separate switches

The hooks write two very different things, and you can turn them off independently:

| Setting | Turns off | What you lose |
|---------|-----------|---------------|
| `hooks.auto_save` | Everything — all three hooks pass straight through | Both layers below |
| `hooks.mine_transcript` | Only the raw transcript mine | The verbatim archive of the session, tool output included |

The **diary checkpoint** is a single compressed entry built from your recent
prompts (truncated, no assistant text and no tool output). It is what
`mempalace_diary_read` returns at the start of the next session, and it costs one
drawer.

The **transcript mine** files the whole JSONL session verbatim — every Bash
result, every file read, every build error. That is the point of it, and it is
also its cost: a long session can file thousands of drawers, and anything a
command printed goes in exactly as printed. MemPalace stores verbatim by design
and does not redact, so if your tool output contains a token, so does the palace.

Continuity without the archive — checkpoints on, raw mine off:

```json
{
  "hooks": {
    "auto_save": true,
    "mine_transcript": false
  }
}
```

Nothing at all, hooks left installed:

```json
{
  "hooks": {
    "auto_save": false
  }
}
```

Both have environment equivalents, which win over the config file:

```bash
export MEMPALACE_HOOKS_AUTO_SAVE=false
export MEMPALACE_HOOKS_MINE_TRANSCRIPT=false
```

With `auto_save` off, the stop and precompact hooks pass through without
blocking. With only `mine_transcript` off, checkpoints still land — including at
`PreCompact`, where the hook files one instead of mining so compaction is never
silent. Either way you can still archive a session by hand with
`mempalace mine <transcript> --mode convos`.

### Running a hub

If the palace is served by a hub (`mempalace serve` — see
[shared brain](../website/guide/shared-brain.md)), that process holds the writer
lease for its whole lifetime and hooks cannot write to the palace directly. Both
capture paths forward to it over HTTP automatically: the mine through the
`mempalace` CLI, the checkpoint through the hub's `mempalace_diary_write`. Set
`MEMPALACE_HUB_FORWARD=0` to force direct writes (they will be refused while the
hub is up).

### mempalace CLI

The relevant commands are:

```bash
mempalace mine <dir>               # Mine all files in a directory
mempalace mine <dir> --mode convos # Mine conversation transcripts only
```

The hooks resolve the repo root automatically from their own path, so they work regardless of where you install the repo.

## How It Works (Technical)

### Save Hook (Stop event)

```
User sends message → AI responds → Claude Code fires Stop hook
                                            ↓
                                    Hook counts human messages in JSONL transcript
                                            ↓
                              ┌─── < 15 since last save ──→ echo "{}" (let AI stop)
                              │
                              └─── ≥ 15 since last save
                                            ↓
                                    Auto-mine transcript → palace (tool output captured)
                                            ↓
                                    {"decision": "block", "reason": "save tool output verbatim..."}
                                            ↓
                                    AI saves to palace (topics, decisions, quotes)
                                            ↓
                                    AI tries to stop again
                                            ↓
                                    stop_hook_active = true
                                            ↓
                                    Hook sees flag → echo "{}" (let it through)
```

The `stop_hook_active` flag prevents infinite loops: block once → AI saves → tries to stop → flag is true → we let it through.

### PreCompact Hook

```
Context window getting full → Claude Code fires PreCompact
                                        ↓
                                Find transcript (from input or session_id lookup)
                                        ↓
                                Auto-mine transcript → palace (tool output captured)
                                        ↓
                                {"decision": "block", "reason": "save tool output verbatim..."}
                                        ↓
                                AI saves everything
                                        ↓
                                Compaction proceeds
```

No counting needed — compaction always warrants a save. The auto-mine captures raw tool output before the AI gets a chance to summarize it away.

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

**`MEMPAL_PYTHON` override for the hook's internal Python calls.** The save hook parses its JSON input and counts transcript messages with `python3`. When the harness is launched from a GUI on macOS — `open -a`, Spotlight, the dock — its `PATH` is the minimal `/usr/bin:/bin:/usr/sbin:/sbin` inherited from `launchd`, not your shell PATH. If `python3` isn't on that PATH, those internal calls fail and the hook can't count exchanges.

Point the hook at any Python 3 interpreter to fix it:

```bash
export MEMPAL_PYTHON="/usr/bin/python3"                   # system Python is fine
export MEMPAL_PYTHON="$HOME/.venvs/mempalace/bin/python"  # or your venv
```

Resolution priority: `$MEMPAL_PYTHON` (if set and executable) → `$(command -v python3)` → bare `python3`. The interpreter only needs `json` and `sys` from the standard library — `mempalace` itself does not need to be installed in it.

Note: the `mempalace mine` auto-ingest runs via the `mempalace` CLI, so that command also needs to be on the hook's `PATH`. Installing with `pipx install mempalace` or `uv tool install mempalace` puts it on a stable global location; otherwise extend the hook environment's `PATH` to include your venv's `bin/`.

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
