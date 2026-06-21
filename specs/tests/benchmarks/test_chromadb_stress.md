# Spec: ChromaDB Stress Benchmark Suite

Source: `tests/benchmarks/test_chromadb_stress.py`

## Purpose

A benchmark/stress test module that probes the raw vector-store ("ChromaDB") access patterns used by MemPalace to determine: at what collection size fetching all metadata becomes dangerous (memory growth), how query latency degrades as the collection grows, and how much faster batched insertion is versus sequential insertion (tests/benchmarks/test_chromadb_stress.py:L1-L8). All test classes are marked as benchmarks (tests/benchmarks/test_chromadb_stress.py:L35-L35,L69-L69,L107-L107,L165-L166).

## Dependencies / Collaborators

- A `PalaceDataGenerator` type, constructed with a deterministic `seed` and an optional `scale`, used to populate a palace and to generate random text. It is imported from `tests.benchmarks.data_generator` (tests/benchmarks/test_chromadb_stress.py:L16-L16,L49-L51).
- A `record_metric(group, key, value)` recording function imported from `tests.benchmarks.report`; every reported metric is emitted through it (tests/benchmarks/test_chromadb_stress.py:L17-L17).
- A persistent vector-store client opened at a filesystem path, exposing a collection named `mempalace_drawers` (tests/benchmarks/test_chromadb_stress.py:L53-L54,L81-L82).
- Test fixtures `tmp_path` (a temporary directory) and `bench_scale` (a scaling parameter passed to the data generator) are supplied by the test harness (tests/benchmarks/test_chromadb_stress.py:L47-L47,L76-L76,L170-L170).

## Resident-Memory Measurement Helper

`_get_rss_mb()` returns the current process resident set size (RSS) in megabytes (tests/benchmarks/test_chromadb_stress.py:L20-L32). If a process-introspection library is available it returns `rss_bytes / (1024*1024)` (tests/benchmarks/test_chromadb_stress.py:L22-L24). Otherwise it reads the OS max-RSS usage and converts: on Darwin (macOS) the raw value is in bytes, so it divides by `1024*1024`; on other platforms the raw value is in kilobytes, so it divides by `1024` (tests/benchmarks/test_chromadb_stress.py:L26-L32). The observable contract is a megabyte-denominated RSS value with platform-correct unit conversion.

## Test: Fetch-All-Metadata Memory Growth (`TestGetAllMetadatasOOM`)

Parametrized over collection sizes `[1000, 2500, 5000, 10000]` (tests/benchmarks/test_chromadb_stress.py:L44-L46). For each size `n_drawers`:

1. Constructs a data generator with `seed=42` and the provided `bench_scale`, then populates a palace at `<tmp_path>/palace` with exactly `n_drawers` drawers and no injected "needles" (tests/benchmarks/test_chromadb_stress.py:L49-L51).
2. Opens the persistent client at that palace path and gets the `mempalace_drawers` collection (tests/benchmarks/test_chromadb_stress.py:L53-L54).
3. Measures RSS before, fetches all metadata in a single unbounded request, measures wall-clock elapsed time in milliseconds, then measures RSS after (tests/benchmarks/test_chromadb_stress.py:L56-L60).

Invariant: the number of metadata records returned equals `n_drawers` (tests/benchmarks/test_chromadb_stress.py:L62-L62). RSS delta is computed as `rss_after - rss_before` (tests/benchmarks/test_chromadb_stress.py:L63-L63).

Reported metrics under group `chromadb_get_all`: `rss_delta_mb_at_<n_drawers>` (RSS delta rounded to 2 decimals) and `latency_ms_at_<n_drawers>` (elapsed milliseconds rounded to 1 decimal) (tests/benchmarks/test_chromadb_stress.py:L65-L66).

## Test: Query Latency Degradation (`TestQueryDegradation`)

Parametrized over the same sizes `[1000, 2500, 5000, 10000]` (tests/benchmarks/test_chromadb_stress.py:L73-L75). For each size `n_drawers`:

1. Populates a palace at `<tmp_path>/palace` with `n_drawers` drawers, no needles, using `seed=42` and `bench_scale` (tests/benchmarks/test_chromadb_stress.py:L77-L79), and opens the `mempalace_drawers` collection (tests/benchmarks/test_chromadb_stress.py:L81-L82).
2. Runs a fixed list of five text queries: "authentication middleware optimization", "database connection pooling strategy", "error handling retry logic", "deployment pipeline configuration", "load balancer health check" (tests/benchmarks/test_chromadb_stress.py:L84-L90).
3. For each query, requests the top 5 results including documents and distances, recording per-query wall-clock latency in milliseconds (tests/benchmarks/test_chromadb_stress.py:L93-L97).

Invariant: each query must return a non-empty document result set (tests/benchmarks/test_chromadb_stress.py:L98-L98).

Statistics: average latency is the mean of the five latencies; p95 latency is taken as the element at index `floor(len * 0.95)` of the latencies sorted ascending (tests/benchmarks/test_chromadb_stress.py:L100-L101). Reported metrics under group `chromadb_query`: `avg_latency_ms_at_<n_drawers>` and `p95_latency_ms_at_<n_drawers>`, each rounded to 1 decimal (tests/benchmarks/test_chromadb_stress.py:L103-L104).

## Test: Sequential vs Batched Insertion (`TestBulkInsertPerformance`)

Inserts a fixed `n_docs = 500` documents two ways and compares timing (tests/benchmarks/test_chromadb_stress.py:L111-L117). Content is 500 random text strings, each generated with a length between 400 and 800 (tests/benchmarks/test_chromadb_stress.py:L116-L117).

Sequential path: creates/opens an empty `mempalace_drawers` collection at `<tmp_path>/seq` (the directory is created first), then adds documents one at a time. Each document `i` has id `seq_<i>` and metadata `{wing: "test", room: "bench", chunk_index: i}`. Total wall-clock time in milliseconds is recorded (tests/benchmarks/test_chromadb_stress.py:L119-L132).

Batched path: creates/opens an empty `mempalace_drawers` collection at `<tmp_path>/batch` (directory created first), then adds documents in batches of `batch_size = 100`. For batch range `[batch_start, batch_end)` where `batch_end = min(batch_start+100, n_docs)`, ids are `batch_<i>` and metadata is `{wing: "test", room: "bench", chunk_index: i}` for each `i` in the range (tests/benchmarks/test_chromadb_stress.py:L134-L151). Total wall-clock time in milliseconds is recorded.

Speedup is `sequential_ms / max(batched_ms, 0.01)` (the divisor is floored at 0.01 to avoid division by zero) (tests/benchmarks/test_chromadb_stress.py:L153-L153).

Invariants: both collections must contain exactly `n_docs` documents after insertion (tests/benchmarks/test_chromadb_stress.py:L155-L156).

Reported metrics under group `chromadb_insert`: `sequential_ms` (rounded 1), `batched_ms` (rounded 1), `speedup_ratio` (rounded 2), `n_docs` (= 500), and `batch_size` (= 100) (tests/benchmarks/test_chromadb_stress.py:L158-L162).

## Test: Incremental Collection Growth (`TestMaxCollectionSize`)

Marked both as a benchmark and as slow (tests/benchmarks/test_chromadb_stress.py:L165-L166). Grows a collection in fixed-size batches and measures per-batch latency to expose insertion-time degradation as the collection grows (tests/benchmarks/test_chromadb_stress.py:L168-L171).

Setup: data generator with `seed=42` and `bench_scale`; the target document count is `min(cfg["drawers"], 10000)`, i.e. the generator's configured drawer count capped at 10,000 (tests/benchmarks/test_chromadb_stress.py:L172-L174). An empty `mempalace_drawers` collection is created/opened at `<tmp_path>/palace` (directory created first) (tests/benchmarks/test_chromadb_stress.py:L176-L179).

Loop: with `batch_size = 500`, iterates `batch_num` over `0, 500, 1000, ...` up to `target`. Each iteration inserts `n = min(batch_size, target - batch_num)` documents (so the final batch may be partial). Each document is random text of length 400-800; ids are `growth_<batch_num + i>` for offset `i` in `[0, n)`; metadata is `{wing: gen.wings[i % len(gen.wings)], room: "bench", chunk_index: i}` for `i` ranging over `[batch_num, batch_num + n)` (tests/benchmarks/test_chromadb_stress.py:L181-L192). Note the `wing` index `i` and `chunk_index` `i` are drawn from the absolute range `[batch_num, batch_num+n)` while ids use the offset form `batch_num + i` with `i` in `[0,n)` — both yield the same absolute id sequence (tests/benchmarks/test_chromadb_stress.py:L188-L192).

Per batch the add is timed in milliseconds, the running total inserted is incremented by `n`, and a record `{at_size: total_inserted, batch_ms: <rounded 1>}` is appended to a list (tests/benchmarks/test_chromadb_stress.py:L194-L198).

Invariant: the collection count after all batches equals the running total inserted (tests/benchmarks/test_chromadb_stress.py:L200-L200).

Reported metrics under group `chromadb_growth`: `first_batch_ms` (the first record's batch_ms), `last_batch_ms` (the last record's batch_ms), `total_inserted`, and `batch_times` (the full list of per-batch `{at_size, batch_ms}` records, recorded verbatim) (tests/benchmarks/test_chromadb_stress.py:L202-L206).

## Side Effects

- Creates palace data directories under the harness-provided temporary path: `<tmp_path>/palace`, `<tmp_path>/seq`, `<tmp_path>/batch` (tests/benchmarks/test_chromadb_stress.py:L50,L78,L120-L121,L135-L137,L176-L177).
- Writes a persistent vector-store collection named `mempalace_drawers` to disk at those paths (tests/benchmarks/test_chromadb_stress.py:L53-L54,L122-L123,L178-L179).
- Emits all benchmark results through `record_metric` rather than returning them (tests/benchmarks/test_chromadb_stress.py:L65-L66,L103-L104,L158-L162,L203-L206).

## Determinism

All data generation uses `seed=42`, so populated content, query corpus, and inserted documents are reproducible across runs given the same `bench_scale`/configuration (tests/benchmarks/test_chromadb_stress.py:L49,L77,L114,L172).
