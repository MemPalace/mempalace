# Behavior Specification: `mempalace/hallways.py`

## Overview

A **hallway** is a within-wing connection between two entities (people, projects,
concepts, interests), materialized from their co-occurrence across the drawers of
a single wing. Each hallway record asserts the structural fact "these two entities
travel together inside this wing" (mempalace/hallways.py:L1-L18). Hallways are
entity-centric, distinct from the room-centric "tunnel" primitive defined
elsewhere (mempalace/hallways.py:L20-L26).

## Public Surface

The module exports exactly three public functions: `compute_hallways_for_wing`,
`list_hallways`, and `delete_hallway` (mempalace/hallways.py:L58-L62). Helper
functions prefixed with `_` (e.g. `_get_hallway_file`, `_load_hallways`,
`_save_hallways`) are internal but constitute observable contracts described below.

## Persistence Contract

### File location

Hallways are persisted to a single JSON file whose path is derived from the
configured palace path, exposed as `MempalaceConfig.hallway_file`
(mempalace/hallways.py:L73-L78). A legacy hardcoded path
`~/.mempalace/hallways.json` (home directory + `.mempalace` + `hallways.json`)
exists only for one-time orphan detection (mempalace/hallways.py:L81-L83).

### On-disk format

The persisted file is a JSON object with two keys: `schema_version` (integer,
currently `1`) and `hallways` (an array of hallway records)
(mempalace/hallways.py:L55-L55, L133-L136). On read, the loader accepts three
shapes: (a) an object containing a `hallways` key — returns that array or `[]` if
null; (b) a bare JSON array — returned as-is; (c) anything else — returns `[]`
(mempalace/hallways.py:L105-L109).

### Read behavior and corruption tolerance

`_load_hallways` returns `[]` if the configured file is missing or cannot be
parsed (I/O error or invalid JSON are both treated as empty)
(mempalace/hallways.py:L86-L109). If the configured file does not exist but the
legacy file exists at a different path, a single warning is logged naming both
paths; the legacy file is **not** auto-migrated or merged, and `[]` is returned
(mempalace/hallways.py:L111-L119).

### Write behavior (atomicity and permissions)

`_save_hallways` ensures the parent directory exists, then writes the payload
atomically: it writes to a temp file (prefix `.hallways-`, suffix `.tmp`) in the
same directory, then renames it over the target via an atomic replace, so a crash
mid-write cannot corrupt the existing file (mempalace/hallways.py:L122-L146). The
temp file's permissions are restricted to owner read/write only (mode `0600`) on
systems that support it; failure to set permissions is non-fatal and ignored
(mempalace/hallways.py:L142-L145). The JSON is written with 2-space indentation and
non-ASCII characters preserved literally (not escaped) (mempalace/hallways.py:L140-L140).
On any write error, the temp file is removed and the error is re-raised
(mempalace/hallways.py:L147-L152).

## Entity Parsing Contract

Drawer `entities` metadata is parsed by `_parse_entities`. Accepted inputs:
a falsy value yields `[]`; a list/tuple/set yields its stringified, whitespace-
trimmed, non-empty members; a string is split on semicolons (`;`) with each part
trimmed and empties dropped; any other type yields `[]`
(mempalace/hallways.py:L160-L174). Results are deduplicated while preserving
first-seen order, so a drawer mentioning `Aya;Aya` contributes a single `Aya`
(mempalace/hallways.py:L175-L182).

## Hallway ID Contract

`_hallway_id(wing, entity_a, entity_b)` produces a deterministic, symmetric id.
The two entities are sorted before hashing, so the pair `(Aya, Lumi)` and
`(Lumi, Aya)` produce the same id, making re-mines idempotent
(mempalace/hallways.py:L185-L192). The id format is
`hallway_{wing}_{a}_{b}_{suffix}` where `a` and `b` are the sorted entities and
`suffix` is the first 8 hex characters of the SHA-256 hash of the UTF-8 bytes of
`{wing}::{a}::{b}` (mempalace/hallways.py:L192-L195).

## `compute_hallways_for_wing(wing, col=None, min_count=2) -> list[dict]`

### Inputs

- `wing` (string): the wing name to scan.
- `col`: a backing collection that must support `.count()` and paginated
  `.get(limit=..., offset=..., include=...)` (mempalace/hallways.py:L218-L227).
- `min_count` (integer, default 2): minimum co-occurrence count to materialize a
  hallway; clamped to at least 1 via `max(1, int(min_count))`
  (mempalace/hallways.py:L228-L231, L241-L241).

### Early returns

If `col` is `None`, returns `[]` without side effects
(mempalace/hallways.py:L237-L239).

### Step 1 — Fetch and filter drawers

Drawer metadata is fetched by paginating the collection: `total = col.count()`,
then repeatedly calling `.get(limit=5000, offset=offset, include=["metadatas"])`
advancing `offset` by the number of returned metadatas until `offset >= total` or
an empty batch is returned (mempalace/hallways.py:L249-L262). Filtering to the
target wing is done client-side (`meta["wing"] == wing`), deliberately **not** via
a server-side `where` filter, to avoid SQLite variable-count overflow on large
wings (mempalace/hallways.py:L255-L262). Only dictionary metadatas are retained
(mempalace/hallways.py:L259-L261). Any exception during the fetch is caught,
logged as a warning, and causes a return of `[]` (mempalace/hallways.py:L263-L267).
If no matching metadatas are found, returns `[]` (mempalace/hallways.py:L269-L270).

### Step 2 — Count co-occurrences

Each drawer is processed: non-dict metas are skipped
(mempalace/hallways.py:L278-L280); drawers with a truthy `is_sentinel` flag are
skipped as content-free (mempalace/hallways.py:L281-L283); drawers whose parsed
entity list has fewer than 2 entities are skipped (no pair possible)
(mempalace/hallways.py:L284-L287). For each remaining drawer, the room is taken
from the `room` metadata field only if it is a non-empty/non-whitespace string,
else treated as absent (mempalace/hallways.py:L288-L289). For every unordered pair
of distinct entities in the drawer, a per-pair counter is incremented by 1 and the
drawer's room (if present) is added to that pair's room set; self-pairs (`a == b`)
are skipped; the pair key is canonicalized by sorting the two entity names
(mempalace/hallways.py:L291-L302). If no pairs were counted, returns `[]`
(mempalace/hallways.py:L304-L305).

### Step 3 — Materialize records (with dynamics preservation)

Before building records, existing hallways are loaded so that L7 "dynamics" fields
(`strength`, `stability`, `last_activated`, `access_count`) survive recomputes.
For each existing record belonging to this wing, a lookup is built keyed by the
sorted entity pair, copying only those four dynamics fields that are present
(mempalace/hallways.py:L307-L329). The sorted key is required to match the
symmetric id generation so reversed-order persisted records still match
(mempalace/hallways.py:L324-L324).

Records are generated in ascending sorted order of the canonical pair keys
(mempalace/hallways.py:L333-L333). Pairs whose count is below `min_count` are
skipped (mempalace/hallways.py:L334-L336). For each qualifying pair, the rooms are
sorted (mempalace/hallways.py:L338-L338) and a human-readable room summary is built
from the first 3 rooms comma-joined, or `(no room tags)` when empty, with a
`, +N more` suffix appended when more than 3 rooms exist
(mempalace/hallways.py:L339-L341).

Each created record is an object with the following fields
(mempalace/hallways.py:L342-L352):
- `id`: the deterministic hallway id (see ID contract).
- `wing`: the wing name.
- `entity_a`, `entity_b`: the sorted pair (`entity_a <= entity_b`).
- `co_occurrence_count`: the integer co-occurrence count.
- `rooms`: the sorted array of room names where the pair co-occurred.
- `label`: a display string of the form
  `{a} ↔ {b} (co-occur in {count} drawers across {N or "no"} room{s?}: {summary})`,
  where the trailing `s` is present only when the room count is not exactly 1.
- `created_at`: an ISO-8601 UTC timestamp captured once at the start of record
  generation and shared by all records in this run (mempalace/hallways.py:L332-L332).
- `created_by`: the literal string `"auto"`.

After construction, preserved dynamics fields for this pair (if any) are applied
over the record, then `initialize_dynamics_fields` fills any still-missing dynamics
fields without overwriting existing ones (mempalace/hallways.py:L353-L359). The
defaults applied: `strength` = 1.0, `stability` = 1.0, `access_count` = 0, and
`last_activated` defaults to the record's `created_at`
(mempalace/dynamics.py:L51-L55, L96-L101).

### Step 4 — Persist and return

The full list written to disk is all existing records belonging to **other** wings
(preserved unchanged) concatenated with the newly created records for this wing;
this wing's prior records are thereby replaced (mempalace/hallways.py:L361-L363).
The function returns only the list of records created for this wing
(mempalace/hallways.py:L365-L365).

### Invariants and ordering guarantees

- Idempotency: re-running with the same drawers upserts identical ids rather than
  duplicating records, due to symmetric id derivation (mempalace/hallways.py:L185-L195).
- Output records are emitted in ascending sorted order of the canonical entity-pair
  key (mempalace/hallways.py:L333-L333).
- Other wings' records are never mutated by a compute targeting one wing
  (mempalace/hallways.py:L361-L363).
- Dynamics fields accumulated through use are preserved across recomputes
  (mempalace/hallways.py:L307-L329, L356-L358).

## `list_hallways(wing=None) -> list[dict]`

Loads all hallway records. If `wing` is `None`, returns a copy of every record;
otherwise returns only records whose `wing` field equals the argument
(mempalace/hallways.py:L373-L378).

## `delete_hallway(hallway_id) -> bool`

Loads all records, removes every record whose `id` equals `hallway_id`, and if the
count changed, persists the filtered list and returns `True`; if no record matched,
returns `False` without writing (mempalace/hallways.py:L381-L388).

## Side Effects

- Filesystem: reads and writes the configured hallway JSON file and creates its
  parent directory if absent (mempalace/hallways.py:L86-L146).
- Filesystem permissions: sets the persisted file to `0600` on supporting systems
  (mempalace/hallways.py:L142-L145).
- Logging: emits debug, warning logs under logger name `mempalace_hallways`
  (mempalace/hallways.py:L47-L47, L102-L118, L263-L266).
- No network, process spawning, or environment-variable mutation is performed.
