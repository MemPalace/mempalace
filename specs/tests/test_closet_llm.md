# Spec: `closet_llm` — Optional LLM-based closet regeneration

Derived from the test suite `tests/test_closet_llm.py`, which exercises the module
`mempalace.closet_llm` without hitting the network (HTTP is mocked). The tests pin
down four observable surfaces: `LLMConfig`, `_parsed_to_closet_lines`, `_call_llm`,
and `regenerate_closets` (tests/test_closet_llm.py:L1-L19).

## `LLMConfig` — endpoint/key/model resolution

`LLMConfig` is constructed with optional keyword overrides `endpoint`, `key`, and
`model`. When an override is omitted, the value is read from environment variables
`LLM_ENDPOINT`, `LLM_KEY`, and `LLM_MODEL` respectively, and exposed as the
attributes `.endpoint`, `.key`, `.model` (tests/test_closet_llm.py:L26-L33).

Explicit constructor arguments take precedence over the corresponding environment
variable. With `LLM_ENDPOINT=http://env-endpoint/v1` and `LLM_MODEL=env-model` set
but `endpoint="http://flag-endpoint/v1"`, `model="flag-model"` passed, the resulting
config reports the flag values, not the env values (tests/test_closet_llm.py:L35-L40).

A trailing slash on the endpoint is stripped: `endpoint="http://foo/v1/"` yields
`.endpoint == "http://foo/v1"` (tests/test_closet_llm.py:L42-L44).

`LLMConfig` exposes a `.missing()` method returning a list of human-readable strings
naming required-but-unset settings. When neither endpoint nor model is available
(env vars unset and no overrides), the list contains an entry mentioning `ENDPOINT`
and an entry mentioning `MODEL`. The key is optional, so no entry mentions `KEY`
even when the key is unset (tests/test_closet_llm.py:L46-L55). When endpoint and
model are both supplied (even with no key), `.missing()` returns an empty list
(tests/test_closet_llm.py:L57-L60).

## `_parsed_to_closet_lines` — parsed LLM output to closet index lines

Signature: `_parsed_to_closet_lines(parsed, drawer_ids, entities)` where `parsed`
is a dict with keys `topics` (list of strings), `quotes` (list of strings), and
`summary` (string); `drawer_ids` is a list of drawer id strings; `entities` is a
string (e.g. `"Alice;Bob"`) (tests/test_closet_llm.py:L67-L72).

Each topic becomes one closet line of the form
`<topic>|<entities>|→<comma-joined-drawer-ids>`. For topics `["authentication",
"jwt tokens"]`, drawer ids `["d1", "d2"]`, and entities `"Alice;Bob"`, the result
is exactly two lines `authentication|Alice;Bob|→d1,d2` and
`jwt tokens|Alice;Bob|→d1,d2` (tests/test_closet_llm.py:L67-L72).

Quotes and the summary, when present, are also emitted into the returned lines: a
quote `[Igor] we ship Friday` causes `we ship Friday` to appear in the joined
output, and a summary `Release planning discussion` appears verbatim in the joined
output (tests/test_closet_llm.py:L74-L83).

The number of topic-derived lines is capped at 15. Given 20 topics, the returned
list has length 15 (tests/test_closet_llm.py:L85-L88).

## `_call_llm` — OpenAI-compatible chat-completions request/response

Signature: `_call_llm(cfg, source_file, wing, room, content)` returning a tuple
`(parsed, usage)` (tests/test_closet_llm.py:L143-L146).

### Request shape

The request URL is the configured endpoint with `/chat/completions` appended:
endpoint `http://localhost:11434/v1` produces request URL
`http://localhost:11434/v1/chat/completions` (tests/test_closet_llm.py:L147).

When a key is configured, the request carries an `Authorization` header with value
`Bearer <key>` (e.g. `Bearer sk-test`); header-name casing is not significant
(tests/test_closet_llm.py:L149-L150). When no key is configured, no `Authorization`
header is sent at all (tests/test_closet_llm.py:L154-L170).

The request body is JSON containing `model` equal to the configured model
(`llama3:8b`) and a `messages` array whose first element has `role` equal to `"user"`
(tests/test_closet_llm.py:L151-L152).

### Response parsing

The response is the standard chat-completions payload: `choices[0].message.content`
holds a JSON string, and a top-level `usage` object holds `prompt_tokens` and
`completion_tokens`. `_call_llm` parses the content JSON into `parsed` and returns
the `usage` object. For content
`{"topics":["postgres"],...}` and usage `{"prompt_tokens":42,...}`, the call returns
`parsed["topics"] == ["postgres"]` and `usage["prompt_tokens"] == 42`
(tests/test_closet_llm.py:L123-L146).

The message content may be wrapped in a Markdown code fence; such fences are
stripped before JSON parsing. Content ```` ```json\n{...}\n``` ```` parses to the
inner object `{"topics":["t1"],"quotes":[],"summary":""}`
(tests/test_closet_llm.py:L172-L186).

### Invalid-JSON handling and retry

When the message content cannot be parsed as JSON, `_call_llm` returns `parsed` as
`None` (tests/test_closet_llm.py:L188-L204). On JSON-decode failure the call is
retried; the total number of HTTP attempts is 3, after which `parsed` is `None`
(tests/test_closet_llm.py:L206-L225). Retries involve a sleep delay between attempts
(observable only as a `time.sleep` call that the tests patch out)
(tests/test_closet_llm.py:L199-L202, L219-L222).

## `regenerate_closets` — palace-wide LLM closet rebuild

Signature: `regenerate_closets(palace, cfg=None, dry_run=False)` returning a result
dict (tests/test_closet_llm.py:L236, L306, L397).

### Missing-config guard

When configuration is incomplete (no endpoint, no model, and no `cfg` passed), the
function returns a dict with `result["error"] == "missing-config"` and a
`result["missing"]` list containing an entry mentioning `ENDPOINT`. No mining is
attempted (tests/test_closet_llm.py:L232-L238).

### Success result contract

On success the result reports counts `result["processed"]` and `result["failed"]`.
For a palace with one source file that regenerates cleanly, `processed == 1` and
`failed == 0` (tests/test_closet_llm.py:L308).

### Purge regex closets + version stamping

Before writing fresh LLM closets for a source file, any pre-existing closets for the
same `source_file` are purged. A seeded regex-generated closet line
(`STALE_REGEX_TOPIC|;|→drawer_01`, metadata `generated_by: "regex"`) must not appear
in the surviving closet documents after regeneration; the new LLM-derived topic
(e.g. `jwt auth`) does appear (tests/test_closet_llm.py:L256-L318).

Every surviving closet for the source must be LLM-generated and version-stamped:
each closet metadata has `generated_by` starting with the prefix `"llm:"`, and
`normalize_version` equal to the current `NORMALIZE_VERSION` constant. This stamping
prevents a later mine's stale-version gate from treating the LLM closets as
leftovers to rebuild (tests/test_closet_llm.py:L240-L246, L319-L321).

### Paginated drawer fetch

The drawer collection is fetched in fixed batches of 5000, never in a single
unbounded query, to avoid exceeding SQLite's variable limit (32766). For a palace of
12000 drawers, the underlying drawer-collection `get` is invoked exactly 3 times with
`limit == 5000` for every call and offsets `[0, 5000, 10000]` in order. Each fetch
requests both `documents` and `metadatas` in its `include` set; no call ever requests
more than 5000 (tests/test_closet_llm.py:L323-L415).

With `dry_run=True`, no LLM HTTP calls are made and `result["processed"] == 0`, while
the drawer pagination and per-source aggregation still execute
(tests/test_closet_llm.py:L397, L410-L415).

### Per-source aggregation across batch boundaries

Drawers are grouped by their `source_file` metadata, and this grouping is preserved
across pagination boundaries — drawers for the same source split across different
batches still land in a single group. For 7500 drawers alternating between two source
files (3750 each), exactly the two source files
`/src/file_0.md` and `/src/file_1.md` are passed to `_call_llm`, and the `content`
argument for each is the concatenation of that source's 3750 drawer document bodies
joined by a `"\n\n"` separator (so the content contains exactly 3749 occurrences of
`"\n\n"`) (tests/test_closet_llm.py:L417-L494).

### Closet id base uses filename, not raw slash split

The closet id base is derived from the source file's basename (path-separator aware),
not a naive split on `/`. For source `/deep/nested/project/dir/mydoc.md`, the
generated closet ids contain `mydoc.md` and contain no `/` character — they encode
only the filename, never the full path (tests/test_closet_llm.py:L499-L539).

## External collaborators (observable wiring)

`regenerate_closets` obtains the drawer collection via `get_collection` and the
closet collection via `get_closets_collection`, purges per-file closets via
`purge_file_closets`, and writes new lines via `upsert_closet_lines` — all of which
are module-level names patched in tests, indicating they are the integration seams
(tests/test_closet_llm.py:L247-L252, L391-L394, L478-L482).
