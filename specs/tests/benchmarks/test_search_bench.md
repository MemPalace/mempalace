# Spec: Search performance benchmark suite

This module defines a suite of search benchmarks that measure query latency, recall@k, filtering impact, concurrent contention, and result-count scaling as palace size grows. All measurements are emitted as named metrics rather than asserted as pass/fail thresholds (with one exception). (tests/benchmarks/test_search_bench.py:L1-L6)

## External dependencies and contracts

The suite depends on two collaborators with the following contracts:

- A palace data generator constructed with a deterministic seed and a scale selector. It exposes a `populate_palace_directly(palace_path, n_drawers, include_needles)` operation that writes a palace at `palace_path` containing `n_drawers` drawers and returns a 3-tuple; the third element is "needle info", a sequence of records. (tests/benchmarks/test_search_bench.py:L13-L13, tests/benchmarks/test_search_bench.py:L26-L28, tests/benchmarks/test_search_bench.py:L67-L71)
- Each needle-info record is a mapping exposing at least `query` (a search string) and `wing` (a wing identifier). (tests/benchmarks/test_search_bench.py:L79-L80, tests/benchmarks/test_search_bench.py:L137-L138)
- When `include_needles=True`, drawers planted as needles contain the literal substring `NEEDLE_` in their text, which is the observable marker used to detect whether a needle was retrieved. (tests/benchmarks/test_search_bench.py:L87-L88, tests/benchmarks/test_search_bench.py:L129-L129)
- A metric recorder `record_metric(category, name, value)` that persists a single named numeric measurement under a category. (tests/benchmarks/test_search_bench.py:L14-L14, tests/benchmarks/test_search_bench.py:L53-L55)
- The system under test is a `search_memories(query, palace_path, n_results, [wing])` operation that returns a result mapping. A successful result has no `error` key; on failure the mapping contains an `error` key. The result mapping may contain a `results` key holding a list of hit mappings, each hit exposing a `text` field. (tests/benchmarks/test_search_bench.py:L30-L31, tests/benchmarks/test_search_bench.py:L43-L46, tests/benchmarks/test_search_bench.py:L84-L84)

All benchmark groups are tagged with a `benchmark` marker so they can be selected or excluded as a class. (tests/benchmarks/test_search_bench.py:L17-L18, tests/benchmarks/test_search_bench.py:L58-L59, tests/benchmarks/test_search_bench.py:L102-L103, tests/benchmarks/test_search_bench.py:L159-L160, tests/benchmarks/test_search_bench.py:L213-L214)

Each benchmark builds its palace into a fresh temporary directory under the subpath `palace`, and the generator is always seeded with `42` for reproducibility. (tests/benchmarks/test_search_bench.py:L26-L28)

## Benchmark: search latency vs. palace size

Runs once per palace size in the set {500, 1000, 2500, 5000}, configured by a `bench_scale` selector passed to the generator. (tests/benchmarks/test_search_bench.py:L21-L28) The palace is populated without needles. (tests/benchmarks/test_search_bench.py:L28-L28)

It issues a fixed ordered list of five queries: "authentication middleware", "database optimization", "error handling patterns", "deployment configuration", "testing strategy", each requesting 5 results. (tests/benchmarks/test_search_bench.py:L32-L43) For each query it records wall-clock elapsed time in milliseconds and asserts the result contains no `error` key — this is the suite's only hard assertion. (tests/benchmarks/test_search_bench.py:L42-L46)

From the collected per-query latencies it computes the average, the p50 (median, selected as the element at index `floor(count/2)` of the ascending-sorted latencies), and the p95 (the element at index `floor(count * 0.95)`). For five samples this places p50 at index 2 and p95 at index 4. (tests/benchmarks/test_search_bench.py:L48-L51) It emits three metrics under category `search`, each suffixed with the drawer count, rounded to one decimal: `avg_latency_ms_at_<n>`, `p50_ms_at_<n>`, `p95_ms_at_<n>`. (tests/benchmarks/test_search_bench.py:L53-L55)

## Benchmark: recall@k at scale

Runs once per palace size in {500, 1000, 2500, 5000}. (tests/benchmarks/test_search_bench.py:L62-L65) The palace is populated with needles, and the returned needle-info list is used. (tests/benchmarks/test_search_bench.py:L69-L71)

It evaluates up to the first 10 needles (capped at the available needle count). (tests/benchmarks/test_search_bench.py:L77-L79) For each needle it searches with that needle's `query` requesting 10 results; if the result carries an `error` key the needle is skipped (not counted as a miss, but counted in the denominator). (tests/benchmarks/test_search_bench.py:L79-L82) It extracts the ordered `text` values from `results`. A needle is counted as a hit at 5 if any of the first five texts contains `NEEDLE_`, and a hit at 10 if any of the first ten texts contains `NEEDLE_`. (tests/benchmarks/test_search_bench.py:L84-L93)

Recall@5 and recall@10 are computed as hits divided by the number of needle queries attempted (denominator floored at 1 to avoid division by zero). (tests/benchmarks/test_search_bench.py:L95-L96) Two metrics are emitted under category `search_recall`, suffixed with the drawer count and rounded to three decimals: `recall_at_5_at_<n>`, `recall_at_10_at_<n>`. (tests/benchmarks/test_search_bench.py:L98-L99)

## Benchmark: filtered vs. unfiltered search

Runs once. The palace is populated with 2000 drawers including needles. (tests/benchmarks/test_search_bench.py:L106-L112) It evaluates up to the first 10 needles. (tests/benchmarks/test_search_bench.py:L120-L122)

For each needle it performs two searches in order, both requesting 5 results: first an unfiltered search by `query`, then a filtered search passing the needle's `wing` as the `wing` argument; it times each in milliseconds and counts a hit when any of the (up to) five returned hit texts contains `NEEDLE_`. (tests/benchmarks/test_search_bench.py:L122-L142)

It computes average unfiltered and filtered latencies (denominator floored at 1). The latency improvement percentage is `(avg_unfiltered - avg_filtered) / max(avg_unfiltered, 0.01) * 100` — a positive value means filtering was faster. (tests/benchmarks/test_search_bench.py:L144-L146) It emits five metrics under category `search_filter`: `avg_unfiltered_ms` and `avg_filtered_ms` (rounded to one decimal), `latency_improvement_pct` (rounded to one decimal), and `unfiltered_recall_at_5` and `filtered_recall_at_5` (hits over needle count, denominator floored at 1, rounded to three decimals). (tests/benchmarks/test_search_bench.py:L148-L156)

## Benchmark: concurrent search

Runs once. The palace is built at the fixed `small` scale with 2000 drawers and no needles. (tests/benchmarks/test_search_bench.py:L163-L167) This benchmark does not take a `bench_scale` parameter; it hardcodes `small`. (tests/benchmarks/test_search_bench.py:L163-L165)

The workload is a list of ten distinct single-word queries repeated three times for 30 total queries. (tests/benchmarks/test_search_bench.py:L171-L182) Each query runs through a worker that times the 5-result search and reports elapsed milliseconds plus a success flag (true when the result has no `error` key). (tests/benchmarks/test_search_bench.py:L184-L188) The 30 queries are dispatched concurrently across a pool of 4 workers; results are gathered as they complete, accumulating all latencies and counting failures. (tests/benchmarks/test_search_bench.py:L190-L199)

From the ascending-sorted latencies it emits, under category `concurrent_search`: `p50_ms` (index `floor(n/2)`), `p95_ms` (index `floor(n*0.95)`), `p99_ms` (index `floor(n*0.99)`), `avg_ms`, all rounded to one decimal; plus `error_count` (number of failed searches), `total_queries` (30), and `workers` (4) as integer metrics. (tests/benchmarks/test_search_bench.py:L201-L210)

## Benchmark: latency vs. n_results

Runs once per `n_results` value in the ordered set {1, 5, 10, 25, 50}. (tests/benchmarks/test_search_bench.py:L217-L218) The palace is built at fixed `small` scale with 2000 drawers and no needles. (tests/benchmarks/test_search_bench.py:L219-L221)

It runs the same query "authentication middleware" five times at the given result count, timing each in milliseconds, then records the average rounded to one decimal under category `search_n_results` as `avg_ms_at_n_<n_results>`. (tests/benchmarks/test_search_bench.py:L225-L234)

## Invariants and edge cases

- Determinism: every palace is generated from seed 42, so a given (scale, n_drawers, include_needles) produces a repeatable palace. (tests/benchmarks/test_search_bench.py:L26-L28, tests/benchmarks/test_search_bench.py:L67-L67, tests/benchmarks/test_search_bench.py:L108-L108, tests/benchmarks/test_search_bench.py:L165-L165, tests/benchmarks/test_search_bench.py:L219-L219)
- Ordering guarantee for recall: hit detection relies on the order of `text` values in the `results` list reflecting search ranking, since the first 5 / first 10 slices are taken positionally. (tests/benchmarks/test_search_bench.py:L84-L88, tests/benchmarks/test_search_bench.py:L129-L129, tests/benchmarks/test_search_bench.py:L141-L141)
- Error tolerance: in the recall, filter, and concurrent benchmarks, a search returning an `error` key is tolerated (skipped or counted as a non-hit / failure) and does not abort the benchmark; only the latency-curve benchmark hard-asserts absence of errors. (tests/benchmarks/test_search_bench.py:L46-L46, tests/benchmarks/test_search_bench.py:L81-L82, tests/benchmarks/test_search_bench.py:L188-L188)
- Empty-result tolerance: missing `results` is treated as an empty list when extracting hit texts, so a result with no matches yields zero hits without error. (tests/benchmarks/test_search_bench.py:L84-L84, tests/benchmarks/test_search_bench.py:L129-L129)
- Division-by-zero guards: recall denominators are floored at 1 and the improvement-percentage denominator at 0.01. (tests/benchmarks/test_search_bench.py:L95-L96, tests/benchmarks/test_search_bench.py:L146-L146, tests/benchmarks/test_search_bench.py:L152-L155)

## Side effects

- Filesystem: writes a generated palace under a temporary directory at subpath `palace` for each benchmark run. (tests/benchmarks/test_search_bench.py:L27-L28)
- Metric emission: each benchmark's only durable output is the set of `record_metric` calls; the benchmarks produce no return value and (except the latency curve) make no assertions. (tests/benchmarks/test_search_bench.py:L53-L55, tests/benchmarks/test_search_bench.py:L98-L99, tests/benchmarks/test_search_bench.py:L148-L156, tests/benchmarks/test_search_bench.py:L204-L210, tests/benchmarks/test_search_bench.py:L234-L234)
