# Spec: Wing-Name Normalization Migration (`migrate-wings`)

This spec describes the externally observable behavior of the wing-name
normalization migration, as exercised by the test suite. The two units under
test are a pure planner `plan_wing_renames` and a stateful migration
`migrate_wing_names` operating over a backend collection (palace).

## Context / Purpose

A wing name is normalized by stripping leading and trailing separator
characters. Palaces built before this rule filed drawers under un-stripped
names (e.g. `_alpha`). The migration re-keys the `wing` metadata field in place
so those memories become discoverable under the normalized name, merging
collisions when two old names normalize to the same target. Drawer IDs are
opaque and are never changed, and the migration is idempotent
(tests/test_migrate_wings.py:L1-L8).

## Pure Planner: `plan_wing_renames`

### Input

Accepts an ordered list of items, each a pair of `(id, metadata-map)`, where
`metadata-map` may contain a `wing` field plus arbitrary other fields such as
`room` (tests/test_migrate_wings.py:L16-L23).

### Output

Returns a pair `(summary, updates)`
(tests/test_migrate_wings.py:L17-L23).

- `summary` is a mapping keyed by `(old_wing, new_wing)` pairs to the count of
  items whose wing was renamed from `old_wing` to `new_wing`. For input
  containing one item with wing `_alpha` and one with wing `beta_`, the summary
  is `{("_alpha", "alpha"): 1, ("beta_", "beta"): 1}`
  (tests/test_migrate_wings.py:L24-L24). An item whose wing requires no change
  contributes nothing to the summary (tests/test_migrate_wings.py:L33-L36).

- `updates` is a list of `(id, new-metadata-map)` pairs, one entry per item
  that actually changes. Only items whose normalized wing differs from the
  original wing appear (tests/test_migrate_wings.py:L25-L25,L33-L36).

### Normalization Rule

The `wing` value has leading and trailing separator characters removed. A
leading `_` (e.g. `_alpha` to `alpha`) and a trailing `_` (e.g. `beta_` to
`beta`) are both stripped (tests/test_migrate_wings.py:L24-L30).

### Metadata Preservation

For each emitted update, only the `wing` field is rewritten; all other metadata
fields are carried through unchanged. For an item with `{"wing": "_alpha",
"room": "r"}`, the resulting metadata has `wing == "alpha"` and `room == "r"`
(tests/test_migrate_wings.py:L26-L30).

### No-op for Already-Clean Wings

If every item's wing is already normalized (e.g. `already_clean`), both
`summary` and `updates` are empty (tests/test_migrate_wings.py:L33-L36).

### Skipping Empty / Non-String / All-Separator Wings

An item is excluded from `updates` (never stranded) when normalizing its wing
would produce an empty string or when the wing is otherwise unusable. The
following inputs all produce no updates
(tests/test_migrate_wings.py:L39-L48):

- wing `"_"` — normalizes to `""`, so it is skipped rather than relabeled to an
  empty name (tests/test_migrate_wings.py:L42-L42).
- wing `""` — already empty (tests/test_migrate_wings.py:L43-L43).
- wing `None` — non-string value (tests/test_migrate_wings.py:L44-L44).
- metadata map with no `wing` field at all (tests/test_migrate_wings.py:L45-L45).

### Collision Mapping

When two distinct old wings normalize to the same target, both items are
emitted with that single shared target wing. Input wings `_gamma` and `gamma_`
both yield updates whose new `wing` is `gamma` (the set of resulting wing values
is `{"gamma"}`) (tests/test_migrate_wings.py:L51-L58).

## Stateful Migration: `migrate_wing_names`

### Signature / Inputs

Called with a palace directory path (string) and one of two mode flags:

- `confirm=True` performs the migration (tests/test_migrate_wings.py:L107,L128,L150,L152).
- `dry_run=True` plans but does not modify storage (tests/test_migrate_wings.py:L139).

### Return Value

Returns a boolean indicating whether there was work to do:

- Returns `True` when at least one wing needed normalization, both for an
  applied migration (tests/test_migrate_wings.py:L107,L150) and for a dry run
  (tests/test_migrate_wings.py:L139).
- Returns `False` when no wing needed normalization, e.g. a second run after a
  prior successful migration finds nothing left to normalize
  (tests/test_migrate_wings.py:L152).

### Effect: Relabel Old-Format Wings

Drawers stored under un-normalized wing names are re-keyed so their `wing`
metadata becomes the normalized name. The drawer ID is preserved. Given seeded
drawers with wings `_alpha`, `beta_`, and `clean`, after a confirmed migration:

- the drawer originally under `_alpha` is now found under wing `alpha` with its
  original ID `drawer__alpha_r_1` (tests/test_migrate_wings.py:L97-L97,L109-L109).
- the drawer originally under `beta_` is now found under wing `beta` with its
  original ID `drawer_beta__r_2` (tests/test_migrate_wings.py:L98-L98,L110-L110).
- the old wing names `_alpha` and `beta_` contain no drawers afterward
  (tests/test_migrate_wings.py:L111-L112).
- an already-clean wing `clean` is left untouched, still containing its
  original drawer (tests/test_migrate_wings.py:L113-L114).

### Effect: Merge Collision Into Existing Wing

When a legacy wing normalizes to a target that already exists, the migrated
drawer is merged into the existing wing rather than overwriting it. Given a
drawer under `gamma` and a drawer under `_gamma`, after migration wing `gamma`
contains both drawer IDs and `_gamma` is empty
(tests/test_migrate_wings.py:L120-L131).

### Dry-Run Contract

With `dry_run=True`, no drawers are moved: a drawer seeded under wing `_x`
remains under `_x` and the normalized wing `x` stays empty, while the call still
returns `True` to signal pending work (tests/test_migrate_wings.py:L137-L142).

### Idempotence

Running the migration a second time after a successful first run is a no-op: the
first confirmed run returns `True`, the second returns `False`, and the drawer
remains correctly filed under the normalized wing `y` with its original ID
(tests/test_migrate_wings.py:L148-L153).

## Storage / Collection Contract (as relied on by tests)

Seeding writes drawers into a palace collection. Each drawer has an ID, a
document body, a metadata map, and an embedding vector. Embeddings are supplied
explicitly so the migration requires no embedding model — the migration only
reads and writes metadata (tests/test_migrate_wings.py:L64-L76).

Drawer metadata as written by the test seeder consists of the fields `wing`,
`room`, `source_file`, and `chunk_index` (tests/test_migrate_wings.py:L87-L88).

Membership of a wing is determined by querying the collection for items whose
`wing` metadata equals the given value, returning the set of matching IDs
(tests/test_migrate_wings.py:L79-L84). A palace is a directory that must exist
before seeding (tests/test_migrate_wings.py:L91-L93,L117-L119,L134-L137,L145-L148).

<promise>SPEC_WRITTEN path=specs/tests/test_migrate_wings.md citations=31</promise>
