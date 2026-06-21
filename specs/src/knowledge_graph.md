# Behavior Specification — `knowledge_graph.py`

Temporal entity-relationship graph for MemPalace, backed by a local SQLite database. Stores entity nodes and typed relationship edges (triples) with temporal validity windows (`valid_from` → `valid_to`) and provenance links back to verbatim memory (mempalace/knowledge_graph.py:L1-L36).

## Storage Location & Filesystem Side Effects

- Default database path is `~/.mempalace/knowledge_graph.sqlite3` (tilde-expanded to the user home directory) (mempalace/knowledge_graph.py:L49-L49).
- A caller may override the path via the `db_path` constructor argument; falsy/`None` falls back to the default (mempalace/knowledge_graph.py:L130-L131).
- On construction the parent directory of the database is created recursively (no error if it already exists) (mempalace/knowledge_graph.py:L132-L133).
- The parent directory's permissions are best-effort set to owner-only `0o700`; failure to chmod is silently ignored (mempalace/knowledge_graph.py:L134-L137).
- The SQLite database runs in WAL journal mode (mempalace/knowledge_graph.py:L144-L145,L199).

## On-Disk Schema (Observable Contract)

Two tables are created if absent (mempalace/knowledge_graph.py:L144-L176):

`entities` table (mempalace/knowledge_graph.py:L147-L153):
- `id` TEXT primary key
- `name` TEXT (required)
- `type` TEXT defaulting to `'unknown'`
- `properties` TEXT defaulting to `'{}'` (a JSON object string)
- `created_at` TEXT defaulting to the SQL current timestamp

`triples` table (mempalace/knowledge_graph.py:L155-L170):
- `id` TEXT primary key
- `subject` TEXT (required, references `entities.id`)
- `predicate` TEXT (required)
- `object` TEXT (required, references `entities.id`)
- `valid_from` TEXT (nullable)
- `valid_to` TEXT (nullable)
- `confidence` REAL defaulting to `1.0`
- `source_closet` TEXT (nullable)
- `source_file` TEXT (nullable)
- `source_drawer_id` TEXT (nullable; RFC 002 §5.5 provenance)
- `adapter_name` TEXT (nullable; RFC 002 §5.5 provenance)
- `extracted_at` TEXT defaulting to the SQL current timestamp

Indexes are created on `subject`, `object`, `predicate`, and the pair `(valid_from, valid_to)` (mempalace/knowledge_graph.py:L172-L175).

### Schema Migration

On every initialization, existing `triples` tables lacking `source_drawer_id` or `adapter_name` get those columns added (nullable TEXT). Fresh installs already have them, so this is a no-op there (mempalace/knowledge_graph.py:L180-L194).

## Identifier Derivation (Observable Contract)

- Entity IDs are derived from the display name by lowercasing, replacing spaces with underscores, and removing apostrophes (`'`) (mempalace/knowledge_graph.py:L219-L220).
- Predicates are normalized by lowercasing and replacing spaces with underscores (mempalace/knowledge_graph.py:L282,L334,L432).
- Triple IDs have the form `t_{subject_id}_{predicate}_{object_id}_{hash12}` where `hash12` is the first 12 hex chars of SHA-256 over `"{valid_from}|{recorded_at}"`, with `recorded_at` being the insert-time timestamp (mempalace/knowledge_graph.py:L305-L307; mempalace/ids.py:L111-L128).

## Temporal Value Semantics (Observable Contract)

All temporal inputs (`valid_from`, `valid_to`, `as_of`, `ended`) are validated/normalized before use. Accepted forms: `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SSZ`, or `YYYY-MM-DDTHH:MM:SS+00:00` (the `+00:00` form is rewritten to `...Z`). `None` and empty string pass through unchanged; non-string or otherwise malformed values raise an error (mempalace/knowledge_graph.py:L264-L265,L335,L371,L431; mempalace/config.py:L138-L172).

A "date-only" temporal value is exactly 10 chars with dashes at positions 5 and 8 (mempalace/knowledge_graph.py:L52-L53). For comparison:
- A date-only `valid_from`/`as_of` is treated as the start of that day, `T00:00:00Z` (mempalace/knowledge_graph.py:L56-L65,L84-L92).
- A date-only `valid_to` is treated as the end of that day, `T23:59:59Z`, so legacy date-only end-dates remain inclusive for the whole day (mempalace/knowledge_graph.py:L68-L81,L95-L103).
- An `as_of` temporal filter selects triples where `valid_from IS NULL OR normalized(valid_from) <= as_of` AND `valid_to IS NULL OR normalized(valid_to) >= as_of` (mempalace/knowledge_graph.py:L106-L126).

## Public Surface

### `KnowledgeGraph(db_path=None)`
Constructs/opens the graph, ensuring directory, permissions, schema, and migration (mempalace/knowledge_graph.py:L130-L140). Usable as a context manager; exiting the context closes the connection and does not suppress exceptions (mempalace/knowledge_graph.py:L210-L217). A single shared SQLite connection is lazily created and reused; all mutating/reading operations serialize behind an internal lock (mempalace/knowledge_graph.py:L138-L139,L196-L201).

### `close()`
Closes and clears the shared connection under the lock; safe to call when already closed (mempalace/knowledge_graph.py:L203-L208).

### `add_entity(name, entity_type="unknown", properties=None) -> entity_id`
Inserts or replaces an entity node keyed by the derived entity ID. `properties` (a dict, defaulting to empty) is stored as a JSON object string. Returns the entity ID (mempalace/knowledge_graph.py:L224-L235). Because it uses INSERT-OR-REPLACE, re-adding an existing name overwrites its `type` and `properties` (mempalace/knowledge_graph.py:L231-L234).

### `add_triple(subject, predicate, obj, valid_from=None, valid_to=None, confidence=1.0, source_closet=None, source_file=None, source_drawer_id=None, adapter_name=None) -> triple_id`
Adds a relationship `subject → predicate → object` (mempalace/knowledge_graph.py:L237-L262).
- Validates `valid_from` and `valid_to` as ISO temporals (mempalace/knowledge_graph.py:L264-L265).
- Raises an error if `valid_to` is temporally before `valid_from` (inverted interval), using normalized comparison keys (mempalace/knowledge_graph.py:L270-L278).
- Auto-creates subject and object entity rows if missing (INSERT-OR-IGNORE; does not overwrite existing entities) (mempalace/knowledge_graph.py:L284-L295).
- Deduplication: if an identical `(subject, predicate, object)` triple already exists with `valid_to IS NULL` (i.e., still current), no new row is inserted and the existing triple's ID is returned (mempalace/knowledge_graph.py:L297-L303).
- Otherwise inserts a new triple and returns its generated ID (mempalace/knowledge_graph.py:L305-L328).

### `invalidate(subject, predicate, obj, ended=None)`
Marks all currently-valid (`valid_to IS NULL`) triples matching `(subject, predicate, object)` as ended by setting their `valid_to`. `ended` defaults to today's date if omitted, and is ISO-validated (mempalace/knowledge_graph.py:L330-L335,L356-L360). Before updating, it raises an error if the `ended` instant is before any matched triple's `valid_from` (inverted interval) (mempalace/knowledge_graph.py:L340-L354).

### `query_entity(name, as_of=None, direction="outgoing") -> list[dict]`
Returns relationships for an entity (mempalace/knowledge_graph.py:L364-L427).
- `direction`: `"outgoing"` returns edges where the entity is the subject; `"incoming"` where it is the object; `"both"` returns both sets (outgoing first, then incoming) (mempalace/knowledge_graph.py:L383-L425).
- When `as_of` is provided, only temporally-valid facts at that instant are returned per the as-of filter (mempalace/knowledge_graph.py:L371-L378).
- Each result dict contains: `direction`, `subject`, `predicate`, `object`, `valid_from`, `valid_to`, `confidence`, `source_closet`, and `current` (true when `valid_to` is NULL). Subject/object names are the stored display names; for outgoing edges `subject` is the queried name, for incoming `object` is the queried name (mempalace/knowledge_graph.py:L390-L425).

### `query_relationship(predicate, as_of=None) -> list[dict]`
Returns all triples with the given (normalized) predicate, optionally filtered by `as_of`. Each result dict has `subject`, `predicate`, `object`, `valid_from`, `valid_to`, and `current` (mempalace/knowledge_graph.py:L429-L462).

### `timeline(entity_name=None) -> list[dict]`
Returns up to 100 facts ordered by `valid_from` ascending with NULL values last. If `entity_name` is given, only triples where it is subject or object are returned. Each result dict has `subject`, `predicate`, `object`, `valid_from`, `valid_to`, and `current` (mempalace/knowledge_graph.py:L464-L502).

### `stats() -> dict`
Returns `{entities, triples, current_facts, expired_facts, relationship_types}` where `current_facts` counts triples with `valid_to IS NULL`, `expired_facts` is total minus current, and `relationship_types` is the sorted distinct list of predicates (mempalace/knowledge_graph.py:L506-L527).

### `seed_from_entity_facts(entity_facts: dict)`
Bootstraps the graph from a mapping of known facts (e.g. from `fact_checker.py`). For each entry it adds an entity (using `full_name` or the capitalized key as name, `type` defaulting to `"person"`, plus `gender`/`birthday` properties) and derives relationship triples (mempalace/knowledge_graph.py:L531-L577):
- `parent` → `child_of` triple with `valid_from` = birthday (mempalace/knowledge_graph.py:L549-L553).
- `partner` → `married_to` triple (mempalace/knowledge_graph.py:L555-L557).
- `relationship == "daughter"` → `is_child_of`; `"husband"` → `is_partner_of`; `"brother"` → `is_sibling_of`; `"dog"` → `is_pet_of` plus re-adds the entity as type `animal` (mempalace/knowledge_graph.py:L559-L573).
- Each `interest` → `loves` triple with `valid_from="2025-01-01"`, object capitalized (mempalace/knowledge_graph.py:L576-L577).

## Invariants & Ordering Guarantees

- A triple's interval is never inverted: any insert/invalidate that would set `valid_to` earlier than `valid_from` raises an error rather than persisting (mempalace/knowledge_graph.py:L270-L278,L340-L354).
- A "current" fact is exactly one whose `valid_to` is NULL; invalidation is the only path to expiry (mempalace/knowledge_graph.py:L299-L303,L356-L360,L401,L459,L499,L511-L513).
- Duplicate current triples for the same `(subject, predicate, object)` are not created (mempalace/knowledge_graph.py:L297-L303).
- `timeline` orders by `valid_from` ascending with NULLs last and caps at 100 rows (mempalace/knowledge_graph.py:L477-L478,L488-L489).
- `query_entity(direction="both")` always returns outgoing edges before incoming edges (mempalace/knowledge_graph.py:L383-L425).

## Concurrency

The shared SQLite connection allows cross-thread use, and all read/write operations are guarded by a process-internal lock so concurrent calls serialize (mempalace/knowledge_graph.py:L196-L201,L228-L234,L285-L287,L337-L339,L380-L381,L449-L450,L466-L467,L507-L508).
