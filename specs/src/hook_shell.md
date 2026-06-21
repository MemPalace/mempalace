# Behavior Specification: `mempalace/hook_shell.py`

Compatibility helper invoked by the legacy shell hooks `hooks/mempal_save_hook.sh` and `hooks/mempal_precompact_hook.sh`. It parses Claude Code hook JSON and counts UTF-8 JSONL transcript messages, behavior that is safer to centralize here than inline in shell. (mempalace/hook_shell.py:L1-L7)

## Public Surface

The module exposes a command-line entry point and a set of pure helper functions. The CLI is invoked as `python -m mempalace.hook_shell <command> [args]` and dispatches on the first positional argument. (mempalace/hook_shell.py:L118-L155)

### `sanitize_session_id(session_id) -> str`

Accepts any value. Coerces it to a string (a null/falsy value becomes the empty string), then removes every character that is not an ASCII letter, digit, underscore, or hyphen. If the result is empty, returns the literal string `"unknown"`. This keeps session ids safe for use as state-file names. (mempalace/hook_shell.py:L16-L23)

### `normalize_transcript_path(path) -> str`

Accepts any value. Coerces it to a string (null/falsy becomes empty string), replaces every backslash with a forward slash, then strips the control characters NUL (`\x00`), carriage return (`\r`), and newline (`\n`). It deliberately does NOT strip the drive-letter colon, so Windows paths such as `C:\Users\me\.claude\projects\<project>\<session>.jsonl` survive normalization (becoming `C:/Users/me/...`). The only removals are control characters that would corrupt newline-delimited shell parsing. (mempalace/hook_shell.py:L16-L17, L26-L41)

### `parse_stop_payload(payload) -> (session_id, stop_hook_active, transcript_path)`

Given a dictionary payload, returns a 3-tuple of strings: the sanitized `session_id` field, the normalized `stop_hook_active` field, and the normalized `transcript_path` field. Missing fields default to empty string / `False` respectively. (mempalace/hook_shell.py:L53-L58)

The `stop_hook_active` value is normalized to one of exactly two literal strings `"True"` or `"False"`. It is `"True"` when the input value is boolean true, or when its string form (trimmed and lowercased) is one of `"true"`, `"1"`, or `"yes"`; otherwise it is `"False"`. (mempalace/hook_shell.py:L44-L50)

### `parse_precompact_payload(payload) -> (session_id, transcript_path)`

Given a dictionary payload, returns a 2-tuple of strings: the sanitized `session_id` field and the normalized `transcript_path` field, both defaulting from empty string when absent. (mempalace/hook_shell.py:L61-L65)

### `count_human_messages(path) -> int`

Opens the file at `path` as UTF-8, ignoring invalid bytes (fail-soft decoding). Reads it line by line as JSONL. For each line: lines that are not valid JSON are skipped. A line counts as one human message only when its `message` field is an object whose `role` equals `"user"`. A user message whose `content` is a string containing the substring `<command-message>` is excluded from the count. Returns the total count of qualifying user messages. (mempalace/hook_shell.py:L68-L94)

## Standard-Input Payload Loading (internal contract)

The parse commands read their payload from standard input via an internal loader. Empty stdin (exactly the empty string) is treated as a legitimate state and yields an empty payload `{}`, so the success sentinel is still printed and the shell fail-loud guard does not trigger. (mempalace/hook_shell.py:L97-L104)

For non-empty but malformed input, JSON parsing is allowed to raise (no sentinel is printed); the shell hooks capture this on stderr in `last_python_err.log` and write a bounded copy of the raw payload to `last_input.log`. This fail-loud contract is pinned by `tests/test_hooks_bash_compat.py`. If the parsed JSON is valid but is not a JSON object (e.g. an array or scalar), a type error is raised. (mempalace/hook_shell.py:L105-L115)

## CLI Commands and Output Contract (observable)

When invoked with no arguments, prints a usage string to stderr and exits with code `2`. (mempalace/hook_shell.py:L118-L125)

### `parse-stop`

Reads JSON from stdin, parses it as a stop payload, then prints to stdout, each on its own line and in this exact order: the sentinel `__MEMPAL_PARSE_OK__`, the session id, the `stop_hook_active` string (`"True"`/`"False"`), and the transcript path. Exits `0`. The sentinel-first ordering is the signal the shell uses to detect a successful parse. (mempalace/hook_shell.py:L129-L135)

### `parse-precompact`

Reads JSON from stdin, parses it as a precompact payload, then prints to stdout in this exact order: the sentinel `__MEMPAL_PARSE_OK__`, the session id, the transcript path. Exits `0`. (mempalace/hook_shell.py:L137-L142)

### `count-human-messages`

Requires exactly one additional argument (the transcript path). If the argument count is not exactly 2 total, prints an error to stderr and exits `2`. Otherwise prints the integer count of human messages to stdout and exits `0`. If counting raises any error (e.g. the file is missing or unreadable), it prints `0` instead and still exits `0` (fail-soft). (mempalace/hook_shell.py:L144-L152)

### Unknown command

Any unrecognized first argument prints `unknown hook_shell command: <command>` to stderr and exits `2`. (mempalace/hook_shell.py:L154-L155)

## Exit Codes Summary

- `0` — successful parse-stop, parse-precompact, or count-human-messages (including its fail-soft `0` output). (mempalace/hook_shell.py:L135, L142, L149-L152)
- `2` — no arguments, wrong argument count for count-human-messages, or unknown command. (mempalace/hook_shell.py:L125, L147, L155)
- Non-zero raised exception — malformed non-empty stdin during a parse command causes an unhandled error/abort rather than a clean exit, by design. (mempalace/hook_shell.py:L105-L115)

The process entry point invokes `main` and exits with its returned code. (mempalace/hook_shell.py:L158-L159)

## Side Effects

- Reads from standard input for the two parse commands. (mempalace/hook_shell.py:L97-L98)
- Reads the file named by the transcript-path argument for `count-human-messages`. (mempalace/hook_shell.py:L77-L78)
- Writes results to standard output and diagnostics to standard error; performs no network, environment, or other filesystem writes. (mempalace/hook_shell.py:L121-L154)
