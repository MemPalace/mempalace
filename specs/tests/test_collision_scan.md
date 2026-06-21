# Behavior Spec: collision_scan — pre-mining drawer_id collision defense

This spec is derived from the test suite `tests/test_collision_scan.py`, which
exercises the public surface of the `collision_scan` module. The module guards
the batched storage upsert that happens during mining, aborting the mine with an
actionable error if any proposed drawer identifier would collide
(tests/test_collision_scan.py:L1-L14).

## Public Surface

The module exposes two public symbols:

- `CollisionError` — an error type raised when a disallowed collision is
  detected (tests/test_collision_scan.py:L22-L22).
- `assert_no_collisions(proposed, collection)` — the collision-scan entry point
  (tests/test_collision_scan.py:L22-L22).

## Inputs

`assert_no_collisions` takes two arguments
(tests/test_collision_scan.py:L60-L68):

1. `proposed` — an ordered list of pairs. Each pair is
   `(drawer_id, metadata)` where `drawer_id` is a string identifier and
   `metadata` is a key/value map. The relevant metadata keys observed are
   `source_file` (a path string) and `chunk_index` (an integer)
   (tests/test_collision_scan.py:L62-L66). `chunk_index` may be absent for some
   drawer types such as diary entries or sentinels; the scan must compare
   whatever metadata is present and must not crash on the missing key
   (tests/test_collision_scan.py:L198-L211).

2. `collection` — an existing-state store representing the current palace. The
   scan queries it by id; the store returns only rows whose ids are present in
   storage, as a result exposing `ids` and `metadatas` parallel lists
   (tests/test_collision_scan.py:L39-L54). The query is invoked with a set of
   ids to look up (tests/test_collision_scan.py:L48-L54).

## Output / Return Contract

On success the function returns nothing meaningful (a null/none result); the
tests assert the return is none in every passing case
(tests/test_collision_scan.py:L68-L68, tests/test_collision_scan.py:L84-L84,
tests/test_collision_scan.py:L100-L100). The contract is binary: the function
either confirms there are no collisions (returns), or raises
(tests/test_collision_scan.py:L214-L220).

## Collision Definition

A collision exists when a drawer_id appears more than once across the union of
(incoming-vs-incoming) and (incoming-vs-existing) AND the colliding occurrences
carry different identifying metadata (tests/test_collision_scan.py:L4-L13,
tests/test_collision_scan.py:L71-L74). The two collision shapes are:

### Incoming-vs-incoming

Two proposed entries sharing the same drawer_id but with different
`(source_file, chunk_index)` metadata is a collision and must raise
`CollisionError` (tests/test_collision_scan.py:L106-L121). When two proposed
entries share the same drawer_id AND the same `(source_file, chunk_index)`, this
is a duplicate chunk in the batch and must NOT raise — the scan passes because
the downstream write would be identical content
(tests/test_collision_scan.py:L124-L136).

### Incoming-vs-existing

A proposed entry whose drawer_id matches an existing stored drawer with
DIFFERENT stored `(source_file, chunk_index)` is a collision and must raise
`CollisionError`, preventing a silent overwrite of the existing row
(tests/test_collision_scan.py:L142-L160). A proposed entry whose drawer_id
matches an existing drawer AND whose `(source_file, chunk_index)` metadata also
matches is a normal idempotent re-mine of the same chunk and must NOT raise
(tests/test_collision_scan.py:L87-L100).

## Passing (no-error) Cases

- Distinct incoming ids with no overlap against existing state pass
  (tests/test_collision_scan.py:L60-L68).
- Existing drawers with ids different from all incoming ids pass; the scan only
  fires when an id appears more than once across the incoming + existing union
  (tests/test_collision_scan.py:L71-L84).
- An empty batch (`proposed` of length zero) is trivially collision-free and
  must not raise, so callers need not guard the call site
  (tests/test_collision_scan.py:L191-L195).

## Error Behavior and Message Contract

When `CollisionError` is raised, its message must name the colliding drawer_id
and the conflicting `source_file` values. For an incoming-vs-incoming collision
the message contains the drawer_id and both source paths
(tests/test_collision_scan.py:L115-L121). For an incoming-vs-existing collision
the message contains the drawer_id, the incoming source path, and the existing
source path (tests/test_collision_scan.py:L155-L160).

When a batch contains multiple distinct collisions, the error message must
surface ALL of them, not just the first — so a user fixing one and re-running
does not rediscover the next from scratch. The message includes every colliding
drawer_id and every involved source_file
(tests/test_collision_scan.py:L166-L185).

## Backend-error Propagation

If the existing-state store raises while being queried (e.g. a transient backend
error), the scan must NOT swallow that error. The error propagates to the caller
unchanged so the caller can decide whether to abort or proceed; the scan must
not convert a backend failure into a false "no collisions" result
(tests/test_collision_scan.py:L214-L228).

## Invariants Summary

- The scan only raises on genuine collisions: same id with differing identifying
  metadata (tests/test_collision_scan.py:L106-L136,
  tests/test_collision_scan.py:L142-L160).
- Identical re-mines and identical duplicate chunks are tolerated
  (tests/test_collision_scan.py:L87-L100,
  tests/test_collision_scan.py:L124-L136).
- The scan never crashes on absent metadata keys
  (tests/test_collision_scan.py:L198-L211) and never hides backend query
  failures (tests/test_collision_scan.py:L214-L228).
