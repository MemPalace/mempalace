# Spec: Benchmark utilities (`tests/benchmarks/report.py`)

Provides two utilities for benchmark workflows: recording metrics to a shared on-disk JSON file during a test session, and comparing two benchmark JSON files to detect regressions (`tests/benchmarks/report.py:L1-L6`).

## On-disk results file location

A single fixed results file is used for the whole session. Its path is the system temporary directory joined with the filename `mempalace_bench_results.json` (`tests/benchmarks/report.py:L13`). This path is an observable contract.

## `record_metric(category, metric, value)`

Appends one metric value into the session results file, keyed by category then metric name (`tests/benchmarks/report.py:L16-L31`).

Inputs: `category` string grouping key, `metric` string name, `value` any JSON-serializable value (`tests/benchmarks/report.py:L16`).

Behavior and ordering:
- Loads existing results if the file exists, parsing its JSON content as the starting object (`tests/benchmarks/report.py:L18-L22`).
- If the file exists but cannot be read or parsed, existing content is discarded and treated as empty — no error raised (`tests/benchmarks/report.py:L23-L24`).
- If the file does not exist, starting object is empty (`tests/benchmarks/report.py:L18-L19`).
- If `category` is absent, an empty object is created for it (`tests/benchmarks/report.py:L26-L27`).
- The value is stored at `results[category][metric]`, overwriting any prior value (`tests/benchmarks/report.py:L28`).
- The merged object is written back as JSON with 2-space indentation, replacing prior contents (`tests/benchmarks/report.py:L30-L31`).

Observable on-disk contract: top-level keys are categories, each mapping to an object of metric-name to value; there is NO enclosing `results` wrapper key (`tests/benchmarks/report.py:L26-L31`).

## `check_regression(current_report, baseline_report, threshold=0.2)`

Compares a current benchmark file against a baseline and returns a list of human-readable regression descriptions; empty list means no regressions (`tests/benchmarks/report.py:L34-L117`).

Inputs: `current_report` path (`tests/benchmarks/report.py:L42-L43`), `baseline_report` path (`tests/benchmarks/report.py:L44-L45`), `threshold` fractional degradation tolerated, default 0.2 = 20% (`tests/benchmarks/report.py:L34`, `tests/benchmarks/report.py:L40`).

Input file format: both files are JSON objects; comparison reads metrics nested under a top-level `results` key: `report["results"][category][metric]` (`tests/benchmarks/report.py:L88-L94`). This differs from what `record_metric` writes.

### Metric direction classification (`tests/benchmarks/report.py:L77-L86`)
- Case-insensitive: name lowercased before matching (`tests/benchmarks/report.py:L79`).
- "Higher is better" keywords checked first, in order: `improvement`, `recall`, `throughput`, `per_sec`, `files_per_sec`, `drawers_per_sec`, `triples_per_sec`, `speedup`; first substring match yields `higher_better` (`tests/benchmarks/report.py:L51-L60`, `tests/benchmarks/report.py:L80-L82`).
- "Higher is worse" keywords checked next, in order: `latency`, `rss`, `memory`, `oom`, `lock_failures`, `elapsed`, `p50_ms`, `p95_ms`, `p99_ms`, `rss_delta_mb`, `peak_rss_mb`, `errors`, `failures`; first match yields `higher_worse` (`tests/benchmarks/report.py:L61-L75`, `tests/benchmarks/report.py:L83-L85`).
- Matching is substring containment (`tests/benchmarks/report.py:L81`, `tests/benchmarks/report.py:L84`). No match yields `unknown` (`tests/benchmarks/report.py:L86`).
- Ordering invariant: "better" list checked before "worse" so composite names like `latency_improvement_pct` classify as `higher_better` (`tests/benchmarks/report.py:L48-L50`, `tests/benchmarks/report.py:L80-L85`).

### Comparison algorithm and order
- Iterates baseline `results` categories; absent `results` treated as empty (`tests/benchmarks/report.py:L88-L89`).
- Baseline category not in current is skipped (`tests/benchmarks/report.py:L89-L90`).
- Baseline metric not in current category is skipped (`tests/benchmarks/report.py:L91-L93`).
- Regressions produced in baseline-driven iteration order (`tests/benchmarks/report.py:L88-L115`).

### Skip / edge cases
- Non-numeric baseline or current value skipped (`tests/benchmarks/report.py:L95-L96`).
- Baseline value equal to 0 skipped, avoiding division by zero (`tests/benchmarks/report.py:L97-L98`).
- `unknown` direction produces no regression (`tests/benchmarks/report.py:L100-L115`).

### Regression conditions
- `higher_worse`: regression when `curr_val > base_val * (1 + threshold)` (`tests/benchmarks/report.py:L102-L104`).
- `higher_better`: regression when `curr_val < base_val * (1 - threshold)` (`tests/benchmarks/report.py:L109-L111`).
- Strict comparisons; within-band or equal produces no regression (`tests/benchmarks/report.py:L104`, `tests/benchmarks/report.py:L111`).

### Regression description format
`{category}/{metric}: {baseline} -> {current} ({pct}%, threshold {threshold_pct}%)` (`tests/benchmarks/report.py:L106-L108`, `tests/benchmarks/report.py:L113-L115`). Values formatted with two decimals; `pct = ((curr_val - base_val) / base_val) * 100` with leading sign and one decimal; `threshold_pct = threshold * 100` as integer (`tests/benchmarks/report.py:L105`, `tests/benchmarks/report.py:L107`, `tests/benchmarks/report.py:L114`).

### Error behavior
- Both files opened and parsed eagerly at start; missing file or invalid JSON propagates as an error (unlike `record_metric`) (`tests/benchmarks/report.py:L42-L45`). No writes are performed.
