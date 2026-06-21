# Behavior Specification: `mempalace.normalize`

Derived from the test suite `tests/test_normalize.py`. This describes the
observable contract of the transcript-normalization module: a set of parsers
that convert various chat/transcript file formats into a canonical text
transcript, plus content extraction, tool-block formatting, and noise
stripping. All claims cite the test that pins the behavior.

## Public surface

The module exposes: `normalize`, `strip_noise`, `_extract_content`,
`_format_tool_use`, `_format_tool_result`, `_messages_to_transcript`,
`_try_chatgpt_json`, `_try_claude_ai_json`, `_try_claude_code_jsonl`,
`_try_codex_jsonl`, `_try_gemini_json`, `_try_gemini_jsonl`,
`_try_continue_json`, `_try_normalize_json`, `_try_pi_jsonl`,
`_try_slack_json`, and a constant `_SLACK_PROVENANCE_FOOTER`
(tests/test_normalize.py:L4-L22).

## Canonical transcript format

Across all parsers, user/human turns are emitted as lines prefixed with
`> ` (a blockquote marker) and assistant/model turns are emitted as plain
text (tests/test_normalize.py:L341-L350, L474-L484). Distinct conversational
blocks are separated by a blank line (`\n\n` boundary)
(tests/test_normalize.py:L941-L945).

## `normalize(path)` — top-level entry point

Takes a filesystem path (string) and returns the normalized transcript text
(tests/test_normalize.py:L28-L33).

- A plain-text file is returned with its content preserved; e.g. a file
  containing `Hello world` yields output containing `Hello world`
  (tests/test_normalize.py:L28-L33).
- A `.json` file containing a list of `{"role","content"}` messages is parsed
  and the message text appears in the output (tests/test_normalize.py:L35-L40).
- An empty file yields an output whose stripped form is the empty string
  (tests/test_normalize.py:L43-L47). A whitespace-only file likewise yields a
  stripped-empty result (tests/test_normalize.py:L77-L81).
- An unreadable / nonexistent path raises an I/O error whose message contains
  `Could not read` (tests/test_normalize.py:L50-L56).
- A file already containing 3 or more lines beginning with `>` is treated as
  already-normalized and passes through unchanged (exact string equality)
  (tests/test_normalize.py:L59-L65).
- A `.txt` file whose content begins with `[` triggers JSON parsing; the
  parsed message text appears in the output (tests/test_normalize.py:L68-L74).
- A file larger than 500 MB raises an I/O error (before reading) whose message
  contains `too large` (case-insensitive) (tests/test_normalize.py:L1683-L1690).

## `_extract_content(content, tool_use_map=None)`

Normalizes a message `content` value (which may be a string, list, dict, or
null) into flattened text.

- A string returns itself unchanged (tests/test_normalize.py:L87-L88,
  L1654-L1657).
- A list of strings is joined with newlines: `["hello","world"]` →
  `"hello\nworld"` (tests/test_normalize.py:L91-L92).
- A list of typed blocks extracts only `text`-type blocks; non-text blocks
  (e.g. `image`) are dropped: `[{type:text,text:hello},{type:image,...}]` →
  `"hello"` (tests/test_normalize.py:L95-L97). Multiple text blocks are kept in
  order (tests/test_normalize.py:L1643-L1651).
- A dict with a `text` key returns that text: `{"text":"hello"}` → `"hello"`
  (tests/test_normalize.py:L100-L101).
- `None` returns the empty string (tests/test_normalize.py:L104-L105).
- A mixed list of bare strings and text blocks is joined with newlines:
  `["plain",{type:text,text:block}]` → `"plain\nblock"`
  (tests/test_normalize.py:L108-L110).
- A `tool_use` block embedded in content is rendered via the tool-use
  formatter and included in the output alongside text
  (tests/test_normalize.py:L1487-L1495).
- A `tool_result` block is rendered via the tool-result formatter. When a
  `tool_use_map` mapping `tool_use_id` → tool name is supplied, the result is
  formatted using that tool's rules (tests/test_normalize.py:L1498-L1504).
  Without a map entry, a fallback strategy is applied that still emits the
  result prefixed with `→ ` (tests/test_normalize.py:L1507-L1513).

## `_format_tool_use(block)`

Renders a `tool_use` block to a compact one-line breadcrumb. Behavior is
keyed on the tool `name`:

- `Bash`: `"[Bash] " + command`, where only the `command` field is shown (the
  `description` is omitted): `[Bash] lsusb | grep razer`
  (tests/test_normalize.py:L116-L124). A command longer than 200 characters is
  truncated to at most `"[Bash] "` + 200 chars + `"..."` and ends with `...`
  (tests/test_normalize.py:L127-L131).
- `Read`: `"[Read <file_path>]"` (tests/test_normalize.py:L134-L142). When
  `offset` and `limit` are present, a line range is appended as
  `:offset-(offset+limit)`, e.g. offset 10 / limit 50 →
  `[Read /home/jp/file.py:10-60]` (tests/test_normalize.py:L145-L153).
- `Grep`: `"[Grep] <pattern> in <path>"` using the `path` field
  (tests/test_normalize.py:L156-L164), or the `glob` field when `path` is
  absent: `[Grep] TODO in *.py` (tests/test_normalize.py:L167-L175).
- `Glob`: `"[Glob] <pattern>"` (tests/test_normalize.py:L178-L186).
- `Edit`: `"[Edit <file_path>]"` (tests/test_normalize.py:L189-L197).
- `Write`: `"[Write <file_path>]"` (tests/test_normalize.py:L200-L208).
- Any unknown tool: rendered as `"[<name>] "` followed by a textual summary of
  the input that includes input values, e.g. `[mcp__mempalace__search]` with
  `firmware probe` present (tests/test_normalize.py:L211-L220). The summary is
  truncated to at most `"[<name>] "` + 200 chars + `"..."`, ending with `...`
  (tests/test_normalize.py:L223-L227).

## `_format_tool_result(content, tool_name)`

Renders a tool result. Output (when non-empty) is prefixed with `→ `.

- `Bash`, short output: kept in full, prefixed with `→ `
  (tests/test_normalize.py:L233-L237).
- `Bash`, long output (more than 40 lines): truncated to a head + tail with a
  gap marker. For 60 lines, lines 0-19 and 40-59 are kept while lines 20-39 are
  replaced by a `20 lines omitted` marker (tests/test_normalize.py:L240-L251).
  Output of exactly 40 lines is not truncated (no `omitted` marker)
  (tests/test_normalize.py:L254-L261).
- `Read`, `Edit`, `Write` results are entirely omitted, returning the empty
  string (file content lives in project mining; diffs live in git)
  (tests/test_normalize.py:L264-L279).
- `Grep` / `Glob`: short output is kept with each line prefixed `→ `
  (tests/test_normalize.py:L282-L287). Output beyond 20 lines is capped: the
  first 20 lines (indices 0-19) are kept, line 20+ dropped, and a
  `<remaining> more matches` marker is appended (e.g. `10 more matches` for 30
  lines, `5 more matches` for 25 lines)
  (tests/test_normalize.py:L290-L307).
- Unknown tool, short output: kept, prefixed `→ `
  (tests/test_normalize.py:L310-L313).
- Unknown tool, output over 2 KB: truncated, ending with
  `... [truncated, <N> chars]` where N is the original length; total result
  length stays under ~2200 chars (tests/test_normalize.py:L316-L321).
- Result `content` may itself be a list of text blocks; all block text is
  included (tests/test_normalize.py:L324-L329).
- Empty result content returns the empty string
  (tests/test_normalize.py:L332-L335).

## `_try_claude_code_jsonl(text)` — Claude Code JSONL

Parses newline-delimited JSON where each line has a `type` (`human`/`user` for
user, `assistant` for assistant) and a `message` object with `content`.

- A valid two-line session yields `> <user text>` and the assistant text
  (tests/test_normalize.py:L341-L350). `type:"user"` is equivalent to
  `type:"human"` (tests/test_normalize.py:L352-L359).
- Fewer than 2 messages returns null (tests/test_normalize.py:L362-L365).
- Malformed JSON lines are skipped without aborting; remaining valid lines
  still produce a transcript (tests/test_normalize.py:L368-L375). Non-dict JSON
  entries (e.g. a JSON list) are skipped (tests/test_normalize.py:L378-L385).
- `tool_use` and `tool_result` blocks are captured: a Bash tool_use renders as
  `[Bash] ...`, its result renders as `→ ...`, and surrounding text turns
  appear (tests/test_normalize.py:L1516-L1557). `Read` tool_use keeps the
  `[Read <path>]` breadcrumb while its result content is omitted
  (tests/test_normalize.py:L1560-L1599).
- A user message whose content contains ONLY tool_results (no text) does NOT
  create an additional `>` user turn; only genuine user-text turns are counted
  (tests/test_normalize.py:L1602-L1640).
- `thinking`-type blocks are ignored; neither `thinking` nor `signature` text
  appears in the output (tests/test_normalize.py:L1660-L1680).

## `_try_codex_jsonl(text)` — Codex JSONL

Requires a `session_meta`-type record to be present; otherwise returns null
(tests/test_normalize.py:L402-L409).

- A valid session with `event_msg` records carrying `payload.type`
  `user_message` / `agent_message` produces `> <user>` and assistant text
  (tests/test_normalize.py:L391-L399).
- Records whose `type` is not `event_msg` (e.g. `response_item`) are skipped
  and do not contribute their text (tests/test_normalize.py:L412-L421).
- A message field that is not a string is tolerated (skipped) without aborting
  (tests/test_normalize.py:L424-L432). Empty/whitespace-only message text is
  skipped (tests/test_normalize.py:L435-L443). A `payload` that is not a dict
  is tolerated (tests/test_normalize.py:L446-L454).

## `_try_gemini_jsonl(text)` — Gemini CLI JSONL

Requires a `session_metadata` record; without one, returns null to avoid
false-positives against Claude Code / Codex JSONL routed through the dispatch
chain (tests/test_normalize.py:L502-L510, L587-L597).

- Valid sessions emit `user` turns as `> ...` and `gemini` turns as assistant
  text (tests/test_normalize.py:L474-L499). `content` is an array of
  `{"text":...}` blocks; multiple blocks in one message are concatenated in
  order into one turn (tests/test_normalize.py:L538-L555).
- `message_update` records (token-count deltas) are ignored; no `tokens` /
  `input` text leaks into the output (tests/test_normalize.py:L513-L526).
- Fewer than 2 conversational messages returns null
  (tests/test_normalize.py:L528-L535).
- A message whose `content` yields no text is skipped (no empty turn emitted)
  (tests/test_normalize.py:L558-L570).
- Malformed JSON lines mid-stream are skipped; parsing continues
  (tests/test_normalize.py:L573-L584).
- `user`/`gemini` turns appearing BEFORE the `session_metadata` sentinel are
  silently discarded; only turns after the sentinel contribute
  (tests/test_normalize.py:L600-L616).

## `_try_gemini_json(data)` — Gemini JSON (3 layouts)

Accepts three layouts: Layout 1 `{"contents":[...]}` with `parts` arrays;
Layout 2a `{"messages":[...]}`; Layout 2b a flat top-level list
(tests/test_normalize.py:L622-L692).

- The `model` role is treated as the assistant role
  (tests/test_normalize.py:L641-L692).
- Multiple `text` parts within one message are joined with a single space:
  `"Part one. Part two."` (tests/test_normalize.py:L694-L710).
- Non-text parts (`inline_data`, `function_call`, …) are skipped; their data
  (e.g. `image/png`) does not bleed into the transcript
  (tests/test_normalize.py:L712-L731).
- Input with no `role="model"` entry returns null, so this parser does not
  claim Claude/ChatGPT exports that use `assistant`
  (tests/test_normalize.py:L733-L744).
- Fewer than 2 entries returns null (tests/test_normalize.py:L746-L749).
- Scalar / non-dict / non-list input (`"not a dict"`, `42`, `None`) returns
  null cleanly (tests/test_normalize.py:L751-L755).
- Ordering contract: this parser runs BEFORE `_try_claude_ai_json` in the
  dispatch chain so a `{"messages":[...model...]}` payload is recognized as
  Gemini (preserving model turns) rather than being claimed by the Claude
  parser which would drop them (tests/test_normalize.py:L655-L677, L757-L777).

## `_try_claude_ai_json(data)` — Claude.ai exports

- A flat list of `{"role","content"}` messages produces `> <user>` turns
  (tests/test_normalize.py:L783-L790).
- A dict with a `messages` key, or with a `chat_messages` key, is accepted
  (tests/test_normalize.py:L793-L801, L829-L837).
- Privacy-export shape: a list of conversation objects each containing
  `chat_messages` (or `messages`) is accepted; `human`/`ai` roles map to
  user/assistant (tests/test_normalize.py:L804-L815, L856-L870).
- Non-dict input returns null (tests/test_normalize.py:L818-L820). Fewer than 2
  messages returns null (tests/test_normalize.py:L823-L826).
- Non-dict items within a conversation's message list are skipped, and a
  top-level non-conversation item is skipped (tests/test_normalize.py:L840-L853).
- The `sender` field is accepted as an alternative to `role`
  (tests/test_normalize.py:L873-L885). When `content` is empty, the `text`
  field is used as a fallback (tests/test_normalize.py:L888-L900). A null
  `text` field must not crash; `content` is used instead
  (tests/test_normalize.py:L903-L915).
- Multiple conversations produce separate transcript blocks (split on the
  blank-line boundary); each conversation appears as its own block
  (tests/test_normalize.py:L918-L945). Conversations with fewer than 2 messages
  are skipped (tests/test_normalize.py:L948-L966).

## `_try_chatgpt_json(data)` — ChatGPT export

Expects a dict with a `mapping` of nodes, each node having `parent`, `message`,
`children`, forming a tree (tests/test_normalize.py:L972-L1000).

- A valid mapping produces `> <user text>` turns
  (tests/test_normalize.py:L972-L1000).
- A dict without a `mapping` key returns null (tests/test_normalize.py:L1003-L1005).
- Non-dict input (e.g. a list) returns null (tests/test_normalize.py:L1008-L1010).
- When the root node itself carries a message (no synthetic root), a fallback
  path is used and parsing still succeeds (tests/test_normalize.py:L1013-L1044).
- Fewer than 2 messages returns null (tests/test_normalize.py:L1047-L1066).

## `_try_slack_json(data)` — Slack export

- A list of `{"type":"message","user","text"}` records is accepted; message
  text appears in the output (tests/test_normalize.py:L1072-L1079).
- Non-list input returns null (tests/test_normalize.py:L1082-L1084). Fewer than
  2 messages returns null (tests/test_normalize.py:L1087-L1090).
- Records whose `type` is not `message` (e.g. `channel_join`) are skipped
  (tests/test_normalize.py:L1093-L1100). Three or more distinct speakers are
  handled (tests/test_normalize.py:L1103-L1111). Empty-text messages are
  skipped (tests/test_normalize.py:L1114-L1121). The `username` field is a
  fallback when `user` is absent (tests/test_normalize.py:L1124-L1130).
- Each emitted message is prefixed with its original speaker ID in brackets,
  e.g. `[U1]`, `[U2]` (tests/test_normalize.py:L1146-L1154). The speaker placed
  first is still attributed to their own ID rather than appearing as an
  anonymous user turn (tests/test_normalize.py:L1157-L1166).
- Speaker IDs containing brackets or newlines are sanitized so injected
  sequences like `] injected` or `\n> fake` do not pass through (preventing
  chunk-boundary injection) (tests/test_normalize.py:L1169-L1179).
- The transcript ends with `_SLACK_PROVENANCE_FOOTER` (a footer, not a header,
  so it cannot become a standalone search drawer via paragraph chunking); the
  footer text mentions `multi-party` and `positional`
  (tests/test_normalize.py:L1133-L1142).

## `_try_continue_json(data)` — Continue.dev JSON

Expects a dict with a `history` list of `{"role","content"}` entries
(tests/test_normalize.py:L1185-L1203).

- Multi-turn `user`/`assistant` history produces `> <user>` and assistant
  text in order (tests/test_normalize.py:L1185-L1203).
- `system` messages are skipped (their text never appears)
  (tests/test_normalize.py:L1205-L1218).
- `tool` messages are appended to the preceding assistant turn, rendered as
  `[tool] <content>` (tests/test_normalize.py:L1220-L1236). A `tool` message
  with no preceding assistant turn is ignored
  (tests/test_normalize.py:L1413-L1424).
- Code blocks (triple-backtick fences) in content are preserved verbatim
  (tests/test_normalize.py:L1238-L1253). Content given as a list of text blocks
  is supported (tests/test_normalize.py:L1255-L1266).
- Empty history, single message, or missing `history` key returns null
  (tests/test_normalize.py:L1269-L1287). Non-dict input returns null
  (tests/test_normalize.py:L1290-L1295). A `history` value that is not a list
  returns null (tests/test_normalize.py:L1298-L1302).
- Non-dict history entries are skipped (tests/test_normalize.py:L1305-L1318).
  Entries missing `role` are skipped (tests/test_normalize.py:L1320-L1332).
  Entries missing `content` are skipped (tests/test_normalize.py:L1334-L1346).
  Entries with empty/whitespace content are skipped — verified by counting
  emitted `>` user turns (tests/test_normalize.py:L1348-L1361). Non-string,
  non-list content (int, null) is skipped
  (tests/test_normalize.py:L1398-L1410).
- Unicode / CJK content and emoji are preserved
  (tests/test_normalize.py:L1364-L1381). Very long messages (50,000 chars) are
  handled without error (tests/test_normalize.py:L1384-L1396).
- Integrates via `normalize()`: a `.json` file with a `history` is detected and
  parsed (tests/test_normalize.py:L1427-L1441).

## `_try_pi_jsonl(text)` — Pi agent JSONL

Requires a `session` record that includes a `version` field; otherwise returns
null. A `session` record missing `version` is not a valid header
(tests/test_normalize.py:L1883-L1902).

- Message records have shape `{"type":"message","message":{"role","content"}}`.
  User string content is captured as `> <text>`
  (tests/test_normalize.py:L1850-L1860). Assistant content given as
  `[{type,text}]` blocks is captured (tests/test_normalize.py:L1863-L1881).
- `toolResult`-role records are skipped (operational, not conversation); their
  text does not appear (tests/test_normalize.py:L1905-L1921).
- Fewer than 2 captured turns returns null
  (tests/test_normalize.py:L1923-L1931).
- Malformed JSON lines and non-dict entries are tolerated, not fatal
  (tests/test_normalize.py:L1933-L1943).

## `_try_normalize_json(text)` — dispatcher

- Invalid JSON input returns null (tests/test_normalize.py:L1447-L1449).
- Valid JSON of an unrecognized schema returns null
  (tests/test_normalize.py:L1452-L1454).

## `_messages_to_transcript(messages, spellcheck=...)`

Takes an ordered list of `(role, text)` pairs and produces the canonical
transcript. User turns become `> <text>`; assistant turns become plain text
(tests/test_normalize.py:L1460-L1466). The function accepts a `spellcheck`
flag; when an external `spellcheck_user_text` hook exists it may be applied
(tests/test_normalize.py:L1462-L1466).

- Two consecutive `user` messages each become their own `> ` turn
  (tests/test_normalize.py:L1468-L1474).
- A leading `assistant` message (no preceding user) is emitted as plain text
  before the first `> ` turn (tests/test_normalize.py:L1476-L1482).

## `strip_noise(text)` — verbatim-safety boundary

Removes system-injected chrome while preserving all user-authored prose. The
governing principle is "Verbatim always": never delete user text.

User-content preservation (output equals input with surrounding whitespace
trimmed):

- Prose mentioning `stop hook` / multiple stop-hook sentences is preserved
  intact (tests/test_normalize.py:L1704-L1714).
- Inline `<system-reminder>...</system-reminder>` tags occurring within user
  prose are preserved (tests/test_normalize.py:L1716-L1725).
- A `(ctrl+o to expand)` hint embedded in prose is preserved
  (tests/test_normalize.py:L1727-L1735).
- Inline `CURRENT TIME:` text inside a sentence is preserved
  (tests/test_normalize.py:L1737-L1739).
- Inline `+50 lines` marker inside prose is preserved
  (tests/test_normalize.py:L1741-L1743).
- A dangling/unclosed `<system-reminder>` in one message must NOT merge with a
  closing tag in a later message and delete everything between; all three
  message bodies survive (tests/test_normalize.py:L1745-L1757).

System-chrome removal:

- A line-anchored `<system-reminder>` block (including a blockquote-prefixed
  `> <system-reminder>...` shape) is stripped while the real message survives
  (tests/test_normalize.py:L1763-L1779).
- A standalone `Ran N <Hook> hook` line is stripped; the known hook names are
  `Stop`, `PreCompact`, `PreToolUse`, `PostToolUse`, `UserPromptSubmit`
  (tests/test_normalize.py:L1780-L1789).
- A standalone `CURRENT TIME: ...` line is stripped
  (tests/test_normalize.py:L1791-L1795). A standalone collapsed-lines marker
  `… +N lines` is stripped (tests/test_normalize.py:L1797-L1801).
- Claude Code collapsed-output chrome `[N tokens] (ctrl+o to expand)` is
  stripped while the surrounding output text survives
  (tests/test_normalize.py:L1803-L1809).
- Each known noise tag is stripped (both the tag and its enclosed junk) while
  real content survives: `system-reminder`, `command-message`, `command-name`,
  `task-notification`, `user-prompt-submit-hook`, `hook_output`
  (tests/test_normalize.py:L1811-L1822).
- Excessive blank lines are collapsed to no more than 3 consecutive newlines
  (no run of 4+ newlines remains) (tests/test_normalize.py:L1825-L1831).
