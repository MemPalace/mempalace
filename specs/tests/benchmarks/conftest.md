# Spec: Benchmark Test Configuration (`tests/benchmarks/conftest.py`)

This module provides test-suite configuration for the benchmark test group: command-line options, per-test and per-session fixtures, an in-memory results collector, and an end-of-run JSON report writer (`tests/benchmarks/conftest.py:L1-L1`).

## Command-line options

Two options are registered for the benchmark suite (`tests/benchmarks/conftest.py:L13-L24`):

- `--bench-scale`: selects a scale level. Allowed values are exactly `small`, `medium`, `large`, `stress` (`tests/benchmarks/conftest.py:L10-L18`). Default is `small`. Any value outside the allowed set is rejected (`tests/benchmarks/conftest.py:L14-L19`). Documented scale meanings: small=1K, medium=10K, large=50K, stress=100K (`tests/benchmarks/conftest.py:L18-L18`).
- `--bench-report`: a path for JSON report output. Default is unset/null (`tests/benchmarks/conftest.py:L20-L24`).

## Session-scoped value fixtures

- `bench_scale`: yields the configured `--bench-scale` value, computed once per test session (`tests/benchmarks/conftest.py:L27-L30`).
- `bench_report_path`: yields the configured `--bench-report` value, or null when unset, computed once per session (`tests/benchmarks/conftest.py:L33-L36`).
- `bench_results`: yields a fresh results collector object, shared across all tests in the session (`tests/benchmarks/conftest.py:L87-L90`).

## Per-test directory/path fixtures

Each of these is created fresh for every individual test inside that test's temporary directory:

- `palace_dir`: creates a subdirectory named `palace` and returns its path as a string. The directory is created on disk before being returned (`tests/benchmarks/conftest.py:L39-L44`).
- `kg_db`: returns the string path to a file named `test_kg.sqlite3` in the temp directory. The file itself is NOT created — only the path string is produced (`tests/benchmarks/conftest.py:L47-L50`).
- `config_dir`: creates a subdirectory named `config`, writes a `config.json` file inside it, and returns the config subdirectory path as a string (`tests/benchmarks/conftest.py:L53-L61`). The written `config.json` is a JSON object with exactly two keys: `palace_path` set to the path of a sibling `palace` directory (under the temp root, not necessarily created), and `collection_name` set to the literal string `mempalace_drawers` (`tests/benchmarks/conftest.py:L58-L60`).
- `project_dir`: creates a subdirectory named `project` and returns its path object (created on disk) (`tests/benchmarks/conftest.py:L64-L69`).

Invariant: each test gets isolated paths because all fixtures root under a per-test temporary directory (`tests/benchmarks/conftest.py:L40-L69`).

## Results collector contract

The session-shared collector holds metrics keyed by category then metric name (`tests/benchmarks/conftest.py:L75-L84`). It exposes a `record(category, metric, value)` operation: if the category has not been seen, an empty mapping is created for it, then `value` is stored under `results[category][metric]`, overwriting any prior value for the same category/metric pair (`tests/benchmarks/conftest.py:L81-L84`). The collector starts empty (`tests/benchmarks/conftest.py:L78-L79`).

## End-of-run JSON report

After all tests complete, a terminal-summary hook may write a JSON report (`tests/benchmarks/conftest.py:L93-L94`).

Gating: if `--bench-report` is unset/null, the hook does nothing and writes no file (`tests/benchmarks/conftest.py:L95-L97`).

When a report path is set, a report object is assembled with the following keys (`tests/benchmarks/conftest.py:L117-L129`):

- `timestamp`: current local date-time in ISO 8601 format (`tests/benchmarks/conftest.py:L118-L118`).
- `git_sha`: short commit hash of `HEAD`. If retrieving the hash fails for any reason, the value is the literal string `unknown` (`tests/benchmarks/conftest.py:L103-L108`, `tests/benchmarks/conftest.py:L119-L119`).
- `python_version`: the running language/runtime version string (`tests/benchmarks/conftest.py:L120-L120`).
- `chromadb_version`: the storage-backend library version; if it cannot be determined, the literal string `unknown` (`tests/benchmarks/conftest.py:L110-L115`, `tests/benchmarks/conftest.py:L121-L121`).
- `scale`: the configured `--bench-scale` value, defaulting to `small` (`tests/benchmarks/conftest.py:L122-L122`).
- `system`: an object with `os` (lowercased operating-system name), `cpu_count` (number of CPUs, may be null), and `platform` (full platform descriptor string) (`tests/benchmarks/conftest.py:L123-L127`).
- `results`: a mapping of collected metrics, initially empty (`tests/benchmarks/conftest.py:L128-L128`).

Results ingestion: the hook looks for a file named `mempalace_bench_results.json` in the system temp directory. If present, its JSON contents replace the `results` field, and the file is then deleted. If reading or parsing fails, `results` is left as the empty mapping and no error propagates (`tests/benchmarks/conftest.py:L131-L139`). This establishes a cross-process contract: individual tests communicate metrics to the report by writing this temp file.

Output: the parent directory of the (absolute-resolved) report path is created if missing, then the report object is written to the report path as JSON indented by 2 spaces (`tests/benchmarks/conftest.py:L141-L143`). A confirmation line `Benchmark report written to: <path>` is emitted to the terminal afterward (`tests/benchmarks/conftest.py:L144-L144`).

## Side effects summary

- Filesystem: creates per-test `palace`, `config`, and `project` subdirectories and a `config.json` file under temp dirs (`tests/benchmarks/conftest.py:L42-L69`); reads and deletes a temp `mempalace_bench_results.json` (`tests/benchmarks/conftest.py:L132-L137`); creates report parent directory and writes the report file (`tests/benchmarks/conftest.py:L141-L143`).
- Process/network: invokes git to read the HEAD short hash (`tests/benchmarks/conftest.py:L104-L106`).
- All failure paths for git, backend version, and results-file read are non-fatal and fall back to defaults (`tests/benchmarks/conftest.py:L107-L108`, `tests/benchmarks/conftest.py:L114-L115`, `tests/benchmarks/conftest.py:L138-L139`).
