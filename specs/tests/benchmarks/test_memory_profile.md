# Behavior Spec: `tests/benchmarks/test_memory_profile.py`

Memory-profiling benchmark suite. It measures resident-set-size (RSS) growth and
heap allocations across the highest-risk repeated-call code paths: repeated
search, repeated tool-status, and repeated Layer1 generation
(tests/benchmarks/test_memory_profile.py:L1-L9).

All tests are tagged with a `benchmark` marker so the harness can select or skip
them as a group (tests/benchmarks/test_memory_profile.py:L34, L71, L114, L148).

## Dependencies / Collaborators

The suite depends on a palace data generator (`PalaceDataGenerator`) and a metric
recorder (`record_metric`) imported from sibling benchmark modules
(tests/benchmarks/test_memory_profile.py:L15-L16). The generator is constructed
with a fixed integer `seed` and a string `scale` and exposes
`populate_palace_directly(palace_path, n_drawers, include_needles)` to build a
palace on disk (tests/benchmarks/test_memory_profile.py:L40-L42). `record_metric`
takes a string group name, a string metric key, and a numeric value
(tests/benchmarks/test_memory_profile.py:L62-L68).

## RSS measurement contract: `_get_rss_mb()`

Returns the current process resident set size expressed in megabytes as a float
(tests/benchmarks/test_memory_profile.py:L19-L31). When a process-introspection
facility is available it returns `rss_bytes / (1024 * 1024)`
(tests/benchmarks/test_memory_profile.py:L20-L23). When unavailable it falls back
to the OS max-RSS counter: on Darwin the raw counter is bytes and is divided by
`1024*1024`; on all other platforms the raw counter is kilobytes and is divided by
`1024` (tests/benchmarks/test_memory_profile.py:L24-L31). The two branches both
yield megabytes, but the value semantics differ (current RSS vs. peak/max RSS).

## Test: `test_search_rss_growth`

Builds a palace under `<tmp_path>/palace` with 1,000 drawers and no needles
(tests/benchmarks/test_memory_profile.py:L38-L42). Issues exactly 200 search calls
against `search_memories(query, palace_path=..., n_results=5)`, cycling the query
through a fixed 5-element list `["authentication", "database", "deployment",
"error handling", "testing"]` selected by `i % 5`
(tests/benchmarks/test_memory_profile.py:L44-L54). Records an initial RSS reading
labeled `start` before the loop, then takes a reading every 50 calls (after calls
50, 100, 150, 200) (tests/benchmarks/test_memory_profile.py:L47-L56). Computes
growth as `end_rss - start_rss` where start is the first reading and end is the
last (tests/benchmarks/test_memory_profile.py:L58-L60).

Emits metrics under group `memory_search`: `rss_start_mb`, `rss_end_mb`,
`rss_growth_mb` (each rounded to 2 decimals), `n_calls` = 200, and
`growth_per_100_calls_mb` = `growth / (n_calls / 100)` rounded to 2 decimals
(tests/benchmarks/test_memory_profile.py:L62-L68).

## Test: `test_tool_status_repeated_calls`

Builds a palace under `<tmp_path>/palace` with 2,000 drawers and no needles
(tests/benchmarks/test_memory_profile.py:L77-L79). Constructs a config object with
`config_dir = <tmp_path>/cfg` and overrides its in-memory file config so
`palace_path` points at the generated palace; constructs a knowledge graph backed
by `<tmp_path>/kg.sqlite3`; and injects both into the MCP server module by
overriding its `_config` value and its `_get_kg` accessor to return the test graph
(tests/benchmarks/test_memory_profile.py:L81-L89). These overrides are scoped to
the test (auto-reverted afterward).

Calls `tool_status()` 50 times (tests/benchmarks/test_memory_profile.py:L91-L98).
Asserts on every call that the returned object's `total_drawers` field equals 2,000
— establishing the contract that `tool_status()` returns a mapping containing an
integer `total_drawers` key reflecting the palace size
(tests/benchmarks/test_memory_profile.py:L98-L99). Records a `start` RSS reading
before the loop and a reading every 10 calls (after 10, 20, 30, 40, 50)
(tests/benchmarks/test_memory_profile.py:L94-L101). Growth = last − first reading
(tests/benchmarks/test_memory_profile.py:L103-L105).

Emits metrics under group `memory_tool_status`: `rss_start_mb`, `rss_end_mb`,
`rss_growth_mb` (rounded to 2 decimals), `n_calls` = 50, and `palace_size` = 2,000
(tests/benchmarks/test_memory_profile.py:L107-L111).

## Test: `test_layer1_repeated_generate`

Builds a palace under `<tmp_path>/palace` with 2,000 drawers and no needles
(tests/benchmarks/test_memory_profile.py:L120-L122). Constructs a `Layer1` object
bound to that palace path (tests/benchmarks/test_memory_profile.py:L124-L126).
Calls `layer.generate()` 30 times; asserts each returned value is a string
containing the substring `"L1"` — establishing that `Layer1.generate()` returns a
text/string output that includes an `L1` marker
(tests/benchmarks/test_memory_profile.py:L128-L134). Records a `start` RSS reading
before the loop and a reading every 10 calls (after 10, 20, 30); growth = last −
first (tests/benchmarks/test_memory_profile.py:L129-L140).

Emits metrics under group `memory_layer1`: `rss_start_mb`, `rss_end_mb`,
`rss_growth_mb` (rounded to 2 decimals), and `n_calls` = 30
(tests/benchmarks/test_memory_profile.py:L142-L145).

## Test: `test_search_heap_top_allocators`

Builds a palace under `<tmp_path>/palace` with 1,000 drawers and no needles
(tests/benchmarks/test_memory_profile.py:L154-L156). Takes a heap snapshot, runs
100 identical searches with query `"test query"`, `n_results=5`, then takes a
second snapshot and stops heap tracking
(tests/benchmarks/test_memory_profile.py:L160-L167). Compares the two snapshots
grouped by source line and keeps the top 10 allocation deltas, recording for each a
`file` (traceback location string), `size_kb` (allocated bytes / 1024 rounded to 1
decimal), and `count` (number of allocations)
(tests/benchmarks/test_memory_profile.py:L169-L178).

Emits metrics under group `heap_search`: `top_10_growth_kb` = sum of the 10
`size_kb` values rounded to 1 decimal, and `n_searches` = 100
(tests/benchmarks/test_memory_profile.py:L180-L182).

## Side effects and invariants

- All filesystem writes are confined to the per-test temporary directory
  (`palace`, `cfg`, `kg.sqlite3` under `tmp_path`); no other paths are touched
  (tests/benchmarks/test_memory_profile.py:L41, L85-L87, L121).
- All palaces are generated with the same fixed seed 42 and `scale="small"`,
  making fixture content deterministic across runs
  (tests/benchmarks/test_memory_profile.py:L40, L77, L120, L154).
- Every test always records `start` as its first RSS reading and uses the last
  recorded reading as `end`, so growth is always over the full call range
  (tests/benchmarks/test_memory_profile.py:L50, L58-L60, L95, L130).
- The tests assert observable contracts of the system under test:
  `tool_status()["total_drawers"]` equals the configured drawer count, and
  `Layer1.generate()` output contains `"L1"` (tests/benchmarks/test_memory_profile.py:L99, L134).
