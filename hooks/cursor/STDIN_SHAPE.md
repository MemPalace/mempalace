# Cursor Hook Stdin Shape — Reference

This file documents the JSON payloads the Cursor IDE sends to the
MemPalace hook scripts in `hooks/cursor/`. It exists so a future
contributor does not have to re-discover the schema by writing a
probe hook.

**Source:** [`cursor.com/docs/hooks.md`](https://cursor.com/docs/hooks.md),
fetched 2026-05-27. Cursor's hook system is documented as a stable
v1 schema (`{"version": 1, ...}` at the top of `hooks.json`).

If you suspect Cursor has changed the payload shape since that fetch
date, re-verify against the upstream docs and update both this file
and `hooks/cursor/lib/common.sh::mempal_parse_stdin`. The hook
scripts deliberately ignore fields they do not consume, so adding
new fields is non-breaking.

## Common fields (all events)

Every hook receives these on stdin in addition to its event-specific
fields. Source: docs section "Common schema → Input (all hooks)".

```json
{
  "conversation_id": "string",
  "generation_id":   "string",
  "model":           "string",
  "hook_event_name": "string",
  "cursor_version":  "string",
  "workspace_roots": ["<absolute path>"],
  "user_email":      "string | null",
  "transcript_path": "string | null"
}
```

**Field notes (verified):**

- `conversation_id` is the stable per-conversation ID. The Cursor
  `stop` event does **not** carry a `session_id` — only
  `conversation_id`. MemPalace keys its counter files on this. Cursor
  `sessionStart` does carry a `session_id`, and the docs note it is
  "same as `conversation_id`".
- `generation_id` changes every user message. We do not use it.
- `transcript_path` may be `null` if the user has disabled
  transcripts in Cursor settings. The hooks degrade gracefully when
  the value is empty.
- `workspace_roots` is normally a single-entry array but multi-root
  workspaces are supported; MemPalace uses index `[0]`.

## Event-specific fields

### `stop` (consumed by `mempal_save_hook_cursor.sh`)

```json
{
  "status":     "completed" | "aborted" | "error",
  "loop_count": 0
}
```

- `loop_count` indicates how many times this stop hook has already
  triggered an automatic followup for this conversation (starts at
  0). When `loop_count > 0` we know our own previous `followup_message`
  is currently being processed — the save hook returns `{}` so the
  agent can finish. Equivalent to Claude Code's `stop_hook_active`.
- The per-script `loop_limit` (default 5 for Cursor hooks, configurable
  via the `loop_limit` field on the hook entry in `hooks.json`) is
  defense-in-depth on top of our own check. The example `hooks.json`
  in `examples/cursor/` sets `loop_limit: 1`.

**Allowed output fields** (only):

```json
{ "followup_message": "<text to auto-submit as next user turn>" }
```

### `preCompact` (consumed by `mempal_precompact_hook_cursor.sh`)

```json
{
  "trigger":               "auto" | "manual",
  "context_usage_percent": 85,
  "context_tokens":        120000,
  "context_window_size":   128000,
  "message_count":         45,
  "messages_to_compact":   30,
  "is_first_compaction":   true
}
```

**Critical constraint:** preCompact is documented as **observational
only**. It cannot block compaction and its allowed output fields are
limited to:

```json
{ "user_message": "<short message shown to the user when compaction occurs>" }
```

There is **no** `followup_message` and **no** `decision: block` on
this event — unlike Claude Code's `PreCompact`. MemPalace works
around this by:

1. Running `mempalace mine` synchronously inside the hook so the
   verbatim transcript lands in the palace before compaction
   summarises it.
2. Dropping a `cursor_<conversation_id>.pending` marker that the next
   `stop` invocation reads and uses to force a save followup
   regardless of its counter.

### `sessionStart` (consumed by `mempal_wake_hook_cursor.sh`)

```json
{
  "session_id":          "<unique session identifier>",
  "is_background_agent": true,
  "composer_mode":       "agent" | "ask" | "edit"
}
```

`session_id` equals `conversation_id` on this event (docs are
explicit about this).

**Allowed output fields:**

```json
{
  "env":                { "<key>": "<value>" },
  "additional_context": "<text added to conversation's initial system context>"
}
```

`additional_context` is the field MemPalace uses. The schema also
accepts `continue` and `user_message` but the docs explicitly note
"current callers do not enforce them; session creation is not
blocked even when continue is false". We do not emit either.

## Environment variables (all hooks)

Cursor sets these env vars on every hook execution; the hook scripts
fall back to them when JSON parsing fails for any reason.

| Variable                  | Description                                       |
|---------------------------|---------------------------------------------------|
| `CURSOR_PROJECT_DIR`      | Workspace root (= `workspace_roots[0]`)           |
| `CURSOR_VERSION`          | Cursor version string                             |
| `CURSOR_USER_EMAIL`       | Authenticated user email (if logged in)           |
| `CURSOR_TRANSCRIPT_PATH`  | Conversation transcript path (if transcripts on)  |
| `CURSOR_CODE_REMOTE`      | `"true"` if running in a remote workspace         |
| `CLAUDE_PROJECT_DIR`      | Alias for `CURSOR_PROJECT_DIR` (Claude compat)    |

## Exit code semantics

Cursor interprets command-hook exit codes as follows
(docs "Hook Types → Command-Based Hooks → Exit code behavior"):

- `0` — success, use the JSON output.
- `2` — block the action (equivalent to `permission: "deny"`).
- Other — hook failed; action proceeds (fail-open by default).

MemPalace hooks always exit `0` and emit either `{}` (no-op) or a
valid JSON response. We never use exit code `2`; nothing MemPalace
does should ever block an agent action.

## Working directory contract

- **User hooks** (`~/.cursor/hooks.json`) run from `~/.cursor/`.
- **Project hooks** (`.cursor/hooks.json`) run from the project root.

The MemPalace hooks always resolve their sibling `lib/common.sh` via
`BASH_SOURCE[0]` so the working directory does not matter for the
script's own loading — only the `command` path in `hooks.json` needs
to point at the absolute location of the script.

## Transcript file format (out of scope)

The format of the file at `transcript_path` is **not documented by
Cursor** as of the fetch date above. MemPalace deliberately does not
parse it: the save hook counts `stop` invocations (each one
corresponds to one assistant turn) and hands the transcript to
`mempalace mine`, which has its own normaliser layer.

If you need to consume the transcript directly, probe its shape with
a throw-away hook that does `cat > /tmp/cursor-transcript-sample.txt`
and inspect the output — there is no shortcut.
