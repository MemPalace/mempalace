# Behavior Spec: `general_extractor` (derived from its test suite)

This spec describes the observable contract of the `general_extractor` module as
constrained by `tests/test_general_extractor.py`. Each claim cites the test that
asserts it. The module under test is imported as `mempalace.general_extractor`
and exposes the public surface: `ALL_MARKERS`, `NEGATIVE_WORDS`, `POSITIVE_WORDS`,
`extract_memories`, and the helpers `_extract_prose`, `_get_sentiment`,
`_has_resolution`, `_is_code_line`, `_score_markers`, `_split_into_segments`
(tests/test_general_extractor.py:L3-L14).

## `extract_memories(text, chunk_size=800)`

Primary entry point. Takes a body of text and returns a list of "memory" records.
Each record is a map with at least the keys `memory_type` (string), `content`
(string), and `chunk_index` (integer) (tests/test_general_extractor.py:L47-L47,
L119-L120, L264-L271).

### Empty / no-signal input

- Empty string input returns an empty list (tests/test_general_extractor.py:L20-L22).
- Text containing no recognized markers (e.g. a plain pangram sentence) returns
  an empty list (tests/test_general_extractor.py:L25-L27).
- Input paragraphs shorter than 20 characters are skipped; e.g. `"ok sure"`
  yields an empty list (tests/test_general_extractor.py:L30-L33).

### Memory type classification

A returned record's `memory_type` is one of five categories: `decision`,
`preference`, `milestone`, `problem`, `emotional`
(tests/test_general_extractor.py:L229-L235).

- Text expressing a choice between alternatives with rationale produces at least
  one record, and at least one has `memory_type == "decision"`
  (tests/test_general_extractor.py:L39-L47).
- Text expressing preferences/instructions ("I prefer...", "always...", "never...")
  produces at least one record with `memory_type == "preference"`
  (tests/test_general_extractor.py:L53-L61).
- Text expressing accomplishment/breakthrough ("It finally works!", "figured out",
  "breakthrough") produces at least one record with `memory_type == "milestone"`
  (tests/test_general_extractor.py:L67-L75).
- Text describing bugs/errors/root causes produces at least one record whose type
  set includes `"problem"` OR `"milestone"`; resolved problems are reclassified
  as milestones (tests/test_general_extractor.py:L81-L91).
- Text expressing feelings ("proud", "love", "happy", "grateful") produces at
  least one record with `memory_type == "emotional"`
  (tests/test_general_extractor.py:L97-L105).

### `chunk_index` ordering invariant

When two or more records are returned, their `chunk_index` values form a
contiguous zero-based sequence `0, 1, ..., N-1` in result order
(tests/test_general_extractor.py:L111-L120). This sequential, gapless ordering
holds even across a mix of normal and oversized segments
(tests/test_general_extractor.py:L301-L312).

### Oversized-segment chunking (verbatim preservation)

A single recognized segment longer than the effective `chunk_size` is split into
multiple records rather than truncated or dropped
(tests/test_general_extractor.py:L254-L263).

- Every emitted record's `content` length is at most the effective `chunk_size`
  (tests/test_general_extractor.py:L264-L266).
- All slices of one oversized segment share a single `memory_type`
  (tests/test_general_extractor.py:L267-L269).
- Verbatim contract: concatenating the `content` of all slices, in order, exactly
  reproduces the original segment text after leading/trailing whitespace is
  stripped (tests/test_general_extractor.py:L270-L273).
- The default chunk size is 800 characters
  (tests/test_general_extractor.py:L259-L266).
- A caller-supplied `chunk_size` argument overrides the default and governs the
  maximum slice length (e.g. `chunk_size=400` caps every slice at 400)
  (tests/test_general_extractor.py:L276-L285).

### Sub-cap segment passthrough

A recognized segment shorter than the effective `chunk_size` produces exactly one
record whose `content` equals the input text verbatim (no rewriting) and whose
`memory_type` reflects its classification (e.g. `decision`)
(tests/test_general_extractor.py:L288-L298).

## `_score_markers(text, markers)`

Returns a pair `(score, keywords)` where `score` is a number and `keywords` is a
collection (tests/test_general_extractor.py:L127-L132).

- When the text matches marker keywords for a category, `score` is greater than 0
  and at least one keyword is returned
  (tests/test_general_extractor.py:L126-L132).
- When the text matches no marker, `score` is exactly `0.0`
  (tests/test_general_extractor.py:L135-L137).

The second argument is a per-category marker set selected from `ALL_MARKERS`,
e.g. `ALL_MARKERS["decision"]` (tests/test_general_extractor.py:L129-L136).

## `_get_sentiment(text)`

Returns one of the strings `"positive"`, `"negative"`, or `"neutral"`
(tests/test_general_extractor.py:L143-L152).

- Text dominated by positive words ("happy", "proud", "breakthrough") returns
  `"positive"` (tests/test_general_extractor.py:L143-L144).
- Text dominated by negative words ("bug", "crash", "failure") returns
  `"negative"` (tests/test_general_extractor.py:L147-L148).
- Text with neither returns `"neutral"`
  (tests/test_general_extractor.py:L151-L152).

## `_has_resolution(text)`

Returns a boolean indicating whether the text expresses a resolved state
(tests/test_general_extractor.py:L158-L163).

- Text indicating a fix that now works ("I fixed the auth bug and it works now")
  returns `True` (tests/test_general_extractor.py:L158-L159).
- Text describing an ongoing unresolved problem ("The server keeps crashing")
  returns `False` (tests/test_general_extractor.py:L162-L163).

## `_is_code_line(line)`

Returns a boolean classifying a single line as code vs. prose
(tests/test_general_extractor.py:L169-L177).

- Lines that are import statements, shell-prompt commands (`$ ...`), or fenced
  code-block markers (` ```python `) are classified as code → `True`, even with
  leading whitespace (tests/test_general_extractor.py:L169-L172).
- Normal prose sentences are classified as not-code → `False`
  (tests/test_general_extractor.py:L175-L176).
- An empty string is not code → `False` (tests/test_general_extractor.py:L177).

## `_extract_prose(text)`

Removes code regions from a block of text, returning the remaining prose
(tests/test_general_extractor.py:L183-L195).

- Content inside fenced code blocks (delimited by ` ``` `) is stripped out, while
  surrounding prose lines are retained; e.g. given prose around a fenced block,
  the result excludes `"import os"` but includes `"Hello world"` and `"Goodbye"`
  (tests/test_general_extractor.py:L183-L188).
- If stripping code would leave nothing, the function falls back to returning the
  original text (result length is non-zero)
  (tests/test_general_extractor.py:L191-L195).

## `_split_into_segments(text)`

Splits text into a list of segments using paragraph, conversational-turn, or
chunk-based strategies (tests/test_general_extractor.py:L201-L222).

- Text separated by blank lines (double newlines) splits into one segment per
  paragraph; three paragraphs → three segments
  (tests/test_general_extractor.py:L201-L204).
- Text formatted as alternating speaker turns ("Human:"/"Assistant:") triggers
  turn-based splitting, producing at least 3 segments for 5 Q/A pairs
  (tests/test_general_extractor.py:L207-L214).
- A long run of single-newline-separated lines with no blank-line breaks produces
  at least one chunked segment (tests/test_general_extractor.py:L217-L222).

## Module constants

- `ALL_MARKERS` is a map whose key set is exactly
  `{"decision", "preference", "milestone", "problem", "emotional"}`
  (tests/test_general_extractor.py:L228-L235).
- `POSITIVE_WORDS` contains at least `"happy"` and `"proud"`
  (tests/test_general_extractor.py:L241-L244).
- `NEGATIVE_WORDS` contains at least `"bug"` and `"crash"`
  (tests/test_general_extractor.py:L246-L248).
