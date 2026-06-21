# Behavior Spec: Known-Entities Registry (`add_to_known_entities` and friends)

This spec is derived from the test suite that pins the behavior of the
known-entities registry wire-up between init and the miner. The registry is a
JSON file (default location `~/.mempalace/known_entities.json`) whose path is a
module-level setting that can be overridden, and whose loads are served through
an in-memory cache keyed by file mtime
(`tests/test_known_entities_registry.py:L1-L24`).

## Public Surface

The behaviors under test belong to these operations
(`tests/test_known_entities_registry.py:L15-L16`):

- `add_to_known_entities(entities: dict, wing: str | None = None) -> str` — merge confirmed entities into the registry; returns the registry file path.
- `_load_known_entities() -> set` — return the flat set of all known entity names across categories.
- `_load_known_entities_raw() -> dict` — return the raw parsed registry contents.
- `_extract_entities_for_metadata(text: str) -> str` — scan text for known entity names and return them as a `;`-joined string.
- `get_topics_by_wing() -> dict` — return the per-wing topics map.

## Registry Path and Cache

The registry file path is a module-level configurable value
(`_ENTITY_REGISTRY_PATH`) and the loader is backed by a cache record carrying
`mtime`, `names` (a set of known names), and `raw` (the parsed document)
(`tests/test_known_entities_registry.py:L18-L24`). A write through
`add_to_known_entities` must invalidate the cache so that a subsequent load in
the same process observes the new contents without a process restart
(`tests/test_known_entities_registry.py:L151-L163`).

## Fresh-File Creation

When the registry file does not exist, `add_to_known_entities` creates it and
persists the merged entities. Given `{"people": ["Alice", "Bob"], "projects":
["foo"]}`, the resulting file contains a `people` list with both names and a
`projects` list equal to `["foo"]`
(`tests/test_known_entities_registry.py:L30-L36`).

`add_to_known_entities` returns the registry file path as a string
(`tests/test_known_entities_registry.py:L39-L41`).

An empty input (`{}`) is a no-op merge: the file may or may not be written, and
if written it contains either an empty document or only empty category values
(`tests/test_known_entities_registry.py:L44-L50`).

Empty-string and null names are skipped. Given `{"people": ["Alice", "", None]}`
only `Alice` is stored (`tests/test_known_entities_registry.py:L53-L56`).

## Union and Deduplication (list-format categories)

Merging into an existing list category is a union that preserves the existing
order and appends only new names. Starting from `{"people": ["Alice", "Bob"]}`
and adding `["Bob", "Carol"]` yields `["Alice", "Bob", "Carol"]` — `Bob` is not
duplicated and `Carol` is appended last
(`tests/test_known_entities_registry.py:L62-L67`).

Dedup is case-insensitive but preserves the first-seen casing variant. Starting
from `{"people": ["Alice"]}` and adding `["alice", "ALICE", "Bob"]` yields
`["Alice", "Bob"]`; lowercase/uppercase variants of an existing name do not
create new entries (`tests/test_known_entities_registry.py:L70-L75`).

Categories the caller did not mention are left untouched. Adding `{"people":
["Bob"]}` to `{"people": ["Alice"], "places": ["Paris", "Tokyo"]}` leaves
`places` exactly `["Paris", "Tokyo"]` and produces `people` equal to
`["Alice", "Bob"]` (`tests/test_known_entities_registry.py:L78-L84`).

New categories absent from the existing registry are added. Adding
`{"projects": ["foo", "bar"]}` to `{"people": ["Alice"]}` leaves `people`
unchanged and adds `projects` equal to `["foo", "bar"]`
(`tests/test_known_entities_registry.py:L87-L92`).

Duplicates within a single input call are collapsed case-insensitively. Adding
`{"people": ["Alice", "alice", "Alice"]}` yields `["Alice"]`
(`tests/test_known_entities_registry.py:L95-L98`).

## Dict-Format Categories

A category may already exist as a `{name: code}` map rather than a list. In that
case new names are added as keys without overwriting existing codes. Starting
from `{"people": {"Alice": "ALC", "Bob": "BOB"}}` and adding `["Alice",
"Carol"]`: `Alice` keeps code `"ALC"`, `Bob` is untouched at `"BOB"`, and
`Carol` is added with a null code
(`tests/test_known_entities_registry.py:L104-L114`).

Dict-format dedup is also case-insensitive, and non-string new names are
stringified. Starting from `{"people": {"Alice": "ALC"}}` and adding `["alice",
123]` yields `{"Alice": "ALC", "123": None}` — `alice` matches existing `Alice`
(no new key), and integer `123` becomes string key `"123"` with null value
(`tests/test_known_entities_registry.py:L117-L121`).

## Error Tolerance

A malformed (non-JSON) existing registry is treated as empty and the merge
starts fresh. Given file contents `"{ not valid json"` and adding `{"people":
["Alice"]}`, the result is exactly `{"people": ["Alice"]}`
(`tests/test_known_entities_registry.py:L127-L131`).

A structurally unexpected existing registry (a JSON array rather than an object)
is likewise discarded and the merge starts fresh. Given `["unexpected",
"array"]` and adding `{"people": ["Alice"]}`, the result is `{"people":
["Alice"]}` (`tests/test_known_entities_registry.py:L134-L138`).

An input category whose value is not a list is ignored for merging. Adding
`{"people": ["Alice"], "weird": "not a list"}` produces a registry where
`people` equals `["Alice"]` and the `weird` value is either absent or left as
the raw non-list value (`tests/test_known_entities_registry.py:L141-L145`).

## Cache and Raw Views After Write

After a write, `_load_known_entities` returns the flat set of all stored names
across categories: after adding `{"people": ["Alice", "Bob"], "projects":
["foo"]}`, the set contains `Alice`, `Bob`, and `foo`. Prior to any write the
flat set is empty (`tests/test_known_entities_registry.py:L151-L163`).

`_load_known_entities_raw` reflects the written document: after adding
`{"people": ["Alice"]}` the raw view's `people` key equals `["Alice"]`
(`tests/test_known_entities_registry.py:L166-L169`).

## Unicode On-Disk Contract

Non-ASCII names are written literally (not escaped) so the file stays
human-readable in UTF-8. After adding `{"people": ["Gergő Móricz", "Arturo
Domínguez"]}`, the raw UTF-8 file text contains the literal substrings `Gergő`
and `Móricz`, and the names round-trip through JSON parsing
(`tests/test_known_entities_registry.py:L175-L183`).

## End-to-End Recall Contract

Names registered via `add_to_known_entities` must be recognized by the miner's
entity-extraction metadata pass. After registering people `["Julia Grib",
"Kevin Heifner"]` and projects `["hyperion-history", "mempalace"]`, calling
`_extract_entities_for_metadata` on text mentioning all four returns a string
whose `;`-separated tokens include each of those four names
(`tests/test_known_entities_registry.py:L189-L208`). The output of
`_extract_entities_for_metadata` is a `;`-joined string of recognized entity
names (`tests/test_known_entities_registry.py:L203-L208`).

## `topics_by_wing` — Cross-Wing Tunnel Signal

When `add_to_known_entities` is called with a `wing` argument and a `topics`
category, the topics are stored two ways: appended to a flat `topics` list
(existing-style aggregate) AND recorded under `topics_by_wing[wing]` as the
list of that wing's topics. Adding `{"people": ["Alice"], "topics": ["Angular",
"OpenAPI"]}` with `wing="wing_alpha"` makes `topics` contain `Angular` and sets
`topics_by_wing["wing_alpha"]` to `["Angular", "OpenAPI"]`
(`tests/test_known_entities_registry.py:L214-L223`).

Re-running for the same wing replaces that wing's list with the latest call
rather than accumulating. Adding `["Angular", "OpenAPI"]` then `["OpenAPI",
"Postgres"]` for `wing_alpha` leaves `topics_by_wing["wing_alpha"]` equal to
`["OpenAPI", "Postgres"]` (`tests/test_known_entities_registry.py:L226-L232`).

Multiple wings coexist independently. Adding `["foo"]` for `wing_a` and
`["foo", "bar"]` for `wing_b` yields `topics_by_wing` equal to `{"wing_a":
["foo"], "wing_b": ["foo", "bar"]}`
(`tests/test_known_entities_registry.py:L235-L239`).

When no `wing` is provided, no `topics_by_wing` entry is created, but the flat
`topics` list is still saved. Adding `{"topics": ["foo"]}` with no wing produces
a document with no `topics_by_wing` key and `topics` equal to `["foo"]`
(`tests/test_known_entities_registry.py:L242-L247`).

Per-wing topics are deduplicated case-insensitively, preserving first-observed
casing. Adding `["OpenAPI", "openapi", "OPENAPI"]` for `wing_a` yields
`topics_by_wing["wing_a"]` equal to `["OpenAPI"]`
(`tests/test_known_entities_registry.py:L250-L254`).

`get_topics_by_wing` reads the registry and returns the per-wing topics map.
After registering topics for `wing_a` and `wing_b` it returns `{"wing_a":
["foo"], "wing_b": ["foo", "bar"]}`
(`tests/test_known_entities_registry.py:L257-L261`). When the registry has no
per-wing topics, `get_topics_by_wing` returns an empty map
(`tests/test_known_entities_registry.py:L264-L266`).

Wing names recorded in `topics_by_wing` must NOT leak into the flat known-names
set returned by `_load_known_entities`; only the topic strings themselves are
recognized. After adding `{"topics": ["Angular"]}` for
`wing="wing_super_secret_project"`, the flat known set contains `Angular` but
does not contain `wing_super_secret_project`
(`tests/test_known_entities_registry.py:L269-L276`).
