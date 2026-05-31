# Antigravity hook STDIN / STDOUT contract

This file documents the exact wire format the Antigravity IDE uses
when invoking the MemPalace hook scripts. All fields are verbatim
from Google's official Antigravity hooks documentation
(`https://antigravity.google/docs/hooks?app=antigravity`, accessed
2026-05-27). See [INVESTIGATION.md](INVESTIGATION.md) for the
provenance audit.

## Wire format

Hooks receive **JSON on stdin** and must emit **JSON on stdout**.
Field names are **camelCase**.

Hook execution timeout defaults to 30 seconds. The MemPalace plugin
sets the Stop hook timeout to 30s and the PreInvocation hook timeout
to 5s in the rendered `hooks.json`.

## Common stdin fields (every event)

| Field                   | Type            | Notes                                                  |
|-------------------------|-----------------|--------------------------------------------------------|
| `conversationId`        | string          | UUID of the active agent conversation.                 |
| `workspacePaths`        | array<string>   | Absolute workspace dirs. **First element is canonical**. |
| `transcriptPath`        | string          | Absolute path to `transcript.jsonl`.                   |
| `artifactDirectoryPath` | string          | Path to conversation artifacts and screenshots.        |

## Stop event

### Stdin (additional fields)

| Field               | Type    | Notes                                                                                |
|---------------------|---------|--------------------------------------------------------------------------------------|
| `executionNum`      | integer | Sequence number of the execution attempt for this conversation.                      |
| `terminationReason` | string  | `"model_stop"`, `"max_steps_exceeded"`, `"error"`, etc.                              |
| `error`             | string  | Optional. Set when termination was caused by a system error.                         |
| `fullyIdle`         | boolean | **Required.** True iff all background commands and async tasks have completed.       |

### Stdout

| Field      | Type   | Notes                                                                                                  |
|------------|--------|--------------------------------------------------------------------------------------------------------|
| `decision` | string | If `"continue"`, **forces** the agent to keep running. Anything else allows the stop.                  |
| `reason`   | string | Optional. If `decision == "continue"`, injected as a system message into the conversation.             |

**MemPalace policy**: the save hook ALWAYS emits `{}` and exits 0. It
NEVER emits `{"decision": "continue"}` — that would force an infinite
agent loop. There is an explicit refusal in
`mempal_save_hook_antigravity.sh` to ever construct a stdout JSON
object containing the literal word `"continue"` in a decision field.

### MemPalace gating

The save hook short-circuits with `{}` (no save triggered) when ANY
of the following hold:

1. `MEMPAL_DISABLE_HOOK=1` (or `true`/`yes`) is set.
2. `MEMPALACE_HOOKS_AUTO_SAVE=false` (or `0`/`no`) is set.
3. `~/.mempalace/config.json` has `hooks.auto_save: false`.
4. `~/.mempalace/` directory does not exist (user nuked the palace).
5. Stdin is malformed or empty (sentinel-guarded parse failure).
6. `fullyIdle == false` (background tasks still running; defer save).
7. `terminationReason == "error"` (transcript may be corrupt).
8. `transcriptPath` validation fails (not a `.json`/`.jsonl`, or `..` traversal).
9. The transcript file does not exist on disk.
10. The save counter has not yet hit `count % MEMPAL_SAVE_INTERVAL == 0`.
11. A pending save is still running for this conversation (less than 1 hour old).
12. The `mempalace` CLI is not on `$PATH`.

When the modulo gate is hit and validation passes, the hook spawns
`mempalace mine <transcript-dir> --mode convos --wing <inferred>` in
the background and returns `{}` immediately.

## PreInvocation event

### Stdin (additional fields)

| Field             | Type    | Notes                                                            |
|-------------------|---------|------------------------------------------------------------------|
| `invocationNum`   | integer | Sequence number of the current model invocation (1-based).       |
| `initialNumSteps` | integer | Number of steps currently in the trajectory.                     |

### Stdout

| Field         | Type           | Notes                                                                                                  |
|---------------|----------------|--------------------------------------------------------------------------------------------------------|
| `injectSteps` | array<object>  | Optional. Steps to inject before the model is called. Each step has one of: `{"toolCall": {...}}`, `{"userMessage": "..."}`, `{"ephemeralMessage": "..."}` |

The `ephemeralMessage` form is what the MemPalace wake hook emits — it
delivers the wake-up text to the model on this turn but does not
persist into the transcript, so subsequent invocations don't see a
duplicate.

### MemPalace gating

The wake hook short-circuits with `{}` (no injection) when ANY of:

1. Any kill switch trips (same five conditions as the save hook).
2. `invocationNum != 1` — we only inject on the first model call of
   each conversation, mimicking Cursor's `sessionStart` semantics.
3. The atomic `mkdir`-based loop guard is already taken (this
   conversation already received a wake injection).
4. `mempalace wake-up --wing <inferred>` exits non-zero, times out
   (500ms hard cap), or produces empty output.

When the gates pass and `mempalace wake-up` returns text, the hook
emits:

```json
{
  "injectSteps": [
    { "ephemeralMessage": "<verbatim wake-up output>" }
  ]
}
```

The wake hook NEVER emits a `decision` field — that field belongs to
the Stop event. There is a final guard against any stdout that
contains a `decision` key.

## Worked example: Stop event

### Input

```json
{
  "executionNum": 1,
  "terminationReason": "model_stop",
  "error": "",
  "fullyIdle": true,
  "conversationId": "ec33ebf9-0cba-4100-8142-c61503f6c587",
  "workspacePaths": ["/home/me/projects/mempalace"],
  "transcriptPath": "/home/me/projects/mempalace/.gemini/jetski/transcript.jsonl",
  "artifactDirectoryPath": "/home/me/projects/mempalace/.gemini/jetski/artifacts"
}
```

### Output (always)

```json
{}
```

(Side effects: counter `~/.mempalace/hook_state/antigravity_save_count_<id>` is
incremented; if the modulo gate fires, a background `mempalace mine`
subprocess is spawned with the transcript directory and the inferred
wing `wing_mempalace`.)

## Worked example: PreInvocation, first invocation

### Input

```json
{
  "invocationNum": 1,
  "initialNumSteps": 0,
  "conversationId": "ec33ebf9-0cba-4100-8142-c61503f6c587",
  "workspacePaths": ["/home/me/projects/mempalace"],
  "transcriptPath": "/home/me/projects/mempalace/.gemini/jetski/transcript.jsonl",
  "artifactDirectoryPath": "/home/me/projects/mempalace/.gemini/jetski/artifacts"
}
```

### Output (when the palace has memory for `wing_mempalace`)

```json
{
  "injectSteps": [
    {
      "ephemeralMessage": "<exact verbatim text from `mempalace wake-up --wing wing_mempalace`>"
    }
  ]
}
```

### Output (when invocationNum != 1, or any gate trips)

```json
{}
```

## State files

All hook state lives under `~/.mempalace/hook_state/` (overridable
via `$MEMPAL_STATE_DIR`) and is namespaced with the `antigravity_`
prefix to coexist with Claude Code, Cursor, and Codex hook state in
the same directory.

| File                                          | Purpose                                       |
|-----------------------------------------------|-----------------------------------------------|
| `antigravity_hook.log`                        | All hook activity, ISO8601Z timestamps.       |
| `antigravity_save_count_<conversationId>`     | Per-conversation Stop counter.                |
| `antigravity_pending_<conversationId>`        | Marker file for in-flight save subprocess.    |
| `antigravity_woke_<conversationId>` (dir)     | Atomic mkdir marker for wake injection.       |
| `antigravity_last_input.log`                  | 4 KB cap, mode 0600, set on parse failure.    |
| `antigravity_last_python_err.log`             | Python stderr from the JSON parser, mode 0600.|

## Environment variables

| Variable                       | Default              | Purpose                                                   |
|--------------------------------|----------------------|-----------------------------------------------------------|
| `MEMPAL_PYTHON`                | `$(command -v python3)` | Override the Python interpreter used by the hooks.        |
| `MEMPAL_STATE_DIR`             | `~/.mempalace/hook_state` | Override the hook state directory.                        |
| `MEMPAL_SAVE_INTERVAL`         | `15`                 | Save every Nth Stop fire. Floored to >= 1 (no /0).        |
| `MEMPAL_DISABLE_HOOK`          | unset                | Set to `1` / `true` / `yes` to disable both hooks.        |
| `MEMPALACE_HOOKS_AUTO_SAVE`    | unset                | Set to `false` / `0` / `no` to disable both hooks.        |
