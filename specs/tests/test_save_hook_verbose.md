# Spec: tests/test_save_hook_verbose.py

## Purpose

This is a test module that asserts behavioral requirements on the shell hook script `hooks/mempal_save_hook.sh`. It is a TDD specification expressed as tests: the save hook must support a verbose/silent toggle so developers can see diaries and code in chat while regular users get silent background saves (tests/test_save_hook_verbose.py:L1-L6). The tests do not exercise application code directly; instead they read the hook script's source text and assert on string content.

## Test Group: Save hook verbose/silent toggle

A single test class groups two cases asserting the hook has a verbose/silent toggle (tests/test_save_hook_verbose.py:L11-L12).

### Locating the hook script (shared input contract)

Both tests resolve the hook path identically: starting from the directory containing this test file, go up exactly one directory level, then descend into a `hooks` subdirectory and target the file named `mempal_save_hook.sh` (tests/test_save_hook_verbose.py:L16-L20, tests/test_save_hook_verbose.py:L32-L36). This implies a repository layout where `tests/` and `hooks/` are sibling directories under a common parent, and `hooks/mempal_save_hook.sh` must exist and be readable as text (tests/test_save_hook_verbose.py:L21, tests/test_save_hook_verbose.py:L37). If the file is absent or unreadable, the read fails before any assertion.

### Case 1: Hook checks a verbose flag

The test reads the entire hook script as a string (tests/test_save_hook_verbose.py:L21) and passes only if the source text contains at least one of the literal substrings `VERBOSE`, `verbose`, `SILENT`, or `silent` (case-sensitive substring match, any one suffices) (tests/test_save_hook_verbose.py:L22). The behavioral contract: the hook must read a `MEMPAL_VERBOSE` or similar flag (tests/test_save_hook_verbose.py:L14-L15). On failure, the assertion message states the hook has no verbose/silent toggle and prescribes adding a `MEMPAL_VERBOSE` flag: when true the hook blocks and asks the agent to write; when false it saves silently (tests/test_save_hook_verbose.py:L23-L28).

### Case 2: Verbose mode produces two decision paths

The test reads the hook script source (tests/test_save_hook_verbose.py:L37) and checks for the presence of two distinct decision outputs. A "block" path is present if the source contains either `"decision": "block"` or `'decision': 'block'` (tests/test_save_hook_verbose.py:L39). An "allow" path is present if the source contains either `"decision": "allow"` or `'decision': 'allow'` (tests/test_save_hook_verbose.py:L40). The test passes only if BOTH the block path and the allow path are present (tests/test_save_hook_verbose.py:L41). The observable contract is that the hook emits a `decision` field whose value is `block` in verbose/developer mode (so the agent writes the diary visibly in chat) and `allow` in silent mode (tests/test_save_hook_verbose.py:L30-L31, tests/test_save_hook_verbose.py:L38). On failure, the assertion message explains both `block` (verbose/developer) and `allow` (silent) paths are required and reports which of the two were found (tests/test_save_hook_verbose.py:L41-L44).

## Side effects and invariants

The only side effect is reading the hook file from disk; no files are written, no network or process interaction occurs (tests/test_save_hook_verbose.py:L21, tests/test_save_hook_verbose.py:L37). The tests are pure assertions over file text and have no ordering dependency between them (tests/test_save_hook_verbose.py:L14-L28, tests/test_save_hook_verbose.py:L30-L44). Both checks are content-presence checks only; they do not verify the hook actually selects between paths at runtime, only that both string forms appear somewhere in the script.
