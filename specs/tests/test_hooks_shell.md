# Behavior Spec: `tests/test_hooks_shell.py`

Integration test suite verifying the Python-interpreter resolution contract of the legacy POSIX shell hook scripts (`hooks/mempal_save_hook.sh`, `hooks/mempal_precompact_hook.sh`). The shell hooks perform their own interpreter discovery (unlike the Python `hooks_cli.py`), and these tests pin down the resolution order and crash-safety guarantees (tests/test_hooks_shell.py:L1-L18).

## Platform Gating

The entire suite is skipped when running on a Windows-class operating system (`os.name == "nt"`), because the hook scripts are POSIX/bash-only (tests/test_hooks_shell.py:L36-L36).

## Subjects Under Test

Two hook scripts are located relative to the repository root (two directory levels above this test file): the Stop hook at `hooks/mempal_save_hook.sh` and the PreCompact hook at `hooks/mempal_precompact_hook.sh` (tests/test_hooks_shell.py:L31-L33). The active tests exercise only the save hook; the precompact path is referenced but not invoked by the test bodies (tests/test_hooks_shell.py:L32-L33, L121-L165).

## Fake Python Shim Contract (`_write_fake_python`)

The test harness manufactures a fake `python3` executable used to observe and steer the hook's interpreter selection. The shim is a bash script that (tests/test_hooks_shell.py:L42-L78):

- Records each invocation by appending the shim's own filename (basename) as a line to a marker log file, when a marker file path is supplied. The marker-file mechanism is required (rather than stderr) because the hook redirects some interpreter calls to `2>/dev/null`, making stderr-based observation unreliable (tests/test_hooks_shell.py:L50-L62).
- Simulates a "mempalace not installed in this interpreter" condition when `can_import_mempalace` is False: it exits with status 1 for an invocation of the form `-c <code-containing-"import mempalace">`, and for an invocation of the form `-m mempalace` (tests/test_hooks_shell.py:L63-L72).
- For every other invocation (JSON parsing, heredoc stdin processing, etc.) it delegates transparently to the real interpreter, preserving all arguments (tests/test_hooks_shell.py:L73-L74).
- The created shim file is made executable for user, group, and other (tests/test_hooks_shell.py:L76-L78).

## Hook Invocation Harness (`_run_hook`)

Each hook is run under `bash` with a deliberately minimal, controlled environment to emulate the constrained PATH of a GUI-launched harness on macOS (tests/test_hooks_shell.py:L7-L8, L81-L105):

- The base environment contains only `HOME` (inherited or `/tmp` fallback) and `PATH` (inherited or `/usr/bin:/bin` fallback). No `MEMPAL_*` variables are inherited — the hook starts from a clean slate (tests/test_hooks_shell.py:L89-L93).
- An optional `path_prefix` list is prepended to `PATH` using the platform path separator, ahead of the base `PATH` (tests/test_hooks_shell.py:L94-L95).
- Optional `env_overrides` are applied last and may set or clear variables such as `MEMPAL_PYTHON` and `HOME` (tests/test_hooks_shell.py:L96-L97).
- The hook receives the test's input dictionary serialized as JSON on standard input (tests/test_hooks_shell.py:L98-L104).
- Standard output and standard error are captured as text, and the run is bounded by a 30-second timeout (tests/test_hooks_shell.py:L98-L105).

The standard stdin payload used by these tests is a JSON object with keys `session_id` (string), `stop_hook_active` (boolean), and `transcript_path` (string), e.g. `{"session_id": "abc", "stop_hook_active": false, "transcript_path": ""}` (tests/test_hooks_shell.py:L122-L123).

## Interpreter Resolution Contract

These are the externally observable guarantees the tests assert about the save hook.

### 1. Explicit `MEMPAL_PYTHON` override wins over PATH

When `MEMPAL_PYTHON` is set to an existing, executable interpreter, the hook must use that interpreter in preference to anything found on `PATH`. The test supplies an import-capable, marker-emitting shim via `MEMPAL_PYTHON`, runs the save hook, and asserts the hook exits with return code 0 and that the override shim's name appears in the marker log (i.e. the override was actually invoked) (tests/test_hooks_shell.py:L111-L132).

### 2. Non-executable `MEMPAL_PYTHON` is ignored, not fatal

When `MEMPAL_PYTHON` points to a file that exists but is not executable, the hook must not crash (no "permission denied" failure). It must fall back to PATH-based resolution. The test writes a non-executable file, sets `MEMPAL_PYTHON` to it, and asserts the hook still exits with return code 0 (tests/test_hooks_shell.py:L134-L148).

### 3. Fallback to PATH when `MEMPAL_PYTHON` is unset/empty

When `MEMPAL_PYTHON` is empty, the hook resolves `python3` from `PATH`. The test places an import-capable, marker-emitting shim named exactly `python3` first on `PATH` (via `path_prefix`), runs the save hook with `MEMPAL_PYTHON=""`, and asserts the hook exits with return code 0 and that `python3` appears in the marker log (proving the PATH-resident interpreter was used) (tests/test_hooks_shell.py:L150-L170).

## Crash-Safety Invariant

Across all resolution paths the overarching invariant is that the hook never exits non-zero due to interpreter resolution problems: an unusable override, a missing/non-importable interpreter, or absence of an installed `mempalace` must result in the auto-ingest being logged-and-skipped rather than crashing, so Claude Code's Stop hook does not observe a non-zero exit (tests/test_hooks_shell.py:L9-L17, L126-L128, L146-L148, L166-L166).

## Observable Contracts Summary

- Exit code 0 is required from the save hook in every tested scenario (tests/test_hooks_shell.py:L126, L146, L166).
- Interpreter selection is observable via a marker log file to which each interpreter invocation appends its basename, one per line (tests/test_hooks_shell.py:L59-L62, L129-L132, L167-L170).
- Resolution priority order: explicit executable `MEMPAL_PYTHON` first, then `python3` discovered on `PATH` (tests/test_hooks_shell.py:L9-L11, L111-L170).
