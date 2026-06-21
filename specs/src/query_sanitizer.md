# Spec: query_sanitizer

Mitigates "system prompt contamination" in search queries: AI agents sometimes prepend long system-prompt text (thousands of characters) to a short search question, which causes embedding-based retrieval to fail. This module extracts the likely real search intent from a possibly contaminated input string (mempalace/query_sanitizer.py:L1-L19).

## Public Surface

### `sanitize_query(raw_query: str) -> dict`

The sole public function. Takes a single raw query string and returns a result object describing the sanitized query and which method produced it (mempalace/query_sanitizer.py:L41-L60).

The returned object always has exactly these keys:
- `clean_query` (string): the sanitized query to use for downstream embedding search.
- `was_sanitized` (boolean): whether any transformation beyond pass-through was applied.
- `original_length` (integer): character length of the (processed) input considered.
- `clean_length` (integer): character length of `clean_query`.
- `method` (string): one of `"passthrough"`, `"question_extraction"`, `"tail_sentence"`, `"tail_truncation"`.

(mempalace/query_sanitizer.py:L49-L59)

## Constants (observable thresholds)

- `MAX_QUERY_LENGTH` = 250: outputs are guarded to not exceed this length; also the size of the final truncation window (mempalace/query_sanitizer.py:L29).
- `SAFE_QUERY_LENGTH` = 200: inputs at or below this length are treated as clean and passed through unchanged (mempalace/query_sanitizer.py:L30).
- `MIN_QUERY_LENGTH` = 10: extracted candidates shorter than this are considered failed extractions and skipped (mempalace/query_sanitizer.py:L31).
- Quote characters recognized for stripping are the single quote `'` and double quote `"` (mempalace/query_sanitizer.py:L32).

## Processing Order and Behavior

The function evaluates steps in a fixed order and returns at the first step that produces a result.

### Empty / whitespace-only input
If `raw_query` is empty, null/absent, or contains only whitespace, the function returns immediately with `clean_query` equal to the raw input (or empty string if the input was absent), `was_sanitized` = false, `method` = `"passthrough"`, and both lengths equal to the raw input length (0 if absent) (mempalace/query_sanitizer.py:L61-L68). Note: `clean_query` here is the un-trimmed raw value, and the reported lengths are of that raw value, not of the trimmed string.

### Surrogate stripping and trimming (pre-processing for all non-empty paths)
For any non-empty input, the string is first whitespace-trimmed, then any lone/invalid UTF-16 surrogate code points are replaced with the Unicode replacement character U+FFFD so the result is legal UTF-8 (mempalace/query_sanitizer.py:L70-L72, mempalace/config.py:L29-L31). `original_length` for all subsequent steps is the length of this trimmed, surrogate-cleaned string (mempalace/query_sanitizer.py:L73).

### Step 1 — Short-query passthrough
If the processed length is at or below `SAFE_QUERY_LENGTH` (200), the function returns the processed string unchanged with `was_sanitized` = false and `method` = `"passthrough"`. Both lengths equal `original_length` (mempalace/query_sanitizer.py:L105-L113).

### Step 2 — Question extraction
The processed query is segmented two ways: by a sentence splitter (any run of `.`, `!`, `?`, fullwidth `。`, `！`, `？`, or newlines) (mempalace/query_sanitizer.py:L34-L35, L117), and by newlines alone (mempalace/query_sanitizer.py:L119-L124).

Newline segments are scanned from last to first; a segment qualifies if it ends with `?` or fullwidth `？` (optionally followed by trailing whitespace and a single trailing quote) (mempalace/query_sanitizer.py:L37-L38, L126-L130). If no newline segment qualifies, the sentence-split fragments are scanned from last to first; a fragment qualifies if it contains `?` or `？` anywhere (mempalace/query_sanitizer.py:L132-L136). Because both scans append to the same list in reverse order, the chosen candidate is the first appended element (mempalace/query_sanitizer.py:L138-L140) — i.e. the last (latest-occurring) newline segment ending in a question mark takes precedence; if none, the last sentence fragment containing a question mark is used.

The chosen candidate is whitespace-trimmed. If it is at least `MIN_QUERY_LENGTH` (10) characters, it is accepted: if it exceeds `MAX_QUERY_LENGTH` it is passed through the trim routine (see Trim routine below), and the function returns with `was_sanitized` = true and `method` = `"question_extraction"` (mempalace/query_sanitizer.py:L140-L156). If the candidate is shorter than `MIN_QUERY_LENGTH`, Step 2 produces no result and processing falls through to Step 3 (mempalace/query_sanitizer.py:L141-L143).

### Step 3 — Tail-sentence extraction
Newline segments are walked from last to first. For each segment of length at least `MIN_QUERY_LENGTH`, the trim routine is applied; if the trimmed result is still at least `MIN_QUERY_LENGTH` characters, the function returns it with `was_sanitized` = true and `method` = `"tail_sentence"`. Otherwise it continues to the previous segment (mempalace/query_sanitizer.py:L158-L178).

### Step 4 — Tail truncation (fallback)
If no earlier step returned, the last `MAX_QUERY_LENGTH` (250) characters of the processed query are taken and whitespace-trimmed, returned with `was_sanitized` = true and `method` = `"tail_truncation"` (mempalace/query_sanitizer.py:L180-L192).

## Trim routine (length guard)

Used in Steps 2 and 3. Given a candidate:
1. Strip wrapping quotes: repeatedly remove a matched pair of identical leading/trailing quote characters (single or double), trimming whitespace each iteration; if stripping empties the string, the routine yields empty string. After the loop, a single unmatched leading quote and/or a single unmatched trailing quote is also stripped (mempalace/query_sanitizer.py:L75-L87, L89-L90).
2. If the quote-stripped candidate is at most `MAX_QUERY_LENGTH`, it is returned as-is (mempalace/query_sanitizer.py:L91-L92).
3. Otherwise the candidate is split into sentence fragments (same splitter as Step 2), each fragment quote-stripped, and scanned from last to first; the first fragment whose length is in the inclusive range `[MIN_QUERY_LENGTH, MAX_QUERY_LENGTH]` is returned (mempalace/query_sanitizer.py:L94-L101).
4. If no fragment qualifies, the last `MAX_QUERY_LENGTH` characters of the candidate are returned, whitespace-trimmed (mempalace/query_sanitizer.py:L103).

## Invariants

- The output `method` is always one of the four named values, and `was_sanitized` is true exactly when `method` is not `"passthrough"` (mempalace/query_sanitizer.py:L62-L67, L108-L112, L150-L156, L172-L178, L186-L192).
- For non-empty inputs, `clean_length` equals the character length of `clean_query` (mempalace/query_sanitizer.py:L151-L154, L173-L176, L187-L190).
- A clean output produced by a trimming step is bounded by `MAX_QUERY_LENGTH`, except potentially the Step 2 candidate when its length is between `MIN_QUERY_LENGTH` and `MAX_QUERY_LENGTH` (returned directly without trimming) (mempalace/query_sanitizer.py:L143-L144).

## Side Effects

- No filesystem, network, process, or environment side effects.
- Whenever sanitization occurs (Steps 2, 3, 4), a warning is logged to the logger named `"mempalace_mcp"` with a message of the form `Query sanitized: <original_length> → <clean_length> chars (method=<method>)` (mempalace/query_sanitizer.py:L26, L145-L149, L167-L171, L183-L185). No log is emitted for pass-through or empty inputs.
