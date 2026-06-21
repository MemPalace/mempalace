# Spec: `mempalace/sweeper.py`

Message-granular miner that ingests individual user/assistant messages from a
Claude Code session JSONL file into a palace, catching content the file-level
miners drop. Each drawer holds exactly one message exchange (~1-5 KB, no size
caps) (mempalace/sweeper.py:L1-L23).

## Public surface

- `parse_claude_jsonl(path: str) -> Iterator[dict]` — streams normalized message records from a JSONL file (mempalace/sweeper.py:L88-L141).
- `get_palace_cursor(collection, session_id: str) -> Optional[str]` — returns the max stored timestamp for a session (mempalace/sweeper.py:L147-L177).
- `sweep(jsonl_path: str, palace_path: str, source_label: Optional[str] = None) -> dict` — ingests one JSONL file (mempalace/sweeper.py:L193-L299).
- `sweep_directory(dir_path: str, palace_path: str) -> dict` — recursively sweeps all `.jsonl` files in a directory (mempalace/sweeper.py:L302-L347).
- Helpers `_flatten_content(content) -> str` and `_drawer_id_for_message(session_id, message_uuid) -> str` (mempalace/sweeper.py:L56-L85, L183-L190).

## Content flattening

`_flatten_content` normalizes message content to a plain string. A string is
returned as-is. A list of content blocks is processed verbatim per block, joined
by newlines, dropping empty parts (mempalace/sweeper.py:L56-L85). Block rendering:
- `text` block → its `text` field (mempalace/sweeper.py:L73-L74).
- `tool_use` block → `[tool_use: <name or ?> input=<json of input or {}>]` (mempalace/sweeper.py:L75-L79).
- `tool_result` block → `[tool_result: <json of content or "">]` (mempalace/sweeper.py:L80-L81).
- any other block type → `[<type>: <json of whole block>]` (mempalace/sweeper.py:L82-L83).
- Non-dict list items are skipped; non-string non-list content is stringified (mempalace/sweeper.py:L70-L71, L85).

JSON serialization of tool inputs/contents/blocks uses a string fallback for
non-serializable values, never truncating (mempalace/sweeper.py:L78-L83).

## JSONL parsing contract

`parse_claude_jsonl` opens the file as UTF-8 with malformed bytes replaced
(mempalace/sweeper.py:L105). For each line: blank lines are skipped; lines that
fail JSON parsing are skipped silently (mempalace/sweeper.py:L106-L113). A record
is yielded only if ALL of these hold: top-level `type` is `"user"` or
`"assistant"`; `message` is a dict; `message.role` is `"user"` or `"assistant"`;
`timestamp` is present and truthy; `uuid` is present; a session id is present
(from `sessionId` or fallback `session_id`); flattened content is non-empty after
stripping (mempalace/sweeper.py:L114-L134). Each yielded record is:
`{session_id, uuid, timestamp (ISO 8601 string), role, content (flattened string)}`
(mempalace/sweeper.py:L135-L141). Non-message record types are filtered out
(mempalace/sweeper.py:L100-L104).

## Cursor resolution

`get_palace_cursor` queries the collection for all drawers where
`session_id` matches, requesting metadatas, and returns the lexical maximum of
their `timestamp` values, or `None` if no timestamps exist (mempalace/sweeper.py:L160-L177).
ISO-8601 timestamps are compared as strings, not parsed (mempalace/sweeper.py:L150-L152).
If the backend query raises, it logs a WARNING and returns `None`, which causes
the caller to treat the session as empty and re-ingest every message (safe
because of deterministic IDs) (mempalace/sweeper.py:L160-L172).

## Drawer ID determinism

A drawer's ID is `sweep_<session_id>_<message_uuid>`, using the full session id
(no prefix) (mempalace/sweeper.py:L183-L190). This determinism makes reruns
idempotent: the same message always maps to the same drawer ID.

## Sweep algorithm and ordering

`sweep` creates/opens the collection at `palace_path` (`create=True`)
(mempalace/sweeper.py:L217). It iterates records in file order. The cursor for a
session is resolved lazily on first encounter and cached per session
(mempalace/sweeper.py:L262-L267). A record is skipped (counted in
`drawers_skipped`) only if its cursor is not `None` AND `timestamp < cursor`
(strict less-than). Records at `timestamp == cursor` are NOT skipped, so messages
sharing the max timestamp that were not yet ingested are still picked up
(mempalace/sweeper.py:L196-L205, L268-L270).

For each non-skipped record, a drawer is staged with:
- document text `"<ROLE-UPPERCASE>: <content>"` (mempalace/sweeper.py:L273).
- metadata: `session_id`, `timestamp`, `message_uuid`, `role`,
  `source_file` (= `source_label` if given else `jsonl_path`),
  `filed_at` (current local time ISO string), `ingest_mode = "sweep"`
  (mempalace/sweeper.py:L274-L282).

## Batching and metric honesty

Writes are batched at size 64 (mempalace/sweeper.py:L227, L288-L289), with a
final flush after the loop (mempalace/sweeper.py:L291). On each flush: a
pre-flight existence check fetches which of the batch IDs already exist; rows not
present count as `drawers_added`, rows present count as `drawers_already_present`;
then all rows are upserted (mempalace/sweeper.py:L229-L260). If the pre-check
raises, it logs a WARNING and treats all batch rows as new — the upsert still
runs, so the metric may over-count on reruns but no data is lost
(mempalace/sweeper.py:L236-L248). Upsert is idempotent: re-running a sweep
rewrites identical rows under identical IDs.

## sweep return shape

`sweep` returns `{drawers_added, drawers_already_present, drawers_upserted,
drawers_skipped, cursor_by_session}` where `drawers_upserted = drawers_added +
drawers_already_present` and `cursor_by_session` maps each seen session id to its
resolved cursor (string or `None`) (mempalace/sweeper.py:L293-L299).

## Directory sweep

`sweep_directory` resolves `dir_path` (expanding `~`) and finds all `*.jsonl`
files recursively, sorted (mempalace/sweeper.py:L310-L311). Each file is swept
with its path as `source_label` (mempalace/sweeper.py:L319-L321). If a file's
sweep raises, the error is logged, a WARNING line is printed to stderr, the
failure is recorded as `{file, error}`, and processing continues
(mempalace/sweeper.py:L322-L326). It returns `{files_attempted (total discovered,
including failures), files_succeeded (completed without error), drawers_added,
drawers_already_present, drawers_skipped, per_file (list of {file, added,
already_present, skipped}), failures}` (mempalace/sweeper.py:L327-L347).

## Side effects

- Filesystem reads: the input JSONL file(s) (mempalace/sweeper.py:L105, L311).
- Palace writes via the collection backend obtained from `get_collection`
  (mempalace/sweeper.py:L48, L217, L251-L255).
- stderr output on per-file failure in directory mode (mempalace/sweeper.py:L324).
- Logging at WARNING/ERROR levels (mempalace/sweeper.py:L50, L166, L242, L323).
