# Spec: tests/test_instructions_cli.py

Behavior specification for the test suite covering the `instructions_cli` module's
instruction-text output command. The tests pin the observable contract of the
`run_instructions` entry point and two module-level constants it depends on.

## Subject under test

The tests exercise a unit named `instructions_cli` exposing three public symbols:
`AVAILABLE`, `INSTRUCTIONS_DIR`, and `run_instructions` (tests/test_instructions_cli.py:L7).

- `INSTRUCTIONS_DIR` is a filesystem directory path. Instruction documents live
  inside it as files named `<name>.md` (tests/test_instructions_cli.py:L13).
- `AVAILABLE` is an iterable collection of instruction names (strings). It is
  iterable and each element can be passed to `run_instructions`
  (tests/test_instructions_cli.py:L21-L24). In tests it is also patchable to a
  list of names (tests/test_instructions_cli.py:L40).
- `run_instructions(name)` takes a single string argument `name` and produces
  output on standard streams (no return value is asserted)
  (tests/test_instructions_cli.py:L14).

## Contract: valid known name prints the document verbatim

When `run_instructions` is called with a name that has a corresponding
`<name>.md` file in `INSTRUCTIONS_DIR`, it prints the full text content of that
file to standard output. The printed output, after trimming surrounding
whitespace, equals the file's text content (read as UTF-8) after the same
trimming (tests/test_instructions_cli.py:L12-L16). The name `"init"` is a
concrete example of a valid name with an `init.md` file on disk
(tests/test_instructions_cli.py:L12-L13).

## Contract: every advertised name succeeds

For every name present in `AVAILABLE`, calling `run_instructions(name)` succeeds
without raising an error and emits non-empty output to standard output (output
length strictly greater than zero) (tests/test_instructions_cli.py:L19-L24). This
is an invariant linking the advertised name list to on-disk document
availability: each advertised name must produce printable content.

## Error: unknown name

When `run_instructions` is called with a name not recognized as available, it
terminates the process with exit code 1 (tests/test_instructions_cli.py:L29-L31).
Before exiting it writes an error message to standard error. The error text
contains the phrase `Unknown instructions: <name>` (with the offending name
interpolated, e.g. `Unknown instructions: nonexistent`) and also contains the
literal phrase `Available:` (tests/test_instructions_cli.py:L32-L34). The
`Available:` line communicates the set of valid names to the user.

## Error: known name but missing file on disk

When a name is considered available (present in `AVAILABLE`) but its backing
`<name>.md` file does not exist within `INSTRUCTIONS_DIR`, `run_instructions`
terminates the process with exit code 1 (tests/test_instructions_cli.py:L37-L43).
It writes an error message to standard error containing the phrase
`Instructions file not found` (tests/test_instructions_cli.py:L44-L45). This is a
distinct failure mode from the unknown-name case: here the name passes the
availability check but the document is absent from disk
(tests/test_instructions_cli.py:L39-L42).

## Stream separation invariant

Successful output is written exclusively to standard output
(tests/test_instructions_cli.py:L15-L16, L23-L24), while all error diagnostics
(unknown name, missing file) are written exclusively to standard error
(tests/test_instructions_cli.py:L32-L34, L44-L45). The two error paths both use
exit code 1 (tests/test_instructions_cli.py:L31, L43).
