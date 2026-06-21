# Behavior Spec: Entity Registry

Derived from the test suite `tests/test_entity_registry.py`, which exercises the
public surface of the `EntityRegistry` component and two associated module-level
data constants. Each section describes externally observable behavior that any
implementation must satisfy.

## Module-level data: `COMMON_ENGLISH_WORDS`

A set/collection of common English words used for ambiguity detection. It MUST
contain at least the entries `"ever"`, `"grace"`, `"will"`, `"may"`, and
`"monday"` (tests/test_entity_registry.py:L25-L30). Every entry in the collection
MUST be lowercase — i.e. each word equals its own lowercased form
(tests/test_entity_registry.py:L33-L35).

## Module-level data: `PERSON_CONTEXT_PATTERNS`

A non-empty collection of patterns used to recognize person-referring context.
It MUST contain at least one element (tests/test_entity_registry.py:L41-L42).

## Construction and persistence

### `EntityRegistry.load(config_dir)`

`load` accepts a configuration directory and returns a registry instance. When
the directory contains no prior registry file, the loaded registry MUST have an
empty `people` mapping (`{}`), an empty `projects` list (`[]`), a `mode` of
`"personal"`, and an empty `ambiguous_flags` list (`[]`)
(tests/test_entity_registry.py:L48-L53).

### On-disk file

The persisted state is stored in a file named `entity_registry.json` inside the
config directory. After calling `save()`, that file MUST exist
(tests/test_entity_registry.py:L70-L73).

### Save/load roundtrip

State set via `seed` and then `save`d MUST be recoverable by a fresh `load` from
the same directory: the `mode`, registered people (keyed by name), and projects
all survive the roundtrip (tests/test_entity_registry.py:L56-L67). `seed`
implicitly persists state, since a subsequent independent `load` observes the
seeded values without an explicit `save` call in that test
(tests/test_entity_registry.py:L58-L67).

### Atomic-write contract

`save()` MUST write atomically via a temporary sidecar file plus a rename, with
no leftover temp file on success. After a successful `save`, no file matching
`entity_registry.json.tmp*` may remain in the config directory
(tests/test_entity_registry.py:L76-L81).

If the rename step fails (simulated by the OS rename/replace operation raising an
error), the failure MUST propagate as an error to the caller, the previous
on-disk `entity_registry.json` content MUST remain byte-for-byte unchanged, and
no `entity_registry.json.tmp*` sidecar may be left behind
(tests/test_entity_registry.py:L84-L122).

If the durability/flush step fails before the rename (simulated by the fsync
operation raising an error), the same guarantees hold: the error propagates, the
prior file content is preserved byte-for-byte (the rename never occurred), and no
`entity_registry.json.tmp*` sidecar litter remains
(tests/test_entity_registry.py:L125-L163).

## `seed(mode, people, projects, aliases=...)`

`seed` configures the registry in one call.

People are supplied as records with at least `name`, `relationship`, and
`context` fields. Each non-empty person becomes an entry in `people` keyed by
name, preserving the supplied `relationship`, and additionally tagged with
`source = "onboarding"` and `confidence = 1.0`
(tests/test_entity_registry.py:L169-L183). Person records with an empty `name`
MUST be skipped and not registered (tests/test_entity_registry.py:L226-L233).

Projects are stored as a list in the order supplied, replacing prior projects
(tests/test_entity_registry.py:L186-L189).

`mode` is stored verbatim; arbitrary mode strings such as `"work"`, `"combo"`, or
`"personal"` are accepted (tests/test_entity_registry.py:L188-L195).

Ambiguity flagging: any seeded person whose name (lowercased) is a common English
word MUST be added to `ambiguous_flags` in lowercase form; names that are not
common words MUST NOT appear. For example seeding `"Grace"` adds `"grace"` to
`ambiguous_flags`, while `"Riley"` does not produce `"riley"`
(tests/test_entity_registry.py:L198-L210).

Aliases: an optional `aliases` mapping of alias-name to canonical-name registers
each alias as an additional entry in `people`. The alias entry carries a
`canonical` field pointing at the canonical name. Seeding person `"Maxwell"` with
`aliases={"Max": "Maxwell"}` yields both `"Maxwell"` and `"Max"` in `people`,
where `people["Max"]["canonical"] == "Maxwell"`
(tests/test_entity_registry.py:L213-L223).

## `lookup(word, context=...)`

`lookup` resolves a token to an entity classification and returns a record with
at least `type`, `confidence`, and (for matches) `name`.

A known person resolves to `type = "person"`, `confidence = 1.0`, and the
canonical `name` (tests/test_entity_registry.py:L239-L249). A known project
resolves to `type = "project"`, `confidence = 1.0`
(tests/test_entity_registry.py:L252-L257). An unrecognized token resolves to
`type = "unknown"`, `confidence = 0.0`
(tests/test_entity_registry.py:L260-L265).

Lookup is case-insensitive: looking up `"riley"` resolves the registered person
`"Riley"` as `type = "person"` (tests/test_entity_registry.py:L268-L276).

Aliases resolve: looking up an alias (`"Max"`) returns `type = "person"`
(tests/test_entity_registry.py:L279-L288).

Context-sensitive disambiguation for ambiguous names: when a name is also a
common English word, the optional `context` string decides the classification. A
person-like context resolves to `type = "person"` (e.g. `lookup("Grace",
context="I went with Grace today")`)
(tests/test_entity_registry.py:L294-L302). A non-person/conceptual usage resolves
to `type = "concept"` (e.g. `lookup("Ever", context="have you ever tried this")`)
(tests/test_entity_registry.py:L305-L313).

## `research(word, auto_confirm=..., allow_network=...)`

`research` attempts to infer an entity type for an unknown word and returns a
record with at least `inferred_type`, `confidence`, and `word`.

Local-only by default: without `allow_network=True`, `research` MUST NOT perform
any network/Wikipedia lookup. For an unknown word it returns
`inferred_type = "unknown"`, `confidence = 0.0`, `word` equal to the queried
word, and a `note` containing the phrase `"network lookup disabled"`
(tests/test_entity_registry.py:L319-L333).

With `allow_network=True`, `research` performs the external Wikipedia lookup and
returns its inferred type; a person result yields `inferred_type = "person"`
(tests/test_entity_registry.py:L336-L346).

Caching: a result obtained via `allow_network=True` MUST be cached so that a
later `research` call for the same word returns the cached result
(`inferred_type = "person"`) without performing any further network lookup
(tests/test_entity_registry.py:L349-L367). Conversely, a local-only result for an
uncached word MUST NOT be persisted into the cache; after a local-only
`research("Xander")` the word `"Xander"` is absent from the `wiki_cache` mapping
(tests/test_entity_registry.py:L370-L376).

A negative/404 Wikipedia result (e.g. inferred type `"unknown"` with low
confidence and no summary/title) MUST be surfaced as `inferred_type = "unknown"`
with `confidence < 0.5`, never coerced into `"person"`
(tests/test_entity_registry.py:L394-L410).

## `confirm_research(word, entity_type=..., relationship=...)`

After a `research` call that found a person (with `auto_confirm=False`),
`confirm_research` promotes the word into the registry. The confirmed entity is
added to `people` keyed by its name, with `source = "wiki"`
(tests/test_entity_registry.py:L379-L391).

## `extract_people_from_query(text)`

Returns the set/collection of known people whose names appear in the given query
text. Only registered people that occur in the text are returned: for
`"What did Riley say about the weather?"` with `Riley` and `Devon` registered,
`"Riley"` is included and `"Devon"` is not
(tests/test_entity_registry.py:L416-L428).

## `extract_unknown_candidates(text)`

Returns candidate names found in the text that are not already known entities.
Capitalized/name-like tokens that are not registered are returned (e.g.
`"Saoirse"` from `"Saoirse went to the store"`)
(tests/test_entity_registry.py:L434-L438). Tokens that match a registered person
MUST be excluded; with `Riley` registered, `"Riley went to the store"` yields no
`"Riley"` candidate (tests/test_entity_registry.py:L441-L449).

## `summary()`

Returns a human-readable string summary of registry state that includes the
current `mode`, registered people names, and project names. For a registry in
`"personal"` mode with person `"Riley"` and project `"MemPalace"`, the summary
contains the substrings `"personal"`, `"Riley"`, and `"MemPalace"`
(tests/test_entity_registry.py:L455-L465).

## Internal state observable in tests

The registry exposes an internal data mapping (`_data`) holding at least a
`wiki_cache` sub-mapping used by the caching contract above
(tests/test_entity_registry.py:L376).
