# Behavior Spec: Within-Wing Hallway Primitive

This spec describes the observable behavior of the hallway module exercised by
`tests/test_hallways.py`. A "hallway" is a bridge INSIDE a single wing that
connects two entities (people, projects, concepts, interests) when they appear
together in enough drawers across that wing (tests/test_hallways.py:L1-L10). The
implementation under test lives in `mempalace/hallways.py` and is referenced
through these tests as the authoritative contract.

## Test Environment / Fixtures

The storage backend dependency (`chromadb`) is mocked at import time so the
module can load even when that dependency is absent
(tests/test_hallways.py:L15-L19).

Hallway records persist to a JSON file. Tests redirect the file-path resolver
`_get_hallway_file()` to return `<tmp>/hallways.json` and redirect
`_legacy_hallway_file()` to `<tmp>/legacy-hallways.json`, keeping all tests on
the configured-path branch and away from a legacy-file warning branch
(tests/test_hallways.py:L22-L35). This implies the module exposes two
path-resolver hooks: `_get_hallway_file()` and `_legacy_hallway_file()`.

A drawer collection is modeled as a paginated fetch interface: it exposes
`count()` returning the total number of drawers, and `get(limit=, offset=,
include=, where=, ids=)` returning a page as a mapping with keys `ids` (list of
synthetic drawer ids like `drawer_<n>`) and `metadatas` (the slice of drawer
metadata dicts) (tests/test_hallways.py:L38-L54). Drawer metadata dicts carry at
least the fields `wing`, `room`, and `entities` (a string of entity tokens), and
optionally `is_sentinel` (tests/test_hallways.py:L106-L109,
L239-L250). `compute_hallways_for_wing` MUST iterate drawers via this paginated
`count()`+`get(limit=, offset=)` pattern to stay under the storage backend's
variable limit (tests/test_hallways.py:L38-L41).

## Storage Primitives: `_load_hallways` / `_save_hallways`

`_load_hallways()` returns the list of persisted hallway records. When the
backing file does not exist, it returns an empty list
(tests/test_hallways.py:L63-L65). When the backing file exists but contains
invalid/corrupt JSON, it also returns an empty list rather than raising
(tests/test_hallways.py:L67-L70).

`_save_hallways(records)` writes a list of hallway-record dicts to the backing
file such that a subsequent `_load_hallways()` returns an equal list (lossless
round trip) (tests/test_hallways.py:L72-L86). The file is UTF-8 encoded
(tests/test_hallways.py:L69). A hallway record may contain at minimum these
fields and they survive the round trip: `id`, `wing`, `entity_a`, `entity_b`,
`co_occurrence_count`, `rooms` (list of room names), and `label`
(tests/test_hallways.py:L74-L86).

## `compute_hallways_for_wing(wing, col=, min_count=)`

Computes hallways for one wing by counting entity-pair co-occurrence across the
wing's drawers, persists them, and returns the resulting records
(tests/test_hallways.py:L94-L99). Parameters: `wing` (string wing id), `col`
(the drawer collection), and an optional `min_count` threshold
(tests/test_hallways.py:L99, L127).

### Co-occurrence and pairing

Each drawer's `entities` field is a token string. Tokens within a drawer are
separated by `;` (e.g. `"Aya;Lumi;Ever"`) (tests/test_hallways.py:L122-L124). A
drawer contributes a co-occurrence to a pair only if it mentions at least two
entities; drawers with one entity (`"Aya"`) or none (`""`) contribute no pairs
(tests/test_hallways.py:L102-L112).

Entity pairs are symmetric: `"Aya;Lumi"` and `"Lumi;Aya"` refer to the same
hallway, and both drawers count toward the same pair's co-occurrence count with
no double-bookkeeping (tests/test_hallways.py:L189-L203). The co-occurrence
count equals the number of distinct drawers in which the pair appears, NOT the
number of rooms — three drawers across two rooms yields count 3
(tests/test_hallways.py:L219-L232).

Entities are not restricted to people; any token (e.g. `consciousness`) is
treated identically to a person name, so a person↔concept pair is a valid
hallway when it meets the threshold (tests/test_hallways.py:L136-L157).

### Threshold

A hallway record is produced for a pair only when its co-occurrence count is at
least `min_count`. With `min_count=2`, a pair co-occurring in 3 drawers yields
exactly one hallway record (tests/test_hallways.py:L114-L134). With
`min_count=3`, a pair co-occurring in only 2 drawers yields no record
(tests/test_hallways.py:L159-L169).

### Empty / degenerate cases

A wing with no drawers returns an empty list and does not error
(tests/test_hallways.py:L95-L100). A wing where no drawer has two entities
returns an empty list (tests/test_hallways.py:L102-L112).

### Drawers excluded from computation

Drawers flagged with `is_sentinel: True` are skipped entirely and contribute no
co-occurrences, even if they name two entities — two sentinel drawers naming
`"Aya;Lumi"` yield no hallway (tests/test_hallways.py:L234-L254).

### Record fields produced

Each produced hallway record carries: `wing` (the computed wing), `entity_a`
and `entity_b` (the two paired entities), `co_occurrence_count` (drawer count),
and `rooms` (the SET of distinct rooms in which the pair co-occurred)
(tests/test_hallways.py:L128-L134, L152-L157, L219-L231).

### Deterministic / idempotent IDs

Each record has an `id` beginning with the prefix `hallway_`
(tests/test_hallways.py:L184-L187). The id is deterministic for a given wing and
entity pair: re-running `compute_hallways_for_wing` on the same wing and pair
produces the same id (tests/test_hallways.py:L171-L186). The id is also
symmetric in the entity pair. The module exposes a helper `_hallway_id(wing,
entity_a, entity_b)` that canonicalizes the pair by sorting so the id is
identical regardless of argument order (tests/test_hallways.py:L444,
L171-L187).

### Persistence side effect

After `compute_hallways_for_wing` runs, the backing JSON file exists and
`_load_hallways()` returns the newly computed records, including the computed
pair(s) (tests/test_hallways.py:L205-L217).

## Query API

`list_hallways(wing=None)` returns persisted hallway records. With no filter it
returns all records (tests/test_hallways.py:L263-L271). When given a `wing`, it
returns only records whose `wing` field matches that wing
(tests/test_hallways.py:L273-L283).

`delete_hallway(id)` removes the record whose `id` matches. It returns `True`
when a record was removed, and the record is gone from `_load_hallways()`
afterward while other records remain (tests/test_hallways.py:L285-L296). It
returns `False` when no record matches the given id, leaving storage unchanged
(tests/test_hallways.py:L298-L301).

## L7 Dynamics Integration

Hallway records produced by `compute_hallways_for_wing` MUST carry living-
connection dynamics fields so the dynamics layer can operate on them
(tests/test_hallways.py:L309-L315). These reference constants
`DEFAULT_STRENGTH` and `DEFAULT_STABILITY` from `mempalace/dynamics.py`
(tests/test_hallways.py:L318, L382).

### New record defaults

Every newly created hallway record carries: `strength` equal to
`DEFAULT_STRENGTH`, `stability` equal to `DEFAULT_STABILITY`, `access_count`
equal to `0`, and a `last_activated` field. `last_activated` MUST equal the
record's `created_at` so decay begins at creation time rather than at recompute
time (tests/test_hallways.py:L317-L340). Records also carry `created_at` and
`created_by` fields (tests/test_hallways.py:L451-L452).

### Recompute preserves accumulated dynamics

When `compute_hallways_for_wing` is re-run on the same drawer set, any
previously accumulated dynamics on existing pairs MUST be preserved, not reset.
If a stored record's `strength`, `access_count`, and `stability` were bumped
(e.g. to 2.5, 7, 1.8), recomputing the same wing leaves those exact values
intact (tests/test_hallways.py:L342-L376).

### Recompute initializes only genuinely new pairs

When a recompute discovers a pair that did not previously exist in the wing's
hallways, that new record gets default dynamics (`strength ==
DEFAULT_STRENGTH`), and must NOT inherit dynamics from any unrelated existing
record. Simultaneously, pre-existing pairs keep their accumulated values — e.g.
an existing `Aya↔Lumi` with bumped `strength` 3.5 stays 3.5 while a newly
discovered `Aya↔Ever` starts at `DEFAULT_STRENGTH`
(tests/test_hallways.py:L378-L421).

### Canonicalized preservation lookup

The dynamics-preservation lookup MUST canonicalize the entity pair by sorting,
matching the symmetric id generation. If a persisted record stores the pair in
reversed (non-sorted) order — e.g. `entity_a="Lumi"`, `entity_b="Aya"` while its
`id` was generated from `_hallway_id("wing_aya","Aya","Lumi")` — a recompute that
produces the pair in sorted order must still match the existing record and
preserve all its dynamics (`strength` 4.2, `stability` 1.9, `access_count` 33,
`last_activated`), producing exactly one record rather than resetting to defaults
(tests/test_hallways.py:L423-L479).

<promise>SPEC_WRITTEN path=specs/tests/test_hallways.md citations=40</promise>
