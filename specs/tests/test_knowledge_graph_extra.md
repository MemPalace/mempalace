# Behavior Spec: Knowledge Graph — Entity-Fact Seeding & Temporal Relationship Queries

This file is a test suite that pins down the externally observable behavior of the
`KnowledgeGraph` component, specifically its `seed_from_entity_facts` ingestion path
and the time-aware `query_relationship` path
(tests/test_knowledge_graph_extra.py:L1-L5). The behaviors below are the contracts the
implementation must satisfy; they constitute an implementable specification of that
component regardless of language.

## Construction & Storage

A `KnowledgeGraph` is constructed from a single `db_path` string identifying its
backing database file. In tests it is given a path inside a temporary directory
(`<tmp>/kg.db`), so the store is a file-backed, per-instance, isolated database
(tests/test_knowledge_graph_extra.py:L8-L10).

## Stats Contract

`stats()` returns a mapping that includes an integer-valued key `"entities"` reporting
the count of distinct entities currently stored
(tests/test_knowledge_graph_extra.py:L25-L26, L94-L95).

## Query Result Shape

Both `query_entity(...)` and `query_relationship(...)` return a collection of result
records. Each record is a mapping exposing at least the keys `"predicate"` and
`"object"` (tests/test_knowledge_graph_extra.py:L27-L30, L86-L88, L103-L104).
`query_entity` accepts a `direction` argument; `direction="outgoing"` returns the
relationships in which the named entity is the subject
(tests/test_knowledge_graph_extra.py:L27-L28, L43, L58, L72, L85).

## `seed_from_entity_facts(facts)`

Input is a mapping keyed by an entity short-key (e.g. `"alice"`, `"max"`). Each value
is a fact mapping. Recognized fact fields observed: `full_name`, `type`, `gender`,
`partner`, `relationship`, `birthday`, `parent`, `sibling`, `owner`, `interests`
(tests/test_knowledge_graph_extra.py:L14-L95). The method has no asserted return value;
its effects are observed via `stats()` and subsequent queries.

### Entity creation

Every fact entry creates at least one entity. A fact carrying only `full_name` and no
relationship fields still results in at least one stored entity, raising the
`"entities"` count to >= 1 (tests/test_knowledge_graph_extra.py:L90-L95). More generally
any non-empty seed yields `entities >= 1`
(tests/test_knowledge_graph_extra.py:L24-L26).

### Relationship emission rules

Entities are queried by their `full_name` value (e.g. `"Alice Smith"`, `"Max"`,
`"Emma"`, `"Rex"`), confirming the entity is keyed/named by `full_name`
(tests/test_knowledge_graph_extra.py:L17, L27, L36, L43, L54, L58, L66, L72).

- A person fact with `partner` set and `relationship: "husband"` emits, on the person's
  outgoing relationships, BOTH a `married_to` predicate AND an `is_partner_of`
  predicate (tests/test_knowledge_graph_extra.py:L14-L30).
- A person fact with `parent` set and `relationship: "daughter"` emits, on the child's
  outgoing relationships, BOTH a `child_of` predicate AND an `is_child_of` predicate
  (tests/test_knowledge_graph_extra.py:L32-L46).
- A person fact with `sibling` set and `relationship: "brother"` emits an
  `is_sibling_of` predicate on the entity's outgoing relationships
  (tests/test_knowledge_graph_extra.py:L48-L60).
- An animal fact (`type: "animal"`) with `owner` set and `relationship: "dog"` emits an
  `is_pet_of` predicate on the animal's outgoing relationships
  (tests/test_knowledge_graph_extra.py:L62-L74).

Relationship predicates are derived from the structural fact field (partner/parent/
sibling/owner) and possibly the `relationship` label; the kinship `relationship` value
(e.g. "husband", "daughter", "brother") does not change the canonical predicate name,
since "brother" still yields `is_sibling_of` and "daughter" still yields
`is_child_of`/`child_of` (tests/test_knowledge_graph_extra.py:L39-L46, L53-L60).

### Interests emission and normalization

A fact with an `interests` list emits one outgoing relationship per interest using the
predicate `loves`, with the interest as the `object`. Each interest string is
normalized to title/capitalized casing in the stored object: input `["swimming",
"chess"]` produces objects `"Swimming"` and `"Chess"`
(tests/test_knowledge_graph_extra.py:L76-L88).

## `add_triple(subject, predicate, object, valid_from=..., valid_to=...)`

Adds a single relationship triple with an optional validity interval. `valid_from` and
`valid_to` are date strings in `YYYY-MM-DD` form. `valid_to` may be omitted, denoting an
open-ended (still-valid) interval (tests/test_knowledge_graph_extra.py:L100-L101).

## `query_relationship(predicate, as_of=...)` — temporal filtering

Queries all triples for a given `predicate`. The `as_of` argument is a date string that
restricts results to triples whose validity interval contains that date. A triple with
`valid_from="2020-01-01"` and `valid_to="2024-12-31"` is included for `as_of=
"2023-06-01"`, while a triple with `valid_from="2025-01-01"` (interval starting after the
as-of date) is excluded. Thus only the object `"Acme"` appears and `"NewCo"` does not
(tests/test_knowledge_graph_extra.py:L99-L105). The interval test is inclusive of
`valid_from` and `valid_to` boundaries and excludes intervals that begin after `as_of`
(tests/test_knowledge_graph_extra.py:L100-L105).
