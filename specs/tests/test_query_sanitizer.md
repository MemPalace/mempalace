# Spec: Query Sanitizer Behavior (from test_query_sanitizer.py)

This spec describes the observable contract of the `sanitize_query` function and
its associated length constants, as pinned by the test suite. The function exists
to mitigate "system prompt contamination" — situations where a large block of
prepended context (system prompt, wake-up output, memory document) is concatenated
in front of a user's real query, so that the real search intent must be recovered
(tests/test_query_sanitizer.py:L1-L9).

## Public Surface

The module exposes a single function `sanitize_query` and three integer constants:
`MAX_QUERY_LENGTH`, `MIN_QUERY_LENGTH`, and `SAFE_QUERY_LENGTH`
(tests/test_query_sanitizer.py:L11-L16).

### Constants

- `MAX_QUERY_LENGTH` equals `250`. This is the hard cap on the length of any
  produced clean query (tests/test_query_sanitizer.py:L147-L148).
- `MIN_QUERY_LENGTH` is the minimum length an extracted candidate sentence must
  reach to be accepted by the question/tail extraction steps; candidates shorter
  than this are rejected and the pipeline falls through to a later step
  (tests/test_query_sanitizer.py:L158-L164).
- `SAFE_QUERY_LENGTH` is the threshold below or equal to which a query is passed
  through unchanged (tests/test_query_sanitizer.py:L39-L49).

### `sanitize_query(query) -> result`

Input: `query` is a string, an empty string, or null/None
(tests/test_query_sanitizer.py:L22-L37).

Output: a record (dictionary/map) with at least the following fields:

- `clean_query` (string): the recovered/cleaned query text
  (tests/test_query_sanitizer.py:L24, L180).
- `was_sanitized` (boolean): whether the clean query differs from / required
  processing of the input (tests/test_query_sanitizer.py:L25, L182-L190).
- `method` (string): the pipeline stage that produced the result; one of
  `"passthrough"`, `"question_extraction"`, `"tail_sentence"`, or
  `"tail_truncation"` (tests/test_query_sanitizer.py:L26, L61, L108, L130).
- `original_length` (integer): the length of the input after leading/trailing
  whitespace is stripped (tests/test_query_sanitizer.py:L49, L170-L174).
- `clean_length` (integer): equal to the length of `clean_query`
  (tests/test_query_sanitizer.py:L176-L180).

## Pipeline Ordering

`sanitize_query` runs up to four ordered stages. Earlier stages take precedence;
a stage is only reached if all earlier stages declined to produce an accepted
result (tests/test_query_sanitizer.py:L3-L8).

### Step 1 — Passthrough (short queries)

If the input length is less than or equal to `SAFE_QUERY_LENGTH`, the query is
returned unchanged with `was_sanitized = false` and `method = "passthrough"`
(tests/test_query_sanitizer.py:L19-L43).

- An empty-string input returns `clean_query = ""`, `was_sanitized = false`,
  `method = "passthrough"` (tests/test_query_sanitizer.py:L28-L32).
- A null/None input returns `was_sanitized = false`, `method = "passthrough"`
  (tests/test_query_sanitizer.py:L34-L37).
- An input whose length is exactly `SAFE_QUERY_LENGTH` is treated as safe and
  passes through (tests/test_query_sanitizer.py:L39-L43).
- An input one character longer than `SAFE_QUERY_LENGTH` enters the sanitization
  pipeline; in that case `original_length` equals `SAFE_QUERY_LENGTH + 1`. The
  pipeline may or may not alter the text (tests/test_query_sanitizer.py:L45-L49).

### Step 2 — Question extraction

For an over-length input, the function searches for question sentences (sentences
ending in a question mark). When a qualifying question is found, it is returned
with `was_sanitized = true` and `method = "question_extraction"`
(tests/test_query_sanitizer.py:L52-L61).

- Both the ASCII question mark `?` and the full-width Japanese question mark `？`
  are recognized as question terminators
  (tests/test_query_sanitizer.py:L63-L69).
- When multiple question sentences exist, the last one is selected
  (tests/test_query_sanitizer.py:L71-L75).
- A question appearing earlier (e.g. inside a prepended system prompt) is
  superseded when a real question appears later in the text; the later/real
  question is recovered (tests/test_query_sanitizer.py:L77-L84).
- A found question whose extracted sentence is shorter than `MIN_QUERY_LENGTH`
  (e.g. `"OK?"`, 3 characters) is rejected at this step, and the pipeline falls
  through to a later step; the overall result still has `was_sanitized = true`
  (tests/test_query_sanitizer.py:L158-L164).

### Step 3 — Tail sentence extraction

When no qualifying question mark is found, the function extracts the last
meaningful sentence/fragment of the text. The result has `was_sanitized = true`
and `method = "tail_sentence"` (tests/test_query_sanitizer.py:L87-L96).

- For command-style queries appended after a long prompt, the recovered text
  contains the trailing intent and `method` is `"tail_sentence"` (or
  `"question_extraction"` if a question terminator is present)
  (tests/test_query_sanitizer.py:L90-L96).
- For keyword-style trailing text appended after a newline, the trailing keywords
  are recovered (tests/test_query_sanitizer.py:L98-L103).
- When a long input ends with a final sentence after many repeated filler
  sentences, the exact final sentence is recovered: input
  `("Prompt sentence. " * 30) + "Final search intent for architecture migration"`
  yields `clean_query == "Final search intent for architecture migration"` with
  `method == "tail_sentence"` (tests/test_query_sanitizer.py:L105-L109).
- A long trailing candidate has surrounding wrapping quote characters stripped:
  input ending in `"\n" + '"' + ("x" * 260) + '"'` yields a `clean_query` that
  neither starts nor ends with a quote character, is truncated to
  `MAX_QUERY_LENGTH` characters (equal to `"x" * MAX_QUERY_LENGTH`), and whose
  length is at most `MAX_QUERY_LENGTH` (tests/test_query_sanitizer.py:L111-L118).
- When the trailing candidate has no sentence delimiters but reaches at least
  `MIN_QUERY_LENGTH` (e.g. `("x" * 260) + "IMPORTANT_QUERY_CONTENT"`), it is still
  handled by tail-sentence extraction: `method == "tail_sentence"` and the
  trailing token `"IMPORTANT_QUERY_CONTENT"` is preserved in the output
  (tests/test_query_sanitizer.py:L137-L141).

### Step 4 — Tail truncation (fallback)

When no acceptable sentence/question can be extracted, the fallback takes the last
`MAX_QUERY_LENGTH` characters of the text. The result has `was_sanitized = true`,
`method = "tail_truncation"`, and `clean_query` length at most `MAX_QUERY_LENGTH`
(tests/test_query_sanitizer.py:L121-L130).

- Input consisting only of many short newline-separated segments (none reaching
  `MIN_QUERY_LENGTH`), such as 200 lines of `"ab"`, triggers the fallback:
  `method == "tail_truncation"` and `clean_length <= MAX_QUERY_LENGTH`
  (tests/test_query_sanitizer.py:L124-L130).
- The fallback preserves the tail of the input: input
  `("x" * 1000) + "IMPORTANT_QUERY_CONTENT"` yields a `clean_query` that contains
  `"IMPORTANT_QUERY_CONTENT"` (tests/test_query_sanitizer.py:L132-L135).

## Invariants and Length Guards

- `clean_query` never exceeds `MAX_QUERY_LENGTH` characters, regardless of input.
  A single very long question sentence (e.g. `("a" * 1000) + "?"` after a prompt)
  is truncated so that the output length is at most `MAX_QUERY_LENGTH`
  (tests/test_query_sanitizer.py:L150-L156).
- `clean_length` always equals the actual length of `clean_query`
  (tests/test_query_sanitizer.py:L176-L180).
- `original_length` always equals the length of the input after stripping leading
  and trailing whitespace (tests/test_query_sanitizer.py:L170-L174).
- `was_sanitized` is true exactly when the input was processed/changed by a
  non-passthrough stage, and false when the query passed through unchanged
  (tests/test_query_sanitizer.py:L182-L190).
- Recovered output for contaminated inputs is at least `MIN_QUERY_LENGTH`
  characters when a meaningful tail exists (tests/test_query_sanitizer.py:L209-L210).

## Real-World Contamination Scenarios

The function must recover the real trailing query from large prepended blocks:

- A prepended MemPalace wake-up banner (~1000 chars) followed by a real question
  yields `was_sanitized = true`, output length at most `MAX_QUERY_LENGTH`, and
  output length at least `MIN_QUERY_LENGTH`
  (tests/test_query_sanitizer.py:L196-L210).
- A prepended memory document (~750 chars) followed by `"\n"` and a real question
  yields `was_sanitized = true` with `method` being either `"question_extraction"`
  or `"tail_sentence"` (tests/test_query_sanitizer.py:L212-L225).
- A ~2000-character system prompt followed by a real question (Issue #333) yields
  `was_sanitized = true`, `original_length > 2000`, `clean_length <=
  MAX_QUERY_LENGTH`, and `method == "question_extraction"`
  (tests/test_query_sanitizer.py:L227-L236).

## Side Effects

This is a pure transformation: no filesystem, network, process, or environment
side effects are exercised or implied by any test
(tests/test_query_sanitizer.py:L1-L236).
