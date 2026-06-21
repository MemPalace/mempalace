# Behavior Spec — `tests/benchmarks/test_knowledge_graph_bench.py`

A benchmark suite measuring the SQLite-backed temporal knowledge graph
(`KnowledgeGraph`) for insertion throughput, query latency, temporal-filter
correctness, and concurrent access. All tests in the file are tagged with the
`benchmark` marker so they are selected/excluded as a group
(`tests/benchmarks/test_knowledge_graph_bench.py:L17`, `:L48`, `:L83`, `:L114`,
`:L160`, `:L263`). The module docstring states the suite covers triple
insertion throughput, query latency, temporal accuracy, and concurrent access
(`tests/benchmarks/test_knowledge_graph_bench.py:L1-L5`).

## External dependencies / contracts consumed

- A data generator `PalaceDataGenerator(seed=..., scale=...)` whose method
  `generate_kg_triples(n_entities=..., n_triples=...)` returns a pair
  `(entities, triples)`. `entities` is an iterable of `(name, etype)` pairs;
  `triples` is an iterable of 5-tuples `(subject, predicate, obj, valid_from,
  valid_to)` (`tests/benchmarks/test_knowledge_graph_bench.py:L13`, `:L23-L26`,
  `:L33`, `:L38`).
- A reporting sink `record_metric(group, key, value)` used to emit named
  numeric metrics under string group labels
  (`tests/benchmarks/test_knowledge_graph_bench.py:L14`, `:L44-L45`).
- The system under test `mempalace.knowledge_graph.KnowledgeGraph`, imported
  lazily inside each test (`tests/benchmarks/test_knowledge_graph_bench.py:L28`,
  `:L54`, `:L89`, `:L120`, `:L166`, `:L215`, `:L269`).

## `KnowledgeGraph` surface exercised (inferred contract)

- Constructed with a keyword `db_path` pointing at a SQLite file path; each test
  uses a fresh temp file named `kg.sqlite3`
  (`tests/benchmarks/test_knowledge_graph_bench.py:L30`, `:L56`).
- `add_entity(name, etype)` registers a named entity of a given type (e.g.
  `"person"`, `"project"`, `"concept"`)
  (`tests/benchmarks/test_knowledge_graph_bench.py:L33-L34`, `:L59`, `:L65`,
  `:L171`).
- `add_triple(subject, predicate, obj, valid_from=..., valid_to=...)` inserts a
  temporal relationship; `valid_to` is optional (open-ended when omitted)
  (`tests/benchmarks/test_knowledge_graph_bench.py:L38-L39`, `:L66`, `:L133`).
  Dates are passed as ISO `YYYY-MM-DD` strings
  (`tests/benchmarks/test_knowledge_graph_bench.py:L130`, `:L133`).
- `query_entity(name, as_of=...)` returns the relationships for an entity,
  optionally filtered to those valid at a given `as_of` date; the result is
  expected to be a list (`tests/benchmarks/test_knowledge_graph_bench.py:L72`,
  `:L144-L146`, `:L151`, `:L156`).
- `timeline()` with no entity filter performs a full scan and (per the test
  comment) returns at most 100 rows
  (`tests/benchmarks/test_knowledge_graph_bench.py:L85`, `:L102`, `:L106`).
- `stats()` returns aggregate graph statistics
  (`tests/benchmarks/test_knowledge_graph_bench.py:L285`).

## Insertion throughput (`TestTripleInsertionRate`)

Parameterized over `n_triples` in {200, 1000, 5000}
(`tests/benchmarks/test_knowledge_graph_bench.py:L21`). Generates entities
(count `min(n_triples // 2, 200)`) and triples, inserts all entities first, then
times insertion of all triples
(`tests/benchmarks/test_knowledge_graph_bench.py:L23-L40`). Throughput is
`n_triples / max(elapsed, 0.001)` — the divisor is floored at 0.001 s to avoid
division by zero (`tests/benchmarks/test_knowledge_graph_bench.py:L42`). Emits
`kg_insert/triples_per_sec_at_<n>` (rounded to 1 decimal) and
`kg_insert/elapsed_sec_at_<n>` (rounded to 3 decimals)
(`tests/benchmarks/test_knowledge_graph_bench.py:L44-L45`).

## Query latency vs relationship count (`TestQueryEntityLatency`)

Creates one hub entity `"Hub"`, then for each target in {10, 50, 100} attaches
that many `works_on` relationships to distinct `Node_<target>_<i>` entities, all
valid from `2025-01-01` (`tests/benchmarks/test_knowledge_graph_bench.py:L59-L66`).
The hub therefore ends with `10+50+100 = 160` total relationships
(`tests/benchmarks/test_knowledge_graph_bench.py:L77`). Runs `query_entity("Hub")`
20 times, recording per-call latency in milliseconds
(`tests/benchmarks/test_knowledge_graph_bench.py:L69-L74`). Emits
`kg_query/avg_ms_with_<total>_rels` (avg, 2 decimals) and
`kg_query/total_relationships`
(`tests/benchmarks/test_knowledge_graph_bench.py:L76-L80`).

## Timeline latency (`TestTimelinePerformance`)

Parameterized over `n_triples` in {200, 1000, 5000}
(`tests/benchmarks/test_knowledge_graph_bench.py:L87`). Builds a graph from
generated entities/triples, then times `timeline()` (no filter, full scan) over
10 iterations, recording per-call ms
(`tests/benchmarks/test_knowledge_graph_bench.py:L91-L108`). Emits
`kg_timeline/avg_ms_at_<n>` (avg, 2 decimals)
(`tests/benchmarks/test_knowledge_graph_bench.py:L110-L111`).

## Temporal query accuracy (`TestTemporalQueryAccuracy`)

Seeds three entities (`Alice` person, `ProjectA`/`ProjectB` projects) and two
known relationships: Alice works_on ProjectA valid `2024-01-01`..`2024-06-30`,
and Alice works_on ProjectB valid from `2024-07-01` (open-ended)
(`tests/benchmarks/test_knowledge_graph_bench.py:L124-L133`). Adds 500 generated
noise triples over 50 generated entities
(`tests/benchmarks/test_knowledge_graph_bench.py:L136-L141`). The expected
correctness contract: querying Alice `as_of="2024-03-15"` should surface
ProjectA, and `as_of="2024-09-15"` should surface ProjectB
(`tests/benchmarks/test_knowledge_graph_bench.py:L143-L146`). Emits
`kg_temporal/march_query_results` and `kg_temporal/sept_query_results` as the
result-list lengths, defaulting to 0 when the result is not a list
(`tests/benchmarks/test_knowledge_graph_bench.py:L148-L157`).

## Concurrent writers (`TestSQLiteConcurrentAccess.test_concurrent_writers`)

Pre-creates 100 `Entity_<i>` concept entities
(`tests/benchmarks/test_knowledge_graph_bench.py:L171-L172`). Launches 4 threads,
each performing 50 `add_triple` writes (`relates_to`) against a shared
`KnowledgeGraph` instance; per-thread it counts successes and exceptions
("lock failures") rather than letting them propagate
(`tests/benchmarks/test_knowledge_graph_bench.py:L174-L194`). Threads are joined
with a 30-second timeout each
(`tests/benchmarks/test_knowledge_graph_bench.py:L196-L202`). Emits under group
`kg_concurrent`: `total_failures`, `total_successes`, `elapsed_sec` (2 decimals),
`threads` (4), and `triples_per_thread` (50)
(`tests/benchmarks/test_knowledge_graph_bench.py:L204-L211`). The observable
contract under test is that concurrent writes to the SQLite-backed graph may
raise (counted as failures) and the suite tolerates this without crashing.

## Concurrent read/write (`TestSQLiteConcurrentAccess.test_concurrent_read_write`)

Seeds 50 `E_<i>` concept entities and 200 `links` triples (valid from
`2025-01-01`) (`tests/benchmarks/test_knowledge_graph_bench.py:L220-L223`). Runs
2 reader threads (each 50 `query_entity` calls) and 2 writer threads (each 50
`add_triple` `new_rel` calls valid from `2025-06-01`) simultaneously, counting
exceptions separately for reads and writes
(`tests/benchmarks/test_knowledge_graph_bench.py:L225-L257`). Emits
`kg_concurrent_rw/read_errors` and `kg_concurrent_rw/write_errors` as summed
failure counts (`tests/benchmarks/test_knowledge_graph_bench.py:L259-L260`).

## Stats latency (`TestKGStats`)

Parameterized over `n_triples` in {200, 1000, 5000}
(`tests/benchmarks/test_knowledge_graph_bench.py:L267`). Builds a graph from
generated data, then times `stats()` over 10 iterations recording per-call ms,
and emits `kg_stats/avg_ms_at_<n>` (avg, 2 decimals)
(`tests/benchmarks/test_knowledge_graph_bench.py:L271-L290`).

## Invariants and side effects

- Each test operates on an isolated temp SQLite file, so no cross-test state
  leaks (`tests/benchmarks/test_knowledge_graph_bench.py:L30`, `:L56`, `:L96`,
  `:L122`, `:L168`, `:L217`, `:L276`).
- The only externally observable output is the stream of `record_metric` calls;
  none of the tests assert on values — they are pure measurements
  (`tests/benchmarks/test_knowledge_graph_bench.py:L44-L45`, `:L79-L80`,
  `:L111`, `:L148-L157`, `:L207-L211`, `:L259-L260`, `:L290`).
- Generated data uses fixed seeds (42) for reproducibility
  (`tests/benchmarks/test_knowledge_graph_bench.py:L23`, `:L91`, `:L136`,
  `:L271`).
