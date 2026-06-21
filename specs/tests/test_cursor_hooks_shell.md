# Spec: tests/test_cursor_hooks_shell.py

This is a behavioral test suite that pins the observable contracts of the three Cursor hook shell scripts and their shared library. The tests are themselves an executable specification of the hook scripts; this document captures the contracts those tests assert. An implementer satisfying every claim below satisfies the test suite.

## Subjects under test

The suite exercises files under the repository's `hooks/cursor/` directory: a save hook `mempal_save_hook_cursor.sh`, a precompact hook `mempal_precompact_hook_cursor.sh`, a wake hook `mempal_wake_hook_cursor.sh`, and a shared library `lib/common.sh` (tests/test_cursor_hooks_shell.py:L40-L45). The three hook scripts are collectively parametrized for source-level and universal-behavior tests under the ids `save_hook`, `precompact_hook`, `wake_hook` (tests/test_cursor_hooks_shell.py:L49-L53). The entire suite is skipped on Windows because the hooks are POSIX-only (tests/test_cursor_hooks_shell.py:L55).

## Test harness / invocation contract

Hooks are invoked as `bash <hook>` with the test-controlled stdin piped in, and the test asserts the process exit code equals an expected value (default 0) (tests/test_cursor_hooks_shell.py:L88-L106). The harness builds a clean environment containing `HOME` set to a sandbox directory, a `PATH` inherited from the ambient environment, and `MEMPAL_PYTHON` forced to the test runner's Python interpreter so the hook always finds a Python able to parse JSON; callers may prepend `PATH` entries and merge additional environment variables (tests/test_cursor_hooks_shell.py:L79-L87). The child process umask is forced to `022` (permissive) before exec so that any resulting `0600` file mode is provably caused by the hook's own internal `umask 077` rather than an ambient restrictive umask (tests/test_cursor_hooks_shell.py:L95-L101). Each invocation has a 30-second timeout (tests/test_cursor_hooks_shell.py:L94).

## On-disk state layout (observable contract)

All hook state lives under `<HOME>/.mempalace/hook_state/` (tests/test_cursor_hooks_shell.py:L153-L154). Within that directory the suite asserts these named artifacts: the shared log `cursor_hook.log` (tests/test_cursor_hooks_shell.py:L157-L159), the malformed-input dump `cursor_last_input.log` (tests/test_cursor_hooks_shell.py:L281-L283), a Python error log `cursor_last_python_err.log` (tests/test_cursor_hooks_shell.py:L316-L317), per-conversation counter files named `cursor_<conversation_id>.count` (tests/test_cursor_hooks_shell.py:L328-L331), per-conversation pending markers named `cursor_<conversation_id>.pending` (tests/test_cursor_hooks_shell.py:L429-L434), and a GC sweep marker `cursor_last_sweep` (tests/test_cursor_hooks_shell.py:L730-L732). Configuration is read from `<HOME>/.mempalace/config.json` (tests/test_cursor_hooks_shell.py:L254-L256).

## Input payload shapes

A `stop` payload is a JSON object carrying `conversation_id`, `loop_count`, `status`, `model`, `hook_event_name="stop"`, `transcript_path`, and `workspace_roots` (a list whose first element drives wing inference) (tests/test_cursor_hooks_shell.py:L109-L125). A `preCompact` payload carries `conversation_id`, `hook_event_name="preCompact"`, `trigger`, `transcript_path`, and `workspace_roots` (tests/test_cursor_hooks_shell.py:L128-L137). A `sessionStart` payload carries `conversation_id`, `session_id`, `hook_event_name="sessionStart"`, `is_background_agent`, `composer_mode`, and `workspace_roots` (tests/test_cursor_hooks_shell.py:L140-L150).

## bash 3.2 source-level compatibility

Each hook script and `common.sh` must pass `bash -n` (syntax check) with exit code 0 (tests/test_cursor_hooks_shell.py:L166-L181). After stripping comment lines, no hook script and not `common.sh` may contain the tokens `mapfile` or `readarray`, because those builtins are absent on macOS `/bin/bash` 3.2 (tests/test_cursor_hooks_shell.py:L183-L203). `common.sh` must use `sed -n 'Np'`-style line extraction: after stripping comments it must contain at least seven occurrences of the literal `sed -n '`, corresponding to the sentinel plus six fields read by the stdin parser (tests/test_cursor_hooks_shell.py:L205-L218).

## Kill switches (apply to all three hooks)

When `MEMPAL_DISABLE_HOOK` is set to any of `1`, `true`, `yes`, or `on`, every hook must short-circuit and emit exactly the empty JSON object `{}`, creating no log state (tests/test_cursor_hooks_shell.py:L225-L237). When `MEMPALACE_HOOKS_AUTO_SAVE` is set to any of `false`, `0`, `no`, or `off`, every hook must short-circuit and emit `{}` (tests/test_cursor_hooks_shell.py:L239-L250). When `<HOME>/.mempalace/config.json` contains `{"hooks": {"auto_save": false}}`, every hook must short-circuit and emit `{}` (tests/test_cursor_hooks_shell.py:L252-L260).

## Malformed and empty stdin (apply to all three hooks)

Given non-JSON stdin, each hook must still emit parseable JSON equal to `{}` and exit 0 so Cursor proceeds (tests/test_cursor_hooks_shell.py:L267-L271). On parse failure the hook must append a warning line containing the substring `WARN: input parse failed` to `cursor_hook.log`, and must write the raw payload to `cursor_last_input.log` (tests/test_cursor_hooks_shell.py:L273-L283). The dump file `cursor_last_input.log` must have file mode exactly `0600` (tests/test_cursor_hooks_shell.py:L285-L290). The dump is capped at exactly 4096 bytes: feeding 4097 bytes of input yields a dump file of size exactly 4096 (tests/test_cursor_hooks_shell.py:L292-L298). Empty stdin must emit `{}` and must NOT create the dump file at all (tests/test_cursor_hooks_shell.py:L300-L305). A successful parse (using the event-appropriate well-formed payload per hook) must leave no `cursor_last_python_err.log` file, i.e. that file is cleaned up on success (tests/test_cursor_hooks_shell.py:L307-L317).

## Save hook: per-conversation counter

The save hook maintains a per-conversation counter that increments atomically across invocations. Three `stop` invocations for conversation `conv-A` below the trigger threshold each emit `{}` and leave `cursor_conv-A.count` containing the text `3` (tests/test_cursor_hooks_shell.py:L324-L331). Counters are isolated per conversation: two invocations for `conv-A` and one for `conv-B` leave `cursor_conv-A.count` = `2` and `cursor_conv-B.count` = `1` (tests/test_cursor_hooks_shell.py:L333-L338).

## Save hook: threshold / followup_message

The trigger interval is configured by `MEMPAL_SAVE_INTERVAL`. With interval `3`, the first two `stop` invocations emit `{}` and the third emits a JSON object containing the key `followup_message` (tests/test_cursor_hooks_shell.py:L340-L350). The followup message string must reference the real MCP tool names `mempalace_add_drawer`, `mempalace_check_duplicate`, and `mempalace_diary_write`, and must contain the literal `cursor-ide` (diary entries are tagged `agent_name=cursor-ide`) (tests/test_cursor_hooks_shell.py:L351-L357). With interval `1`, the followup message references the wing inferred from `workspace_roots[0]` (`/Users/test/sampleProj` infers wing `sampleproj`) (tests/test_cursor_hooks_shell.py:L359-L364).

`MEMPAL_SAVE_INTERVAL=0` must be coerced to the default interval of `15` (rather than causing a division-by-zero crash); three invocations each succeed with exit 0 and emit `{}` because the coerced interval is never reached (tests/test_cursor_hooks_shell.py:L366-L379).

## Save hook: followup opt-out

The followup is ON by default at the threshold; with no silence flag and interval `1`, the threshold emits a `followup_message` (tests/test_cursor_hooks_shell.py:L391-L396). `MEMPAL_CURSOR_SILENT` set to any of `1`, `true`, `yes`, `on` suppresses the followup, yielding `{}` even at the threshold (tests/test_cursor_hooks_shell.py:L398-L404). `MEMPAL_VERBOSE` set to any of `false`, `0`, `no`, `off` likewise suppresses the followup, yielding `{}` (tests/test_cursor_hooks_shell.py:L406-L412). Silencing must not disable bookkeeping: with the followup silenced, the per-conversation counter still advances (two invocations of `conv-S` leave `cursor_conv-S.count` = `2`) (tests/test_cursor_hooks_shell.py:L414-L423). A pending marker that would normally force a followup is, under silence, consumed (deleted) but emits `{}` (tests/test_cursor_hooks_shell.py:L425-L434).

## Save hook: loop prevention

A `stop` payload with `loop_count > 0` short-circuits the save hook to `{}` even when the trigger interval is `1`, and writes no counter file (tests/test_cursor_hooks_shell.py:L441-L452). A `stop` payload with `loop_count = 0` does not short-circuit; at interval `1` it emits a `followup_message` (tests/test_cursor_hooks_shell.py:L454-L461).

## Save hook: pending-save marker from preCompact

A pending marker file `cursor_<conv>.pending` present in the state directory forces a followup on the very next `stop` for that conversation regardless of the counter, even with `MEMPAL_SAVE_INTERVAL=1000` (far above where the counter would trigger); the response contains `followup_message` and the marker is deleted (consumed on read) (tests/test_cursor_hooks_shell.py:L468-L489). The marker is per-conversation: a marker for `conv-OTHER` does not affect a `stop` for `conv-1` (which takes the counter path and emits `{}`), and the `conv-OTHER` marker is not consumed by `conv-1`'s invocation (tests/test_cursor_hooks_shell.py:L491-L504).

## preCompact hook

The preCompact hook emits a JSON object containing `user_message`, and never `followup_message` nor `decision` (tests/test_cursor_hooks_shell.py:L511-L517). It drops a pending-save marker file `cursor_<conv>.pending` for the conversation (tests/test_cursor_hooks_shell.py:L520-L523). It logs a line to `cursor_hook.log` containing the substrings `event=preCompact`, `conv=<conversation_id>`, and `trigger=<trigger>` (e.g. `trigger=auto`) (tests/test_cursor_hooks_shell.py:L525-L530).

## Wake (sessionStart) hook

The wake hook emits a JSON object containing `additional_context` (tests/test_cursor_hooks_shell.py:L537-L542). That context string references the inferred wing (e.g. `sampleproj`) and the real MCP tool names `mempalace_search` and `mempalace_diary_read`, plus the literal `cursor-ide` (tests/test_cursor_hooks_shell.py:L543-L548). When `workspace_roots` is absent from the payload, wing inference falls back to the `CURSOR_PROJECT_DIR` environment variable (e.g. `/Users/test/envFallback` infers wing `envfallback`) (tests/test_cursor_hooks_shell.py:L550-L572).

## Wing inference: mempal_infer_wing (in common.sh)

The shared function `mempal_infer_wing` is invoked by sourcing `common.sh` and calling the function with the path as a positional argument; its inferred wing is written to stdout (tests/test_cursor_hooks_shell.py:L578-L601). Its contract: the basename of a normal path is used (`/Users/me/myproject` -> `myproject`) (tests/test_cursor_hooks_shell.py:L605-L606); a trailing slash is stripped (`/Users/me/myproject/` -> `myproject`) (tests/test_cursor_hooks_shell.py:L608-L609); the root path `/` falls back to `root` (tests/test_cursor_hooks_shell.py:L611-L612); empty input falls back to `cursor_session` (tests/test_cursor_hooks_shell.py:L614-L615); spaces in the basename are collapsed to underscores (`/Users/me/my project` -> `my_project`) (tests/test_cursor_hooks_shell.py:L617-L618); the basename is lowercased (`/Users/me/MyApp` -> `myapp`) for case-stable wing scoping (tests/test_cursor_hooks_shell.py:L620-L626); and a Windows-style backslash path is handled by basename extraction (`C:\Users\me\MyProj` -> `myproj`) (tests/test_cursor_hooks_shell.py:L628-L634).

## State TTL: mempal_state_ttl_days (in common.sh)

The function `mempal_state_ttl_days` resolves the configured TTL (in days) from `MEMPAL_STATE_TTL_DAYS` and writes it to stdout. Resolution rules: unset/empty resolves to `30`; a non-numeric value `abc` resolves to `30`; a plain number `45` resolves to `45`; leading-zero values are stripped of octal interpretation so `08` -> `8` and `007` -> `7`; and `0` resolves to `0` (tests/test_cursor_hooks_shell.py:L671-L693).

## State garbage collection: mempal_gc_stale_state (in common.sh)

The function `mempal_gc_stale_state` sweeps stale per-conversation state. Files `cursor_<id>.count` and `cursor_<id>.pending` older than the TTL (aged 40 days against the 30-day default) are removed, while recent state of the same naming is preserved (tests/test_cursor_hooks_shell.py:L697-L710). GC must never touch shared logs or other editors' state, even when those files are aged far past the TTL: `cursor_hook.log`, `cursor_last_input.log`, `cursor_last_python_err.log`, any `antigravity_*` state file, and `hook.log` must all survive (tests/test_cursor_hooks_shell.py:L712-L728). A successful sweep creates the marker file `cursor_last_sweep` (tests/test_cursor_hooks_shell.py:L730-L732). GC is throttled to once per 24 hours: if a recent `cursor_last_sweep` marker exists, a subsequent stale file is not swept (tests/test_cursor_hooks_shell.py:L734-L744). GC is gated by the kill switch: a disabled hook (`MEMPAL_DISABLE_HOOK=1`) must neither sweep stale state nor create the `cursor_last_sweep` marker (tests/test_cursor_hooks_shell.py:L746-L762).

## Logging discipline

Log timestamps must be ISO 8601 UTC: a save-hook log line contains both `T` and the substring `Z]` (the `Z` suffix denotes UTC and is locale-independent), guarding against a format that would lose the date or timezone (tests/test_cursor_hooks_shell.py:L768-L775).

## Helper utilities (test-internal)

`_age_file` backdates a file's access and modification times by N days to simulate stale state (tests/test_cursor_hooks_shell.py:L665-L667). `_run_common_snippet` sources `common.sh` and runs an arbitrary bash snippet against a sandboxed `HOME` with `MEMPAL_PYTHON` forced, returning stdout and asserting exit 0 (tests/test_cursor_hooks_shell.py:L640-L662).
