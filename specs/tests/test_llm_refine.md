# Behavior Spec: `mempalace.llm_refine` (derived from `tests/test_llm_refine.py`)

This spec describes the observable behavior of the `mempalace.llm_refine` module as
pinned by its test suite. The suite uses an offline fake LLM provider for
deterministic, network-free verification (tests/test_llm_refine.py:L1-L4). All claims
cite the test that constrains them.

## Provider Contract

The module consumes a "provider" object with two methods. `classify(system, user, json_mode=True)`
returns a response object carrying `text`, `model`, `provider`, and `raw` fields; it is
invoked once per batch (tests/test_llm_refine.py:L35-L41). A provider may raise an
`LLMError`-typed exception to signal transport failure, or raise an interrupt
(cancellation) signal; the fake provider raises a cancellation on a configurable Nth call
and otherwise returns the same canned text every call (tests/test_llm_refine.py:L26-L41).
A `check_available()` method returns an availability flag and a status string
(tests/test_llm_refine.py:L43-L44).

## `_collect_contexts(lines, name, max_lines=...)`

Returns lines from `lines` that mention `name`, capped at `max_lines` results
(tests/test_llm_refine.py:L50-L59). Matching is case-insensitive: a lowercase mention
matches a capitalized query (tests/test_llm_refine.py:L62-L65). Matching respects token
boundaries — `"Go"` matches `"Go is a language."` and `"go-v1 shipped."` but NOT the
substring inside `"forgot"` (tests/test_llm_refine.py:L68-L75). Identical lines are
deduplicated: three input lines with two distinct values yield two outputs
(tests/test_llm_refine.py:L78-L82). Each returned line is truncated to at most 240
characters (tests/test_llm_refine.py:L85-L88). When no line mentions `name`, the result
is an empty list (tests/test_llm_refine.py:L91-L92).

## `_build_user_prompt(items)`

Given an ordered list of `(name, type, contexts)` tuples, the prompt enumerates entries
with 1-based numbering (`"1. Alice"`, `"2. Bob"`) and inlines each entry's context lines
(tests/test_llm_refine.py:L98-L107). An entry with no context lines is rendered with the
literal marker `"(no context available)"` (tests/test_llm_refine.py:L108).

## `_parse_response(text, expected_names)` and `_extract_json_candidates(text)`

Parses an LLM response into a mapping of `name -> (LABEL, reason)`.

- The `label` field value is canonicalized to upper case: input `"person"` yields label
  `"PERSON"` and preserves the `reason` (tests/test_llm_refine.py:L114-L117).
- A `"type"` field is accepted as an alias for `"label"` (tests/test_llm_refine.py:L120-L124).
- Any unrecognized label maps to `"AMBIGUOUS"` (tests/test_llm_refine.py:L127-L130).
- The returned name is restored to its canonical casing from `expected_names`: a model
  returning `"mempalace"` is matched back to the expected `"MemPalace"`
  (tests/test_llm_refine.py:L133-L138).
- Markdown code fences (```` ```json ```` … ```` ``` ````) are stripped before parsing
  (tests/test_llm_refine.py:L141-L144).
- JSON embedded after prose is extracted and parsed (tests/test_llm_refine.py:L147-L150),
  including fenced JSON after prose (tests/test_llm_refine.py:L153-L156).
- Non-JSON bracket tokens appearing before the real payload (e.g. `"[note]"`) are ignored;
  the actual JSON object is still parsed (tests/test_llm_refine.py:L165-L168).
- A top-level JSON array (no wrapping `{"classifications": ...}` object) is accepted
  (tests/test_llm_refine.py:L176-L180).
- Malformed / non-JSON input yields an empty mapping `{}` (tests/test_llm_refine.py:L171-L173).

`_extract_json_candidates(text)` returns candidate JSON substrings found in free text; an
embedded array `[{"name": "Y", "label": "PERSON"}]` surrounded by prose appears among the
candidates (tests/test_llm_refine.py:L159-L162).

## `_apply_classifications(detected, decisions, allow_project_promotions=True)`

Takes a detected-entity structure with `people`, `projects`, `uncertain` buckets and a
`decisions` map of `name -> (LABEL, reason)`. Returns `(new, reclassified_count, dropped_count)`.

- A `PERSON` decision moves an entry into the `people` bucket and sets its `type` to
  `"person"`; this counts as one reclassification and zero drops
  (tests/test_llm_refine.py:L186-L211).
- A `COMMON_WORD` decision removes the entry entirely (dropped count incremented, bucket
  emptied) (tests/test_llm_refine.py:L214-L231).
- Entries with no corresponding decision are left untouched, with zero reclassifications
  and zero drops (tests/test_llm_refine.py:L234-L252).
- On reclassification the entry's `signals` list gains a marker derived from the chosen
  label (e.g. `"LLM: person"`) and a marker containing the decision `reason`
  (tests/test_llm_refine.py:L255-L272).
- A `TOPIC` decision routes the entry into a dedicated `topics` bucket (not `projects`,
  not `uncertain`), setting `type` to `"topic"`, counted as a reclassification
  (tests/test_llm_refine.py:L275-L298).
- An `AMBIGUOUS` decision moves the entry to the `uncertain` bucket, counted as a
  reclassification (tests/test_llm_refine.py:L301-L320).
- When `allow_project_promotions=False`, a `PROJECT` decision on an `uncertain` entry does
  NOT promote it: the entry stays in `uncertain` with `type` `"uncertain"`, the `projects`
  bucket stays empty, and the reclassification count stays zero
  (tests/test_llm_refine.py:L323-L346).
- With promotions allowed (the default), a `PROJECT` decision promotes an `uncertain`
  entry into the `projects` bucket with `type` `"project"`, counted as a reclassification
  (tests/test_llm_refine.py:L349-L367).

## Authoritative-Source Filters

`_is_authoritative_person(entry)` returns true only when the entry's `signals` contain a
git-commit signal (e.g. `"5 commits across 2 repos"`); a pronoun-proximity signal is not
authoritative (tests/test_llm_refine.py:L373-L375).

`_is_authoritative_project(entry)` returns true when `signals` contain a manifest signal
(e.g. `"package.json, 12 of your commits"`) or a commit-count signal (e.g.
`"57 commits (none by you)"`), but not for a plain code-file-reference signal
(tests/test_llm_refine.py:L378-L381).

## `refine_entities(detected, corpus_text, provider, ...)`

Drives the full refinement and returns a result object with fields: `batches_total`,
`batches_completed`, `cancelled`, `merged` (the updated bucket structure), `reclassified`,
`dropped`, and `errors` (tests/test_llm_refine.py:L426-L450).

### End-to-end merge behavior
Given a detected set and a provider returning classifications for Foo→PROJECT,
Never→COMMON_WORD, Alice→PERSON, the result reports one batch total and one completed,
`cancelled` false; Alice lands in `people`, the pre-existing `Igor` remains in `people`
untouched, `Never` is dropped (no longer in `uncertain`), and `dropped == 1`
(tests/test_llm_refine.py:L426-L450).

### Selecting which entries are sent to the LLM
- High-confidence (>= 0.95) projects backed by a manifest signal (e.g. `pyproject.toml`)
  are NOT sent to the LLM — the provider is never called (`call_count == 0`)
  (tests/test_llm_refine.py:L453-L471).
- High-confidence projects backed only by a regex/code-reference signal (no authoritative
  source) ARE sent for review; the provider is called once, and a returned `TOPIC` label
  reclassifies the entry out of `projects` into the `topics` bucket
  (tests/test_llm_refine.py:L474-L500).
- People with an authoritative git signal are skipped, while regex/pronoun-only people are
  reviewed: in a set with one git-backed person and one pronoun-only person, the provider
  is called once, the git-backed person survives, and the pronoun-only person classified
  as `COMMON_WORD` is dropped (`dropped == 1`) (tests/test_llm_refine.py:L503-L531).

### Promotion gating in the full flow
With `allow_project_promotions=False`, an LLM `PROJECT` decision on an uncertain entry
keeps it in `uncertain` (projects bucket stays empty) while still appending an
`"LLM: project"` signal marker to record the model's opinion (tests/test_llm_refine.py:L534-L560).

### Empty / no-op input
When all detected buckets are empty, the function returns `batches_total == 0`,
`reclassified == 0`, and `merged` equal to the input (tests/test_llm_refine.py:L563-L569).

### Error handling
When the provider raises an `LLMError`, the result's `errors` list is non-empty and
contains the error message text; no reclassification occurs and `cancelled` is false
(tests/test_llm_refine.py:L572-L584). When the provider returns unparseable text, an error
containing `"could not parse"` is recorded (tests/test_llm_refine.py:L614-L617).

### Cancellation (Ctrl-C)
With candidates spanning multiple batches (`batch_size` controls batch count, e.g. 50
candidates at `batch_size=25` → 2 batches), an interrupt during the second batch sets
`cancelled` to true, records `batches_completed == 1` (first batch finished, second
interrupted), and `batches_total == 2`, returning a partial result
(tests/test_llm_refine.py:L587-L611).

## `collect_corpus_text(dir, max_files=..., max_bytes_per_file=...)`

Reads prose files from a directory and concatenates their text.

- Includes prose files (`.md`, `.txt`) and skips non-prose source files (e.g. `.py`):
  given `a.md`, `b.txt`, `c.py`, the result contains the prose contents but not the `.py`
  contents (tests/test_llm_refine.py:L623-L630).
- Prefers more recently modified files: with `max_files=1` and one newer and one older
  file, the newer file's content is included and the older's is excluded
  (tests/test_llm_refine.py:L633-L648).
- A missing directory yields an empty string (tests/test_llm_refine.py:L651-L652).
- Per-file byte intake is capped by `max_bytes_per_file`: a 100,000-byte file read with a
  500-byte cap yields output bounded near that cap (`len <= 600`, allowing for added
  newlines) (tests/test_llm_refine.py:L655-L659).
