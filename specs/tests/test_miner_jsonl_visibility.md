# Spec: test_miner_jsonl_visibility

Behavior specification distilled from `tests/test_miner_jsonl_visibility.py`. This is a
test module that asserts observable contracts of the project miner (`scan_project` and
two module-level constants) regarding `.jsonl` file visibility. The spec below describes
the contracts these tests enforce on the system under test, so they are implementable in
any language.

## Purpose

This module is a regression/TDD guard ensuring the project miner does not silently drop
`.jsonl` transcript files (ChatGPT exports, Claude Code transcripts) when walking a
project directory (tests/test_miner_jsonl_visibility.py:L1-L22).

## System Under Test (imported surface)

The tests depend on three names exported by the miner module: a numeric constant
`MAX_FILE_SIZE`, a collection `READABLE_EXTENSIONS`, and a function `scan_project`
(tests/test_miner_jsonl_visibility.py:L28-L28).

## Contracts Asserted

### Contract 1 — `.jsonl` is a readable extension

The readable-extensions whitelist `READABLE_EXTENSIONS` MUST contain the literal entry
`".jsonl"` (tests/test_miner_jsonl_visibility.py:L40-L40). The collection must support
membership testing against the string `".jsonl"`. The rationale is that the whitelist
already contains `".json"`, and `.jsonl` is conceptually the same line-delimited text
content; excluding it causes silent data loss when the miner filters files by suffix
(tests/test_miner_jsonl_visibility.py:L33-L50).

### Contract 2 — `scan_project` discovers `.jsonl` files

`scan_project(path)` takes a directory path (passed as a string) and returns an iterable
of file path objects, each of which exposes a `name` attribute giving the file's base name
(tests/test_miner_jsonl_visibility.py:L64-L65). Given a directory containing a file named
`transcript.jsonl` whose contents are newline-delimited JSON lines, the returned collection
MUST include an entry whose name is `transcript.jsonl`; the file must not be silently
dropped (tests/test_miner_jsonl_visibility.py:L52-L70). The test populates the file with
four JSON-object lines each of the shape `{"role": ..., "content": ...}`, demonstrating
that valid jsonl transcript content is accepted (tests/test_miner_jsonl_visibility.py:L57-L62).

### Contract 3 — file-size cap is at least 100 MB

The numeric constant `MAX_FILE_SIZE` MUST be greater than or equal to 100 MB, i.e.
`100 * 1024 * 1024` bytes (tests/test_miner_jsonl_visibility.py:L82-L82). The rationale is
that long sessions produce transcripts exceeding the legacy 10 MB cap and were silently
dropped by a size filter; the cap must accommodate realistic transcript sizes
(tests/test_miner_jsonl_visibility.py:L72-L89).

### Contract 4 — `scan_project` accepts files up to (at least) 50 MB by reported size

`scan_project` decides inclusion based on the file's reported size (the `st_size` of the
file's stat result), not by reading the whole file (tests/test_miner_jsonl_visibility.py:L106-L120).
When a real on-disk `.jsonl` file exists with valid extension and text content, but its
reported size is 50 MB (`50 * 1024 * 1024`), `scan_project` MUST still include that file in
its returned collection (tests/test_miner_jsonl_visibility.py:L91-L126). The test verifies
this by intercepting the size lookup for the file named `big_transcript.jsonl` to report the
fake 50 MB size while leaving other stat fields (mode) intact
(tests/test_miner_jsonl_visibility.py:L108-L119).

## Inputs / Outputs / Types

- Input to `scan_project`: a directory path as a string (tests/test_miner_jsonl_visibility.py:L64-L64, L120-L120).
- Output of `scan_project`: an iterable of path-like objects, each with a `name` property
  returning the base filename as a string (tests/test_miner_jsonl_visibility.py:L65-L65, L122-L122).
- `READABLE_EXTENSIONS`: a membership-testable collection of file-suffix strings including
  `".jsonl"` (tests/test_miner_jsonl_visibility.py:L40-L40).
- `MAX_FILE_SIZE`: an integer byte count, at least `104857600` (tests/test_miner_jsonl_visibility.py:L82-L82).

## Side Effects (test harness only)

Each discovery test creates a temporary directory, writes a `.jsonl` file into it, invokes
`scan_project` against that directory, then the temporary directory is cleaned up
(tests/test_miner_jsonl_visibility.py:L54-L64, L98-L120). The 50 MB test writes only a small
real file and reports a fabricated size rather than writing 50 MB to disk
(tests/test_miner_jsonl_visibility.py:L102-L116). These behaviors belong to the test harness,
not to the system contract.

## Invariants

- No `.jsonl` file is silently excluded by extension filtering
  (tests/test_miner_jsonl_visibility.py:L40-L50).
- No `.jsonl` file is silently excluded by the size cap for files within realistic
  transcript sizes up to at least 50 MB (tests/test_miner_jsonl_visibility.py:L82-L82, L123-L126).
