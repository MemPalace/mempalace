# Behavior Spec: `split_mega_files`

This spec is derived from the test suite at `tests/test_split_mega_files.py`, which exercises the public surface of the `split_mega_files` module. The module splits a single "mega" transcript file (one that concatenates multiple recorded Claude Code sessions) into one file per session, and extracts session metadata (people, timestamps, subjects). All claims below cite the test that pins the behavior.

## Module-level state and config

The module exposes mutable configuration state used by name extraction:

- `_KNOWN_NAMES_PATH`: filesystem path to a JSON config file of known names. Tests override it to point at temp files (`tests/test_split_mega_files.py:L10`, `:L20`, `:L30`).
- `_KNOWN_NAMES_CACHE`: an in-memory cache of the parsed config; tests reset it to a sentinel "unloaded" value before each scenario (`tests/test_split_mega_files.py:L11`, `:L21`, `:L31`).
- `_FALLBACK_KNOWN_PEOPLE`: a default list of known people used when no config file is present (`tests/test_split_mega_files.py:L13`).
- `KNOWN_PEOPLE`: the effective list of names used during extraction; tests override it directly (`tests/test_split_mega_files.py:L41`, `:L49`).

## Config loading: `_load_known_people()` and `_load_username_map()`

When the config file does not exist, `_load_known_people()` returns the fallback list `_FALLBACK_KNOWN_PEOPLE`, and `_load_username_map()` returns an empty mapping (`tests/test_split_mega_files.py:L9-L14`).

When the config file contains a JSON array of strings, `_load_known_people()` returns that array verbatim and `_load_username_map()` returns an empty mapping (`tests/test_split_mega_files.py:L17-L24`).

When the config file contains a JSON object with a `"names"` array and a `"username_map"` object, `_load_known_people()` returns the value of `"names"` and `_load_username_map()` returns the value of `"username_map"` (`tests/test_split_mega_files.py:L27-L34`). The `username_map` maps an OS username to a person's display name, e.g. `{"jdoe": "John"}` (`tests/test_split_mega_files.py:L29`).

## Config loading internals: `_load_known_names_config(force_reload=False)`

On first call with an array-config present, the parsed array is stored into `_KNOWN_NAMES_CACHE` (`tests/test_split_mega_files.py:L57-L64`).

The result is cached: once loaded, a subsequent call returns the previously cached value even if the underlying file has changed on disk; it does not re-read (`tests/test_split_mega_files.py:L81-L91`).

Passing `force_reload=True` discards the cache and re-reads the file from disk, returning and caching the new contents (`tests/test_split_mega_files.py:L66-L68`).

When the config file contains invalid JSON, `_load_known_names_config()` returns a null/absent value (no exception is raised) (`tests/test_split_mega_files.py:L71-L78`).

## Person extraction: `extract_people(lines)`

Input is a sequence of text lines; output is a list of detected person names (`tests/test_split_mega_files.py:L44`, `:L50`).

When a line references a filesystem path containing an OS username that appears in the `username_map` (e.g. `/Users/jdoe/project`), the mapped display name (e.g. `"John"`) is included in the result (`tests/test_split_mega_files.py:L37-L45`).

Names from the `KNOWN_PEOPLE` list that appear literally in the content are detected and returned. Given content mentioning both "Alice" and "Ben", the result is exactly `["Alice", "Ben"]`, preserving the order in which the names appear and producing no duplicates (`tests/test_split_mega_files.py:L48-L51`).

## Session-start detection: `is_true_session_start(lines, index)`

Returns a boolean indicating whether the line at `index` begins a genuine new session (`tests/test_split_mega_files.py:L97-L99`).

A line beginning with `Claude Code v...` is a true session start when the following lines do NOT contain context-restore markers (`tests/test_split_mega_files.py:L97-L99`).

It returns false when a nearby line contains a `Ctrl+E to show N previous messages` marker, indicating a restored/continued session rather than a fresh start (`tests/test_split_mega_files.py:L102-L111`).

It returns false when a nearby line contains the phrase `previous messages` (another context-restore indicator) (`tests/test_split_mega_files.py:L114-L123`).

## Session boundary detection: `find_session_boundaries(lines)`

Input is the full list of lines; output is a list of integer line indices at which true sessions begin (`tests/test_split_mega_files.py:L129-L147`).

With two genuine session starts in the input, it returns the two starting indices in ascending order, e.g. `[0, 7]` (`tests/test_split_mega_files.py:L129-L147`).

When the content contains no session-start markers, it returns an empty list (`tests/test_split_mega_files.py:L150-L152`).

A `Claude Code v...` block that is actually a context restore (carries a `Ctrl+E ... previous messages` marker) is not counted as a boundary; given one true start plus one restore block, only one boundary is returned (`tests/test_split_mega_files.py:L155-L172`).

## Timestamp extraction: `extract_timestamp(lines)`

Returns a pair `(human, iso)` (`tests/test_split_mega_files.py:L180`).

When a line contains a timestamp of the form `⏺ <h>:<mm> <AM|PM> <Weekday>, <Month> <day>, <year>` (e.g. `⏺ 2:30 PM Wednesday, March 25, 2026`), the `human` value is a compact form `YYYY-MM-DD_<h><mm><AMPM>` (e.g. `2026-03-25_230PM`) and the `iso` value is the date `YYYY-MM-DD` (e.g. `2026-03-25`) (`tests/test_split_mega_files.py:L178-L182`).

When no timestamp is present, both elements of the pair are null/absent (`tests/test_split_mega_files.py:L185-L189`).

Only the first 50 lines are scanned for a timestamp; a timestamp that appears at line 52 or later is not found (`human` is null/absent) (`tests/test_split_mega_files.py:L192-L195`).

## Subject extraction: `extract_subject(lines)`

Returns a string summarizing the session's subject, derived from user prompt lines (lines beginning with `> `) (`tests/test_split_mega_files.py:L201-L204`).

The first substantive user prompt is used; its text appears (case-insensitively) in the returned subject, e.g. a prompt about "authentication" yields a subject containing "authentication" (`tests/test_split_mega_files.py:L201-L204`).

Prompt lines that are shell/tool commands (e.g. `> cd /some/dir`, `> git status`) are skipped in favor of the first natural-language question prompt (`tests/test_split_mega_files.py:L207-L210`).

Very short prompts (e.g. `> ok`, `> yes`) are skipped in favor of the first longer prompt (`tests/test_split_mega_files.py:L219-L222`).

When no usable prompt is found, the subject falls back to the literal string `"session"` (`tests/test_split_mega_files.py:L213-L216`).

The subject is truncated to at most 60 characters; a 100-character prompt yields a subject of length <= 60 (`tests/test_split_mega_files.py:L225-L228`).

## File splitting: `split_file(path, output_dir, dry_run=False)`

Input is the source mega-file path (string), an output directory path (string, or null meaning "same directory as source"), and an optional `dry_run` flag. Output is a list of output file paths (`tests/test_split_mega_files.py:L251`, `:L261`, `:L271`, `:L278`).

A mega-file structure for testing is a concatenation of N sessions, each starting with a `Claude Code v1.<i>` line, followed by a `> ` prompt line, followed by additional content lines (`tests/test_split_mega_files.py:L234-L244`).

When the source contains two or more sessions, the file is split: at least two output paths are returned, and (in normal mode) each returned path exists on disk after the call (`tests/test_split_mega_files.py:L247-L254`).

With `dry_run=True`, the same set of output paths is computed and returned (at least two), but no files are actually written to disk; none of the returned paths exist afterward (`tests/test_split_mega_files.py:L257-L264`).

A file with fewer than two sessions is not split; the function returns an empty list and writes nothing (`tests/test_split_mega_files.py:L267-L272`).

When `output_dir` is null, the output files are written into the same directory as the source file; each returned path's parent directory equals the source's directory (`tests/test_split_mega_files.py:L275-L281`).

Tiny session fragments shorter than 10 lines are skipped and not written; any file that is written has nonzero size (`tests/test_split_mega_files.py:L284-L292`).

## Invariants / contracts summary

- Config cache is read-once unless `force_reload=True` is supplied (`tests/test_split_mega_files.py:L81-L91`, `:L66-L68`).
- Invalid config JSON degrades gracefully to a null result rather than an error (`tests/test_split_mega_files.py:L71-L78`).
- `extract_people` preserves first-appearance order and dedups (`tests/test_split_mega_files.py:L48-L51`).
- Boundary indices are ascending and exclude context-restore blocks (`tests/test_split_mega_files.py:L129-L147`, `:L155-L172`).
- Timestamp scan window is the first 50 lines (`tests/test_split_mega_files.py:L192-L195`).
- Subject length cap is 60 characters with `"session"` fallback (`tests/test_split_mega_files.py:L225-L228`, `:L213-L216`).
- Split threshold is 2+ sessions; per-fragment minimum is 10 lines; `dry_run` suppresses all writes (`tests/test_split_mega_files.py:L267-L272`, `:L284-L292`, `:L257-L264`).
