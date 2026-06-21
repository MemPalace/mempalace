# Behavior Spec — `tests/test_dedup.py`

This file is a test suite that pins down the observable contract of the
`mempalace.dedup` module: near-duplicate drawer detection and removal across a
storage collection (tests/test_dedup.py:L1-L6). It exercises four public
functions: `get_source_groups`, `dedup_source_group`, `show_stats`, and
`dedup_palace`. The tests interact with a storage collection abstraction that
exposes `count()`, `get(...)`, `query(...)`, and `delete(...)` operations
(tests/test_dedup.py:L13-L15, L113-L121, L169-L180). The implementation of
`dedup` and the collection backend are mocked, so the contracts below are
inferred from how the suite asserts on calls and return values.

## `get_source_groups(collection, min_count, source_pattern=None, *, wing=None)`

Groups drawers by their `source_file` metadata value and returns a mapping from
source-file name to the list of drawer ids belonging to that file
(tests/test_dedup.py:L28-L30). Drawers are read from the collection (which
reports its total via `count()` and yields records via paginated `get(...)`
calls that each return `ids` and `metadatas`; iteration ends when a `get`
returns an empty `ids` list) (tests/test_dedup.py:L14-L27).

A group is only included in the result when it contains at least `min_count`
drawers. A file with 5 drawers and `min_count=5` is returned with all 5 ids
(tests/test_dedup.py:L28-L30); a file with only 2 drawers and `min_count=5`
yields an empty result mapping (tests/test_dedup.py:L33-L47).

When `source_pattern` is provided, only source files whose name matches the
pattern (substring match, e.g. pattern `"project_a"` matches
`"project_a.txt"`) are eligible; non-matching files are excluded even if they
exceed `min_count` (tests/test_dedup.py:L50-L69).

When `wing` is provided, it is passed to the collection's `get(...)` call as a
filter `where={"wing": <wing>}` (tests/test_dedup.py:L88-L91).

When a drawer's metadata has no `source_file` key, that drawer is grouped under
the literal key `"unknown"` (tests/test_dedup.py:L94-L105).

## `dedup_source_group(collection, ids, threshold, dry_run)`

Given a list of drawer ids belonging to one source group, decides which drawers
to keep and which to delete as near-duplicates. Returns a pair `(kept,
deleted)` — two lists of drawer ids (tests/test_dedup.py:L122-L124).

Drawer documents and metadata are fetched via `collection.get(...)` returning
`ids`, `documents`, and `metadatas` (tests/test_dedup.py:L113-L117).
Similarity is determined by `collection.query(...)`, which returns nested `ids`
and `distances` arrays; a smaller distance means more similar
(tests/test_dedup.py:L118-L121).

When two documents are semantically far apart (distance `0.8` with
`threshold=0.15`), both are kept and nothing is deleted
(tests/test_dedup.py:L118-L124). When two documents are near-identical
(distance `0.05` below threshold `0.15`), one is kept and one is deleted
(tests/test_dedup.py:L137-L143).

Documents that are too short are deleted regardless of similarity: a document
of `"tiny"` is placed in `deleted` (tests/test_dedup.py:L150-L154). A document
that is empty/absent (a `None` value) is likewise deleted
(tests/test_dedup.py:L161-L165).

When `dry_run` is false, deletions are actually performed by calling
`collection.delete(...)` exactly once (tests/test_dedup.py:L179-L180). The
dry-run cases never assert a delete call, implying `dry_run=True` reports the
plan without mutating the collection (tests/test_dedup.py:L122, L141, L153,
L164, L194).

If the similarity query raises an error, the function does not propagate it and
instead keeps all drawers in the group (both ids returned in `kept`, none
deleted) (tests/test_dedup.py:L193-L195).

## `show_stats(palace_path)`

Opens the palace collection via `get_collection` and prints/produces dedup
statistics over the source groups (tests/test_dedup.py:L206-L225). It accepts a
`palace_path` string and must complete without raising for a valid collection
that has groupable drawers (tests/test_dedup.py:L225). The collection is
obtained through the module-level `get_collection` dependency
(tests/test_dedup.py:L206, L223).

## `dedup_palace(palace_path, source_pattern=None, wing=None, dry_run=...)`

Orchestrates a full-palace dedup pass. It obtains the collection via
`get_collection`, computes source groups via `get_source_groups`, and runs
`dedup_source_group` on each qualifying group (tests/test_dedup.py:L231-L243).

For every non-empty source group returned, `dedup_source_group` is invoked (once
per group); a single group of 5 ids triggers exactly one such call
(tests/test_dedup.py:L239-L243).

`get_source_groups` is called with the collection, a default `min_count` of `5`,
a `source_pattern` of `None`, and the `wing` keyword forwarded from the caller —
i.e. `get_source_groups(collection, 5, None, wing="test_wing")`
(tests/test_dedup.py:L255-L256).

When there are no source groups (empty mapping), `dedup_source_group` is never
called (tests/test_dedup.py:L262-L269).

## Invariants and contracts

- `get_source_groups` only emits groups whose size is `>= min_count`
  (tests/test_dedup.py:L33-L47).
- Missing `source_file` metadata maps to the bucket key `"unknown"`
  (tests/test_dedup.py:L94-L105).
- `dedup_source_group` returns `(kept, deleted)` where membership is mutually
  exclusive; short and empty documents are always deleted; query failures cause
  conservative full retention (tests/test_dedup.py:L122-L195).
- Mutation only occurs when `dry_run` is false, via a single `delete` call
  (tests/test_dedup.py:L179-L180).
- `dedup_palace` defaults `min_count` to `5` and forwards `wing`
  (tests/test_dedup.py:L255-L256), and skips dedup work entirely when no groups
  qualify (tests/test_dedup.py:L262-L269).
