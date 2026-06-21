# Behavior Spec: tests/test_hook_shell.py

This test file specifies the externally observable contract of the `hook_shell`
module (importable as `mempalace.hook_shell`) and its command-line interface
(invoked as the module `mempalace.hook_shell`). The behaviors below are the
ground-truth requirements that the module under test must satisfy.

## Module-level functions

### normalize_transcript_path(path) → string

Converts a filesystem path into a forward-slash-normalized form while preserving
all path content. Windows-style backslash separators are converted to forward
slashes, but the Windows drive prefix (e.g. `C:`) and every path segment are kept
verbatim (tests/test_hook_shell.py:L8-L14).

Spaces and non-ASCII / Unicode characters inside path segments (including
multi-byte emoji and accented/Cyrillic text) are preserved unchanged; only
separators are rewritten (tests/test_hook_shell.py:L17-L23).

### parse_stop_payload(payload: object) → (session_id, stop_active, transcript_path)

Accepts a structured payload (a mapping) and returns a 3-tuple of strings.

The `session_id` field is strictly sanitized: disallowed characters such as `.`,
`/`, spaces, and `!` are stripped so that input `"../bad session!!"` yields the
string `"badsession"` (tests/test_hook_shell.py:L26-L35).

The `stop_hook_active` field is coerced to a boolean-like string. A truthy input
(`"yes"`) maps to the literal string `"True"` (tests/test_hook_shell.py:L26-L36).

The `transcript_path` field is normalized like `normalize_transcript_path` —
backslashes become forward slashes and the drive prefix, spaces, and Unicode are
preserved — but it is NOT over-sanitized (i.e. its content characters are not
stripped the way `session_id` is). Input
`C:\Users\Me User\.claude\projects\emoji 🧠\session.jsonl` yields
`C:/Users/Me User/.claude/projects/emoji 🧠/session.jsonl`
(tests/test_hook_shell.py:L26-L37).

### count_human_messages(transcript_path: string) → integer

Reads a JSONL transcript file at the given path, decoding it as UTF-8 tolerantly
(non-ASCII content such as emoji, accented Latin, and Cyrillic must not cause a
failure) (tests/test_hook_shell.py:L61-L77).

It counts only human/user messages, applying these rules:
- A line whose decoded record has `message.role == "user"` counts as a human
  message (tests/test_hook_shell.py:L64-L68).
- A user message whose content contains the marker `<command-message>` is NOT
  counted (it is ignored) (tests/test_hook_shell.py:L69).
- A record with `message.role == "assistant"` is NOT counted
  (tests/test_hook_shell.py:L71).
- A malformed/unparseable JSON line (e.g. `{bad json`) is tolerated and does not
  abort processing; it is skipped (tests/test_hook_shell.py:L72-L73).

For the example transcript containing one valid user message, one user message
with a `<command-message>` marker, one assistant message, and one bad-JSON line,
the result is `1` (tests/test_hook_shell.py:L77).

## Command-line interface

The module is invoked as an executable module with a subcommand as the first
argument; payloads are read from standard input as JSON text.

### Subcommand: `parse-precompact`

Reads a JSON payload from stdin. On success it prints, on stdout, three lines in
this exact order: the sentinel `__MEMPAL_PARSE_OK__`, then the `session_id`, then
the normalized `transcript_path` (tests/test_hook_shell.py:L40-L58).

For input session `"sess-1"` and transcript path
`D:\Claude\projects\-Users-me-App\session.jsonl`, the three output lines are
`__MEMPAL_PARSE_OK__`, `sess-1`, and
`D:/Claude/projects/-Users-me-App/session.jsonl` — confirming backslash-to-slash
normalization and drive-prefix preservation at the CLI layer
(tests/test_hook_shell.py:L41-L58). Exit status is success (0) on well-formed
input (tests/test_hook_shell.py:L46-L52).

On malformed non-empty stdin (e.g. `"not-json garbage"`), the command fails loud:
the process exit code is non-zero, the `__MEMPAL_PARSE_OK__` sentinel does NOT
appear on stdout, and standard error contains either the word "traceback" or
"json" (case-insensitive) (tests/test_hook_shell.py:L103-L114).

### Subcommand: `parse-stop`

Reads a JSON payload from stdin.

On malformed non-empty stdin (e.g. `"not-json garbage"`), the command fails loud:
non-zero exit code, no `__MEMPAL_PARSE_OK__` sentinel on stdout, and stderr
contains "traceback" or "json" (case-insensitive)
(tests/test_hook_shell.py:L89-L100).

On empty stdin (the empty string), the command treats it as an empty payload and
succeeds (exit code 0). Stdout begins with the three lines `__MEMPAL_PARSE_OK__`,
`unknown`, and `False` (in that order) — i.e. a missing session id defaults to the
string `"unknown"` and a missing stop-active flag defaults to the string
`"False"`. Standard error is empty in this case
(tests/test_hook_shell.py:L117-L128).

### Subcommand: `count-human-messages <path>`

Takes a transcript file path as a positional argument, performs the same counting
logic as `count_human_messages`, and prints the resulting integer count to stdout
(trailing whitespace aside). For the example transcript it prints `1` and exits
with success (tests/test_hook_shell.py:L79-L86).

## Observable contracts (summary)

- Stdout success sentinel for parse subcommands is the exact literal
  `__MEMPAL_PARSE_OK__` as the first output line
  (tests/test_hook_shell.py:L54-L57, L127).
- Default values when fields are absent: session id → `"unknown"`,
  stop-active → `"False"` (tests/test_hook_shell.py:L127).
- Path normalization rule: backslash → forward slash, preserving drive prefix,
  spaces, and Unicode; never strip content of transcript paths
  (tests/test_hook_shell.py:L8-L23, L37, L57).
- Failure mode: non-zero exit, no success sentinel, diagnostic on stderr
  (tests/test_hook_shell.py:L98-L100, L112-L114).
