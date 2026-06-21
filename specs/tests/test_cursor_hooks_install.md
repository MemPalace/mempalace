# Spec: Cursor Hooks Installer Contract Tests

These are contract tests for `hooks/cursor/install.sh`. They define the observable
behavior of that installer script. The spec below describes the behavior the
installer MUST exhibit, as asserted by the tests. Each claim cites the test that
encodes the contract.

## Test environment / system under test

The system under test is the script located at `<repo-root>/hooks/cursor/install.sh`,
where `<repo-root>` is the parent of the parent of the test file's directory
(tests/test_cursor_hooks_install.py:L26-L27). The installer is POSIX-only; the
entire test module is skipped when running on Windows (tests/test_cursor_hooks_install.py:L29-L29).

## Invocation contract

The installer is invoked as a shell program. Standard test invocations pass
`--scope project --target <target>` plus any additional flags
(tests/test_cursor_hooks_install.py:L55-L63). The process is run with a 30-second
timeout (tests/test_cursor_hooks_install.py:L64-L70).

The installer reads environment variables:
- `HOME` — used to locate the default install directory under
  `~/.mempalace/hooks/cursor/`; tests force a sandboxed HOME so defaults do not
  touch the real home (tests/test_cursor_hooks_install.py:L46-L51).
- `PATH` — standard process path (tests/test_cursor_hooks_install.py:L52-L52).
- `MEMPAL_PYTHON` — points the installer at a specific interpreter for its JSON
  merge step, so the merge runs even when PATH lacks `python3`
  (tests/test_cursor_hooks_install.py:L42-L53).

The installer's exit code is the primary success/failure signal: a successful run
exits 0, and tests assert on specific non-zero codes for error cases
(tests/test_cursor_hooks_install.py:L39-L75).

## On-disk target contract

The target hooks file is located at `<target>/.cursor/hooks.json`
(tests/test_cursor_hooks_install.py:L78-L79). The file is a JSON document with an
integer `version` field equal to `1` and an object `hooks` field
(tests/test_cursor_hooks_install.py:L141-L143). The `hooks` object maps event names
(e.g. `stop`, `sessionStart`, `preCompact`, `afterFileEdit`, `beforeShellExecution`)
to arrays of entry objects; each entry object has at least a `command` field holding
a string path, and may carry additional fields such as `loop_limit`
(tests/test_cursor_hooks_install.py:L189-L196, tests/test_cursor_hooks_install.py:L338-L342).

## Help and argument validation

### `--help`
Running with `--help` exits 0 and prints to stdout a usage description that mentions
each of the flags `--scope`, `--target`, `--variant`, `--dry-run`, and `--uninstall`
(tests/test_cursor_hooks_install.py:L102-L115).

### Syntax
The script must be syntactically valid as a shell script (`bash -n` exits 0)
(tests/test_cursor_hooks_install.py:L93-L99).

### Unknown flag
An unrecognized flag (e.g. `--bogus-flag`) causes a non-zero exit
(tests/test_cursor_hooks_install.py:L118-L129).

### Invalid `--scope`
A `--scope` value that is not a recognized scope causes a non-zero exit and writes
the word "scope" (case-insensitive) to stderr
(tests/test_cursor_hooks_install.py:L371-L383).

### Invalid `--variant`
A `--variant` value that is not recognized causes a non-zero exit and writes the
word "variant" (case-insensitive) to stderr
(tests/test_cursor_hooks_install.py:L386-L398).

## Variant contract

Two variants exist, selected via `--variant`:

- The default variant (no `--variant` flag) wires at least the events
  `sessionStart`, `stop`, and `preCompact` into the `hooks` object
  (tests/test_cursor_hooks_install.py:L158-L161).
- The `minimal` variant wires only the `stop` event. The `sessionStart` and
  `preCompact` events MUST NOT be present (unless already present in a seed file)
  (tests/test_cursor_hooks_install.py:L163-L177).

The MemPalace stop-hook command always references a script path whose basename is
`mempal_save_hook_cursor.sh` (tests/test_cursor_hooks_install.py:L228-L230).

## `--dry-run` contract

When `--dry-run` is passed:
- The target file `<target>/.cursor/hooks.json` MUST NOT be written
  (tests/test_cursor_hooks_install.py:L136-L138).
- The merged configuration MUST still be printed to stdout as valid JSON
  containing `version == 1` and a `hooks` key, so the user can review it
  (tests/test_cursor_hooks_install.py:L139-L143).
- No hook scripts may be copied to the install directory; the install directory
  MUST NOT be created (tests/test_cursor_hooks_install.py:L145-L156).

## Merge-preservation contract (install)

When merging MemPalace entries into an existing `hooks.json`, the installer MUST NOT
disturb unrelated configuration:

- Entries under unrelated events (e.g. `afterFileEdit`, `beforeShellExecution`) are
  preserved exactly (tests/test_cursor_hooks_install.py:L184-L206).
- Existing entries under an event MemPalace also touches (e.g. a user's own `stop`
  hook) are preserved; MemPalace's entry is added alongside, not in place of. After
  install the `stop` array contains both the user's entry and a MemPalace entry
  whose command contains `mempal_save_hook_cursor.sh`
  (tests/test_cursor_hooks_install.py:L208-L230).
- If `<target>/.cursor/` does not yet exist, the installer creates the directory and
  the `hooks.json` file, and the resulting config contains the `stop` event
  (tests/test_cursor_hooks_install.py:L232-L239).

### Malformed existing file
If the existing `hooks.json` is not valid JSON, the merge step fails with exit code
`2`, the file is left byte-for-byte unchanged, and stderr contains either the
substring "not valid JSON" or "Refusing to overwrite"
(tests/test_cursor_hooks_install.py:L241-L250).

## Idempotency contract

Running install twice produces an identical config file; the second run's parsed
config equals the first run's parsed config
(tests/test_cursor_hooks_install.py:L257-L265). Specifically, the `stop` array
contains exactly one entry whose command contains `mempal_save_hook_cursor.sh`
after re-running; the MemPalace entry is never duplicated
(tests/test_cursor_hooks_install.py:L266-L271).

## `--uninstall` contract

When `--uninstall` is passed:

- Only MemPalace entries are removed. A user's own entry on a shared event (e.g.
  their `stop` hook command `/usr/local/bin/my-stop-hook.sh`) remains, while the
  MemPalace entry is removed (tests/test_cursor_hooks_install.py:L278-L300).
- Unrelated events (e.g. `afterFileEdit`) are left untouched
  (tests/test_cursor_hooks_install.py:L301-L304).
- Events that were only ever wired by MemPalace (e.g. `sessionStart`, `preCompact`)
  are removed entirely from the `hooks` object rather than left as empty arrays
  (tests/test_cursor_hooks_install.py:L305-L309).
- If after uninstall no user hooks remain (the config would be effectively empty,
  i.e. `{"version": 1, "hooks": {}}`), the `hooks.json` file is removed entirely
  rather than left as an empty config (tests/test_cursor_hooks_install.py:L311-L320).
- If the target `hooks.json` does not exist, uninstall must not crash and must not
  create the file (tests/test_cursor_hooks_install.py:L322-L328).
- With both `--uninstall` and `--dry-run`, the target file MUST NOT be mutated; its
  contents before and after are identical (tests/test_cursor_hooks_install.py:L330-L351).

## Scope contract

With `--scope project --target <target>`, the file is written at
`<target>/.cursor/hooks.json` and nowhere else; in particular it MUST NOT be written
into `$HOME/.cursor/hooks.json` (tests/test_cursor_hooks_install.py:L357-L368).

## `--install-dir` path resolution contract

The `--install-dir` value determines the directory baked into the hook command paths
written into `hooks.json`.

- A relative `--install-dir` MUST be resolved to an absolute path against the
  process's current working directory before being written into `hooks.json`. The
  resulting `stop` command begins with `/` and starts with `<cwd>/<install-dir>`.
  The installer creates the directory itself; it need not pre-exist
  (tests/test_cursor_hooks_install.py:L412-L452).
- An absolute `--install-dir` is preserved verbatim; the resulting `stop` command
  starts with the given absolute path unchanged
  (tests/test_cursor_hooks_install.py:L454-L469).

## Helper seeding format (test fixtures)

Tests seed an existing config by writing a JSON object (with `version` and `hooks`)
to `<target>/.cursor/hooks.json`, creating the `.cursor` directory as needed
(tests/test_cursor_hooks_install.py:L82-L87). This confirms the on-disk format the
installer reads is the same JSON object described above.
