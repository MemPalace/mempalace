# Spec: MCP Server Tool Performance Benchmarks

Source: `tests/benchmarks/test_mcp_bench.py`

## Purpose

This module is a performance benchmark suite that exercises MCP server tool handler functions directly (not over a transport) to validate production-readiness findings about memory and latency behavior of read-path tools (tests/benchmarks/test_mcp_bench.py:L1-L10). Each test populates a synthetic palace of a given size, points the MCP server's configuration at it, calls a tool, and records timing/memory metrics (tests/benchmarks/test_mcp_bench.py:L66-L227).

All benchmark classes/tests are tagged with a `benchmark` marker so they can be selected or skipped as a group (tests/benchmarks/test_mcp_bench.py:L69, L112, L131, L150, L186, L210).

## External Dependencies (collaborators)

- A palace data generator that can build a palace at a directory path with an exact drawer count (tests/benchmarks/test_mcp_bench.py:L17, L24-L29).
- A metric recorder `record_metric(category, key, value)` used to persist all measured numbers (tests/benchmarks/test_mcp_bench.py:L18, L91).
- The MCP server module exposing tool handler functions and module-level `_config`, `_get_kg`, and `_get_collection` (tests/benchmarks/test_mcp_bench.py:L34-L45, L81, L121, L140, L159, L195, L219).
- A persistent vector store client keyed by directory path with a named collection `mempalace_drawers` (tests/benchmarks/test_mcp_bench.py:L14, L171-L172).

## Test Harness Helpers

### `_make_palace(tmp_path, n_drawers, scale="small")`

Creates a palace populated with exactly `n_drawers` drawers under `<tmp_path>/palace` and returns that path as a string (tests/benchmarks/test_mcp_bench.py:L24-L29). The generator is constructed deterministically with a fixed seed of `42` and the given `scale` (default `"small"`), and population excludes "needle" entries (tests/benchmarks/test_mcp_bench.py:L26-L28).

### `_patch_mcp_config(monkeypatch, palace_path, tmp_path)`

Redirects the MCP server to a test environment for the duration of one test (tests/benchmarks/test_mcp_bench.py:L32-L45). It:
- Builds a config object whose config directory is `<tmp_path>/cfg` (tests/benchmarks/test_mcp_bench.py:L37).
- Forces the config's file-backed settings to a single mapping `{"palace_path": palace_path}`, overriding the resolved palace path (tests/benchmarks/test_mcp_bench.py:L38-L39).
- Replaces the MCP server's module-level `_config` with this config (tests/benchmarks/test_mcp_bench.py:L44).
- Constructs a knowledge graph backed by `<tmp_path>/kg.sqlite3` and replaces the server's `_get_kg` accessor with one that always returns this graph instance (tests/benchmarks/test_mcp_bench.py:L43, L45).

All overrides are scoped to the test via the patching mechanism and are reverted afterward (tests/benchmarks/test_mcp_bench.py:L32-L45).

### `_get_rss_mb()`

Returns the current process resident set size (RSS) in megabytes (tests/benchmarks/test_mcp_bench.py:L48-L63). Primary path reads process memory and divides bytes by 1024*1024 (tests/benchmarks/test_mcp_bench.py:L51-L53). Fallback path reads peak RSS from OS resource usage and normalizes units by platform: on macOS ("Darwin") the raw value is treated as bytes and divided by 1024*1024; otherwise it is treated as kilobytes and divided by 1024 (tests/benchmarks/test_mcp_bench.py:L55-L63). This unit handling is an observable contract: the reported number is megabytes regardless of platform.

## Benchmark Cases

### `tool_status` memory growth — `test_tool_status_rss_growth`

Parametrized over palace sizes `[500, 1000, 2500, 5000]` (tests/benchmarks/test_mcp_bench.py:L73, L75). For each size it builds the palace, patches config, samples RSS before and after a single `tool_status()` call, and computes the delta (tests/benchmarks/test_mcp_bench.py:L76-L87). Invariants asserted: the result contains no `"error"` key, and `result["total_drawers"]` equals the requested `n_drawers` exactly (tests/benchmarks/test_mcp_bench.py:L88-L89). It records the RSS delta in MB (rounded to 2 decimals) under category `mcp_status`, key `rss_delta_mb_at_<n_drawers>` (tests/benchmarks/test_mcp_bench.py:L91).

### `tool_status` latency — `test_tool_status_latency`

Same sizes and setup (tests/benchmarks/test_mcp_bench.py:L93-L97). Performs one warm-up call, then times a single subsequent `tool_status()` call in milliseconds (tests/benchmarks/test_mcp_bench.py:L102-L106). Asserts the result has no `"error"` key (tests/benchmarks/test_mcp_bench.py:L108). Records latency in ms (rounded to 1 decimal) under category `mcp_status`, key `latency_ms_at_<n_drawers>` (tests/benchmarks/test_mcp_bench.py:L109).

### `tool_list_wings` latency — `test_list_wings_latency`

Parametrized over `[500, 1000, 2500, 5000]` (tests/benchmarks/test_mcp_bench.py:L116). Times a single `tool_list_wings()` call (tests/benchmarks/test_mcp_bench.py:L123-L125). Invariant: the result contains a `"wings"` key (tests/benchmarks/test_mcp_bench.py:L127). Records latency in ms (1 decimal) under category `mcp_list_wings`, key `latency_ms_at_<n_drawers>` (tests/benchmarks/test_mcp_bench.py:L128).

### `tool_get_taxonomy` latency — `test_get_taxonomy_latency`

Parametrized over `[500, 1000, 2500, 5000]` (tests/benchmarks/test_mcp_bench.py:L135). Times a single `tool_get_taxonomy()` call (tests/benchmarks/test_mcp_bench.py:L142-L144). Invariant: the result contains a `"taxonomy"` key (tests/benchmarks/test_mcp_bench.py:L146). Records latency in ms (1 decimal) under category `mcp_taxonomy`, key `latency_ms_at_<n_drawers>` (tests/benchmarks/test_mcp_bench.py:L147).

### Client re-instantiation overhead — `test_reinstantiation_overhead`

Uses a fixed palace of 500 drawers (tests/benchmarks/test_mcp_bench.py:L156). Performs `n_calls = 50` (tests/benchmarks/test_mcp_bench.py:L161). It first measures total milliseconds to call the server's `_get_collection()` 50 times, asserting each returned collection is non-null (tests/benchmarks/test_mcp_bench.py:L163-L168). It then constructs a single persistent client at `palace_path`, gets the `mempalace_drawers` collection once, and measures total milliseconds to call `count()` on that cached collection 50 times (tests/benchmarks/test_mcp_bench.py:L170-L176). It computes an overhead ratio = uncached_total / max(cached_total, 0.01), where the denominator is floored at 0.01 ms to avoid division by zero (tests/benchmarks/test_mcp_bench.py:L178). It records, under category `client_reinstantiation`, four metrics: `uncached_total_ms` (1 decimal), `cached_total_ms` (1 decimal), `overhead_ratio` (2 decimals), and `n_calls` (integer 50) (tests/benchmarks/test_mcp_bench.py:L180-L183).

### `tool_search` latency — `test_search_latency`

Parametrized over `[500, 1000, 2500, 5000]` (tests/benchmarks/test_mcp_bench.py:L190). Runs three fixed queries in order: `"authentication middleware"`, `"database migration"`, `"error handling"`, each via `tool_search(query=q, limit=5)`, timing each call individually in ms (tests/benchmarks/test_mcp_bench.py:L197-L203). Invariant: each result contains no `"error"` key (tests/benchmarks/test_mcp_bench.py:L204). It records the arithmetic mean of the three latencies (1 decimal) under category `mcp_search`, key `avg_latency_ms_at_<n_drawers>` (tests/benchmarks/test_mcp_bench.py:L206-L207).

### Duplicate-check latency — `test_duplicate_check_latency`

Parametrized over `[500, 1000, 2500]` (note: this case omits 5000) (tests/benchmarks/test_mcp_bench.py:L214). Times a single `tool_check_duplicate(content=...)` call against the fixed string `"This is unique test content for duplicate checking benchmark."` (tests/benchmarks/test_mcp_bench.py:L221-L224). Invariant: the result contains no `"error"` key (tests/benchmarks/test_mcp_bench.py:L226). Records latency in ms (1 decimal) under category `mcp_duplicate_check`, key `latency_ms_at_<n_drawers>` (tests/benchmarks/test_mcp_bench.py:L227).

## Observable Contracts Summary

- Tool result shapes relied upon: `tool_status` returns a mapping with numeric `total_drawers` and may carry an `"error"` key on failure (tests/benchmarks/test_mcp_bench.py:L88-L89); `tool_list_wings` returns a mapping with `"wings"` (tests/benchmarks/test_mcp_bench.py:L127); `tool_get_taxonomy` returns a mapping with `"taxonomy"` (tests/benchmarks/test_mcp_bench.py:L146); `tool_search` and `tool_check_duplicate` return mappings that omit `"error"` on success (tests/benchmarks/test_mcp_bench.py:L204, L226).
- Metric categories and key-name templates emitted are part of the reporting contract: `mcp_status`, `mcp_list_wings`, `mcp_taxonomy`, `client_reinstantiation`, `mcp_search`, `mcp_duplicate_check`, with size-suffixed keys as listed above (tests/benchmarks/test_mcp_bench.py:L91, L109, L128, L147, L180-L183, L207, L227).
- The vector collection name is `mempalace_drawers` (tests/benchmarks/test_mcp_bench.py:L172).

## Side Effects

- Creates palace directories and a SQLite knowledge-graph file under a per-test temporary directory (tests/benchmarks/test_mcp_bench.py:L27-L28, L37, L43).
- Mutates MCP server module globals (`_config`, `_get_kg`) during each test, restored after (tests/benchmarks/test_mcp_bench.py:L44-L45).
- Writes benchmark metrics through `record_metric` (tests/benchmarks/test_mcp_bench.py:L91, L109, L128, L147, L180-L183, L207, L227).
- No network, environment-variable, or process-spawn side effects are introduced by this file; all collaborators run in-process (tests/benchmarks/test_mcp_bench.py:L9, L24-L45).
