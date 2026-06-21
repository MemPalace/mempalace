# Spec: tests/test_hooks_bash_compat.py

Regression test suite validating that the two shell hook scripts under `hooks/`
remain compatible with macOS stock bash 3.2.57 and that their fail-loud
diagnostic guard behaves correctly. This spec describes the observable
contract the hook scripts MUST satisfy; the test file is the executable
encoding of that contract (tests/test_hooks_bash_compat.py:L1-L15).

## Subjects Under Test

Two hook scripts are exercised, located relative to the repository root:
`hooks/mempal_save_hook.sh` (the "save hook") and
`hooks/mempal_precompact_hook.sh` (the "precompact hook")
(tests/test_hooks_bash_compat.py:L27-L29). Most tests run against both hooks;
parametrization labels them `save_hook` and `precompact_hook`
(tests/test_hooks_bash_compat.py:L34-L38).

## Platform / Skip Contract

The entire suite is skipped on Windows (`os.name == "nt"`) because the hook
scripts are POSIX shell scripts (tests/test_hooks_bash_compat.py:L40).

## Hook Invocation Contract

A hook is invoked by running `bash <hook_path>` with the test's JSON (or raw)
payload supplied on stdin, capturing stdout and stderr as text
(tests/test_hooks_bash_compat.py:L79-L87). The hook runs under a controlled
environment containing only `HOME` (pointed at a temp directory) and `PATH`,
plus any test-supplied extra environment variables
(tests/test_hooks_bash_compat.py:L73-L78). The child process is started with an
ambient umask of `0o022` so that any resulting `0600` file modes are
attributable solely to the hook's own internal `umask 077`, not the runner's
environment (tests/test_hooks_bash_compat.py:L86, L66-L71). A successful run
exits with code 0 by default; a non-matching exit code is a failure
(tests/test_hooks_bash_compat.py:L88-L92). Invocation must complete within 30
seconds (tests/test_hooks_bash_compat.py:L85).

## State Directory & Diagnostic Files

The hooks write all state under `$HOME/.mempalace/hook_state/`
(tests/test_hooks_bash_compat.py:L146, L151, L183). Within that directory the
observable files are:
- `hook.log` — the hook's primary diagnostic log (tests/test_hooks_bash_compat.py:L146-L147).
- `last_input.log` — a bounded dump of the raw stdin payload, written only on parse failure (tests/test_hooks_bash_compat.py:L185, L253).
- `last_python_err.log` — captured stderr of the inline parser, written only on parse failure (tests/test_hooks_bash_compat.py:L307).

## Source-Level Requirements (bash 3.2 compatibility)

When hook source is read with `#`-prefixed comment lines stripped
(tests/test_hooks_bash_compat.py:L43-L46), the following must hold:
- The tokens `mapfile` and `readarray` (bash 4.0-only array builtins) MUST NOT appear in either hook's non-comment source (tests/test_hooks_bash_compat.py:L99-L106).
- Each hook MUST use `sed -n '...'` line extraction at least twice (for the parser sentinel plus the session_id) (tests/test_hooks_bash_compat.py:L108-L120).
- Each hook MUST pass a bash syntax check (`bash -n <hook>` returns 0) (tests/test_hooks_bash_compat.py:L122-L129).

## Happy-Path Session ID Extraction

### Save hook
Given stdin `{"session_id": "abc12345", "stop_hook_active": false, "transcript_path": ""}`
(tests/test_hooks_bash_compat.py:L138-L141):
- Stdout MUST be a JSON object equal to `{}` (the empty object) — no debug output may leak to stdout, since the calling harness parses it (tests/test_hooks_bash_compat.py:L144-L145).
- `hook.log` MUST contain the substring `Session abc12345:`, i.e. the real session_id, never the `unknown` fallback (tests/test_hooks_bash_compat.py:L146-L147).
- `hook.log` MUST NOT contain `WARN: input parse failed` (tests/test_hooks_bash_compat.py:L150).
- Neither `last_input.log` nor `last_python_err.log` may be created on success (tests/test_hooks_bash_compat.py:L151-L155).

### Precompact hook
Given stdin `{"session_id": "abc12345", "transcript_path": ""}`
(tests/test_hooks_bash_compat.py:L160):
- Stdout MUST equal `{}` (tests/test_hooks_bash_compat.py:L163).
- `hook.log` MUST contain `PRE-COMPACT triggered for session abc12345` (tests/test_hooks_bash_compat.py:L164-L165).
- `hook.log` MUST NOT contain `WARN: input parse failed`; neither sidecar dump file may exist (tests/test_hooks_bash_compat.py:L166-L171).

## Fail-Loud Guard Contract (both hooks)

The guard distinguishes a genuine parse failure (detected by a missing parser
"sentinel") from legitimate inputs, and only on genuine failure does it warn and
dump the raw input (tests/test_hooks_bash_compat.py:L174-L178).

### Fires on malformed input
Given non-JSON stdin (`"not-json garbage"`):
- `hook.log` MUST contain `WARN: input parse failed (sentinel missing)` (tests/test_hooks_bash_compat.py:L181-L186).
- `last_input.log` MUST contain the raw input text (`not-json garbage`) (tests/test_hooks_bash_compat.py:L185, L187).

### Does NOT fire on empty stdin
Given empty stdin, no dump file is written; if `hook.log` exists it must not contain `WARN: input parse failed`. This protects the `[ -n "$INPUT" ]` short-circuit (tests/test_hooks_bash_compat.py:L189-L199).

### Does NOT fire on a sanitizer-emptied session_id
Given `{"session_id": "сессия", ...}` (non-ASCII Cyrillic), the session_id is
sanitized to empty and defaults to `unknown`, but the sentinel was still
printed, so the guard skips: `last_input.log` MUST NOT exist
(tests/test_hooks_bash_compat.py:L201-L219).

### Does NOT fire on literal session_id == "unknown"
Given `{"session_id": "unknown", ...}` — a clean parse — the guard MUST skip and
not create `last_input.log` (tests/test_hooks_bash_compat.py:L221-L236).

## Dump Discipline (both hooks)

### Bounded to exactly 4096 bytes, overwrite-not-append
A 4097-byte non-JSON payload yields a `last_input.log` of exactly 4096 bytes —
the cap fires at exactly 4096 (tests/test_hooks_bash_compat.py:L238-L256). A
subsequent parse failure with a smaller payload (`"tiny"`) overwrites the file:
it still exists and its contents equal `tiny` (shrinks, does not append)
(tests/test_hooks_bash_compat.py:L257-L262).

### Byte-based cap under UTF-8 locale
With `LANG=C.UTF-8` and `LC_ALL=C.UTF-8`, a payload of 2000 copies of U+4E2D
(3 bytes each = 6000 bytes UTF-8) still produces a `last_input.log` of exactly
4096 bytes — the cap is byte-based (`head -c`), not character-based
(tests/test_hooks_bash_compat.py:L264-L288).

### Not world-readable
After a failure, `last_input.log` MUST have mode exactly `0600`
(tests/test_hooks_bash_compat.py:L290-L298).

## Inline Parser stderr Capture (both hooks)

On parse failure, the inline parser's stderr MUST be captured to
`last_python_err.log` (tests/test_hooks_bash_compat.py:L300-L308). Its contents
MUST contain either `Traceback` or (case-insensitively) `json`, indicating a
recognizable parser/decode error (tests/test_hooks_bash_compat.py:L309-L315).
On a populated failure write, `last_python_err.log` MUST have mode exactly
`0600` (tests/test_hooks_bash_compat.py:L317-L324).
