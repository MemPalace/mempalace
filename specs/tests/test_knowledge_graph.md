# Behavior Spec: Temporal Knowledge Graph

Derived from the test suite at `tests/test_knowledge_graph.py`, which exercises a
`KnowledgeGraph` component backed by a single on-disk SQLite database file. The
component models entities and temporal subject-predicate-object triples
(relationships with optional validity intervals). The spec below describes the
externally observable contract that any implementation must satisfy to pass these
tests (tests/test_knowledge_graph.py:L1-L11).

## Construction & Storage

The component is constructed from a single SQLite database file path; it is the
sole on-disk side effect (tests/test_knowledge_graph.py:L308-L308, L318-L318). The
fixtures construct it with a `db_path` pointing at a `.sqlite3` file inside a temp
directory (tests/conftest.py:L219-L223).

The SQLite connection operates in Write-Ahead Logging journal mode: querying
`PRAGMA journal_mode` on a connection obtained from `_conn()` returns the string
`"wal"` (tests/test_knowledge_graph.py:L156-L161).

The component lazily holds an internal connection handle exposed for testing via
`_conn()` and an internal `_connection` attribute (tests/test_knowledge_graph.py:L158-L159, L308-L313).

## Entity Operations

`add_entity(name, entity_type=...)` creates or updates an entity and returns its
normalized string ID (tests/test_knowledge_graph.py:L14-L16). The returned ID is
derived from the name by: lowercasing (`"Alice"` -> `"alice"`)
(tests/test_knowledge_graph.py:L15-L16) and replacing internal whitespace with
underscores while preserving punctuation such as the period (`"Dr. Chen"` ->
`"dr._chen"`) (tests/test_knowledge_graph.py:L18-L20).

Adding the same name twice is an upsert (insert-or-replace): it does not raise, and
the entity count remains 1 even when `entity_type` differs between the two calls
(tests/test_knowledge_graph.py:L22-L27).

## Triple Operations

`add_triple(subject, predicate, object, valid_from=..., valid_to=...)` creates a
relationship and returns a triple ID string (tests/test_knowledge_graph.py:L31-L33).
The triple ID has the form `t_<subject>_<predicate>_<object>_<suffix>`, where the
subject/predicate/object components are the normalized lowercase tokens and a
trailing disambiguating suffix follows (tests/test_knowledge_graph.py:L32-L33,
L38-L39, L74, L79, L82). Object names with internal spaces are normalized in the ID
the same way as entities (e.g. `"Acme Corp"` contributes `acme_corp`)
(tests/test_knowledge_graph.py:L33, conftest.py:L238).

Adding a triple auto-creates any referenced entities that do not yet exist: after
`add_triple("Alice","knows","Bob")` on an empty graph, the entity count is 2
(tests/test_knowledge_graph.py:L31-L35).

`valid_from` and `valid_to` are optional temporal bounds (tests/test_knowledge_graph.py:L37-L39).

### Duplicate / re-add behavior

Adding an identical, still-current triple (same subject, predicate, object) a second
time returns the SAME existing triple ID rather than creating a duplicate
(tests/test_knowledge_graph.py:L41-L44).

Once a triple has been invalidated (closed via `invalidate(...)`), adding the same
subject/predicate/object again creates a NEW triple with a different ID, because the
prior one was closed (tests/test_knowledge_graph.py:L46-L50).

### Interval validation at write time

If BOTH `valid_from` and `valid_to` are supplied and `valid_to` is strictly before
`valid_from`, `add_triple` raises an error whose message matches `before valid_from`
(tests/test_knowledge_graph.py:L52-L63). This guard exists to prevent intervals that
would be invisible to every temporal query (tests/test_knowledge_graph.py:L53-L55).

Equal `valid_from` and `valid_to` (same-day / point-in-time facts) are accepted and
return a valid triple ID (tests/test_knowledge_graph.py:L65-L74).

The inversion guard fires ONLY when both bounds are set. A triple with only
`valid_from` set, or only `valid_to` set, is always accepted regardless of the other
bound (tests/test_knowledge_graph.py:L76-L82).

## Temporal Date / DateTime Format Contract

Date bounds may be date-only (`YYYY-MM-DD`) or UTC datetimes in `Z`-suffixed
ISO form (`YYYY-MM-DDTHH:MM:SSZ`) (tests/test_knowledge_graph.py:L180-L188,
L248-L254).

A date-only `valid_from`/`valid_to` is interpreted against datetime queries such
that a legacy date-only fact dated `2026-05-06` matches an `as_of` of
`2026-05-06T15:00:00Z` (anywhere within that calendar day)
(tests/test_knowledge_graph.py:L179-L191). The same fact does NOT match an `as_of`
earlier than the start of that day (`2026-05-05T23:59:59Z`)
(tests/test_knowledge_graph.py:L193-L204) nor at/after the start of the next day
(`2026-05-07T00:00:00Z`) (tests/test_knowledge_graph.py:L206-L217). That is, a
date-only `valid_to` is treated as inclusive through the end of that day for
interval-containment checks: with `valid_to="2026-05-06"`, an `as_of` of
`2026-05-06T20:00:00Z` still matches (tests/test_knowledge_graph.py:L247-L259).

Rejected datetime forms (each raises an error at the knowledge-graph layer):
- timezone-offset datetimes such as `2026-05-06T20:30:00-05:00`
  (tests/test_knowledge_graph.py:L219-L226)
- naive datetimes with no zone such as `2026-05-07T01:23:00`
  (tests/test_knowledge_graph.py:L228-L235)
- space-separated datetimes such as `2026-05-06 20:00:00` (here supplied as
  `valid_to` while `valid_from` is well-formed) (tests/test_knowledge_graph.py:L237-L245)

When mixing a datetime `valid_from` with a date-only `valid_to`, the inversion guard
still applies using the end-of-day interpretation of the date-only bound: a
`valid_from` of `2026-05-07T01:00:00Z` against `valid_to="2026-05-06"` is rejected,
and the error message includes both `valid_to='2026-05-06'` and
`valid_from='2026-05-07T01:00:00Z'` (tests/test_knowledge_graph.py:L261-L272).

## Querying Entities

`query_entity(name, direction=..., as_of=...)` returns a list of relationship
records (tests/test_knowledge_graph.py:L86-L112). Each record is a mapping that
includes at least the keys `subject`, `predicate`, `object`, `direction`,
`valid_to`, and `current` (tests/test_knowledge_graph.py:L88, L94, L98, L104,
L125-L126).

`direction` controls which relationships are returned relative to the named entity:
- `"outgoing"` returns triples where the entity is the subject; e.g. for `Alice` the
  predicates `parent_of` and `works_at` appear (tests/test_knowledge_graph.py:L86-L90).
- `"incoming"` returns triples where the entity is the object; e.g. for `Max`, a
  record with subject `Alice` and predicate `parent_of` appears
  (tests/test_knowledge_graph.py:L92-L94).
- `"both"` returns both; the set of record `direction` values includes both
  `"outgoing"` and `"incoming"` (tests/test_knowledge_graph.py:L96-L100). Each record
  carries its own `direction` field (tests/test_knowledge_graph.py:L98-L100).

`as_of` filters results to facts valid at the given point in time. With the seeded
graph (Alice at `Acme Corp` valid 2020-01-01..2024-12-31, and Alice at `NewCo` valid
from 2025-01-01) (tests/conftest.py:L238-L239):
- `as_of="2023-06-01"` returns `Acme Corp` as a `works_at` object but not `NewCo`
  (tests/test_knowledge_graph.py:L102-L106).
- `as_of="2025-06-01"` returns `NewCo` but not `Acme Corp`
  (tests/test_knowledge_graph.py:L108-L112).

## Querying Relationships

`query_relationship(predicate, as_of=...)` returns all triples with the given
predicate (tests/test_knowledge_graph.py:L114-L116). In the seeded graph the
predicate `does` yields 2 results (swimming and chess)
(tests/test_knowledge_graph.py:L114-L116, conftest.py:L236-L237).

`query_relationship` honors the same temporal date/datetime comparison rules: a
date-only same-day fact (`2026-05-06`) matches `as_of="2026-05-06T15:00:00Z"`, and
the returned record exposes `subject` and `object`
(tests/test_knowledge_graph.py:L274-L287).

## Invalidation

`invalidate(subject, predicate, object, ended=...)` closes a currently-valid triple
by setting its `valid_to` to the `ended` date (tests/test_knowledge_graph.py:L120-L126).
After invalidating `Max does chess` with `ended="2026-01-01"`, querying `Max`
outgoing returns exactly one `chess` record whose `valid_to` equals `"2026-01-01"`
and whose `current` field is `False` (tests/test_knowledge_graph.py:L120-L126).

`invalidate` rejects timezone-offset datetime `ended` values such as
`2026-05-06T20:30:00-05:00` by raising an error
(tests/test_knowledge_graph.py:L289-L303).

## Timeline

`timeline(entity=None)` returns a chronologically meaningful list of triple records
(tests/test_knowledge_graph.py:L129-L153). Each record includes `subject` and
`object` fields (tests/test_knowledge_graph.py:L136). The seeded graph yields at
least 4 records for the global timeline (tests/test_knowledge_graph.py:L130-L132).

When called with an entity name, results are restricted to triples touching that
entity (the entity appears in the union of all records' `subject` and `object`
values) (tests/test_knowledge_graph.py:L134-L136).

Both the global timeline and the entity-filtered timeline cap the result count at
100 records: after inserting 105 qualifying triples, each returns exactly 100
(tests/test_knowledge_graph.py:L139-L153).

## Stats

`stats()` returns a mapping with at least the keys `entities`, `triples`,
`current_facts`, and `expired_facts` (tests/test_knowledge_graph.py:L165-L174). On an
empty graph both `entities` and `triples` are 0 (tests/test_knowledge_graph.py:L165-L168).

For the seeded graph (4 explicit entities plus auto-created objects, 5 triples, one
of which — `Alice works_at Acme Corp` — has a past `valid_to`): `entities` is at
least 4, `triples` equals 5, `current_facts` equals 4, and `expired_facts` equals 1
(tests/test_knowledge_graph.py:L170-L174, conftest.py:L230-L240). A fact is counted
as expired when its `valid_to` lies in the past (tests/test_knowledge_graph.py:L174).

## Connection Lifecycle

`close()` closes the underlying SQLite connection and resets the internal
`_connection` handle to `None`; after `close()`, any previously obtained connection
object is unusable (raises a database programming error on use)
(tests/test_knowledge_graph.py:L306-L315).

The component supports context-manager usage: entering yields the graph for use
(entities can be added inside the block) and exiting automatically closes the
connection, leaving `_connection` as `None` and the prior connection object unusable
(tests/test_knowledge_graph.py:L317-L324).
