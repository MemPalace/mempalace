# Behavior Specification: `wal.py`

## Purpose

A side-effect-free write-ahead log (WAL) for MemPalace write operations. It exposes WAL audit logging to callers that need it without triggering unwanted import-time side effects such as stdout-to-stderr redirection that occur when importing the MCP server module (mempalace/wal.py:L1-L11).

## Storage Location & On-Disk Contract

- The WAL file is located at `~/.mempalace/wal/write_log.jsonl` (the `~` is expanded to the user's home directory) (mempalace/wal.py:L23-L23).
- The WAL file is an append-only JSONL file: one JSON object per line, each line terminated by a newline character (mempalace/wal.py:L94-L95).
- Each appended JSON object has exactly these top-level keys: `timestamp`, `operation`, `params`, `result` (mempalace/wal.py:L83-L88).
  - `timestamp`: an ISO-8601 formatted string representing the local wall-clock time at which the entry was written (mempalace/wal.py:L84-L84).
  - `operation`: the operation name string passed by the caller (mempalace/wal.py:L86-L86, mempalace/wal.py:L74-L74).
  - `params`: an object containing the caller-supplied parameters after redaction (see Redaction) (mempalace/wal.py:L86-L86).
  - `result`: the caller-supplied result object, or `null` when no result was provided (mempalace/wal.py:L87-L87, mempalace/wal.py:L74-L74).
- Values not directly JSON-serializable are coerced to their string representation rather than causing failure (mempalace/wal.py:L95-L95).

## Public Surface

### `_ensure_wal() -> None`

Lazily creates and hardens the WAL directory. Behavior:

- It must NOT be invoked at import time; it runs only on the first real write, preserving the kill-switch contract whereby a user who removed `~/.mempalace` does not have it silently recreated merely by importing the module (mempalace/wal.py:L32-L45).
- Directory hardening is attempted at most once per directory path. After an attempt (success or failure), the directory path is cached so subsequent calls for the same path return immediately without retrying (mempalace/wal.py:L55-L58, mempalace/wal.py:L69-L71).
- The cache is keyed on the WAL directory path; if the WAL file path is changed (e.g. in tests), initialization runs again for the new path (mempalace/wal.py:L48-L50, mempalace/wal.py:L56-L58).
- Hardening sets the WAL directory permissions to `0o700` (mempalace/wal.py:L59-L60).
- If the directory does not yet exist (the `chmod` raises a not-found error), the directory is created with all needed parent directories, then permissions are set to `0o700` (mempalace/wal.py:L61-L64).
- Any filesystem error or unsupported-operation error during chmod or mkdir is suppressed and is non-fatal; the path is still cached so it is not retried on every write (mempalace/wal.py:L65-L71).
- The parent `~/.mempalace` directory retains its default (umask) permission mode; only the `wal` subdirectory is set to `0o700` (mempalace/wal.py:L51-L53, mempalace/wal.py:L60-L64).

### `_wal_log(operation: str, params: dict, result: dict = None)`

Appends a single write operation entry to the WAL.

- Inputs: `operation` (string), `params` (object/dict), and optional `result` (object/dict, defaults to absent/null) (mempalace/wal.py:L74-L74).
- Output: none (the function returns nothing; its effect is the file append) (mempalace/wal.py:L74-L97).

#### Redaction (observable contract)

- Before writing, each key in `params` is inspected. Keys belonging to the redaction set have their values replaced; all other keys pass through unchanged (mempalace/wal.py:L77-L82).
- The redaction key set is exactly: `content`, `content_preview`, `document`, `entry`, `entry_preview`, `query`, `text` (mempalace/wal.py:L27-L29).
- For a redacted key whose value is a string, the value is replaced with the literal text `[REDACTED <N> chars]`, where `<N>` is the character length of the original string (mempalace/wal.py:L79-L80).
- For a redacted key whose value is not a string, the value is replaced with the literal text `[REDACTED]` (mempalace/wal.py:L79-L80).

#### Write mechanics & invariants

- Before appending, `_ensure_wal()` is called to lazily set up the directory (mempalace/wal.py:L90-L92).
- The WAL file is opened for write in append-create mode and created (if absent) with permission mode `0o600` (mempalace/wal.py:L93-L93).
- The serialized entry plus a trailing newline is appended to the file using UTF-8 encoding (mempalace/wal.py:L94-L95).
- Append ordering: entries are appended in the order `_wal_log` is called; the file grows monotonically (append-only, never truncated or rewritten) (mempalace/wal.py:L93-L95).

#### Error / edge-case behavior

- Any failure during directory setup or the file append (the directory setup shares the append's exception handler) is caught; the failure is recorded to the module logger at error level with message `WAL write failed: <error>` and is non-fatal — it never raises out of the function nor crashes the calling tool (mempalace/wal.py:L89-L97).

## Side Effects

- Filesystem: creates `~/.mempalace/wal/` directory (mode `0o700`) and the `write_log.jsonl` file (mode `0o600`); appends to that file (mempalace/wal.py:L23-L23, mempalace/wal.py:L60-L64, mempalace/wal.py:L93-L95).
- Logging: emits an error-level log record on failure (mempalace/wal.py:L21-L21, mempalace/wal.py:L96-L97).
- No network, no process spawning, no environment mutation, and no import-time side effects (mempalace/wal.py:L1-L11).
