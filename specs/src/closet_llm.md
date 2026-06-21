# Spec: closet_llm — LLM-based closet (index) regeneration

## Purpose & scope

This module regenerates "closet" index lines for a memory palace by sending drawer
content to a user-configured, OpenAI-compatible Chat Completions LLM endpoint. It is
optional and opt-in: regex closets are always created by the miner, and this path
regenerates them afterward using whatever LLM the user configures (mempalace/closet_llm.py:L1-L37).
The module communicates over plain HTTP(S) requests and adds no third-party
dependencies (mempalace/closet_llm.py:L35-L47).

## Constants / contract values

- Maximum content characters sent to the LLM: 30000 (mempalace/closet_llm.py:L58).
- Maximum output tokens requested from the LLM: 1500 (mempalace/closet_llm.py:L59).
- HTTP request timeout: 60 seconds (mempalace/closet_llm.py:L60).

## Configuration resolution: `LLMConfig`

`LLMConfig(endpoint, key, model)` resolves connection settings with CLI/explicit
arguments taking precedence over environment variables (mempalace/closet_llm.py:L93-L104).

- `endpoint`: explicit argument else env `LLM_ENDPOINT` (default empty string),
  with any trailing `/` characters stripped (mempalace/closet_llm.py:L102).
- `key`: explicit argument else env `LLM_KEY` (default empty string)
  (mempalace/closet_llm.py:L103).
- `model`: explicit argument else env `LLM_MODEL` (default empty string)
  (mempalace/closet_llm.py:L104).

When a non-empty endpoint is provided, its URL scheme must be `http` or `https`
(case-insensitive). Any other scheme (e.g. `file://`) causes construction to fail
with an error stating the endpoint must use `http://` or `https://` and naming the
offending scheme. This is a privacy guard preventing exfiltration of local files
via a misconfigured endpoint (mempalace/closet_llm.py:L105-L112).

`missing()` returns a list of human-readable names of required-but-unset settings:
`"LLM_ENDPOINT (or --endpoint)"` when endpoint is empty, and `"LLM_MODEL (or --model)"`
when model is empty. The key is never reported as missing because it is optional for
local inference servers (mempalace/closet_llm.py:L114-L121).

## Prompt contract

The request prompt is built from a fixed template that includes the source file path
(truncated to 100 characters), wing, room, and content (truncated to 30000 characters),
and instructs the model to output a JSON object with exactly the fields `topics`,
`quotes`, and `summary` (mempalace/closet_llm.py:L62-L90, L136-L141). The template
directs: 8-15 topics including proper nouns and distinctive terms (no generic words),
2-5 exact verbatim quotes optionally attributed with a `[Speaker]` prefix, a 2-3
sentence summary covering who/what/why, output in the content's language, and valid
JSON only with no code fences or commentary (mempalace/closet_llm.py:L81-L90).

If a localized language instruction is available and is not English, it is appended
to the prompt as a "Language instruction:" line; if unavailable the prompt is sent
unmodified (mempalace/closet_llm.py:L129-L143).

## LLM call: `_call_llm`

Performs a single logical LLM request (with retries) against
`{endpoint}/chat/completions` using HTTP POST (mempalace/closet_llm.py:L124-L162).

Request body is JSON containing `model`, `max_tokens` (1500), and a `messages` array
with one user-role message holding the prompt (mempalace/closet_llm.py:L145-L151).
Headers always include `Content-Type: application/json`; an
`Authorization: Bearer <key>` header is added only when a key is set
(mempalace/closet_llm.py:L153-L155).

Response handling: reads the response body, parses it as JSON, extracts
`choices[0].message.content`, strips surrounding whitespace, strips a leading
` ```json ` or ` ``` ` fence and a trailing ` ``` ` fence, then parses the remaining
text as JSON (mempalace/closet_llm.py:L162-L169). On success returns the parsed JSON
object and the response's `usage` object (or null if absent)
(mempalace/closet_llm.py:L170).

Retry/error behavior (up to 3 attempts, 0-indexed): on JSON decode failure, retry
with exponential backoff of `2**attempt` seconds for the first two attempts, then
return `(None, None)` (mempalace/closet_llm.py:L159-L175). On HTTP error, retry only
for status codes 429 or 503 on the first two attempts (same backoff), otherwise
return `(None, None)` (mempalace/closet_llm.py:L176-L181). On any other exception,
retry only if the error message contains "rate" (case-insensitive) on the first two
attempts, otherwise return `(None, None)` (mempalace/closet_llm.py:L182-L187). If all
attempts are exhausted, returns `(None, None)` (mempalace/closet_llm.py:L187).

## Closet line format: `_parsed_to_closet_lines`

Converts the parsed LLM JSON into closet pointer lines. A drawer reference string is
formed by joining the first three drawer IDs with commas (mempalace/closet_llm.py:L190-L193).
Each output line has the on-disk shape `<text>|<entities>|→<drawer_ref>`
(mempalace/closet_llm.py:L196-L201):

- Up to the first 15 `topics`, one line each, with the topic as the text
  (mempalace/closet_llm.py:L195-L196).
- Up to the first 5 `quotes`, one line each, with the quote as the text
  (mempalace/closet_llm.py:L197-L198).
- If a non-empty `summary` exists, one additional line with the summary truncated to
  200 characters as the text (mempalace/closet_llm.py:L199-L201).

Missing fields default to empty collections / empty string, producing fewer lines
(mempalace/closet_llm.py:L195-L201).

## Main operation: `regenerate_closets`

Signature: `regenerate_closets(palace_path, wing=None, sample=0, dry_run=False, cfg=None)`
(mempalace/closet_llm.py:L206-L212).

If `cfg` is omitted, a fresh `LLMConfig()` is built from the environment
(mempalace/closet_llm.py:L219-L220). If required config is missing, prints guidance
to use env vars or CLI flags and returns `{"error": "missing-config", "missing": [...]}`
without contacting any endpoint (mempalace/closet_llm.py:L221-L226).

Opens the drawers collection without creating it and the closets collection
(mempalace/closet_llm.py:L228-L229). If the drawer count is zero, prints "No drawers
in palace." and returns `{"processed": 0}` (mempalace/closet_llm.py:L231-L234).

### Drawer fetch and grouping

Drawers are fetched in pages of 5000 by offset until the total count is reached,
each batch including documents and metadata. Pagination is required because a single
unbounded fetch exceeds SQLite's variable limit on large palaces
(mempalace/closet_llm.py:L236-L243). The loop stops early if a batch returns no IDs
(mempalace/closet_llm.py:L244-L245).

Drawers are grouped by their `source_file` metadata (default "unknown")
(mempalace/closet_llm.py:L247-L256). When `wing` is provided, drawers whose `wing`
metadata differs are skipped (mempalace/closet_llm.py:L250-L252). For each source the
group accumulates ordered drawer IDs, document contents, and the first-seen metadata
record (mempalace/closet_llm.py:L253-L256). The offset advances by the number of IDs
in each batch (mempalace/closet_llm.py:L257).

### Source selection and ordering

Sources are processed in dictionary insertion order. When `sample > 0`, only the
first `sample` sources are processed (mempalace/closet_llm.py:L259-L261). A status
line naming source count, endpoint, and model is printed; in dry-run mode an
additional "DRY RUN" notice is printed (mempalace/closet_llm.py:L263-L267).

### Per-source processing

For each source, content is the group's documents joined by blank lines (`\n\n`), and
wing/room/entities are read from the group metadata (mempalace/closet_llm.py:L274-L280).

In dry-run mode, prints an indexed line with the source basename and content length
and performs no LLM call and no writes (mempalace/closet_llm.py:L282-L284).

Otherwise it calls the LLM. If parsing failed (no parsed object), increments the
failed counter, prints a `[FAIL]` line, and skips to the next source
(mempalace/closet_llm.py:L286-L290). On success, token usage (`prompt_tokens`,
`completion_tokens`, defaulting to 0) is accumulated into running input/output totals
(mempalace/closet_llm.py:L292-L294).

Closet lines are generated and a closet ID base is formed as
`closet_{wing}_{room}_{basename(source)[:30]}`, using the path basename so
Windows-style paths and different drives do not collide
(mempalace/closet_llm.py:L296-L300).

### Write atomicity and side effects

Writes are serialized per source under a mine lock keyed by the source, preventing
races with concurrent regex-closet rebuilds. Within the lock, all existing closets
for that source are purged, then the new closet lines are upserted under the closet
ID base (mempalace/closet_llm.py:L302-L322). The upserted closet metadata records:
`wing`, `room`, `source_file`, `generated_by` = `"llm:<model>"`, `filed_at` = current
local timestamp in ISO 8601, `entities`, and `normalize_version` = `NORMALIZE_VERSION`.
The normalize-version stamp prevents the miner's stale-drawer gate from rebuilding
over these LLM closets on a later run (mempalace/closet_llm.py:L307-L322).

After a successful write, increments the processed counter and prints an `[OK]` line
with source basename and topic count (mempalace/closet_llm.py:L324-L326).

### Return value and final output

Prints a "Done." summary with processed and failed counts, and—when any tokens were
counted—a token-usage line (mempalace/closet_llm.py:L328-L330). Returns a dictionary
`{"processed", "failed", "input_tokens", "output_tokens"}` with the accumulated
counts (mempalace/closet_llm.py:L332-L337).

## CLI entry point

Run as a module, it parses arguments and invokes `regenerate_closets`
(mempalace/closet_llm.py:L340-L374):

- `--palace`: palace path, default `~/.mempalace/palace` (expanded)
  (mempalace/closet_llm.py:L346-L350).
- `--wing`: limit to one wing, default none (mempalace/closet_llm.py:L351).
- `--sample`: integer, process only first N source files, default 0
  (mempalace/closet_llm.py:L352).
- `--dry-run`: flag, list work without calling the LLM (mempalace/closet_llm.py:L353).
- `--endpoint`: overrides `$LLM_ENDPOINT` (mempalace/closet_llm.py:L354-L358).
- `--key`: overrides `$LLM_KEY`, optional (mempalace/closet_llm.py:L359-L363).
- `--model`: overrides `$LLM_MODEL` (mempalace/closet_llm.py:L364-L368).

The resolved `LLMConfig` is built from these flags and passed through; note that an
invalid endpoint scheme raises during config construction before any work begins
(mempalace/closet_llm.py:L371-L374, L105-L112).
