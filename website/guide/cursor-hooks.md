# Cursor IDE Hooks

Three hooks for the [Cursor](https://cursor.com) IDE that save memories
automatically and inject recall context at session start. No manual "save"
commands needed.

These are additive to the existing [Claude Code + Codex hooks](/guide/hooks).
You can run both — they share the same `~/.mempalace/hook_state/`
directory and the same kill switches.

::: tip Pair this with the Cursor plugin
The hooks here only handle the auto-save side. To also get MemPalace's
MCP server, slash commands (`/mempalace-search`, etc.), and the
guided `mempalace` skill, install the bundled
[Cursor plugin](https://github.com/MemPalace/mempalace/blob/main/.cursor-plugin/README.md) —
it's the `.cursor-plugin/` folder at the repo root, dropped into
`~/.cursor/plugins/local/mempalace`. The plugin and the hooks are
orthogonal: install whichever you want, in any order. The plugin
deliberately does **not** wire hooks itself because Cursor's hooks
system is configured per-user/per-project (in `~/.cursor/hooks.json`),
not per-plugin.
:::

## What They Do

| Hook | When It Fires | What Happens |
|------|---------------|--------------|
| **Wake Hook** | `sessionStart` — when a new Cursor conversation opens | Returns `additional_context` telling the agent to recall scoped to the wing inferred from the workspace root. Cursor-only — Claude Code has no equivalent. |
| **Save Hook** | `stop` — after every agent turn | Counts stop invocations per conversation. Every 15 (default), emits a `followup_message` telling the agent to file the session into MemPalace and write a diary entry. |
| **PreCompact Hook** | `preCompact` — right before context compaction | Runs `mempalace mine` synchronously on the transcript before compaction summarises it. Drops a pending-save marker so the next stop forces a save followup. |

**Two-layer capture:** the save and precompact hooks both mine the JSONL
transcript directly into the palace (capturing verbatim tool output — Shell
results, search findings, build errors). The save hook also nudges the AI
to write structured drawers and a diary entry. Belt-and-suspenders.

## Install — Cursor

The fastest path is the installer that ships in the repo.

Preview the change first (writes nothing, just prints the would-be JSON):

```bash
hooks/cursor/install.sh --scope user --dry-run
```

User scope — applies globally, writes `~/.cursor/hooks.json`:

```bash
hooks/cursor/install.sh --scope user
```

Or project scope — only this repo, writes `<repo>/.cursor/hooks.json`:

```bash
hooks/cursor/install.sh --scope project --target /path/to/your/repo
```

The installer copies the three hook scripts to `~/.mempalace/hooks/cursor/`,
merges the entries into your `hooks.json`, and preserves any unrelated
hooks already in that file. Re-running is idempotent. Pass `--variant
minimal` for the `stop`-only setup, or `--uninstall` to remove the
MemPalace entries (leaves other hooks intact).

### Manual install — `~/.cursor/hooks.json` (user scope)

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [
      { "command": "/absolute/path/to/hooks/cursor/mempal_wake_hook_cursor.sh" }
    ],
    "stop": [
      {
        "command": "/absolute/path/to/hooks/cursor/mempal_save_hook_cursor.sh",
        "loop_limit": 1
      }
    ],
    "preCompact": [
      { "command": "/absolute/path/to/hooks/cursor/mempal_precompact_hook_cursor.sh" }
    ]
  }
}
```

### Manual install — `.cursor/hooks.json` (project scope)

Identical content. Project hooks load in any trusted workspace and are
checked into version control with the project. Cloud agents also load
project hooks.

Make the scripts executable once:

```bash
chmod +x hooks/cursor/mempal_save_hook_cursor.sh \
         hooks/cursor/mempal_precompact_hook_cursor.sh \
         hooks/cursor/mempal_wake_hook_cursor.sh
```

Cursor watches `hooks.json` and reloads automatically after a save. If
hooks still do not fire, restart Cursor and check the Hooks panel in
Settings → Hooks.

## Configuration

All knobs are environment variables. Defaults match the Claude Code hooks
where they overlap.

- **`MEMPAL_SAVE_INTERVAL=15`** — number of `stop` events between save
  followups. Lower = more frequent saves, higher = less interruption.
- **`MEMPAL_CURSOR_SILENT=1`** — suppress the `followup_message` entirely
  (the hook still runs its best-effort background mine and keeps its
  counters). `MEMPAL_VERBOSE=false`/`0`/`no` is equivalent. Note the
  followup is **on by default** for Cursor — see "Why the followup is on
  by default" below.
- **`MEMPAL_STATE_DIR`** — where the hook keeps counter files, the
  pending-save marker, and `cursor_hook.log`. Defaults to
  `~/.mempalace/hook_state/`.
- **`MEMPAL_STATE_TTL_DAYS=30`** — age after which stale
  `cursor_*.count` / `cursor_*.pending` files are swept. The hooks run a
  daily-throttled garbage collection so per-conversation state can't grow
  unbounded; only Cursor's own state is touched.
- **`MEMPAL_DIR`** — optional project directory (code, notes, docs) to
  also mine on each save trigger, with `--mode projects`. The transcript
  is always mined regardless — `MEMPAL_DIR` is purely additive.
- **`MEMPAL_PYTHON`** — path to a Python 3 interpreter. The hook's own
  JSON parsing and the install script's JSON merge use this. Resolution
  order: `$MEMPAL_PYTHON` → `command -v python3` → bare `python3`. Set
  this when Cursor is launched from a GUI on macOS and the inherited
  PATH lacks the Python where you installed MemPalace.
- **`MEMPAL_DISABLE_HOOK=1`** — emergency kill switch. Disables all
  three hooks; they emit `{}` and exit 0.
- **`MEMPALACE_HOOKS_AUTO_SAVE=false`** — same effect as
  `MEMPAL_DISABLE_HOOK=1`. Also honoured via `~/.mempalace/config.json`:

  ```json
  { "hooks": { "auto_save": false } }
  ```

## How It Works

### Wake Hook (`sessionStart`)

```
Cursor opens new conversation → sessionStart fires
                                       ↓
                          Hook reads workspace_roots[0]
                                       ↓
                          Infers wing = basename(workspace_root)
                                       ↓
                {"additional_context": "scope recall to wing=<...>"}
                                       ↓
                Agent reads additional_context before first turn
                                       ↓
                Agent calls mempalace_search + mempalace_diary_read
                wing-scoped on the first relevant question
```

Cursor's `sessionStart` is fire-and-forget — the agent loop does not wait
for a blocking response and does not consume `continue` / `user_message`.
But it does honour `additional_context`, and that is the only field
MemPalace emits.

### Save Hook (`stop` event)

```
User sends message → agent responds → Cursor fires stop hook
                                              ↓
                              Hook reads loop_count from stdin
                                              ↓
                  ┌─── loop_count > 0 (our own followup running) ──→ echo "{}"
                  │
                  └─── loop_count == 0
                                  ↓
                       Check pending-save marker from preCompact
                                  ↓
                   ┌── marker present ──→ delete + emit followup_message
                   │
                   └── no marker
                                  ↓
                  Atomic counter++ for this conversation_id
                                  ↓
              ┌── counter % SAVE_INTERVAL != 0 ──→ echo "{}"
              │
              └── counter % SAVE_INTERVAL == 0
                                  ↓
                 Background: mempalace mine <transcript_dir>
                                  ↓
                  Emit {"followup_message": "save key topics..."}
                                  ↓
                  Cursor auto-submits followup as next user turn
                                  ↓
                  Agent files drawers + writes diary
                                  ↓
                  Agent stops; stop fires again with loop_count = 1
                                  ↓
                  Hook sees loop_count > 0 → echo "{}" → agent stops
```

The `loop_count > 0` short-circuit prevents infinite loops: emit once →
agent saves → stops → we see `loop_count = 1` → we let it through. This
is the Cursor equivalent of Claude Code's `stop_hook_active` flag. The
`loop_limit: 1` in `hooks.json` is defense-in-depth on top.

### PreCompact Hook

```
Context window near full → Cursor fires preCompact (observational)
                                       ↓
                Synchronously: mempalace mine <transcript_dir>
                                       ↓
                Drop pending-save marker for this conversation_id
                                       ↓
                {"user_message": "transcript snapshotted..."}
                                       ↓
                Compaction proceeds (we cannot block it)
                                       ↓
                Next stop event picks up the marker → forces save
```

Cursor's `preCompact` is documented as **observational only** — its only
output field is `user_message`, with no `followup_message` and no way to
block. That is fundamentally different from Claude Code's `PreCompact`
which can block until the AI has saved. We work around the limitation by
mining the verbatim transcript synchronously (zero LLM cost) and queueing
a save nudge for the next agent turn.

::: tip Why synchronous (and what happens on a slow mine)
The pre-compaction mine runs **synchronously** on purpose: compaction is
irreversible, so we must finish ingesting before the hook returns —
background mining would race the compaction. On a very large transcript
this can exceed Cursor's per-hook timeout, in which case Cursor kills the
mine mid-run. That is safe: `mempalace mine` is incremental and
append-only, so a killed mine resumes cleanly on the next invocation
rather than corrupting the palace, and the pending-save marker still
forces a re-mine plus a verbatim save nudge on the next `stop`.
:::

## Cursor-only extras

The features below are not available in the Claude Code or Codex hooks
because their hook surfaces do not expose the necessary events.

- **Session-start recall via `sessionStart`.** The wake hook injects
  wing-scoped recall guidance into the conversation's initial system
  context, so the agent searches the palace before answering anything
  that touches past work. Verified output field — see the [Cursor hooks
  reference](https://cursor.com/docs/hooks.md) section "sessionStart".
- **Per-script `loop_limit`.** Cursor's `loop_limit` (default 5,
  configurable per script) is a hard cap on how many auto-followups
  Cursor will issue. MemPalace sets it to `1` in the example
  `hooks.json` as defense-in-depth on top of its own `loop_count`
  check.
- **Inferred wing from `workspace_roots`.** Both the wake hook and the
  save hook use `basename(workspace_roots[0])` to scope memory
  operations. A user with multiple Cursor workspaces gets per-project
  wings without any manual configuration.

## Debugging

```bash
cat ~/.mempalace/hook_state/cursor_hook.log
```

Example output (ISO-8601 timestamps, event + conversation id, message):

```
[2026-05-27T02:16:01Z] [event=sessionStart] [conv=abc123] workspace=/Users/me/proj wing=proj
[2026-05-27T02:21:33Z] [event=stop]         [conv=abc123] counter 0 -> 1 (interval=15)
[2026-05-27T02:42:09Z] [event=stop]         [conv=abc123] counter 14 -> 15 (interval=15)
[2026-05-27T02:42:09Z] [event=stop]         [conv=abc123] TRIGGERING SAVE at counter=15
[2026-05-27T02:42:11Z] [event=stop]         [conv=abc123] loop_count>0; letting agent stop
[2026-05-27T03:05:44Z] [event=preCompact]   [conv=abc123] trigger=auto transcript=/Users/me/.cursor/.../transcript.txt
[2026-05-27T03:05:46Z] [event=stop]         [conv=abc123] consumed pending-save marker (post-compaction)
```

When a hook can't parse its stdin (corrupt payload, future Cursor schema
change), the raw input — capped at 4096 bytes, mode 0600 — lands at:

```
~/.mempalace/hook_state/cursor_last_input.log
~/.mempalace/hook_state/cursor_last_python_err.log
```

Both are overwritten on each failure, never appended, so a repeating
misconfiguration cannot grow disk usage.

## Cost

**Zero extra tokens spent by the hooks themselves.** The hooks are bash
scripts that run locally. They do not call any API. The `followup_message`
the save hook emits is a normal user turn — it counts the same as any
other user message and does not invoke any extra LLM call beyond the one
the user would otherwise make. To suppress it entirely, set
`MEMPAL_CURSOR_SILENT=1`.

## Why the followup is on by default

The Claude Code hook is **silent by default**: its background `mempalace
mine --mode convos` captures the verbatim transcript on its own (because
`normalize.py` has a Claude Code JSONL parser), and the LLM-driven diary
nudge is opt-in behind `MEMPAL_VERBOSE`.

Cursor is different. Cursor's transcript format is **undocumented** and
`normalize.py` has **no Cursor parser**, so the background mine is
best-effort only and does not yet yield clean verbatim drawers. That makes
the `followup_message` — which drives the agent to file its own in-context
verbatim quotes via `mempalace_add_drawer` / `mempalace_diary_write` — the
**load-bearing verbatim-capture path** for Cursor. Turning it off by
default would leave a default install capturing nothing, so it is on by
default.

If you want the Claude-style "zero tokens in the chat window" behaviour
and accept the reduced capture, set `MEMPAL_CURSOR_SILENT=1` (or
`MEMPAL_VERBOSE=false`). The proper long-term fix is a Cursor transcript
parser in `normalize.py` (tracked follow-up); once that works, this
default flips to silent to match Claude.

## Known limitations

- **Hooks load at session start.** Cursor watches `hooks.json` and reloads
  the wiring when the file changes, but for the freshly-loaded hook
  scripts to take effect on an existing conversation you usually have to
  start a new conversation. This matches the behaviour of Claude Code's
  hook lifecycle.
- **`preCompact` cannot block.** See the diagram above. The
  pending-save marker is the workaround.
- **Transcript file format is opaque.** Cursor does not document the
  schema of the file at `transcript_path`, and `mempalace/normalize.py`
  has no Cursor parser yet, so the background `mempalace mine --mode
  convos` is **best-effort** for Cursor — it does not yet produce clean
  verbatim conversation drawers. The `followup_message` is the
  load-bearing capture path (see below). Adding a Cursor parser to
  `normalize.py` is tracked follow-up work; once it lands, the followup
  can default to silent like the Claude hook.

## Related

- [Auto-Save Hooks (Claude Code + Codex)](/guide/hooks) — the analogous
  feature for those tools.
- [`hooks/cursor/STDIN_SHAPE.md`](https://github.com/MemPalace/mempalace/blob/develop/hooks/cursor/STDIN_SHAPE.md)
  — per-event JSON schema with citations.
- [Claude Code Retention](/guide/claude-code-retention) — broader
  setup checklist if you mix Cursor with Claude Code.
