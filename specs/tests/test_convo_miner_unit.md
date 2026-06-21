# Behavior Spec: convo_miner pure-function unit tests

This file is a test suite asserting the observable contracts of the conversation
miner's pure functions, which need no vector backend
(tests/test_convo_miner_unit.py:L1-L1). The contracts it pins down are described
below as requirements on the implementation under test, drawn from the assertions.
The implementation imports the names `CHUNK_SIZE`, `MIN_CHUNK_SIZE`, `_emit_bounded`,
`_file_chunks_locked`, `chunk_exchanges`, `detect_convo_room`, and `scan_convos`
(tests/test_convo_miner_unit.py:L8-L15).

## `chunk_exchanges(content, chunk_size=CHUNK_SIZE, min_chunk_size=MIN_CHUNK_SIZE)`

Output: a list of drawer records. Each record has at least the keys `content` (the
verbatim text slice) and `chunk_index` (tests/test_convo_miner_unit.py:L28-L30).

Input shape selection — exchange path: when the text contains lines beginning with
`> ` (user-turn markers), it is chunked by exchange. Three or more such turns route
through the exchange-pair path and produce two or more drawers
(tests/test_convo_miner_unit.py:L19-L30, L195-L196).

Input shape selection — paragraph fallback: content with no `>` lines falls back to
paragraph chunking, splitting on blank-line paragraph breaks; three long paragraphs
produce two or more drawers (tests/test_convo_miner_unit.py:L32-L40).

Input shape selection — line-group fallback: long content with no paragraph breaks
is chunked by line groups (the test uses 60 single-newline lines)
(tests/test_convo_miner_unit.py:L42-L53).

### Size bound invariant
Every emitted drawer's `content` length must be at most `chunk_size`
(default `CHUNK_SIZE`). This holds for the line-group fallback path
(tests/test_convo_miner_unit.py:L50-L55), for the paragraph path when a single
paragraph exceeds `CHUNK_SIZE` (a 5000-char paragraph followed by a short tail must
split into multiple bounded drawers) (tests/test_convo_miner_unit.py:L123-L137), and
when a custom `chunk_size` is supplied it governs both the exchange and the paragraph
paths (chunk_size=400 over a 3000-char paragraph yields all drawers ≤ 400)
(tests/test_convo_miner_unit.py:L142-L151).

Exact-boundary behavior: content whose length equals `CHUNK_SIZE` produces exactly
one drawer of length `CHUNK_SIZE` (tests/test_convo_miner_unit.py:L162-L167). Content
of length `8 * CHUNK_SIZE` produces exactly 8 drawers each of length `CHUNK_SIZE`
(tests/test_convo_miner_unit.py:L169-L175).

### Verbatim / ordering invariant
No content may be dropped, reordered, or duplicated. Concatenating all drawer
`content` of a single long paragraph (5000 `a`s) reproduces the input exactly
(tests/test_convo_miner_unit.py:L153-L160). Slices appear in source order: input
`a*CHUNK_SIZE + b*CHUNK_SIZE + c*CHUNK_SIZE` yields three drawers whose contents are,
in order, the `a` block, the `b` block, then the `c` block
(tests/test_convo_miner_unit.py:L177-L187). A trailing paragraph after a split is
preserved as the final drawer with its content intact
(tests/test_convo_miner_unit.py:L138-L140).

AI responses are never truncated. A user turn followed by 13 lines stores all 13
lines in the drawer content — none dropped (tests/test_convo_miner_unit.py:L112-L121).
Blank lines inside an AI response survive: a paragraph break between bodies remains as
`\n\n` in the stored text (tests/test_convo_miner_unit.py:L189-L215). Line structure
(numbered lists, fenced code blocks with indentation) is preserved with original
newlines; lines are not joined by spaces
(tests/test_convo_miner_unit.py:L217-L245).

### Minimum-size / drop behavior
A trailing line-group whose stripped length is at or below `MIN_CHUNK_SIZE` is dropped
rather than emitted as a tiny drawer: 51 short lines `Line 0`..`Line 50` produce
exactly 2 drawers (line groups 0–24 and 25–49); the single-line tail group 50 falls at
or below `MIN_CHUNK_SIZE` and is dropped (tests/test_convo_miner_unit.py:L57-L68).

### Empty / short input
Empty string input returns an empty list (tests/test_convo_miner_unit.py:L70-L72).
Input too short to clear the minimum (`"> hi\nbye"`) returns a list (possibly empty),
i.e. it does not error (tests/test_convo_miner_unit.py:L74-L77).

### Argument validation (error contract)
`chunk_size == 0` raises an error whose message matches `chunk_size must be > 0`
(tests/test_convo_miner_unit.py:L79-L90). `chunk_size < 0` raises the same
`chunk_size must be > 0` error (tests/test_convo_miner_unit.py:L92-L97).
`min_chunk_size < 0` raises an error matching `min_chunk_size must be >= 0`
(tests/test_convo_miner_unit.py:L99-L104). `min_chunk_size == 0` is legal and means
"accept any non-empty chunk"; it returns a list without error
(tests/test_convo_miner_unit.py:L106-L110).

## `_emit_bounded(chunks, content, chunk_size, min_chunk_size)`

A helper that appends size-bounded drawer records onto an existing `chunks` list
(in place). Each appended record has `content` and `chunk_index` keys
(tests/test_convo_miner_unit.py:L248-L259).

Size bound: no appended chunk's `content` exceeds `chunk_size`
(tests/test_convo_miner_unit.py:L251-L254).

Index numbering: `chunk_index` values are assigned sequentially starting at 0 for an
empty input list — 25 chars at chunk_size 10 yields indices `[0, 1, 2]`
(tests/test_convo_miner_unit.py:L256-L259). When the list already contains entries,
numbering continues from the existing count: appending to a list with one entry
(index 0) produces a new entry with `chunk_index == 1`
(tests/test_convo_miner_unit.py:L261-L265).

Empty content is a no-op — nothing is appended
(tests/test_convo_miner_unit.py:L267-L270).

Whole-content floor (not per-slice): the `min_chunk_size` floor is applied to the
stripped length of the whole `content` argument, not to each slice. If the whole input
passes the floor, every slice — including a small trailing remainder — is emitted
verbatim. 23 `z`s at chunk_size 10, min 5 → 3 chunks of lengths `[10, 10, 3]`
reconstructing the input (tests/test_convo_miner_unit.py:L272-L280). A trailing
whitespace-only slice is likewise preserved when the whole passes the floor: `"a"*10 +
" "*10` at chunk_size 10, min 5 → two chunks, the second being the 10 spaces
(tests/test_convo_miner_unit.py:L282-L290). 805 `y`s at chunk_size 800, min 30 → two
chunks of lengths 800 and 5, reconstructing the input — the 5-char tail is preserved
(tests/test_convo_miner_unit.py:L300-L309).

Whole-content below floor is dropped without slicing: an all-whitespace input (stripped
length 0) and a too-short input (`"ab"`, length below 5) both append nothing
(tests/test_convo_miner_unit.py:L292-L298).

## `detect_convo_room(content)`

Output: a room-name string classifying the content. Classifications observed:
- `"technical"` for debug/code/function/error/api content
  (tests/test_convo_miner_unit.py:L313-L315)
- `"planning"` for plan/roadmap/sprint/milestone/deadline content
  (tests/test_convo_miner_unit.py:L317-L319)
- `"architecture"` for architecture/service/component/interface/module/design content
  (tests/test_convo_miner_unit.py:L321-L323)
- `"decisions"` for decided/switch/migrated/chose content
  (tests/test_convo_miner_unit.py:L325-L327)
- `"general"` fallback when no category matches
  (tests/test_convo_miner_unit.py:L329-L331)

## `scan_convos(path)`

Input: a directory path string. Output: a list of file paths (each exposing `.suffix`,
`.name`, and a posix relative-path representation)
(tests/test_convo_miner_unit.py:L335-L341).

Extension filter: `.txt` and `.md` files are included; `.png` (binary/image) files are
excluded (tests/test_convo_miner_unit.py:L335-L343). `.json` files are included
(tests/test_convo_miner_unit.py:L353-L358).

Directory skip: a `.git` directory and its contents are skipped; a tree with a
`.git/config.txt` and a top-level `chat.txt` returns only the one file
(tests/test_convo_miner_unit.py:L345-L351).

Meta-file skip: files ending in `.meta.json` are excluded while their sibling `.json`
is kept — `chat.meta.json` is dropped, `chat.json` is kept
(tests/test_convo_miner_unit.py:L353-L359).

Empty directory returns an empty list (tests/test_convo_miner_unit.py:L361-L363).

Symlink handling (non-Windows): symlinked files are never included in the result and
are logged as skipped. A `link.jsonl` symlink alongside a regular `regular.jsonl`
yields only `regular.jsonl`; standard error contains exactly one `SKIP:` line, the
message form `  SKIP:` (two leading spaces), the file name, and the marker `(symlink)`
(tests/test_convo_miner_unit.py:L365-L389). A dangling symlink (target deleted) is
also skipped, producing an empty result and one `SKIP:` line naming the link with
`(symlink)` (tests/test_convo_miner_unit.py:L391-L410). For a nested symlink, the
logged path is the full path relative to the scan root using forward slashes (e.g.
`deep/subdir/nested.jsonl`), not just the leaf name, and includes `(symlink)`
(tests/test_convo_miner_unit.py:L412-L432).

Side effect: skip diagnostics are written to standard error
(tests/test_convo_miner_unit.py:L385-L389).

## `_file_chunks_locked(collection, source_file, chunks, wing, room, agent, mode)`

Performs locked upsert of drawer chunks into a backend collection. Returns a triple
`(drawers, room_counts, skipped)` (tests/test_convo_miner_unit.py:L463-L466).

Return values: `drawers` is the number of chunks written (5 chunks in → 5)
(tests/test_convo_miner_unit.py:L454-L467); `room_counts` is a mapping (empty in the
test) (tests/test_convo_miner_unit.py:L468); `skipped` is a boolean, `False` when the
file was not already mined (tests/test_convo_miner_unit.py:L457-L469).

Bounded batching invariant: upserts are issued in batches of at most
`DRAWER_UPSERT_BATCH_SIZE` documents. With that constant set to 2 and 5 chunks, the
upsert call sizes are, in order, `[2, 2, 1]`
(tests/test_convo_miner_unit.py:L451-L470).

Pre-mining collision scan: before writing, the implementation probes the collection
(a `get(ids=..., include=...)` call) to detect id collisions
(tests/test_convo_miner_unit.py:L446-L449).

Collaborators invoked: it consults `file_already_mined(collection, source_file)` to
decide whether to skip, acquires a per-file lock via `mine_lock(source_file)`, and
classifies the hall via `_detect_hall_cached(content)`
(tests/test_convo_miner_unit.py:L457-L461).

## Constants (observable contract)

`CHUNK_SIZE` is the default maximum drawer content length; `MIN_CHUNK_SIZE` is the
default minimum-content floor; `DRAWER_UPSERT_BATCH_SIZE` is the maximum number of
documents per upsert batch (tests/test_convo_miner_unit.py:L9, L63, L456).
