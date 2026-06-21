# Behavior Spec: Antigravity Hook Shell Scripts (test surface)

This document specifies the externally observable behavior of two Antigravity
hook shell scripts and their shared library, as asserted by the end-to-end
shell tests. The tests invoke the bash scripts directly via subprocess with
synthetic stdin JSON and assert on stdout, exit code, and state-directory side
effects (tests/test_antigravity_hooks_shell.py:L1-L18).

The artifacts under test are three shell files located relative to the
repository root: `hooks/antigravity/mempal_save_hook_antigravity.sh` (the Stop
event hook), `hooks/antigravity/mempal_wake_hook_antigravity.sh` (the
PreInvocation event hook), and `hooks/antigravity/lib/common.sh` (the shared
library) (tests/test_antigravity_hooks_shell.py:L30-L34).

## Platform Applicability

The entire suite is skipped on Windows (`os.name == "nt"`); the Antigravity
shell hooks require bash 3.2+, and Windows uses a separate code path
(tests/test_antigravity_hooks_shell.py:L36-L40). The soft performance-budget
test additionally skips on `win32` (tests/test_antigravity_hooks_shell.py:L907-L907).

## Invocation Contract & Environment

Hooks are invoked as `bash <script>` with the synthetic payload supplied on
stdin; a dict payload is serialized to JSON text, a string payload is passed
verbatim (tests/test_antigravity_hooks_shell.py:L43-L75). Each invocation runs
in a hermetic environment: `HOME` and `MEMPAL_STATE_DIR` are overridden to
temp directories so no test touches the real `~/.mempalace/hook_state/`, and
both directories are created beforehand (tests/test_antigravity_hooks_shell.py:L56-L61).
The kill-switch / interval environment variables `MEMPAL_DISABLE_HOOK`,
`MEMPALACE_HOOKS_AUTO_SAVE`, and `MEMPAL_SAVE_INTERVAL` are removed from the
inherited environment before each run unless explicitly supplied
(tests/test_antigravity_hooks_shell.py:L62-L67). A default subprocess timeout of
10 seconds applies (tests/test_antigravity_hooks_shell.py:L49-L74).

The palace-existence kill switch keys off `$HOME/.mempalace/` existing; tests
create that directory to make the kill switch pass
(tests/test_antigravity_hooks_shell.py:L78-L80).

## Stop Payload Shape

The Stop (save) event payload contains: `executionNum` (integer),
`terminationReason` (string), `error` (string), `fullyIdle` (boolean),
`conversationId` (string), `workspacePaths` (array of path strings),
`transcriptPath` (string), and `artifactDirectoryPath` (string)
(tests/test_antigravity_hooks_shell.py:L103-L115).

## PreInvocation (Wake) Payload Shape

The wake event payload contains: `invocationNum` (integer), `initialNumSteps`
(integer), `conversationId` (string), `workspacePaths` (array of path
strings), `transcriptPath` (string), and `artifactDirectoryPath` (string)
(tests/test_antigravity_hooks_shell.py:L118-L128).

## Syntax Invariant

All three shell files (save hook, wake hook, common library) must parse
cleanly under `bash -n`, returning exit code 0
(tests/test_antigravity_hooks_shell.py:L134-L143).

## Save Hook: Output Contract

On every path tested, the save hook exits 0 and emits the JSON object `{}` on
stdout (after trimming whitespace) when it short-circuits or defers
(tests/test_antigravity_hooks_shell.py:L161-L162). The save hook must NEVER emit
`{"decision":"continue"}`; the assertion parses stdout as JSON and requires the
`decision` key to differ from `"continue"`, because that output would force
Antigravity into an infinite agent re-execution loop
(tests/test_antigravity_hooks_shell.py:L264-L284).

## Save Hook: Kill Switches / Short-Circuits

The save hook silently emits `{}` and exits 0 under each of the following
conditions, in addition to the normal counter-not-yet-triggered case:

- `MEMPAL_DISABLE_HOOK=1` (tests/test_antigravity_hooks_shell.py:L149-L162).
- `MEMPALACE_HOOKS_AUTO_SAVE=false` (tests/test_antigravity_hooks_shell.py:L165-L178).
- `$HOME/.mempalace/` directory missing — the strongest kill switch
  (tests/test_antigravity_hooks_shell.py:L181-L189).
- `~/.mempalace/config.json` with `{"hooks": {"auto_save": false}}`
  (tests/test_antigravity_hooks_shell.py:L192-L203).
- `terminationReason == "error"` skips the save
  (tests/test_antigravity_hooks_shell.py:L224-L236).

When `fullyIdle` is `false`, the save is deferred and nothing is written to
state: specifically, the counter file `antigravity_save_count_<conversationId>`
must NOT exist because the deferral happens before the counter is incremented
(tests/test_antigravity_hooks_shell.py:L206-L221).

## Save Hook: Malformed / Empty Input (Fail-Open)

Malformed JSON on stdin (e.g. `{not even close to json{`) must not crash the
hook; it emits `{}` and exits 0 (tests/test_antigravity_hooks_shell.py:L239-L251).
Empty stdin likewise emits `{}` and exits 0
(tests/test_antigravity_hooks_shell.py:L254-L261).

## Parser Sentinel Contract (`mempal_parse_stdin`)

The shared `mempal_parse_stdin` function, when sourced from `common.sh` and
called with malformed JSON, must NOT print the success sentinel
`__MEMPAL_PARSE_OK__` on stdout. Bash callers detect parse failure by checking
whether line 1 of the parser output is exactly that sentinel; if the inner
parser caught the error and fell back to an empty object while still printing
the sentinel, the bash-side defense-in-depth branch would never engage. The
function itself does not error even though its inner parser crashes
(tests/test_antigravity_hooks_shell.py:L380-L412).

## Save Hook: Counter Semantics

Each Stop fire increments a per-conversation counter stored at
`<MEMPAL_STATE_DIR>/antigravity_save_count_<conversationId>` as a decimal
integer text value. Across three consecutive fires (with a high
`MEMPAL_SAVE_INTERVAL` to avoid triggering a save) the file holds `1`, then
`2`, then `3` (tests/test_antigravity_hooks_shell.py:L287-L305).

The counter is written atomically via the helper
`mempal_write_counter_atomic "$COUNTER_FILE" "$COUNT"`, not via a bare
truncating redirect `printf '%s' "$COUNT" > "$COUNTER_FILE"`
(tests/test_antigravity_hooks_shell.py:L583-L589). The atomic helper
`mempal_write_counter_atomic()` in `common.sh` creates a temp file via
`mktemp` and promotes it with `mv -f`
(tests/test_antigravity_hooks_shell.py:L614-L619). After repeated fires, no
leftover temp files remain in the state directory — no name containing
`.XXXXXX` and no name beginning with the counter file name plus a dot
(tests/test_antigravity_hooks_shell.py:L605-L611).

## Save Interval Validation (`mempal_save_interval`)

`MEMPAL_SAVE_INTERVAL` must be sanitized so the modulo gate never crashes:

- Value `0` is floored so `count % 0` never occurs; the hook does not crash and
  emits `{}` (tests/test_antigravity_hooks_shell.py:L308-L325).
- A negative value (e.g. `-5`) falls back to the default without crashing
  (tests/test_antigravity_hooks_shell.py:L328-L341).
- Values with leading zeros (`08`, `09`, `008`, `0099`) must have leading zeros
  stripped before arithmetic so bash never interprets them as octal; the hook
  must not crash and stderr must not contain `value too great for base`
  (tests/test_antigravity_hooks_shell.py:L344-L377).

## Save Hook: Python Interpreter Invocation Contract

The save hook source MUST invoke mempalace via `"$MEMPAL_PYTHON_BIN" -m
mempalace`, not the bare `mempalace` console script, and must not contain a
bare `nohup mempalace ` invocation
(tests/test_antigravity_hooks_shell.py:L464-L483). When the resolved interpreter
cannot run `-m mempalace` (a stub interpreter that exits non-zero for any
`-m mempalace` invocation), the hook fails open: it exits 0, emits `{}`, and a
background-written log line containing `is not runnable via` appears in
`<MEMPAL_STATE_DIR>/antigravity_hook.log`
(tests/test_antigravity_hooks_shell.py:L415-L461).

## Save Hook: Backgrounding Structure & Foreground Latency

The probe (`--version` runnability check), the mine, and the pending-marker
cleanup all live in ONE detached background subshell. The source must NOT
contain a sibling `wait "$MINE_PID"`, must NOT contain `kill -0` (the retired
watcher), must still run the probe via `"$MEMPAL_PYTHON_BIN" -m mempalace
--version`, must wrap the block with the detach pattern `) >/dev/null 2>&1 <
/dev/null &`, and must NOT capture `MINE_PID=$!`
(tests/test_antigravity_hooks_shell.py:L486-L524).

Because the probe is backgrounded, the foreground returns immediately even when
`--version` is slow: with a stub interpreter that sleeps 3 seconds on any
`-m mempalace` call, the hook still exits 0, emits `{}`, and returns in under
2 seconds (tests/test_antigravity_hooks_shell.py:L527-L570). Background-written
log lines must be polled (the log file may not contain a line until the
detached subshell flushes), with a default poll timeout of 5 seconds
(tests/test_antigravity_hooks_shell.py:L83-L100).

## Save Hook: Transcript Path Validation

A `transcriptPath` containing a `..` traversal segment (e.g.
`/legit/../etc/passwd`) is rejected even when the interval gate would otherwise
fire (`MEMPAL_SAVE_INTERVAL=1`): the hook exits 0, emits `{}`, and the log file
contains either `invalid transcriptPath rejected` or `does not exist`
(tests/test_antigravity_hooks_shell.py:L641-L660). A `transcriptPath` whose
extension is not `.json` or `.jsonl` (e.g. `/tmp/transcript.txt`) is rejected:
the hook exits 0 and emits `{}`
(tests/test_antigravity_hooks_shell.py:L663-L676).

## State File Namespacing

Every state file the save hook creates begins with the prefix `antigravity_`;
no other-named files appear in the state directory after a run
(tests/test_antigravity_hooks_shell.py:L679-L697). The same namespacing
invariant holds for the wake hook
(tests/test_antigravity_hooks_shell.py:L838-L846). This prevents collisions in
a state directory shared with other tools (Claude Code, Codex, future Cursor)
(tests/test_antigravity_hooks_shell.py:L681-L684).

## Save Hook: Concurrency Guard (Pending Marker)

A fresh pending marker file `antigravity_pending_<conversationId>` causes the
next save to skip even when the interval gate fires (`MEMPAL_SAVE_INTERVAL=1`):
the hook exits 0, emits `{}`, and the log contains `pending save still in
flight` (tests/test_antigravity_hooks_shell.py:L700-L719).

## Wake Hook: Output & Gating Contract

Under `MEMPAL_DISABLE_HOOK=1`, the wake hook is silenced: exit 0, emit `{}`
(tests/test_antigravity_hooks_shell.py:L725-L738). Only `invocationNum == 1`
triggers injection; for `invocationNum` in {0, 2, 5, 100} the hook emits `{}`
(tests/test_antigravity_hooks_shell.py:L741-L756).

A loop guard prevents repeat injection: if the marker directory
`antigravity_woke_<conversationId>` already exists, a second fire for the same
`conversationId` skips, emits `{}`, and logs `already woke this conversation`
(tests/test_antigravity_hooks_shell.py:L759-L777). The guard is a directory
(created via mkdir), not a file (tests/test_antigravity_hooks_shell.py:L765-L767).

The wake hook must never emit a `decision` key (that field is Stop-only);
stdout is parsed as JSON and the `decision` key must be absent
(tests/test_antigravity_hooks_shell.py:L780-L796).

When mempalace cannot be run (PATH stripped to `/usr/bin:/bin` and
`MEMPAL_PYTHON=""` so the inner `python3 -m mempalace` fails), the wake hook
degrades gracefully: exit 0, emit `{}`, never surface a stack trace
(tests/test_antigravity_hooks_shell.py:L799-L835).

## Wake Hook: Python Invocation Contract

The wake hook's inner Python must invoke mempalace via `sys.executable, '-m',
'mempalace'` and must NOT contain a bare `['mempalace', 'wake-up'` invocation,
so the call binds to the interpreter that resolved `MEMPAL_PYTHON`
(tests/test_antigravity_hooks_shell.py:L622-L638).

## Wing Inference

The wing is derived from `workspacePaths[0]`'s leaf directory name; the first
array element is canonical. Hyphens become underscores and the name is
lowercased, prefixed with `wing_`. For a leaf `myproj-with-dashes` the log
records `wing=wing_myproj_with_dashes`
(tests/test_antigravity_hooks_shell.py:L852-L879). An empty `workspacePaths`
array yields `wing=wing_sessions`
(tests/test_antigravity_hooks_shell.py:L882-L901).

## Performance Budget (Soft)

Under the kill switch (`MEMPAL_DISABLE_HOOK=1`), the save hook returns in under
1.5 seconds (a generous CI allowance over the <500ms target). A regression here
indicates a synchronous import or DB connection happening before the
kill-switch short-circuit (tests/test_antigravity_hooks_shell.py:L907-L936).

## State-File Garbage Collection (`mempal_gc_stale_state`)

The GC sweep removes stale per-conversation state older than the TTL. The three
swept shapes are `antigravity_save_count_*`, `antigravity_pending_*`, and
`antigravity_woke_*` (the last being a directory). Artifacts backdated 40 days
are removed; a fresh `antigravity_save_count_*` (default age) survives; and the
protected `antigravity_hook.log` is never swept even when backdated 99 days —
the name glob must not be broad enough to match it
(tests/test_antigravity_hooks_shell.py:L950-L996). Backdating is performed by
setting atime/mtime into the past via file timestamps
(tests/test_antigravity_hooks_shell.py:L942-L947).

The sweep is throttled to once per day via a marker file
`antigravity_last_sweep`: a fresh marker (under 24h old) skips the sweep so a
40-day-old stale counter survives (tests/test_antigravity_hooks_shell.py:L999-L1026);
a marker backdated 2 days (over 24h) lets the sweep run and the stale counter is
removed (tests/test_antigravity_hooks_shell.py:L1029-L1053).

The save hook wires in `mempal_gc_stale_state` (the call appears in the hook
source) and creates the `antigravity_last_sweep` throttle marker on a normal
run (tests/test_antigravity_hooks_shell.py:L1085-L1103). Under the kill switch,
GC does not run and the `antigravity_last_sweep` marker is not created, because
the hook returns before GC (tests/test_antigravity_hooks_shell.py:L1106-L1123).

## TTL Validation (`mempal_state_ttl_days`)

`mempal_state_ttl_days` validates `MEMPAL_STATE_TTL_DAYS` like the save
interval. It prints `30` when the variable is unset, empty, or garbage (e.g.
`abc`); prints `7` for `7`; strips leading zeros so `007` yields `7`; and `0`
yields `0`. Stripping prevents `find -mtime +N` from ever seeing octal-ish
tokens (tests/test_antigravity_hooks_shell.py:L1056-L1082).

## Python Interpreter Resolution (`mempal_resolve_python` / `MEMPAL_PYTHON_BIN`)

Sourcing `common.sh` sets `MEMPAL_PYTHON_BIN`, the interpreter the hooks run as
`"$MEMPAL_PYTHON_BIN" -m mempalace`
(tests/test_antigravity_hooks_shell.py:L1126-L1148). Resolution rules:

- With `MEMPAL_PYTHON` unset, the resolver reads the shebang of a `mempalace-mcp`
  console script found on PATH and returns that interpreter, simulating an
  isolated-env install where the script's interpreter is not the system python3
  (tests/test_antigravity_hooks_shell.py:L1167-L1188).
- The shebang-derived interpreter wins over a decoy `python3` earlier on PATH
  (tests/test_antigravity_hooks_shell.py:L1191-L1211).
- An explicit `MEMPAL_PYTHON` override always wins over shebang derivation
  (tests/test_antigravity_hooks_shell.py:L1214-L1231).
- A `#!/usr/bin/env python3` style shebang is rejected (the first token
  `/usr/bin/env` is not a Python interpreter); the resolver falls through to a
  `python3` on PATH whose basename starts with `python`
  (tests/test_antigravity_hooks_shell.py:L1234-L1254).
- A shebang interpreter that is missing or non-executable is skipped; the
  resolver falls through to a `python3` on PATH
  (tests/test_antigravity_hooks_shell.py:L1257-L1279).
- With no mempalace console scripts on PATH, the resolver falls back to a
  `python3` on PATH (prior behavior); the resolved basename starts with
  `python` (tests/test_antigravity_hooks_shell.py:L1282-L1295).

Resolution helpers in the test construct fake interpreters (executable files
whose basename looks like Python) and fake console scripts (a file with the
given shebang interpreter), both made executable
(tests/test_antigravity_hooks_shell.py:L1151-L1164).
