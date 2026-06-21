# Spec: Claude Plugin Hook Wrapper Scripts (execution behavior)

This is a test module that exercises the runtime behavior of two shell wrapper
scripts shipped under `.claude-plugin/hooks/`
(`tests/test_claude_plugin_hook_wrappers.py:L1-L1`). The behavioral contract
described below belongs to those wrapper scripts; the tests are the observable
specification of what those scripts must do. An implementer porting the wrappers
to any language/shell must satisfy every claim here.

## Scope and applicability

- Two wrapper scripts are under test, each mapped to a logical hook name: the
  script `mempal-stop-hook.sh` corresponds to hook name `stop`, and
  `mempal-precompact-hook.sh` corresponds to hook name `precompact`
  (`tests/test_claude_plugin_hook_wrappers.py:L19-L22`). Both scripts live in the
  directory `.claude-plugin/hooks/` relative to the repository root
  (`tests/test_claude_plugin_hook_wrappers.py:L10-L11`).
- The tests require a `bash` executable to be present; when `bash` cannot be
  located the entire module is skipped
  (`tests/test_claude_plugin_hook_wrappers.py:L12-L17`).
- Every test case is parametrized over both scripts (the `stop` and `precompact`
  variants), so each contract below applies identically to both scripts
  (`tests/test_claude_plugin_hook_wrappers.py:L72-L75`,
  `tests/test_claude_plugin_hook_wrappers.py:L105-L109`,
  `tests/test_claude_plugin_hook_wrappers.py:L136-L139`,
  `tests/test_claude_plugin_hook_wrappers.py:L150-L153`).

## Invocation contract

Each wrapper script is executed via `bash`, with the hook JSON payload supplied
on standard input as text, and the process working directory set to the
repository root (`tests/test_claude_plugin_hook_wrappers.py:L52-L69`). The runner
under test discovery is driven entirely by the `PATH` environment variable: the
test replaces `PATH` with a single directory holding stub executables, so the
wrapper must resolve its runner (`mempalace`, `python3`, or `python`) by searching
`PATH` (`tests/test_claude_plugin_hook_wrappers.py:L59-L69`,
`tests/test_claude_plugin_hook_wrappers.py:L34-L39`).

The wrapper invokes its chosen runner with a fixed argument vector and forwards the
script's standard input verbatim to that runner (verified by stubs that capture
both their argument string `$*` and their stdin)
(`tests/test_claude_plugin_hook_wrappers.py:L42-L49`,
`tests/test_claude_plugin_hook_wrappers.py:L82-L90`).

## Runner resolution order and argument contract

The wrapper resolves which runner to invoke in a strict preference order.

1. **Prefer the `mempalace` CLI.** If a `mempalace` executable is found on `PATH`,
   the wrapper invokes it (in preference to any `python`/`python3` on `PATH`,
   which the test makes fail with exit code 99 to prove they are not used)
   (`tests/test_claude_plugin_hook_wrappers.py:L79-L91`). When the `mempalace` CLI
   is used, it is invoked with exactly the argument string
   `hook run --hook <hook_name> --harness claude-code`, where `<hook_name>` is
   `stop` or `precompact` per the script under test
   (`tests/test_claude_plugin_hook_wrappers.py:L98-L101`).

2. **Fall back to a runnable Python module.** If no `mempalace` CLI is available
   but a Python interpreter (`python3` or `python`) is on `PATH` and can import
   the `mempalace` module, the wrapper invokes that interpreter to run the module.
   The selected interpreter is invoked with exactly the argument string
   `-m mempalace hook run --hook <hook_name> --harness claude-code`
   (`tests/test_claude_plugin_hook_wrappers.py:L105-L132`). This holds whether the
   only available interpreter is named `python3` or `python` (both names are
   tested) (`tests/test_claude_plugin_hook_wrappers.py:L106-L122`).

### Import probe before using a Python interpreter

Before committing to a Python interpreter, the wrapper probes whether that
interpreter can import the `mempalace` module by invoking it with a first
argument of `-c` (an inline-code import check). The test stubs distinguish the
probe call (`$1 == "-c"`) from the real run call
(`tests/test_claude_plugin_hook_wrappers.py:L113-L121`,
`tests/test_claude_plugin_hook_wrappers.py:L161-L178`):

- The probe invocation must be made with `-c` as the first argument
  (`tests/test_claude_plugin_hook_wrappers.py:L115-L116`).
- A probe exit code of `0` means the interpreter can import `mempalace` and is
  eligible to be used (`tests/test_claude_plugin_hook_wrappers.py:L114-L121`,
  `tests/test_claude_plugin_hook_wrappers.py:L170-L178`).
- A non-zero probe exit code means that interpreter is rejected and must not be
  used to run the module (`tests/test_claude_plugin_hook_wrappers.py:L162-L169`).

### `python3` preferred over `python`, with import-aware fallback

When both `python3` and `python` are present, `python3` is probed first. If the
`python3` import probe fails (probe exits non-zero), the wrapper must not use
`python3` to run the module at all — it must fall through to `python`, which (if
its probe succeeds) runs the module with
`-m mempalace hook run --hook <hook_name> --harness claude-code`
(`tests/test_claude_plugin_hook_wrappers.py:L150-L191`). The test additionally
proves the failed interpreter is never invoked for the actual run: the rejected
`python3` stub would write a marker file `bad_python3_used.txt` if its non-probe
branch ran, and that file must not exist after a successful fallback
(`tests/test_claude_plugin_hook_wrappers.py:L156-L166`,
`tests/test_claude_plugin_hook_wrappers.py:L192-L192`).

## Standard input forwarding

The wrapper reads the full hook payload from its own standard input and passes it
unchanged to the chosen runner. The runner stub captures stdin line by line
(preserving content even when the final line lacks a trailing newline) and the
captured bytes must equal the original payload exactly
(`tests/test_claude_plugin_hook_wrappers.py:L42-L49`,
`tests/test_claude_plugin_hook_wrappers.py:L93-L102`,
`tests/test_claude_plugin_hook_wrappers.py:L124-L133`,
`tests/test_claude_plugin_hook_wrappers.py:L182-L191`). Example payloads are
single-line JSON objects such as `{"session_id":"abc123"}`
(`tests/test_claude_plugin_hook_wrappers.py:L93-L93`).

## Standard output passthrough and exit codes

On success the wrapper exits with status code `0` and emits exactly the runner's
standard output, which in the tests is the two-character string `{}` followed by a
newline (`{}\n`) (`tests/test_claude_plugin_hook_wrappers.py:L96-L97`,
`tests/test_claude_plugin_hook_wrappers.py:L127-L128`,
`tests/test_claude_plugin_hook_wrappers.py:L185-L186`). The runner's stdout is
passed through to the wrapper's stdout without modification.

## Error behavior when no runner exists

If no `mempalace` CLI and no usable Python interpreter can be found on `PATH`
(the `PATH` directory is empty), the wrapper must fail: it exits with a non-zero
status code, produces empty standard output, and writes a diagnostic message to
standard error containing the substring
`could not find a runnable mempalace command or module`
(`tests/test_claude_plugin_hook_wrappers.py:L136-L147`).

## Test helper / harness behavior (observable contract of the test fixtures)

- Stub executables are created with file mode `0o755` (executable)
  (`tests/test_claude_plugin_hook_wrappers.py:L29-L39`).
- A dedicated `bin/` directory inside a temporary path holds all stub
  executables, and that directory alone becomes the process `PATH`, isolating the
  wrapper from the real system runners
  (`tests/test_claude_plugin_hook_wrappers.py:L34-L39`,
  `tests/test_claude_plugin_hook_wrappers.py:L59-L60`).
