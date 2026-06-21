# Behavior Specification: `llm_refine`

Optional LLM-based refinement of regex/manifest/git-detected entity candidates.
Takes a phase-1 detection set and asks an LLM provider to reclassify each
candidate into one of a fixed label set, then merges the decisions back. The
module is opt-in; the default initialization path never loads it
(mempalace/llm_refine.py:L1-L19).

## Constants and the observable contract

The following tuning constants define batching and context bounds:
`BATCH_SIZE = 25` candidates per LLM call, `CONTEXT_LINES_PER_CANDIDATE = 3`
context lines per candidate, and `CONTEXT_WINDOW_CHARS = 240` maximum characters
per context line (mempalace/llm_refine.py:L31-L33).

The set of valid labels the LLM may return is exactly
`{PERSON, PROJECT, TOPIC, COMMON_WORD, AMBIGUOUS}`. Any label outside this set
is coerced to `AMBIGUOUS` so the user reviews it (mempalace/llm_refine.py:L35-L37,
L186-L187).

The system prompt instructs the model to pick exactly ONE label per candidate
from the five labels, classifies frameworks/runtimes/APIs/cloud
services/vendors/third-party products as `TOPIC` unless context says it is the
user's own codebase/product, and requires JSON-only output with the schema
`{"classifications": [{"name": "<exact candidate name>", "label": "<LABEL>",
"reason": "<one short sentence>"}]}`, one entry per candidate in input order
(mempalace/llm_refine.py:L40-L58).

## `RefineResult` (returned structure)

`refine_entities` returns a record with fields: `merged` (the updated detected
dict), `reclassified` (count of entries whose type changed), `dropped` (count of
entries removed because labeled `COMMON_WORD`), `errors` (list of per-batch error
message strings from transport/parse failures), `batches_completed`,
`batches_total`, and `cancelled` (boolean) (mempalace/llm_refine.py:L61-L69).

## Public surface: `refine_entities(detected, corpus_text, provider, batch_size=25, show_progress=True, allow_project_promotions=True, corpus_origin=None) -> RefineResult`

### Candidate selection
Only entries in the `people`, `projects`, and `uncertain` buckets of `detected`
are considered. Within `people`, entries that are "authoritative persons" are
skipped; within `projects`, entries that are "authoritative projects" are skipped.
Each surviving entry contributes a `(name, current_type)` pair where current_type
is `person`/`project`/`uncertain` per its source bucket
(mempalace/llm_refine.py:L361-L369).

An "authoritative person" is an entry whose joined signal strings (lowercased)
contain both `commit` and `repo` (i.e., a git author)
(mempalace/llm_refine.py:L311-L314). An "authoritative project" is an entry whose
joined lowercased signals contain any of `package.json`, `pyproject.toml`,
`cargo.toml`, `go.mod`, or contain `commit` (manifest/git-backed)
(mempalace/llm_refine.py:L317-L321).

Candidate names are deduplicated while preserving first-seen order before
batching (mempalace/llm_refine.py:L373-L379).

### Empty case
If no unique candidates remain, the function returns immediately with `merged`
equal to the original `detected`, all counts zero, empty errors, and
`cancelled=False` (mempalace/llm_refine.py:L381-L390).

### Context collection
`corpus_text` is split into lines (empty string yields no lines)
(mempalace/llm_refine.py:L371). For each candidate, up to
`CONTEXT_LINES_PER_CANDIDATE` (3) distinct lines that mention the name are
gathered. Matching is case-insensitive on token boundaries (the name must not be
preceded or followed by a word character). Each matched line is stripped and
truncated to `CONTEXT_WINDOW_CHARS` (240) characters; empty or already-seen
trimmed lines are skipped (mempalace/llm_refine.py:L72-L93,L396).

### Batching and prompts
Candidates are split into batches of `batch_size` (default 25), each enriched
with collected contexts (mempalace/llm_refine.py:L393-L397). The user prompt
begins with `CANDIDATES:` followed by one numbered entry per candidate formatted
`N. <name>  (currently: <type>)`, with each context line on its own line prefixed
`   > `, or `   > (no context available)` when no contexts were found
(mempalace/llm_refine.py:L96-L106).

The effective system prompt is `SYSTEM_PROMPT` plus an optional corpus-origin
preamble (mempalace/llm_refine.py:L404).

### Corpus-origin preamble
When `corpus_origin` is provided and its nested `result.likely_ai_dialogue` is
truthy, a `CORPUS CONTEXT (corpus-origin detection):` preamble is appended. It
optionally states the corpus is AI-dialogue from `primary_platform`, declares the
human author `user_name` should be treated as PERSON, and lists
`agent_persona_names` instructing the model to classify those as PERSON
(downstream tagging treats them as agent personas). It does not add a new label
or change the schema. Returns empty string (no change to the prompt shape) when
`corpus_origin` is falsy or `likely_ai_dialogue` is not truthy
(mempalace/llm_refine.py:L265-L308).

### Per-batch execution and ordering
Batches are processed in order. For each batch the provider's `classify` method
is called with `(system_prompt, user_prompt, json_mode=True)`
(mempalace/llm_refine.py:L406-L411).

- A `KeyboardInterrupt` (Ctrl-C) raised during `classify` sets `cancelled=True`
  and stops processing remaining batches; decisions accumulated so far are still
  applied (mempalace/llm_refine.py:L412-L414,L431-L435).
- An `LLMError` records `"batch <idx>: <error>"` in `errors` and continues to the
  next batch without incrementing the completed counter
  (mempalace/llm_refine.py:L415-L417).
- A successfully returned response is parsed against the batch's candidate names.
  If parsing yields no decisions, `"batch <idx>: could not parse response"` is
  appended to `errors`. Decisions are merged into the running set and
  `batches_completed` is incremented (mempalace/llm_refine.py:L418-L423).

### Progress side effect
When `show_progress` is true, a 40-character progress bar line (`█` filled, `░`
empty) is written to standard error and overwritten in place before and after each
batch, showing `batch <idx>/<total>` and the current candidate name truncated to
30 characters. A trailing newline is written to standard error when processing
finishes (mempalace/llm_refine.py:L324-L331,L407-L408,L424-L429). This is the only
side effect of `refine_entities` itself: it writes progress to stderr and calls
the provider; it does not touch the filesystem.

## Response parsing contract

`_parse_response` extracts JSON from the model text and returns a mapping
`name -> (label, reason)`. JSON extraction tries, in order: the whole trimmed
text; the contents of any ```` ```json ```` / ```` ``` ```` fenced blocks; and
each balanced `{...}` or `[...]` span found by bracket-depth scanning that
respects string literals and escapes (mempalace/llm_refine.py:L109-L150). The
first candidate that parses as JSON wins; if none parse, an empty mapping is
returned (mempalace/llm_refine.py:L159-L167).

The parsed JSON's `classifications` field is used if the top level is an object;
otherwise the top-level value is used directly. If that is not a list, an empty
mapping is returned (mempalace/llm_refine.py:L169-L171). For each list element
that is an object: the name is read from `name` or `candidate`; the label from
`label` or `type` or `classification`; the reason from `reason` (default empty).
Entries lacking a string name or string label are skipped
(mempalace/llm_refine.py:L175-L182). The name's canonical casing is restored from
the expected candidate names (case-insensitive match), the label is
uppercased/trimmed and coerced to `AMBIGUOUS` if not in `VALID_LABELS`, and the
reason is trimmed and truncated to 120 characters
(mempalace/llm_refine.py:L184-L188).

## Merge contract: `_apply_classifications`

Decisions are merged into a fresh detected dict with buckets `people`, `projects`,
`topics`, `uncertain`. Label-to-bucket mapping: `PERSON`->`people`,
`PROJECT`->`projects`, `TOPIC`->`topics`, `AMBIGUOUS`->`uncertain`. Bucket-to-type
mapping yields `person`/`project`/`topic`/`uncertain`
(mempalace/llm_refine.py:L205-L216,L226-L231).

Every entry across all input buckets is examined. An entry with no LLM decision is
kept unchanged in its original bucket (mempalace/llm_refine.py:L218-L237). A
`COMMON_WORD` decision drops the entry (increments `dropped`, entry not added to
any bucket) (mempalace/llm_refine.py:L241-L243).

For other labels the target bucket is taken from the label mapping. If the label is
`PROJECT`, `allow_project_promotions` is false, and the entry is not an
authoritative project, the target is forced to `uncertain`
(mempalace/llm_refine.py:L245-L251). The merged entry gains a new signal string:
`"LLM: <label-lowercased> — <reason>"` when a reason exists, else
`"LLM: <label-lowercased>"`, appended to its existing `signals` list
(mempalace/llm_refine.py:L252-L256). If the target bucket differs from the original
bucket, `reclassified` is incremented and the entry's `type` is updated to the
bucket's type (mempalace/llm_refine.py:L257-L260). The function returns
`(new_detected, reclassified, dropped)` (mempalace/llm_refine.py:L262).

## `allow_project_promotions` semantics

Default true. When false, LLM-only `PROJECT` guesses that are not already
manifest/git-backed are kept in the `uncertain` bucket rather than promoted to
`projects` (mempalace/llm_refine.py:L334-L360,L245-L251).

## Public surface: `collect_corpus_text(project_dir, max_files=30, max_bytes_per_file=20000) -> str`

Gathers prose text from a directory for use as LLM context. `project_dir` is
expanded (user `~`) and resolved to an absolute path; if it is not a directory,
the empty string is returned (mempalace/llm_refine.py:L448-L465).

The directory tree is walked, pruning directories listed in `SKIP_DIRS` and any
directory whose name starts with `.` (mempalace/llm_refine.py:L489-L500,L467). Only
files whose lowercased suffix is in `PROSE_EXTENSIONS` (documented as `.md`,
`.txt`, `.rst`) are considered; files whose modification time cannot be read are
skipped (mempalace/llm_refine.py:L468-L476). Candidates are sorted by modification
time descending (most recently modified first) and the first `max_files` (default
30) are selected (mempalace/llm_refine.py:L477-L478). Each selected file is read as
UTF-8 with replacement on decode errors, reading at most `max_bytes_per_file`
(default 20000) bytes; unreadable files are skipped. The selected chunks are joined
with newlines and returned (mempalace/llm_refine.py:L479-L486). This function reads
from the filesystem (no writes).

## Edge cases and invariants

- Candidate order is preserved through dedup and batching; the prompt asks the
  model to keep input order, but merge is keyed by name, so out-of-order or partial
  model responses are tolerated (mempalace/llm_refine.py:L373-L379,L184-L189).
- A name appearing in multiple input buckets is deduplicated to a single candidate
  by first occurrence (mempalace/llm_refine.py:L373-L379).
- Authoritative persons (git authors) and authoritative projects (manifest/git) are
  never sent to the LLM and pass through unchanged
  (mempalace/llm_refine.py:L365-L368).
- Parse failure or transport failure on a batch never aborts the run; only Ctrl-C
  stops remaining batches (mempalace/llm_refine.py:L412-L422).
