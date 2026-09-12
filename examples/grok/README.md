# Grok hooks

Native Grok install for MemPalace auto-save. This is the supported Grok
path. Do not copy `hooks/mempal_save_hook.sh` and do not rely on the
Claude plugin defaulting to `--harness auto`.

## Install (user scope)

```bash
mkdir -p ~/.grok/hooks
cp examples/grok/hooks.json ~/.grok/hooks/mempalace.json
```

Restart Grok, or run `/hooks` and reload. Confirm `Stop`, `SessionEnd`,
and `PreCompact` show the `mempalace hook run --harness grok` commands.

## Install (project scope)

Copy the same file to `<repo>/.grok/hooks/mempalace.json`, then run
`/hooks-trust` in that workspace.

## Timeouts

Grok observe hooks default to 5 seconds. SessionEnd and PreCompact run
a mine and will be killed at that default.

| Event | timeout (seconds) |
|---|---|
| Stop | 30 |
| SessionEnd | 60 |
| PreCompact | 90 |

If SessionEnd still dies mid-mine, raise the session-end budget too:

```bash
export GROK_SESSION_END_HOOKS_TIMEOUT_MS=60000
```

That env var is milliseconds and is capped at 60 seconds.

## What these hooks do

Same save policy as Claude Code: silent diary checkpoint every 15 user
turns, SessionEnd flush, PreCompact mine. Grok real turns are
`type=user` records with `prompt_index`. Session-end Stop fires
(`reason` `channel_closed` / `shutdown`) and subagent Stops are skipped.

Silent save confirmation does not appear in the Grok TUI. An allowing
Stop hook leaves no trace, and `systemMessage` is Claude-only. The
diary still writes. Optional desktop toasts stay available via
`hooks.desktop_toast` in `~/.mempalace/config.json`.
