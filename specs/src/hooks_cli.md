# Behavior Specification — `mempalace/hooks_cli.py`

Hook logic for MemPalace: implements `session-start`, `stop`, and `precompact` hooks for the `claude-code` and `codex` harnesses. Each hook reads a JSON object from stdin and writes a JSON object to stdout (mempalace/hooks_cli.py:L1-L7).

## Constants & Paths

- The auto-save interval is `15` human messages (mempalace/hooks_cli.py:L22-L22).
- Hook state lives under `~/.mempalace/hook_state/` (`STATE_DIR`) and the palace root is `~/.mempalace/` (`PALACE_ROOT`) (mempalace/hooks_cli.py:L23-L24).
- The number of recent user messages summarized for a checkpoint is `30` (mempalace/hooks_cli.py:L101-L101).
- Supported harnesses are exactly `claude-code` and `codex` (mempalace/hooks_cli.py:L909-L909).

## Entry Point: `run_hook(hook_name, harness)`

Reads a single JSON object from stdin. If stdin is not valid JSON or is empty, it logs a warning and proceeds with an empty data object `{}` rather than failing (mempalace/hooks_cli.py:L1220-L1226). It dispatches `hook_name` to one of: `session-start`, `stop`, `precompact` (mempalace/hooks_cli.py:L1228-L1239). An unknown `hook_name` prints `Unknown hook: <name>` to stderr and exits with code 1 (mempalace/hooks_cli.py:L1234-L1237).

## Kill-Switch: palace root must exist

If `~/.mempalace/` is not a directory, the user has cleared the palace and all hooks short-circuit by emitting `{}` and doing nothing else — no disk writes, no logging, no mining (mempalace/hooks_cli.py:L48-L61). Each hook handler checks this first and returns `{}` when it fails (mempalace/hooks_cli.py:L1061-L1063, L1173-L1175, L1194-L1196). The check uses a directory test, so a stray regular file or broken symlink at that path is treated as absent (mempalace/hooks_cli.py:L56-L61).

## Input Parsing

`_parse_harness_input` rejects any harness not in the supported set by printing `Unknown harness: <name>` to stderr and exiting with code 1 (mempalace/hooks_cli.py:L926-L930). On success it returns three fields derived from the stdin object: `session_id` (sanitized), `stop_hook_active` (defaults to `false`), and `transcript_path` (defaults to empty string) (mempalace/hooks_cli.py:L931-L935).

`session_id` is sanitized to allow only alphanumerics, dash, and underscore; all other characters are stripped, and an empty result becomes the literal string `unknown` (mempalace/hooks_cli.py:L118-L121).

## Transcript Path Validation

A transcript path is accepted only if non-empty, has a `.jsonl` or `.json` extension, and contains no `..` traversal component in the original input; otherwise it is rejected (returns nothing) (mempalace/hooks_cli.py:L124-L140). Rejected non-empty paths are logged as a warning (mempalace/hooks_cli.py:L146-L149).

## Human Message Counting

`_count_human_messages` reads the transcript as JSONL line-by-line and returns a count of human/user messages, returning `0` if the path is rejected, not a file, or unreadable (mempalace/hooks_cli.py:L143-L183). For each JSON line it counts a message when:
- the line has a `message` object with `role == "user"`, EXCEPT when its string content (or the joined `text` of list-content blocks) contains `<command-message>`, which is skipped (mempalace/hooks_cli.py:L158-L170);
- OR the line is a Codex event of shape `{"type": "event_msg", "payload": {"type": "user_message", "message": "..."}}` whose message text does not contain `<command-message>` (mempalace/hooks_cli.py:L171-L178).
Malformed JSON lines are silently skipped (mempalace/hooks_cli.py:L179-L180).

## Recent Message Extraction & Themes

`_extract_recent_messages` returns the last N (default 30) user messages from a transcript, each trimmed and truncated to 200 characters (mempalace/hooks_cli.py:L686-L722). It handles both Claude Code (`message`/`event_message` objects with `role == "user"`, list content joined by `text` blocks) and Codex (`event_msg` payloads) formats (mempalace/hooks_cli.py:L696-L717). Messages containing `<command-message>` or `<system-reminder>`, or empty after stripping, are excluded (mempalace/hooks_cli.py:L705-L709). A missing/unreadable file yields an empty list (mempalace/hooks_cli.py:L688-L721).

`_extract_themes` returns up to `max_themes` (default 3) most-frequent distinctive words from the messages: lowercased, punctuation-stripped, length ≥ 4, alphabetic, and not in the English stopword set (mempalace/hooks_cli.py:L737-L751). The stopword list is English-only (mempalace/hooks_cli.py:L725-L734).

## Wing Derivation from Transcript

`_wing_from_transcript_path` derives a project wing name with this priority (mempalace/hooks_cli.py:L994-L1056):
1. PRIMARY — read `cwd` from the JSONL transcript and use its leaf path segment (mempalace/hooks_cli.py:L1021-L1024).
2. FALLBACK — decode an encoded folder under `/.claude/projects/-<encoded>`, strip a `Users-<user>-`/`home-<user>-` home prefix and one common parent token (mempalace/hooks_cli.py:L1029-L1047).
3. LEGACY — match an explicit `-Projects-<name>` segment (mempalace/hooks_cli.py:L1049-L1053).
4. DEFAULT — `wing_sessions` (mempalace/hooks_cli.py:L1055-L1056).

`_wing_from_jsonl_cwd` scans up to the first 200 lines for the first record containing a non-empty string `cwd`, normalizes backslashes to slashes, takes the leaf segment, lowercases it, replaces spaces and dashes with underscores, and returns `wing_<slug>`; returns nothing if no usable cwd is found (mempalace/hooks_cli.py:L954-L991). In all derivation branches, the wing slug lowercases and replaces spaces/dashes with underscores and is prefixed `wing_` (mempalace/hooks_cli.py:L985-L988, L1045-L1047, L1050-L1053). The common parent prefixes stripped (first match only) are: `git-`, `dev-`, `projects-`, `Projects-`, `src-`, `code-`, `work-`, `Documents-` (mempalace/hooks_cli.py:L942-L951, L1041-L1044).

## Logging Side Effect

`_log` appends `[HH:MM:SS] <message>\n` to `STATE_DIR/hook.log`. It does nothing if the palace root does not exist (so logging never recreates a cleared palace) (mempalace/hooks_cli.py:L189-L213). On first write it creates `STATE_DIR` with permissions `0o700` and a newly created log file with permissions `0o600`; permission/OS errors are swallowed (mempalace/hooks_cli.py:L194-L213).

## Output Side Effect

`_output(data)` serializes `data` as pretty-printed JSON (2-space indent, non-ASCII preserved) plus a trailing newline, UTF-8 encoded, and writes it to the real stdout file descriptor (fd 1, or a saved real-stdout fd from the MCP server module if loaded), falling back to the buffered stdout stream on OS error (mempalace/hooks_cli.py:L216-L245). This is the observable JSON contract returned by every hook.

## Mine Subprocess Concurrency Contract (PID Slots)

Background mines are guarded by a per-target PID-slot directory `STATE_DIR/mine_pids/` (mempalace/hooks_cli.py:L283-L283). The slot key is the SHA-256 (first 16 hex chars) of the mine arguments from the `mine` token onward, so identical `(dir, mode, wing)` invocations collapse to the same slot and distinct targets get independent slots (mempalace/hooks_cli.py:L316-L330). The slot file body format is `{pid} {unix_timestamp}` (mempalace/hooks_cli.py:L409-L419, L507).

A slot claim is atomic via exclusive create-new-file; if the file already exists and a live, non-stale holder occupies it, the claim returns nothing and the spawn is skipped (mempalace/hooks_cli.py:L433-L469). A stale slot (dead PID, or — when timeout > 0 — alive but running longer than the configured timeout) is transparently reclaimed (mempalace/hooks_cli.py:L366-L406, L451-L469).

The mine timeout defaults to 2 hours; it is read from env `MEMPALACE_MINE_TIMEOUT_HOURS` (float, in hours, clamped to ≥ 0); a value of `0` or an unparseable value disables the timeout so slots are reclaimed only when the PID is dead (mempalace/hooks_cli.py:L296-L313, L392-L406). Old-format bare-`{pid}` slot files use the file mtime as the approximate start time (mempalace/hooks_cli.py:L369-L403).

PID liveness is checked cross-platform: on POSIX via a signal-0 probe, on Windows via process-handle query (never terminating the target) (mempalace/hooks_cli.py:L333-L363).

`_spawn_mine` claims the slot before spawning; on a claimed-slot collision it logs and skips (mempalace/hooks_cli.py:L483-L486). The child process is fully detached (new session / detached process group) and inherits the slot path via env var `MEMPALACE_MINE_PID_FILE` so the child can clean its own slot on exit; child stdout/stderr are redirected to `STATE_DIR/hook.log` (mempalace/hooks_cli.py:L27-L45, L287-L288, L472-L509). If the spawn itself fails, the just-claimed slot is released and the error re-raised (mempalace/hooks_cli.py:L498-L505).

## Mine Target Selection

`_get_mine_targets` returns at most one target: `(resolved MEMPAL_DIR, "projects")` when env `MEMPAL_DIR` is set and resolves to an existing directory, else an empty list (mempalace/hooks_cli.py:L248-L264). Transcript ingestion is handled separately to avoid double-mining (mempalace/hooks_cli.py:L248-L257).

## Daemon Routing

If config `hook_use_daemon` is true AND a daemon is already running for the configured palace (a fast localhost health check that never auto-starts a daemon), mine/diary/ingest work is submitted as a daemon job instead of spawning a subprocess (mempalace/hooks_cli.py:L512-L545, L579-L617, L619-L671). When a daemon job submission raises an error, the code does NOT fall back to the direct path (to avoid double-writing verbatim content); it only logs (mempalace/hooks_cli.py:L547-L576, L605-L608, L645-L648, L802-L805, L878-L881). Daemon mine jobs use dedupe key `hook:mine:<mode>:<resolved-source>` (mempalace/hooks_cli.py:L519-L524).

## Diary Checkpoint Write

`_save_diary_direct` builds a compressed diary entry and writes it (via daemon job or direct tool call) under a given `agent_name` and optional `wing` (mempalace/hooks_cli.py:L754-L849). The entry text format is:
`CHECKPOINT:<YYYY-MM-DD>|session:<session_id>|msgs:<count>|recent:<topics>` where `<topics>` is up to the last 10 messages each truncated to 80 chars joined by `|` (mempalace/hooks_cli.py:L780-L785). The diary entry's topic is `checkpoint` (mempalace/hooks_cli.py:L795-L800, L825-L830). On no recent messages it logs and returns `{"count": 0}` (mempalace/hooks_cli.py:L772-L775). On success it returns `{"count": N, "themes": [...]}` and on failure `{"count": 0}` (mempalace/hooks_cli.py:L766-L770, L819-L821, L844-L849).

On a successful save it writes an acknowledgment file `STATE_DIR/last_checkpoint` containing JSON `{"msgs": N, "ts": <ISO-8601 timestamp>}` (mempalace/hooks_cli.py:L809-L816, L834-L841). When `toast` is true it sends a desktop notification on success (mempalace/hooks_cli.py:L817-L818, L842-L843).

The diary `agent_name` is `claude` for the `claude-code` harness and otherwise the harness name itself (mempalace/hooks_cli.py:L912-L923).

## Transcript Ingestion

`_ingest_transcript` does nothing if the transcript is not a file or is smaller than 100 bytes, or if config fails to load (mempalace/hooks_cli.py:L852-L861). Otherwise it mines the transcript's parent directory in `convos` mode into the `sessions` wing — either as a daemon job (dedupe key `hook:mine:convos:<parent>`) or routed through `_spawn_mine` so the per-target PID guard prevents stacked parallel ingests for the same transcript (mempalace/hooks_cli.py:L863-L899). Hook failures never crash the shell; they are logged (mempalace/hooks_cli.py:L900-L906).

## Desktop Toast

`_desktop_toast` launches a detached `notify-send` notification with app name `MemPalace`, icon `brain`, given title (default `MemPalace`) and body; failures are silent (mempalace/hooks_cli.py:L673-L683).

## Hook: `session-start`

Emits `{}` (pass-through, no blocking). Side effects: logs the session start and ensures `STATE_DIR` exists (mempalace/hooks_cli.py:L1171-L1185).

## Hook: `stop`

Pass-through (`{}`) when the palace root is absent or config `hooks_auto_save` is false (mempalace/hooks_cli.py:L1061-L1072). If `stop_hook_active` is truthy (`true`/`1`/`yes`, case-insensitive) AND silent mode is off, it passes through `{}` to break the block loop; on a config-read failure it defaults to assuming silent mode is on so saves proceed (mempalace/hooks_cli.py:L1078-L1089).

It counts human messages, reads the per-session last-save marker file `STATE_DIR/<session_id>_last_save` (an integer count, defaulting to 0 if missing/unparseable), and computes messages since last save (mempalace/hooks_cli.py:L1091-L1106). A save triggers only when messages-since-last is ≥ 15 AND the exchange count is > 0 (mempalace/hooks_cli.py:L1108-L1108). Otherwise it emits `{}` (mempalace/hooks_cli.py:L1167-L1168).

On trigger, the project wing is derived from the transcript path (mempalace/hooks_cli.py:L1120-L1120). Behavior splits on config `hook_silent_save` (defaulting to silent on any config error, with `hook_desktop_toast` for toasts) (mempalace/hooks_cli.py:L1111-L1119):
- SILENT: writes the diary checkpoint directly, ingests the transcript, and auto-ingests `MEMPAL_DIR`. The last-save marker is advanced to the current exchange count ONLY if at least one memory was saved (`count > 0`); on success it emits `{"systemMessage": "✦ <count> memories woven into the palace[ — <themes>]"}`, otherwise `{}` (mempalace/hooks_cli.py:L1122-L1153).
- LEGACY (non-silent): advances the marker first (best-effort, no retry), ingests transcript, auto-ingests, and emits `{"decision": "block", "reason": <STOP_BLOCK_REASON + " Write diary entry to wing=<wing>.">}` (mempalace/hooks_cli.py:L1154-L1166). The reason instructs the agent to use `mempalace_diary_write` and `mempalace_add_drawer` and not native auto-memory files (mempalace/hooks_cli.py:L103-L108).

## Hook: `precompact`

Pass-through (`{}`) when the palace root is absent or config `hooks_auto_save` is false (mempalace/hooks_cli.py:L1188-L1204). Otherwise it logs the trigger, ingests the transcript (if any), then synchronously mines `MEMPAL_DIR` before emitting `{}` to allow compaction to proceed (mempalace/hooks_cli.py:L1206-L1217). `_mine_sync` runs the mine subprocess with a 60-second timeout (or a daemon job awaited up to 60s), swallowing timeouts and OS errors (mempalace/hooks_cli.py:L619-L671).

## Interpreter Resolution

`_mempalace_python` selects the Python interpreter for spawned subprocesses with priority: (1) executable env `MEMPALACE_PYTHON` if set and executable, (2) a venv `bin/python` four directory levels up from this file, (3) an editable-install `venv/bin/python` two levels up, (4) the current interpreter as fallback — guarding against shallow install paths (mempalace/hooks_cli.py:L64-L98).
