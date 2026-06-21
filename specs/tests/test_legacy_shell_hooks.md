# Spec: `tests/test_legacy_shell_hooks.py`

## Purpose

This is a test module that asserts the on-disk content of the two legacy shell
hook scripts shipped in the repository's `hooks/` directory. It verifies that
those scripts delegate parsing logic to a shared module rather than embedding
inline parsing logic, and that specific error message strings are present
(`tests/test_legacy_shell_hooks.py:L11-L27`).

## Resolution of hook file paths

The test suite locates hook files relative to the repository root. The root is
defined as the directory two levels above this test file (the parent of the
directory containing the test file) (`tests/test_legacy_shell_hooks.py:L4`). A
helper reads a named hook file from the `hooks/` subdirectory of the root,
decoding its contents as UTF-8 text (`tests/test_legacy_shell_hooks.py:L7-L8`).
The hook file must exist and be readable as UTF-8; otherwise reading fails.

## Contract: `mempal_save_hook.sh`

The save hook script (`hooks/mempal_save_hook.sh`) MUST satisfy all of the
following observable content constraints (`tests/test_legacy_shell_hooks.py:L11-L18`):

- It MUST invoke the shared parser module with the `parse-stop` subcommand,
  containing the substring `-m mempalace.hook_shell parse-stop`
  (`tests/test_legacy_shell_hooks.py:L14`).
- It MUST invoke the shared module with the `count-human-messages` subcommand,
  containing the substring `-m mempalace.hook_shell count-human-messages`
  (`tests/test_legacy_shell_hooks.py:L15`).
- It MUST contain the error/status string
  `transcript_path not found after normalization`
  (`tests/test_legacy_shell_hooks.py:L16`).
- It MUST NOT contain inline parsing helpers: the substring `safe = lambda`
  must be absent (`tests/test_legacy_shell_hooks.py:L17`), and the substring
  `with open(sys.argv[1]) as f:` must be absent
  (`tests/test_legacy_shell_hooks.py:L18`).

## Contract: `mempal_precompact_hook.sh`

The precompact hook script (`hooks/mempal_precompact_hook.sh`) MUST satisfy all
of the following observable content constraints
(`tests/test_legacy_shell_hooks.py:L21-L26`):

- It MUST invoke the shared parser module with the `parse-precompact`
  subcommand, containing the substring
  `-m mempalace.hook_shell parse-precompact`
  (`tests/test_legacy_shell_hooks.py:L24`).
- It MUST contain the error/status string
  `missing or invalid transcript path after normalization`
  (`tests/test_legacy_shell_hooks.py:L25`).
- It MUST NOT contain the inline parsing helper substring `safe = lambda`
  (`tests/test_legacy_shell_hooks.py:L26`).

## Invariants

These tests collectively enforce that the legacy shell hooks delegate all
transcript parsing and human-message counting to the `mempalace.hook_shell`
module via its subcommands (`parse-stop`, `count-human-messages`,
`parse-precompact`), and contain no inline ad-hoc parsing logic
(`tests/test_legacy_shell_hooks.py:L14-L26`). The UTF-8 message counting
capability is implied by routing through the shared module rather than inline
code (`tests/test_legacy_shell_hooks.py:L11-L18`).

## Side effects and error behavior

The tests read files from the filesystem under `hooks/` only; they perform no
writes, no network, and no process spawning
(`tests/test_legacy_shell_hooks.py:L7-L8`). A failed substring assertion causes
the corresponding test to fail; a missing or non-UTF-8 hook file causes a read
error before assertions run (`tests/test_legacy_shell_hooks.py:L8-L26`).
