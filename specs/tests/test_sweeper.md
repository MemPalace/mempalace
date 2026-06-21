# Behavior Spec — `tests/test_sweeper.py`

This is a TDD test suite that defines the contract for a "tandem sweeper" module
(`mempalace/sweeper.py`) that catches messages the primary file-granularity miner
missed by working at MESSAGE granularity, using timestamp as a coordination cursor
(tests/test_sweeper.py:L1-L16). The behaviors below are the externally observable
contract the implementation must satisfy.

## Module surface required

The implementation must expose, importable from `mempalace.sweeper`:
- `parse_claude_jsonl(path: str) -> iterable of records` (tests/test_sweeper.py:L75-L77)
- `sweep(jsonl_path: str, palace_path: str) -> result dict` (tests/test_sweeper.py:L149-L152)
- `_drawer_id_for_message(session_id: str, uuid: str) -> str` (tests/test_sweeper.py:L239-L243, L264)

It coordinates with `mempalace.palace.get_collection(palace_path, create: bool)` for
backend storage (tests/test_sweeper.py:L238, L262, L306).

## Input format (Claude Code JSONL)

Input is a newline-delimited JSON file, one record per line, terminated by a trailing
newline (tests/test_sweeper.py:L69, L194, L257). Record shapes observed:
- Noise records to be ignored: `{"type": "progress", ...}` and
  `{"type": "file-history-snapshot", "messageId": ...}` (tests/test_sweeper.py:L28-L34, L51-L52).
- User records: `type` = `"user"`, fields `timestamp`, `sessionId`, `uuid`, and
  `message: {role: "user", content: <string>}` (tests/test_sweeper.py:L36-L42).
- Assistant records: `type` = `"assistant"`, fields `timestamp`, `sessionId`, `uuid`,
  and `message: {role: "assistant", content: [ {type:"text", text:...}, ... ]}`
  (tests/test_sweeper.py:L44-L50). Content is a LIST of typed blocks.

## `parse_claude_jsonl` behavior

- Yields ONLY user and assistant records; all noise record types (progress,
  file-history-snapshot) are filtered out, preserving source order
  (tests/test_sweeper.py:L74-L83). For the mock input it yields exactly the role
  sequence `["user", "assistant", "user", "assistant"]` (tests/test_sweeper.py:L79).
- Each yielded record exposes keys `role`, `session_id`, `timestamp`, `uuid`,
  `content` (tests/test_sweeper.py:L78, L88-L92, L98-L100). The source field
  `sessionId` maps to output key `session_id` (tests/test_sweeper.py:L90), and
  `timestamp`/`uuid` pass through unchanged (tests/test_sweeper.py:L91-L92).
- Assistant content (a list of blocks) is flattened to a text string; the text
  payload of text blocks appears in the resulting `content` (e.g. `"Paris"` is a
  substring) (tests/test_sweeper.py:L94-L102).
- Tool blocks must round-trip VERBATIM with no truncation. A `tool_use` block whose
  `input` contains a 5000-character value must appear in full inside the record's
  `content`; no truncation marker and no length cap (the old 500-char cap is
  explicitly disallowed) (tests/test_sweeper.py:L104-L142). For a single-record file
  containing one such assistant message, exactly one record is yielded and the full
  raw 5000-char string is a substring of `content` (tests/test_sweeper.py:L114, L134-L142).

## `sweep` behavior and result contract

`sweep(jsonl_path, palace_path)` returns a result with at least these keys:
- `drawers_added`: count of new message-drawers ingested this run
  (tests/test_sweeper.py:L153, L165-L166, L198, L283).
- `drawers_already_present`: count of records already present in the palace
  (tests/test_sweeper.py:L289-L292).

Observable behaviors:
- Empty palace: a sweep ingests all user/assistant messages. The 4-message mock
  yields `drawers_added == 4` (tests/test_sweeper.py:L148-L156).
- Idempotent: a second sweep over unchanged data is a no-op, returning
  `drawers_added == 0` (tests/test_sweeper.py:L158-L170).
- Resume from cursor: the sweep tracks `max(timestamp)` per session as a cursor. After
  ingesting two messages (timestamps 09:00:00 / 09:00:01) and appending two later
  messages (09:05:00 / 09:05:01) to the same session, a subsequent sweep ingests ONLY
  the 2 new messages (tests/test_sweeper.py:L172-L226).
- Tie at cursor timestamp (crash-recovery): the skip rule must be strictly `<` cursor,
  NOT `<=`, with ties at the exact cursor timestamp broken by deterministic drawer ID
  (tests/test_sweeper.py:L228-L237, L286-L288). Scenario: three messages share one
  timestamp T; two were partially ingested (same drawer IDs the sweeper would use);
  the sweep must pick up the remaining third, returning `drawers_added == 1` and
  `drawers_already_present == 2` (tests/test_sweeper.py:L244-L292).

## Drawer identity and storage contract

- Drawer ID is derived deterministically from `(session_id, uuid)` via
  `_drawer_id_for_message(session_id, uuid)`; the same inputs must yield the same ID
  so that an externally pre-written drawer with that ID is recognized as already
  present (tests/test_sweeper.py:L264, L281-L288).
- Each written drawer carries metadata used for tandem-miner coordination:
  `session_id`, `timestamp`, `message_uuid`, and `role` (where role is one of
  `"user"`/`"assistant"`) (tests/test_sweeper.py:L295-L318). For the mock input every
  drawer has `session_id == "abc"`, a non-empty `timestamp`, a non-empty
  `message_uuid`, and a valid `role` (tests/test_sweeper.py:L311-L318).
- The expected stored metadata schema (from the partial-ingest simulation) is:
  `{session_id, timestamp, message_uuid, role, ingest_mode}` with `ingest_mode` set to
  `"sweep"` (tests/test_sweeper.py:L268-L277). Note the mapping: the parsed record key
  `uuid` is stored under metadata key `message_uuid` (tests/test_sweeper.py:L273, L314).
- Drawer document text for a user message follows the form `"USER: <content>"`
  (tests/test_sweeper.py:L266). The backend collection supports `upsert(ids, documents,
  metadatas)` and `get(include=["metadatas"])` returning `{"metadatas": [...]}`
  (tests/test_sweeper.py:L265-L278, L307-L308).

<promise>SPEC_WRITTEN path=specs/tests/test_sweeper.md citations=24</promise>
