# Spec: `mempalace.ids` — collision-safe ID construction

Behavior specification derived from the test suite `tests/test_ids.py`, which pins the
observable contract of the `mempalace.ids` module (collision-safe identifier construction
for drawers, conversation drawers, sentinels, and knowledge-graph triples). All claims cite
the test file that asserts them.

## Purpose & invariant

The module constructs deterministic, collision-safe string identifiers. The central
invariant under test is that distinct logical inputs must never map to the same ID, even
when their naive concatenation would coincide (the classic boundary-shift collision, e.g.
`"/path/a1" + "23"` vs `"/path/a" + "123"`) (tests/test_ids.py:L48-L62). Identifiers must
also be deterministic: identical inputs always yield the identical ID
(tests/test_ids.py:L65-L71).

## Public surface

### Constant `ID_RECIPE`

A module-level constant whose value is exactly the string `"v3"`
(tests/test_ids.py:L21-L25). This constant tags the recipe version of newly created
drawers; the exact literal value is part of the contract.

### `make_drawer_id_from_chunk(wing, room, source_file, chunk_index) -> str`

Builds a drawer ID from a wing name, room name, source file path, and a chunk index integer
(tests/test_ids.py:L34-L36).

- Output begins with the prefix `drawer_<wing>_<room>_`, namespacing the ID by wing and room
  so cross-wing collisions are impossible regardless of the hash slice
  (tests/test_ids.py:L31-L36, L42-L43).
- After the prefix, the ID ends with a hash slice of exactly 24 characters, each character a
  lowercase hexadecimal digit (`0`-`9`, `a`-`f`) (tests/test_ids.py:L38-L45).
- Deterministic: identical `(wing, room, source_file, chunk_index)` inputs always produce the
  identical ID (tests/test_ids.py:L65-L71).
- Collision-safe across the source-file / chunk-index boundary: `(source_file="/path/a1",
  chunk_index=23)` and `(source_file="/path/a", chunk_index=123)` must yield distinct IDs,
  even though naive concatenation would produce the same `"/path/a123"`
  (tests/test_ids.py:L48-L62).
- Collision-safe when the source path contains a colon: `"C:\Users\foo"` and
  `"C:\Users\foo:"` (with the same chunk index) must yield distinct IDs. The delimiting
  scheme must not be defeated by paths ending in `:digits` (Windows drive letters, URLs like
  `https://host:8080`) (tests/test_ids.py:L74-L82).

### `make_drawer_id_from_content(wing, room, content) -> str`

Builds a drawer ID from a wing name, room name, and a content string
(tests/test_ids.py:L93-L94).

- Output begins with the prefix `drawer_<wing>_<room>_` (tests/test_ids.py:L98-L101).
- Collision-safe across the wing/room boundary: `(wing="foo", room="bar", "x")` and
  `(wing="fooba", room="r", "x")` must yield distinct IDs, even though `"foo"+"bar"` and
  `"fooba"+"r"` concatenate identically (tests/test_ids.py:L88-L95).

### `make_convo_drawer_id(wing, room, source_file, extract_mode, chunk_index) -> str`

Builds a conversation drawer ID from wing, room, source file, an extract-mode string, and a
chunk index (tests/test_ids.py:L114-L115).

- Output begins with the prefix `drawer_<wing>_<room>_` (tests/test_ids.py:L119-L121).
- Collision-safe across the extract-mode boundary: identical wing/room/source/chunk but
  differing `extract_mode` ("general" vs "extract") must yield distinct IDs
  (tests/test_ids.py:L107-L116).

### `make_convo_sentinel_id(source_file, extract_mode) -> str`

Builds a sentinel (registration marker) ID from a source file and an extract-mode string
(tests/test_ids.py:L130).

- Output begins with the prefix `_reg_`, distinct from the `drawer_` prefix so sentinels can
  be filtered out of normal drawer queries (tests/test_ids.py:L127-L131).
- Distinguishes extract modes: same source file with differing `extract_mode` ("general" vs
  "extract") must yield distinct IDs (tests/test_ids.py:L134-L137).

### `make_triple_id(subject, predicate, object, valid_from, recorded_at) -> str`

Builds a knowledge-graph triple ID from subject, predicate, object, a `valid_from` value,
and a `recorded_at` value (tests/test_ids.py:L146).

- Output begins with the prefix `t_<subject>_<predicate>_<object>_`, embedding the triple in
  the ID for searchability in the underlying store (tests/test_ids.py:L143-L147).
- After the prefix, the ID ends with a hash slice of exactly 12 characters (shorter than the
  24-char drawer slice, since the embedded triple already supplies most of the namespace)
  (tests/test_ids.py:L150-L156).
- Collision-safe across the ISO-datetime boundary between `valid_from` and `recorded_at`:
  `(valid_from="2026-01-01", recorded_at="T12:00:00")` and `(valid_from="2026-01-01T12",
  recorded_at=":00:00")` must yield distinct IDs (tests/test_ids.py:L159-L167).

## Private helper `_delimited_sha256(parts, length) -> str`

A helper that hashes an ordered sequence of string `parts` and truncates the hex output to
`length` characters. It is tested directly only as a smoke test (tests/test_ids.py:L170-L188).

- It uses length-prefixed encoding of each part: the parts `("a", "b")` produce exactly the
  SHA-256 hex digest of the byte string `1:a1:b` — i.e. each part is encoded as
  `<byte-length>:<part>` and concatenated before hashing (tests/test_ids.py:L173-L176).
- The length-prefix scheme keeps inputs distinct even when a naive pipe-join would collapse
  them: `("a", "b|c", "d")` and `("a|b", "c", "d")` both pipe-join to `"a|b|c|d"` yet must
  produce distinct hashes (tests/test_ids.py:L178-L182).
- The `length` argument truncates the hexadecimal output to exactly that many characters: a
  `length` of 8 yields an 8-character result; a `length` of 64 yields the full SHA-256 hex
  digest (tests/test_ids.py:L185-L188, L174-L176).

## Observable contracts summary

- Drawer-family IDs are prefixed `drawer_<wing>_<room>_` (tests/test_ids.py:L35, L101, L121).
- Sentinel IDs are prefixed `_reg_` (tests/test_ids.py:L131).
- Triple IDs are prefixed `t_<subject>_<predicate>_<object>_` (tests/test_ids.py:L147).
- Drawer hash slices are 24 lowercase hex chars; triple hash slices are 12 hex chars
  (tests/test_ids.py:L44-L45, L156).
- Hashing is SHA-256 over a length-prefixed (`<len>:<part>`) encoding of the ordered parts,
  truncated to the target length (tests/test_ids.py:L173-L176).
