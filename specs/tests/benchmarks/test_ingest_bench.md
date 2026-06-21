# Behavior Spec — `tests/benchmarks/test_ingest_bench.py`

Ingestion throughput benchmark suite. Measures mining performance at scale:
files/sec and drawers/sec through the full mine pipeline, peak RSS during
mining, isolated chunking throughput, and re-ingest skip overhead
(tests/benchmarks/test_ingest_bench.py:L1-L9). Every test in this module is
tagged with a `benchmark` marker so it can be selected or excluded as a group
(tests/benchmarks/test_ingest_bench.py:L35-L36, L101-L102, L134-L135). All
tests record their results via a `record_metric(category, name, value)` sink
rather than asserting performance thresholds (except where noted)
(tests/benchmarks/test_ingest_bench.py:L17, L61-L64).

## External dependencies / contracts

- A data generator `PalaceDataGenerator(seed=..., scale=...)` is used to
  fabricate inputs. It exposes `generate_project_tree(dir, n_files=N)` returning
  a 4-tuple `(project_path, wing, rooms, files_written)` and an internal text
  generator `_random_text(min_chars, max_chars)`
  (tests/benchmarks/test_ingest_bench.py:L16, L42-L45, L110-L115).
- A metrics sink `record_metric(category: str, name: str, value)` records a
  numeric value under a category/name pair
  (tests/benchmarks/test_ingest_bench.py:L17, L61-L64).
- The mining target produces a persistent vector store at the palace path
  containing a collection named exactly `mempalace_drawers`, whose `count()`
  reports the number of drawers created
  (tests/benchmarks/test_ingest_bench.py:L54-L56, L150-L151).
- The system under test exposes `mine(project_path, palace_path)` and
  `chunk_text(content, filename)` from the miner module
  (tests/benchmarks/test_ingest_bench.py:L48, L108, L121).

## Helper: resident-memory measurement

`_get_rss_mb()` returns the current process resident set size in megabytes
(tests/benchmarks/test_ingest_bench.py:L20-L32). When process-introspection is
available it reports RSS bytes divided by 1024*1024
(tests/benchmarks/test_ingest_bench.py:L22-L24). Otherwise it falls back to OS
max-RSS accounting, converting from bytes on Darwin (divide by 1024*1024) and
from kilobytes elsewhere (divide by 1024)
(tests/benchmarks/test_ingest_bench.py:L26-L32). The observable contract is a
megabyte-valued figure regardless of platform
(tests/benchmarks/test_ingest_bench.py:L29-L32).

## TestMineThroughput — full pipeline throughput

### test_mine_files_per_second
Parametrized over `n_files` ∈ {20, 50, 100}
(tests/benchmarks/test_ingest_bench.py:L39-L40). Generates a project tree of
`n_files` files using a fixed seed of 42 and the configured benchmark scale,
into `<tmp>/project`, with the palace written to `<tmp>/palace`
(tests/benchmarks/test_ingest_bench.py:L42-L46). It times a single `mine` call
end to end (tests/benchmarks/test_ingest_bench.py:L50-L52), then opens the
persistent store at the palace path and reads the count of the
`mempalace_drawers` collection (tests/benchmarks/test_ingest_bench.py:L54-L56).

Derived metrics: `files_per_sec = files_written / max(elapsed, 0.001)` and
`drawers_per_sec = drawer_count / max(elapsed, 0.001)` — elapsed is floored at
0.001 seconds to avoid division by zero
(tests/benchmarks/test_ingest_bench.py:L58-L59). It records four metrics under
category `ingest`, each suffixed with the file count: `files_per_sec_at_<n>`
(rounded to 1 decimal), `drawers_per_sec_at_<n>` (rounded to 1 decimal),
`elapsed_sec_at_<n>` (rounded to 2 decimals), and `drawers_created_at_<n>`
(the raw drawer count) (tests/benchmarks/test_ingest_bench.py:L61-L64).

### test_mine_peak_rss
Generates a 100-file project tree (seed 42, configured scale) and mines it,
sampling resident memory on a background thread every 0.1 seconds for the
duration of the mine (tests/benchmarks/test_ingest_bench.py:L70-L92). It
captures RSS immediately before mining, runs `mine`, then stops sampling and
joins the sampler thread with a 1-second timeout
(tests/benchmarks/test_ingest_bench.py:L89-L92). Peak RSS is the maximum sample
collected, or a fresh reading if no samples were taken; the delta is
`peak_rss - rss_before` (tests/benchmarks/test_ingest_bench.py:L94-L95). Records
`peak_rss_mb` and `rss_delta_mb` under category `ingest`, both rounded to 1
decimal (tests/benchmarks/test_ingest_bench.py:L97-L98).

## TestChunkThroughput — isolated chunking

### test_chunk_text_throughput
Parametrized over `content_size_kb` ∈ {1, 10, 100}
(tests/benchmarks/test_ingest_bench.py:L105-L106). Builds a content string of
approximately the target size: an initial random text seeded between
`size_kb*500` and `size_kb*1200` characters, then padded with newline-joined
random fragments (200–500 chars) until its length reaches at least
`size_kb*1024` bytes (tests/benchmarks/test_ingest_bench.py:L110-L115). It then
calls `chunk_text(content, "bench_file.py")` 50 times, timing the loop and
summing the number of chunks returned each iteration
(tests/benchmarks/test_ingest_bench.py:L117-L123). Derived metrics:
`chunks_per_sec = total_chunks / max(elapsed, 0.001)` and
`kb_per_sec = (len(content)*50/1024) / max(elapsed, 0.001)`
(tests/benchmarks/test_ingest_bench.py:L125-L126). Records, under category
`chunking`, `chunks_per_sec_at_<n>kb` and `kb_per_sec_at_<n>kb`, both rounded to
1 decimal (tests/benchmarks/test_ingest_bench.py:L128-L131).

## TestReingestSkipOverhead — re-mine skip cost

### test_skip_check_cost
Uses a fixed small scale and generates a 50-file project tree (seed 42) into
`<tmp>/project`, palace at `<tmp>/palace`
(tests/benchmarks/test_ingest_bench.py:L140-L144). It mines once, then records
the drawer count of `mempalace_drawers` as `initial_count`
(tests/benchmarks/test_ingest_bench.py:L149-L152). It mines the same tree a
second time, timing that re-mine as `skip_elapsed`
(tests/benchmarks/test_ingest_bench.py:L155-L157).

Invariant (asserted): a re-mine of unchanged files MUST NOT add any new
drawers — `final_count == initial_count`, with failure message "Re-mine should
not add new drawers" (tests/benchmarks/test_ingest_bench.py:L159-L161). This is
the only correctness assertion in the module and encodes the append-only /
already-mined skip contract.

Records, under category `reingest`: `skip_check_elapsed_sec` (rounded to 2
decimals), `files_checked` (the file count), and `skip_check_per_file_ms`
computed as `skip_elapsed*1000 / max(files_written, 1)` rounded to 1 decimal —
the per-file divisor is floored at 1 to avoid division by zero
(tests/benchmarks/test_ingest_bench.py:L163-L169).

## Notes for re-implementation
- Determinism is anchored by a fixed seed of 42 throughout
  (tests/benchmarks/test_ingest_bench.py:L42, L70, L110, L140).
- Scale-driven tests honor an externally supplied benchmark scale, while the
  skip-overhead test pins scale to "small"
  (tests/benchmarks/test_ingest_bench.py:L42, L70, L140).
- Project and palace are always isolated under a per-test temporary directory
  (tests/benchmarks/test_ingest_bench.py:L43-L46, L71-L74, L141-L144).
