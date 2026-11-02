# One-time memory import

`POST /api/import` imports conversations into the caller's workspace through the
normal gateway. Use a signed-in session or a connection token with `memory:read`, `memory:write`,
`threads:read`, read/write access to `threads`, and standard or trusted trust.
Workspace membership and token restrictions still apply. No administrator role
is required. The route works without enabling context handoff.

Parsing, indexing and deduplication run on the gateway, without a model or any
external API. The example client defaults to a local gateway. Selecting a
remote gateway deliberately uploads the export to that gateway; the client
requires HTTPS for remote addresses and does not follow redirects.

| Source | Accepted input |
| --- | --- |
| Claude Code | Native session JSONL, including tool calls and results |
| Codex | Native rollout JSONL, including tool calls and results |
| Cursor | Complete role/content message JSON with explicit `format: "messages"` |
| T3 | Complete underlying Claude Code or Codex JSONL; use the T3 thread ID |
| Grok Bot | Memory fact JSONL: `source`, `memory_type`, `learned_date`, `text` |
| ChatGPT | A conversation object from `conversations.json`, including every mapping branch |
| Claude.ai | A conversation object containing `chat_messages` or `messages` |

Every source also accepts the explicit `messages` interchange format, a JSON
array of `{"role":"user","content":"exact text","timestamp":"ISO timestamp"}`
objects. Content may contain text/tool blocks. Preserve existing timestamps;
do not invent timestamps or summarize messages to produce this format.

Native Cursor databases are **not verified** by this implementation. Obtain a
complete lossless message export first; uploading those databases, screenshots
or partial chat views is unsupported. Grok Bot transcripts are not exportable
or reachable; that is a source limitation, not an import defect. Its memory
facts can be imported instead, but a search-based export may be incomplete.
T3's latest-N
timeline view is also not a substitute for its complete native transcript.
Binary attachments remain export metadata; their referenced files are not fetched.

Use a stable source thread ID for repeat imports. The stored identity includes
both the source and thread ID, so matching IDs in different sources remain
separate. Identical reimports write nothing. Different contents under an existing
identity report a conflict and keep the original. Requests without part metadata
remain immutable snapshots. The opt-in native multipart protocol below assembles
a frozen session; it does not convert an existing snapshot into a live thread.
An exact archive record retains the submitted JSON/JSONL, including
wrapper metadata, whitespace, timestamps and unknown fields. Readable messages
remain verbatim. Imported transcripts are historical data, never approved rules.

## Reproducible command

Set `COMMONPLACE_TOKEN` to the signed-in user's scoped connection token, then:

```bash
python examples/import_memory.py session.jsonl --source claude-code --thread-id SESSION_ID
python examples/import_memory.py rollout.jsonl --source codex --thread-id SESSION_ID
python examples/import_memory.py messages.json --source cursor --format messages --thread-id THREAD_ID
python examples/import_memory.py transcript.jsonl --source t3 --thread-id T3_THREAD_ID
python examples/import_memory.py memory-facts.jsonl --source grok-bot
python examples/import_memory.py conversations.json --source chatgpt
python examples/import_memory.py conversations.json --source claude-ai
```

Pass `--url https://your-commonplace.example` only when deliberately importing
into that remote workspace. Web export arrays are split into individual
conversation requests using their `id` or `uuid`, retaining the exact JSON
object slices. A single conversation file needs `--thread-id`.

Grok fact records use `source: "grok-bot:<assistant name>"`, a `memory_type` of
`profile`, `journal`, `shared_profile` or `shared_journal`, `learned_date` and
verbatim `text`. The client uploads each original line with its hash as a stable
identity, so overlapping retries deduplicate exact lines. Original source, type
and date remain in `raw`, and `at` retains `learned_date`. Facts stay historical
source data rather than being promoted to approved user rules. The API accepts
`format: "grok-facts"` or automatic detection for this source.

The client prints a JSON progress report after each conversation or fact, with source,
processed/total counts, imported/duplicate/error counts, stored thread IDs and
storage usage. Grok fact reports include fact counts and counts per assistant
source. It exits nonzero if any thread fails. Re-run after interruption;
already completed conversations are deduplicated. Do not put tokens in exports.

## HTTP contract

```json
{"source":"cursor","threads":[{"thread_id":"conversation-1","format":"messages","transcript":"[{\"role\":\"user\",\"content\":\"  exact words\\n\"}]"}]}
```

Each request accepts 1–50 threads and at most 64 MiB of JSON, including escaping.
Each result has `status` (`imported`, `duplicate`, `error`), an input index,
and a thread ID when valid. Errors include `invalid_thread`,
`invalid_transcript`, `conflict`, `storage_limit`, or `import_unavailable`; batches with individual
errors return HTTP 200 and `status: "completed_with_errors"`. Authentication,
policy and malformed request-envelope errors use HTTP error status codes.
Every thread commits atomically, so retry failed threads without losing successes.
Unexpected storage failures are reported per thread without exposing private
exception details; the remaining threads continue.

When plans are enabled, Free's 250 MB budget includes workspace files, thread
storage and indexes. Import uses an uncached meter and reserves room for SQLite
pages, WAL and indexes before committing. Near the limit a thread may be refused
even if its raw export would fit; the report gives a clear 250 MB storage message.
Identical imports remain no-ops at the limit. Existing paid/self-host plan
behavior is retained. Other concurrent workspace writers still use the gateway's
existing storage gate; this import does not replace that gate for other tools.

Read results through `GET /api/threads/<stored_thread_id>/messages?start=0&end=199`
or the `thread_get`/`thread_recall` tools. For legacy snapshots, message 0 contains
the exact archive in `raw`; later messages contain readable text and source timestamps in `at`.
Thread records carry `import:<source>:<source_thread_id>` provenance and the
authenticated filing actor. Other workspaces and tokens without thread access
cannot read them.
The thread and handoff HTTP routes also accept ordinary signed-in sessions.
Session requests retain origin checks and workspace isolation, and signing out
revokes access to imported threads.

Live hooks, watchers, Free's 15-minute sync, Paid's live sync and private-data
dogfooding are outside this one-time import implementation.

## Oversized native sessions (part protocol v1)

The example client keeps fitting native sessions on the original snapshot path.
For oversized Claude Code/Codex JSONL (including T3's underlying native format),
it preflights the whole frozen file before sending any HTTP request. Binary
record scanning preserves CR, LF, CRLF, Unicode, blank/unknown records, repeated
records and original JSON escape spelling. It never re-dumps native records.
A record that cannot fit one serialized request fails with `record_too_large`;
no truncation or alternate identity is attempted. The 64 MiB serialized-body cap
includes escaping, the original ID, counters and descriptor overhead.

Each item may opt in with `part`, using an explicit `claude-code` or `codex`
format and the same original `thread_id` on every request:

```json
{"version":1,"session_bytes":123456789,"session_sha256":"<whole-source SHA-256>","index":0,"offset":0,"prefix_sha256":"<preceding-source SHA-256>","sha256":"<exact-part SHA-256>","final":false}
```

Hashes are 64 lowercase hexadecimal characters of exact UTF-8 source bytes,
not the stored canonical message digest. Counters are integers (not booleans)
in `[0, 2**63-1]`, with a positive whole-session length. Parts are nonempty,
record-aligned and ordered by zero-based index and byte offset. Whitespace-only
parts are supported. A nonfinal part needs a physical record terminator; only
the final part can contain a complete record with no final terminator. CRLF
cannot be split across parts. Every nonblank record must be complete valid JSON.
In particular, multipart rejects an incomplete final object. Fitting legacy
snapshots deliberately retain their existing raw live-tail tolerance instead.
Recognition of the bound native format accumulates across blank/unknown parts;
the final session must contain at least one matching native record.

The descriptor binds source, original ID, native format, protocol version and
whole-session fingerprint. Offset, index, prefix and part hashes must match the
durable prefix. Finality is true exactly at the declared length, and the whole
source hash is verified **before** final commit. Partial sessions are explicitly
`incomplete`, not complete snapshots. Final digest or recognition failure leaves
earlier parts incomplete and unchanged. A legacy snapshot at the same source ID
conflicts with multipart, and the reverse also conflicts; no automatic migration
or ID rewrite occurs.

Each accepted part appends a generated `system/other` archive named `import.raw`
(empty readable text), then an `import.part` metadata record, then native parsed
messages. Metadata JSON carries `identity`, `part`, cumulative `recognized`,
`session_state` and the number of following parsed rows (`message_count`). These
fields are client-declared provenance plus server verification of committed
bytes, never approved source instructions. They do not alter the original bytes.
**For multipart, message 0 contains only the first part.** Reconstruct the source
by concatenating only the generated `import.raw` segments in part-index order,
using each adjacent `import.part` record to verify the layout, range and digest;
skip its `message_count` parsed rows before the next generated pair. Do not
concatenate all message `raw` fields: native multi-block records repeat their raw
record on several parsed messages. Legacy snapshot message 0 remains the whole
archive. Both stores retain one `import:<source>:<original-ID>` identity and the
original authenticated filing actor through all appends.

Results retain normal batch counters, with `unit: "part"`: `imported` counts a
committed part, not another session. The row echoes the exact acknowledged
`part`, original ID, format and stored ID, plus the **current** `offset`,
`prefix_sha256`, `next_index` and `session_state: "incomplete" | "complete"`.
`messages` counts parsed rows separately from the generated archive/metadata.
Exact retries are no-ops even at quota. A stale exact retry after later commits
acknowledges that original part and returns the latest cursor. Different bytes,
fingerprints, boundaries, gaps or formats conflict and preserve the original.
Raw archives, metadata, parsed rows, indexes, hashes and finality commit in one
store transaction; quota, append and readback failures roll back together.

After interruption, replay the same frozen plan from part zero. The client
validates each per-item acknowledgement, including identity, hashes and the
cursor against its planned boundaries, before printing flushed JSON progress.
Its `progress.acknowledged_bytes` counts unique acknowledged planned ranges,
not repeated current-cursor values. Before upload it recomputes the whole hash
and length in the final planning pass, and before every request it rereads and
hashes that planned span. Same-size source mutation therefore fails explicitly;
earlier accepted parts remain safe and incomplete. Keep the source frozen for
stable part boundaries; no local progress file grants permission to skip parts.
Old servers may return HTTP 200 with per-item errors. Missing/invalid multipart
acknowledgements stop the client without stripping metadata, changing IDs or
switching to capture. Web arrays, Grok facts and explicit messages keep their
existing paths.

Client buffers are bounded by record/body size plus per-part span metadata.
**Server memory and work are not bounded by request size:** reconstruction and
full-prefix append/readback grow with accumulated history, and repeated parts
can produce quadratic total work. No large-session performance budget is
established here. `prefix_work` exposes `previous_rows`,
`previous_source_bytes` and `readback_rows` for later measurement of this
reconstruction only; these are not total database I/O or peak-memory metrics.
