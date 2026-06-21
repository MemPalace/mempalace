# Spec: `normalize.py` — Chat-export normalization to MemPalace transcript format

Converts arbitrary chat export files into a single canonical "transcript" text format.
Plain text passes through; recognized JSON/JSONL chat schemas are parsed into role-tagged
turns. No network, no API key — all processing is local (mempalace/normalize.py:L1-L19).

## Transcript output contract

The canonical transcript format produced by `_messages_to_transcript` represents a list of
`(role, text)` turns where `role` is one of `"user"` or `"assistant"`. Each user turn is
emitted as a line prefixed with `"> "` (greater-than then a space) followed by the user text;
each assistant turn is emitted as the raw text with no prefix (mempalace/normalize.py:L831-L860).

Ordering and pairing: turns are processed in order. When a user turn is immediately followed by
an assistant turn, the assistant text is emitted on the line directly after the `> ` user line,
consuming both (advance by 2). A user turn not followed by an assistant turn, and any assistant
turn reached on its own, is emitted alone (advance by 1) (mempalace/normalize.py:L844-L858).
After every emitted turn (or user/assistant pair) a single empty line is appended; the lines are
then joined by newlines (mempalace/normalize.py:L859-L860).

Spellcheck side effect: by default (`spellcheck=True`), user-turn text is passed through
`spellcheck_user_text` (imported from `mempalace.spellcheck`) before emission. If that import
fails, no spellcheck is applied. Assistant text is never spellchecked
(mempalace/normalize.py:L831-L850). Detail: the corrector is applied only to user turns.

## Public entry point: `normalize(filepath: str) -> str`

Input: a filesystem path string. Output: a string (transcript or original content)
(mempalace/normalize.py:L116-L150).

Side effects / reads: stats the file for size, then reads it. If the stat fails it raises an
`IOError` whose message is `"Could not read {filepath}: {e}"` (mempalace/normalize.py:L121-L124).
If the file size exceeds 500 MB (`500 * 1024 * 1024` bytes) it raises an `IOError` with message
`"File too large ({MB} MB): {filepath}"` where MB is the integer size in megabytes
(mempalace/normalize.py:L125-L126). The file is read as text using UTF-8 with BOM stripping
(`utf-8-sig`) and undecodable bytes replaced; a read failure raises `IOError`
`"Could not read {filepath}: {e}"` (mempalace/normalize.py:L127-L131).

Empty/whitespace-only files are returned unchanged (their original content)
(mempalace/normalize.py:L133-L134).

Pass-through rule: if the content already contains at least 3 lines whose trimmed text starts
with `">"`, it is treated as an existing transcript and returned unchanged
(mempalace/normalize.py:L136-L139).

JSON dispatch: JSON parsing is attempted only when the file extension (lowercased) is `.json` or
`.jsonl`, OR the first non-whitespace character of the content is `{` or `[`
(mempalace/normalize.py:L144-L145). If a JSON parser produces a non-empty result it is returned;
otherwise the original content is returned unchanged (mempalace/normalize.py:L146-L150).

## JSON dispatch order: `_try_normalize_json(content) -> Optional[str]`

Parsers are attempted in a fixed order; the first that returns a truthy (non-empty) string wins
(mempalace/normalize.py:L153-L188). Order:

1. `_try_claude_code_jsonl` (line-delimited JSON) (mempalace/normalize.py:L156-L158)
2. `_try_codex_jsonl` (mempalace/normalize.py:L160-L162)
3. `_try_gemini_jsonl` (mempalace/normalize.py:L164-L166)
4. `_try_pi_jsonl` (mempalace/normalize.py:L168-L170)
5. Then the whole content is parsed as a single JSON document; if that fails to parse, return
   `None` (no further parsing) (mempalace/normalize.py:L172-L175).
6. On the parsed document, in order: `_try_gemini_json`, `_try_claude_ai_json`,
   `_try_chatgpt_json`, `_try_continue_json`, `_try_slack_json`
   (mempalace/normalize.py:L177-L186). First truthy result wins; else `None`
   (mempalace/normalize.py:L188).

The Gemini JSON parser deliberately precedes the Claude.ai parser so that a `{"messages": [...]}`
wrapper containing model turns is not claimed by Claude (mempalace/normalize.py:L433-L437).

## Minimum-turns invariant

Every parser requires at least 2 collected `(role, text)` messages before producing a transcript;
fewer than 2 yields `None` (so the format is treated as unrecognized). This holds for all parsers
(mempalace/normalize.py:L248-L250, L296-L298, L364-L366, L411-L413, L493-L495, L514-L524,
L583-L585, L625-L627, L680-L682).

## Noise stripping: `strip_noise(text: str) -> str`

Removes system tags, hook output, and UI chrome. All patterns are line-anchored and only applied
inside the Claude Code JSONL path (other formats pass through verbatim)
(mempalace/normalize.py:L96-L113, L141-L143).

Removed tag blocks (case-sensitive tag names, optionally preceded on the line by `"> "`): paired
tags `<name ...>...</name>` for the names `system-reminder`, `command-message`, `command-name`,
`task-notification`, `user-prompt-submit-hook`, `hook_output`. The tag body is lazy and must not
cross a blank line, so an unclosed tag cannot consume across message boundaries; the closing tag
also eats optional trailing spaces/tabs and one newline (mempalace/normalize.py:L43-L63).

Removed whole lines (anchored at line start, optionally after `"> "`, case-sensitive prefix
match) for these prefixes: `CURRENT TIME:`, `VERIFIED FACTS (do not contradict)`,
`AGENT SPECIALIZATION:`, `Checking verified facts...`, `Injecting timestamp...`,
`Starting background pipeline...`, `Checking emotional weights...`, `Auto-save reminder...`,
`Checking pipeline...`, `MemPalace auto-save checkpoint.` (mempalace/normalize.py:L68-L83).

Removed hook-run chrome lines matching `Ran <N> <HookName> hook[s]...` where HookName is one of
`Stop`, `PreCompact`, `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Notification`,
`SessionStart`, `SessionEnd` (line-anchored, optional `"> "`) (mempalace/normalize.py:L88-L90).

Removed collapsed-output marker lines matching `… +<N> lines...` (line-anchored, optional
`"> "`) (mempalace/normalize.py:L93). The substring `[<N> tokens] (ctrl+o to expand)` (with
optional surrounding whitespace) is removed inline anywhere it appears
(mempalace/normalize.py:L108-L110).

After removals, any run of 4 or more consecutive newlines is collapsed to exactly 3 newlines, and
the whole result is trimmed of leading/trailing whitespace (mempalace/normalize.py:L111-L113).

## `_try_claude_code_jsonl(content) -> Optional[str]`

Parses newline-delimited JSON objects; blank lines and lines failing JSON parse or that are not
dict objects are skipped (mempalace/normalize.py:L193-L207). Reads each entry's `type` and nested
`message.content` (mempalace/normalize.py:L204-L208).

Tool-name tracking: for entries of type `assistant` whose content is a list, any block with
`type == "tool_use"` and a non-empty `id` records a mapping `id -> name` (defaulting name to
`"Unknown"`) for later tool-result formatting (mempalace/normalize.py:L210-L216).

User entries (`type` in `human` or `user`): text is extracted via `_extract_content` (with the
tool-name map) and then noise-stripped (mempalace/normalize.py:L218-L227). A message is
"tool-only" when its content is a list consisting entirely of `tool_result` blocks
(mempalace/normalize.py:L219-L222). If the message is tool-only and the previous collected message
is an assistant turn, the tool-result text is appended to that assistant turn (joined by a
newline); otherwise, if not tool-only and there is text, it becomes a new `user` turn. A tool-only
message with no preceding assistant turn is dropped (mempalace/normalize.py:L229-L234).

Assistant entries: extracted and noise-stripped; if the previous collected message is also
assistant, the new text is merged into that turn (newline-joined) — modeling a multi-turn tool
loop — otherwise it becomes a new `assistant` turn (mempalace/normalize.py:L235-L246).

## `_try_codex_jsonl(content) -> Optional[str]` (OpenAI Codex CLI)

Newline-delimited JSON. Requires having seen a `session_meta` entry; `session_meta` lines set a
flag and are skipped (mempalace/normalize.py:L260-L274). Only entries of type `event_msg` are
considered; their `payload` (must be a dict) `type` is read (mempalace/normalize.py:L276-L283).
The `payload.message` must be a string; trimmed; empty skipped (mempalace/normalize.py:L284-L289).
`user_message` payloads become `user` turns, `agent_message` payloads become `assistant` turns
(mempalace/normalize.py:L291-L294). Transcript produced only if there are at least 2 messages AND a
`session_meta` was seen (mempalace/normalize.py:L296-L298).

## `_try_gemini_jsonl(content) -> Optional[str]` (Gemini CLI session JSONL)

Newline-delimited JSON requiring a `session_metadata` sentinel record. Any `user`/`gemini` turn
appearing before the `session_metadata` record is discarded as preamble
(mempalace/normalize.py:L319-L338). Only `user` and `gemini` entry types are kept; others
(including `message_update`) are skipped (mempalace/normalize.py:L340-L342). Each entry's
`content` must be a list; each block's `text` (a non-empty string) is collected and the blocks are
joined in order by newlines (mempalace/normalize.py:L344-L357). `user` -> `user` turn,
`gemini` -> `assistant` turn (mempalace/normalize.py:L359-L362). Requires >= 2 messages and the
sentinel (mempalace/normalize.py:L364-L366).

## `_try_pi_jsonl(content) -> Optional[str]` (Pi agent JSONL)

Newline-delimited JSON requiring a `session` entry that also contains a `version` key (sets a
header flag, skipped) (mempalace/normalize.py:L383-L394). Only `message`-type entries are
processed; their `message` (a dict) provides `role` and `content`
(mempalace/normalize.py:L396-L404). Text extracted via `_extract_content`. `role == "user"` ->
`user` turn; `role == "assistant"` -> `assistant` turn; both require non-empty text
(mempalace/normalize.py:L406-L409). Tool-result roles are not handled here (skipped). Requires
>= 2 messages and the session header (mempalace/normalize.py:L411-L413).

## `_try_gemini_json(data) -> Optional[str]` (Gemini / Google AI Studio JSON)

Accepts three layouts: a dict with `contents`; a dict with `messages`; or a top-level list — the
selected array must be a list of length >= 2 (mempalace/normalize.py:L439-L452). For each dict
item, text is extracted by trying `parts` first (a list whose string elements and `{text}` dict
elements are space-joined and trimmed), else falling back to `_extract_content` on `content`
(mempalace/normalize.py:L456-L476). `role == "user"` -> `user` turn; `role == "model"` ->
`assistant` turn AND sets the disambiguator flag; `role == "assistant"` -> `assistant` turn but
does NOT set the flag (mempalace/normalize.py:L478-L486). The parser returns `None` unless at least
one `role == "model"` entry was seen — this prevents claiming Claude/ChatGPT exports
(mempalace/normalize.py:L488-L495).

## `_try_claude_ai_json(data) -> Optional[str]` (Claude.ai export)

If `data` is a dict, it is replaced by `data["messages"]` or `data["chat_messages"]` (else empty
list); must end up a list (mempalace/normalize.py:L500-L503). Privacy-export shape: if the first
element is a dict containing `chat_messages` or `messages`, each conversation object is parsed
independently via `_collect_claude_messages` (using `chat_messages` then `messages`); each
conversation yielding >= 2 messages becomes its own transcript, and all such transcripts are
joined by a blank-line separator (`"\n\n"`); if none qualify, return `None`
(mempalace/normalize.py:L505-L518). Otherwise treat `data` as a flat message list
(mempalace/normalize.py:L520-L524).

`_collect_claude_messages(items) -> list`: the author field is `role` or, failing that, `sender`;
text is `_extract_content(content)` or a fallback top-level `text` key. `role` in
`{user, human}` -> `user` turn; `role` in `{assistant, ai}` -> `assistant` turn; both require
non-empty text (mempalace/normalize.py:L527-L544).

## `_try_chatgpt_json(data) -> Optional[str]` (ChatGPT conversations.json)

Requires a dict with a `mapping` key (mempalace/normalize.py:L548-L551). Root selection: prefer a
node with `parent is None` AND `message is None` (synthetic root); else the first node with
`parent is None` that has a message is used as fallback (mempalace/normalize.py:L553-L564). Then
the tree is walked from the root by always following the first child (`children[0]`), with a
visited set guarding against cycles (mempalace/normalize.py:L565-L582). For each node with a
message: `author.role` is read; `content.parts` (when content is a dict) string elements are
space-joined and trimmed; `role == "user"` -> `user` turn, `role == "assistant"` -> `assistant`
turn, both requiring text (mempalace/normalize.py:L571-L582). Requires >= 2 messages
(mempalace/normalize.py:L583-L585).

## `_try_slack_json(data) -> Optional[str]` (Slack export)

Requires a top-level list (mempalace/normalize.py:L599). Only items that are dicts with
`type == "message"` are processed (mempalace/normalize.py:L604-L605). Speaker id is `user` or
`username`; it is sanitized by replacing each of `[`, `]`, newline, carriage return, and control
characters (`\x00`-`\x1f`) with `_`, then trimmed — this prevents chunk-boundary injection
(mempalace/normalize.py:L607-L610). Messages with empty text or empty speaker id are skipped
(mempalace/normalize.py:L611-L613).

Role assignment alternates: the first-seen speaker is `user`; each subsequently-seen new speaker
is assigned `assistant` if the last assigned role was `user`, else `user`; an already-seen speaker
keeps its assigned role (mempalace/normalize.py:L614-L622). Each message text is prefixed with
`[<user_id>] ` to preserve the original author (mempalace/normalize.py:L623-L624).

Output contract: when >= 2 messages, the transcript has the provenance footer appended:
`"\n[source: slack-export | multi-party chat — speaker roles are positional, not verified]"`
(mempalace/normalize.py:L29-L31, L625-L627).

## `_try_continue_json(data) -> Optional[str]` (Continue.dev session)

Requires a dict with a `history` key that is a list (mempalace/normalize.py:L638-L642). For each
dict item: `content` may be a list of blocks (`text`-type blocks' `text`, or raw string elements,
newline-joined and trimmed) or a plain string (trimmed); other content types skip the item; empty
text skips (mempalace/normalize.py:L645-L667). `role == "user"` -> `user` turn;
`role == "assistant"` -> `assistant` turn; `role == "tool"` -> the text (prefixed `[tool] `) is
appended (newline-joined) to the previous turn only if that turn is an assistant turn, otherwise
dropped. System and other roles are skipped (mempalace/normalize.py:L669-L679). Requires >= 2
messages (mempalace/normalize.py:L680-L682).

## Content extraction: `_extract_content(content, tool_use_map=None) -> str`

If `content` is a string, returns it trimmed (mempalace/normalize.py:L693-L694). If a list, each
element is handled: raw strings are kept; dict blocks with `type == "text"` contribute their
`text`; `type == "tool_use"` contributes `_format_tool_use(block)`; `type == "tool_result"`
contributes `_format_tool_result(...)` (resolving the tool name from `tool_use_map` by
`tool_use_id`, defaulting `"Unknown"`, omitting empty results). Non-empty parts are newline-joined
and trimmed (mempalace/normalize.py:L695-L713). If `content` is a dict, returns its `text`
trimmed; otherwise returns the empty string (mempalace/normalize.py:L714-L716).

## Tool-use rendering: `_format_tool_use(block) -> str`

Produces a one-line human-readable summary keyed off the tool `name` (default `"Unknown"`); a
list-typed `input` is coerced to empty dict (mempalace/normalize.py:L719-L724):
- `Bash`: `"[Bash] <command>"`, command truncated to 200 chars with `"..."` suffix if longer
  (mempalace/normalize.py:L726-L730).
- `Read`: `"[Read <path>]"`; when both `offset` and `limit` are present,
  `"[Read <path>:<offset>-<offset+limit>]"`, or `"[Read <path>:<offset>+<limit>]"` if the integer
  addition fails (mempalace/normalize.py:L732-L741).
- `Grep`: `"[Grep] <pattern> in <target>"` where target is `path` or `glob` or empty
  (mempalace/normalize.py:L743-L746).
- `Glob`: `"[Glob] <pattern>"` (mempalace/normalize.py:L748-L750).
- `Edit`/`Write`: `"[<name> <path>]"` (mempalace/normalize.py:L752-L754).
- Any other tool: `"[<name> <compact-json-input>]"` with the JSON input truncated to 200 chars
  plus `"..."` if longer (mempalace/normalize.py:L756-L760).

## Tool-result rendering: `_format_tool_result(content, tool_name) -> str`

Normalizes list-of-blocks (text blocks and raw strings, newline-joined) or stringifies non-list
content, then trims; empty yields the empty string (mempalace/normalize.py:L778-L792). Behavior by
tool name:
- `Read`/`Edit`/`Write`: result omitted (empty string) (mempalace/normalize.py:L794-L796).
- `Bash`: if line count <= 40 (2 * 20), each line is prefixed `"→ "`; otherwise the first 20 and
  last 20 lines are kept with an elision line `"→ ... [<omitted> lines omitted] ..."` between them
  (mempalace/normalize.py:L800-L814).
- `Grep`/`Glob`: if line count <= 20, all lines kept (prefixed `"→ "`); otherwise first 20 kept
  with trailing `"→ ... [<remaining> more matches]"` (mempalace/normalize.py:L816-L823).
- Unknown tool: if text exceeds 2048 bytes/chars, truncated to 2048 with suffix
  `"... [truncated, <len> chars]"`; otherwise returned as `"→ <text>"`
  (mempalace/normalize.py:L825-L828).

Constants: Bash head/tail line count = 20; Grep/Glob match cap = 20; unknown-tool byte cap = 2048
(mempalace/normalize.py:L763-L765).

## CLI behavior (`python normalize.py <filepath>`)

When run as a script: if fewer than 2 argv elements, prints `"Usage: python normalize.py <filepath>"`
and exits with status code 1 (mempalace/normalize.py:L863-L868). Otherwise it normalizes the given
path, counts lines whose trimmed text starts with `">"`, and prints the base filename, a summary
line `"Normalized: <N> chars | <Q> user turns detected"`, and a preview of the first 20 lines
under a `"--- Preview (first 20 lines) ---"` header (mempalace/normalize.py:L869-L875).
