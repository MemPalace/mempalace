# MemPalace Cursor IDE Hooks

Auto-save and session-recall hooks for the [Cursor](https://cursor.com) IDE,
matching the behaviour of the existing Claude Code + Codex hooks at the repo
root and adding two Cursor-only capabilities (`sessionStart` recall and a
preCompact transcript snapshot).

For the rendered documentation see
[`website/guide/cursor-hooks.md`](../../website/guide/cursor-hooks.md) or
the published version at
[mempalaceofficial.com/guide/cursor-hooks](https://mempalaceofficial.com/guide/cursor-hooks.html).

## What's here

| File                                | Role                                                                |
|-------------------------------------|---------------------------------------------------------------------|
| `lib/common.sh`                     | Shared bash helpers (parse, log, counter, wing inference, kill switch). Sourced by all three hooks. |
| `mempal_save_hook_cursor.sh`        | Cursor `stop` hook. Counts stop invocations per conversation and mines the Cursor JSONL transcript in the background every `SAVE_INTERVAL` (default 15). Silent by default; `MEMPAL_VERBOSE=true` also emits a `followup_message`. |
| `mempal_precompact_hook_cursor.sh`  | Cursor `preCompact` hook. Runs `mempalace mine --wing` synchronously on the transcript before compaction, then drops a `.pending` marker. The next stop only emits a save nudge when `MEMPAL_VERBOSE=true`. |
| `mempal_wake_hook_cursor.sh`        | Cursor `sessionStart` hook. Returns `additional_context` telling the agent to recall scoped to the wing inferred from the workspace root. Cursor-only — Claude Code has no equivalent. |
| `install.sh`                        | Optional installer. Copies the scripts to `~/.mempalace/hooks/cursor/` and merges entries into `~/.cursor/hooks.json` (or `.cursor/hooks.json` for project scope). Supports `--dry-run` and `--uninstall`. |
| `STDIN_SHAPE.md`                    | Reference. Per-event stdin / stdout schema with citations to the official Cursor docs. |

## Quick install

Preview first (writes nothing, prints the would-be JSON to stdout):

```bash
hooks/cursor/install.sh --scope user --dry-run
```

Apply — writes `~/.cursor/hooks.json` and copies the scripts to `~/.mempalace/hooks/cursor/`:

```bash
hooks/cursor/install.sh --scope user
```

Pass `--scope project --target <repo>` to write `<repo>/.cursor/hooks.json` instead.
The installer never auto-runs — it is a documented opt-in step. We do not
modify your Cursor config on `pip install mempalace` because editor config
is sacred and should never be touched without explicit consent.

## Manual install (no installer)

The minimum wiring is `stop` only. Add to `~/.cursor/hooks.json`:

```json
{
  "version": 1,
  "hooks": {
    "stop": [
      {
        "command": "/absolute/path/to/hooks/cursor/mempal_save_hook_cursor.sh",
        "loop_limit": 1
      }
    ]
  }
}
```

For the full triple (recommended), also wire `sessionStart` and `preCompact`
— see [`examples/cursor/hooks.json`](../../examples/cursor/hooks.json).

After editing the file, Cursor watches `hooks.json` and reloads
automatically. If hooks still do not fire, restart Cursor and check the
Hooks panel in Settings.

## Configuration

All knobs are env vars; defaults match the Claude Code hooks where
possible so a single hook-state directory works for both editors.

| Variable                       | Default                            | Purpose |
|--------------------------------|------------------------------------|---------|
| `MEMPAL_SAVE_INTERVAL`         | `15`                               | Number of `stop` events between background mines (and, if verbose, followups). |
| `MEMPAL_VERBOSE`               | (unset)                            | Set to `true`/`1` to emit a `followup_message` (and a preCompact `user_message`). Silent by default. |
| `MEMPAL_CURSOR_SILENT`         | (unset)                            | No-op compatibility alias. The save hook is already silent by default. |
| `MEMPAL_DIR`                   | (unset)                            | Optional project directory to also mine on each save. Additive — never replaces the transcript mine. |
| `MEMPAL_PYTHON`                | auto-detected                      | Path to a Python 3 interpreter. Fallback order: `$MEMPAL_PYTHON` → `command -v python3` → bare `python3`. Useful when Cursor is launched from a GUI on macOS and the inherited PATH lacks your installed `python3`. |
| `MEMPAL_STATE_DIR`             | `$HOME/.mempalace/hook_state`      | Where the hook keeps its per-conversation counter files, pending-save markers, and `cursor_hook.log`. |
| `MEMPAL_STATE_TTL_DAYS`        | `30`                               | Age (days) after which stale `cursor_*.count` / `cursor_*.pending` state files are garbage-collected. A daily-throttled sweep runs from the hooks; only Cursor state is touched (shared logs and other editors' state are left alone). |
| `MEMPAL_DISABLE_HOOK`          | (unset)                            | Set to `1`/`true`/`yes` to disable all three hooks. Emergency kill switch. |
| `MEMPALACE_HOOKS_AUTO_SAVE`    | (unset)                            | Set to `false`/`0`/`no` to disable. Same semantics as the Claude Code hooks. Also honoured via `~/.mempalace/config.json` → `{"hooks": {"auto_save": false}}`. |

## Debugging

Everything appends to:

```bash
cat ~/.mempalace/hook_state/cursor_hook.log
```

Example log lines (ISO 8601 + event + conversation id):

```
[2026-05-27T02:16:01Z] [event=sessionStart] [conv=abc123] workspace=/Users/me/proj wing=proj
[2026-05-27T02:21:33Z] [event=stop]         [conv=abc123] counter 0 -> 1 (interval=15)
[2026-05-27T02:42:09Z] [event=stop]         [conv=abc123] counter 14 -> 15 (interval=15)
[2026-05-27T02:42:09Z] [event=stop]         [conv=abc123] TRIGGERING SAVE at counter=15
[2026-05-27T02:42:11Z] [event=stop]         [conv=abc123] loop_count>0; letting agent stop
```

When a hook can't parse its stdin (corrupt payload, future Cursor schema
change), the raw input (capped at 4096 bytes, mode 0600) lands at:

```
~/.mempalace/hook_state/cursor_last_input.log
~/.mempalace/hook_state/cursor_last_python_err.log
```

These are overwritten on each failure, never appended, so a repeating
misconfiguration cannot grow disk usage.

## What differs from the Claude Code hooks

| Aspect                  | Claude Code hooks (`hooks/mempal_*.sh`)        | Cursor hooks (`hooks/cursor/*.sh`)                  |
|-------------------------|------------------------------------------------|-----------------------------------------------------|
| Counter key             | `session_id`                                   | `conversation_id` (Cursor's stable per-conv id)     |
| Loop guard              | `stop_hook_active` flag in stdin               | `loop_count` field in stdin                         |
| Counting method         | Parses JSONL transcript for user messages      | Counts `stop` invocations; `normalize.py` parses the Cursor JSONL on mine |
| Capture path            | Background `mine --mode convos` (normalize.py has a Claude parser) | Background `mine --mode convos --wing` (`_try_cursor_jsonl` in normalize.py) |
| Save default            | Silent — diary nudge opt-IN behind `MEMPAL_VERBOSE=true` | Same: silent by default; opt-IN via `MEMPAL_VERBOSE=true`. `MEMPAL_CURSOR_SILENT` is a no-op alias |
| PreCompact behaviour    | `decision: block` forces save before compaction | Pre-mine + pending-save marker (Cursor preCompact is observational-only) |
| sessionStart            | n/a (Claude Code has no equivalent)            | `additional_context` injects recall guidance        |
| State dir               | `$HOME/.mempalace/hook_state` (hardcoded)      | Same default, plus `MEMPAL_STATE_DIR` env override  |
| Kill switch             | `MEMPALACE_HOOKS_AUTO_SAVE=false`              | Same, plus `MEMPAL_DISABLE_HOOK=1` alias            |
| Log file                | `hook.log`                                     | `cursor_hook.log` (kept separate to avoid cross-tool log churn) |

See [`STDIN_SHAPE.md`](STDIN_SHAPE.md) for the per-event schema and
[`website/guide/cursor-hooks.md`](../../website/guide/cursor-hooks.md) for
the full walkthrough with diagrams.

## Why the followup is silent by default

The Claude Code hook is silent by default because its background
`mempalace mine --mode convos` captures the verbatim transcript on its
own. Cursor now has the same path: `normalize.py` parses Cursor agent
JSONL (`_try_cursor_jsonl`), so the `stop` / `preCompact` mines file
clean verbatim drawers without a chat takeover.

Set `MEMPAL_VERBOSE=true` if you also want a diary-nudge followup every
`SAVE_INTERVAL` stops (or after a preCompact marker). `MEMPAL_CURSOR_SILENT`
is kept as a no-op alias for older installs that set it when the
followup was the default.

## Cost

Zero extra LLM tokens spent by the hooks themselves. The hooks are local
bash scripts that run on your machine. When `MEMPAL_VERBOSE=true`, the
followup message the save hook emits is a normal user turn — it counts
the same as any other user message. The default install spends none.
