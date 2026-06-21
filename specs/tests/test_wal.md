# Behavior Spec — `tests/test_wal.py`

This file is a test module that pins down two externally observable contracts of the
`mempalace.wal` write-ahead-log component. The spec below describes the contracts the
tests assert; an implementer must make these hold.

## Component under test

The subject is a `wal` module/component exposing a write-audit ("write-ahead log")
facility. The tests reach it as `mempalace.wal` and exercise its `_wal_log` function
and two module-level configuration values `_WAL_FILE` and `_WAL_INITIALIZED_DIR`
(tests/test_wal.py:L31-L37).

## Contract 1 — Import isolation from the MCP server

Importing the `wal` module MUST NOT, as a side effect, cause the `mcp_server`
component to be loaded (tests/test_wal.py:L5-L24). The rationale recorded in the test
is that the MCP server installs a process-global stdio redirect at import time
(duplicating stderr onto stdout and replacing stdout with stderr); the CLI sync path
and daemon service layer depend on obtaining the WAL logger from `wal` precisely so
they can audit writes without triggering that redirect (tests/test_wal.py:L6-L13).

Observable check: in a freshly started process, after importing only `mempalace.wal`,
the module registry MUST NOT contain `mempalace.mcp_server`
(tests/test_wal.py:L15-L21). The test enforces this in a brand-new subprocess so a
previously-loaded `mcp_server` in the same session cannot mask a regression
(tests/test_wal.py:L11-L13, L22). The subprocess must exit with status code 0 and
emit `ok` on standard output when the contract holds (tests/test_wal.py:L22-L24).

## Contract 2 — `_wal_log` redacts sensitive params and appends a JSONL record

`_wal_log` takes two inputs: an operation name (a string) and a parameters mapping
(string keys to values) (tests/test_wal.py:L37). Calling it writes one record to the
WAL file.

### WAL file location and initialization state

The target WAL file path is read from the module-level value `_WAL_FILE`; the test
points it at `<tmp>/wal/write_log.jsonl`, i.e. a file named `write_log.jsonl` inside a
`wal/` subdirectory (tests/test_wal.py:L33-L34). The module also has a
`_WAL_INITIALIZED_DIR` value tracking whether the containing directory has been
prepared; the test resets it to a "not yet initialized" sentinel (none/unset) before
the call (tests/test_wal.py:L35). Because the test sets `_WAL_INITIALIZED_DIR` to the
uninitialized sentinel and the call still succeeds in writing the file, `_wal_log`
MUST create any missing parent directories of `_WAL_FILE` before writing
(tests/test_wal.py:L33-L39).

### On-disk record format (JSONL contract)

Each `_wal_log` call appends one line of JSON to the WAL file. After a single call,
the file's content (trimmed of surrounding whitespace) parses as one JSON object
(tests/test_wal.py:L39). The object has these fields:

- `operation`: equals the operation-name argument verbatim — here `"op"`
  (tests/test_wal.py:L40).
- `params`: an object mirroring the input parameters mapping, with sensitive values
  redacted as described below (tests/test_wal.py:L41-L42).

### Redaction rule

Within the `params` object, values that carry sensitive verbatim user content are
replaced with a redaction marker; values that are safe are passed through unchanged.
Concretely, the input `{"entry": "secret diary text", "safe": "ok"}` produces a
`params.entry` value whose string begins with `"[REDACTED"` (the original text is not
stored), while `params.safe` retains its original value `"ok"`
(tests/test_wal.py:L37-L42). This demonstrates a key-driven redaction policy: content
keys such as `entry` are redacted, and other keys are preserved as-is
(tests/test_wal.py:L41-L42).

## Notes on types and side effects

- Side effect: `_wal_log` performs a filesystem write to the path in `_WAL_FILE`,
  creating parent directories as needed (tests/test_wal.py:L33-L39).
- Output format: line-delimited JSON (one JSON object per call) with at least the keys
  `operation` and `params` (tests/test_wal.py:L39-L42).
- Configuration surface: `_WAL_FILE` (target path) and `_WAL_INITIALIZED_DIR`
  (directory-prepared sentinel) are overridable module-level values
  (tests/test_wal.py:L34-L35).
