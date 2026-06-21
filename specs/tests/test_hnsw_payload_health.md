# Spec: HNSW Payload Health and Quarantine Tests

This file is a behavioral test suite that pins down the externally observable
contract of three HNSW segment health primitives in the ChromaDB backend:
`_hnsw_link_to_data_ratio`, `_segment_appears_healthy`, and
`quarantine_stale_hnsw`, plus the constant `_HNSW_LINK_TO_DATA_MAX_RATIO`
(tests/test_hnsw_payload_health.py:L4-L9). The spec below describes the behavior
of those primitives as asserted by the tests; an implementer must satisfy every
asserted contract.

## Test fixture: segment layout on disk

A "segment" is a directory whose name is a UUID-style string containing a `-`
(e.g. `11111111-2222-3333-4444-555555555555`)
(tests/test_hnsw_payload_health.py:L30, L67). A segment directory is created
with three files (tests/test_hnsw_payload_health.py:L12-L26):

- `data_level0.bin` — a payload file written with `data_size` zero-bytes
  (default 100) (tests/test_hnsw_payload_health.py:L20).
- `link_lists.bin` — a link-list file written with `link_size` zero-bytes
  (default 100) (tests/test_hnsw_payload_health.py:L21).
- `index_metadata.pickle` — optional (controlled by `write_metadata`, default
  true). When written, it contains a byte payload that begins with marker byte
  `0x80`, has 16 filler bytes, and ends with byte `0x2e` — i.e. a valid pickle
  envelope (begin protocol marker `0x80`, end STOP `0x2e`)
  (tests/test_hnsw_payload_health.py:L23-L26).

## `_hnsw_link_to_data_ratio(seg_dir: str) -> float`

Returns the ratio `size(link_lists.bin) / size(data_level0.bin)` for a segment
directory. With `data_size=100` and `link_size=250`, the returned value is
exactly `2.5` (tests/test_hnsw_payload_health.py:L29-L33). The result is a plain
numeric ratio of file byte sizes.

## `_HNSW_LINK_TO_DATA_MAX_RATIO`

A numeric threshold constant. A link/data ratio strictly greater than this value
indicates a structurally corrupt ("exploded") link-list payload; a ratio at or
below this value is acceptable (tests/test_hnsw_payload_health.py:L41, L53). The
test treats `_HNSW_LINK_TO_DATA_MAX_RATIO` as a boundary: the value exactly at
the threshold is healthy, and one increment past it (ratio
`_HNSW_LINK_TO_DATA_MAX_RATIO + 1`) is unhealthy
(tests/test_hnsw_payload_health.py:L41, L53).

## `_segment_appears_healthy(seg_dir: str) -> bool`

Returns whether a segment's payload is structurally plausible. Observable
contract:

- A segment with a valid pickle metadata envelope but an exploded link-list
  (link size `= 100 * (_HNSW_LINK_TO_DATA_MAX_RATIO + 1)`, data size `100`, so
  ratio above the threshold) is reported as NOT healthy. A valid pickle does not
  override the size-ratio check (tests/test_hnsw_payload_health.py:L36-L45).
- A segment with a valid pickle and a link size exactly at the threshold (link
  `= 100 * _HNSW_LINK_TO_DATA_MAX_RATIO`, data `100`) is reported as healthy
  (tests/test_hnsw_payload_health.py:L48-L57).
- A segment with a real payload (data size `2000`) but a zero-byte
  `link_lists.bin` is reported as NOT healthy. An empty link-list file alongside
  a non-trivial payload is corrupt, not a harmless transient state (regression
  #1457) (tests/test_hnsw_payload_health.py:L116-L127).

## `quarantine_stale_hnsw(palace_path: str, stale_seconds: float) -> list[str]`

Scans a palace directory for HNSW segment directories and moves corrupt/stale
ones aside, returning a list of the new (quarantined) paths.

Inputs/setup observed by the tests: the palace directory contains a
`chroma.sqlite3` file and one or more segment subdirectories
(tests/test_hnsw_payload_health.py:L61-L73). The function takes the palace path
as a string and a `stale_seconds` parameter
(tests/test_hnsw_payload_health.py:L81, L110, L151).

Return value: a list of strings, each a filesystem path to a quarantined
segment. An empty list means nothing was quarantined
(tests/test_hnsw_payload_health.py:L83, L112, L153).

### Structural-corruption detection independent of mtime drift

When a segment's payload is structurally corrupt (link/data ratio above
`_HNSW_LINK_TO_DATA_MAX_RATIO`), it is quarantined even when the SQLite file and
the segment's `data_level0.bin` have identical modification times — i.e. no
mtime drift gate can shield a structurally corrupt segment
(tests/test_hnsw_payload_health.py:L60-L88). In this scenario, with both mtimes
set to the same epoch value `1700000000`
(tests/test_hnsw_payload_health.py:L77-L79) and a generous `stale_seconds` of
`999999` (tests/test_hnsw_payload_health.py:L81):

- exactly one segment is moved (`len(moved) == 1`)
  (tests/test_hnsw_payload_health.py:L83),
- the original segment directory no longer exists
  (tests/test_hnsw_payload_health.py:L84),
- the moved path exists on disk (tests/test_hnsw_payload_health.py:L87), and
- the moved directory's name begins with the original segment name followed by
  the suffix `.drift-` (i.e. `<segment-name>.drift-`)
  (tests/test_hnsw_payload_health.py:L88).

### Healthy payloads are left in place

When a segment has a reasonable payload (link `100`, data `100`, ratio `1.0`)
and a valid pickle, even with SQLite/segment mtimes made identical and a large
`stale_seconds` of `999999`, nothing is quarantined: the returned list is empty
and the segment directory still exists
(tests/test_hnsw_payload_health.py:L91-L113).

### Zero-byte link lists quarantined when stale

A segment with a real payload (data size `2000`) and a zero-byte
`link_lists.bin` is quarantined when it is stale relative to the SQLite file. In
the test, `data_level0.bin` mtime is set to `1700000000` and `chroma.sqlite3`
mtime is set `1000` seconds later (`1700001000`), with `stale_seconds=300`
(tests/test_hnsw_payload_health.py:L130-L151). The result:

- exactly one segment is moved (tests/test_hnsw_payload_health.py:L153),
- the original segment directory no longer exists
  (tests/test_hnsw_payload_health.py:L154),
- the moved path exists (tests/test_hnsw_payload_health.py:L157), and
- the moved directory's name begins with `<segment-name>.drift-`
  (tests/test_hnsw_payload_health.py:L158).

## Side effects

These functions operate on the local filesystem only: they read file sizes and
modification times, and `quarantine_stale_hnsw` renames/moves segment
directories to sibling `*.drift-*` directories under the same palace path
(tests/test_hnsw_payload_health.py:L84, L87-L88, L154, L157-L158). Modification
times are an input the function reads (tests are set up via `os.utime`)
(tests/test_hnsw_payload_health.py:L78-L79, L107-L108, L148-L149).
