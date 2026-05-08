# MemPalace Scale Benchmark Suite

106 tests that benchmark mempalace at scale to validate real-world performance limits.

## Why

MemPalace has strong academic scores (96.6% R@5 on LongMemEval) but no empirical data on how it behaves at scale. Key unknowns:

- `tool_status()` loads ALL metadata into memory — at what palace size does this OOM?
- `PersistentClient` is re-instantiated on every MCP call — what's the overhead?
- Modified files are never re-ingested — what's the skip-check cost at scale?
- How does query latency degrade as the palace grows from 1K to 100K drawers?
- Does wing/room filtering actually improve retrieval, and by how much?
- At what per-room drawer count does recall break regardless of filtering?

This suite finds those answers.

## Quick Start

```bash
# Fast smoke test (~2 min)
uv run pytest tests/benchmarks/ -v --bench-scale=small -m "benchmark and not slow"

# Full small scale (~35 min)
uv run pytest tests/benchmarks/ -v --bench-scale=small

# Medium scale with JSON report
uv run pytest tests/benchmarks/ -v --bench-scale=medium --bench-report=results.json

# Stress test (local only, very slow)
uv run pytest tests/benchmarks/ -v --bench-scale=stress -m stress
```

## Scale Levels

| Level   | Drawers | Wings | Rooms/Wing | KG Triples | Use case            |
|---------|---------|-------|------------|------------|---------------------|
| small   | 1,000   | 3     | 5          | 200        | CI, quick checks    |
| medium  | 10,000  | 8     | 12         | 2,000      | Pre-release testing |
| large   | 50,000  | 15    | 20         | 10,000     | Scale limit finding |
| stress  | 100,000 | 25    | 30         | 50,000     | Breaking point      |

## Test Modules

### Critical Path

| File | What it tests |
|------|--------------|
| `test_mcp_bench.py` | MCP tool response times, unbounded metadata fetch, client re-instantiation overhead |
| `test_chromadb_stress.py` | ChromaDB breaking point, query degradation curve, batch vs sequential insert |
| `test_memory_profile.py` | RSS/heap growth over repeated operations, leak detection |

### Performance Baselines

| File | What it tests |
|------|--------------|
| `test_ingest_bench.py` | Mining throughput (files/sec, drawers/sec), peak RSS, chunking speed, re-ingest skip overhead |
| `test_search_bench.py` | Query latency vs palace size, recall@k with planted needles, concurrent queries, n_results scaling |

### Architectural Validation

| File | What it tests |
|------|--------------|
| `test_palace_boost.py` | Retrieval improvement from wing/room filtering at different scales |
| `test_recall_threshold.py` | Per-room recall ceiling — isolates embedding model limit with all drawers in one bucket |
| `test_knowledge_graph_bench.py` | Triple insertion rate, temporal query accuracy, SQLite concurrent access |
| `test_layers_bench.py` | MemoryStack wake-up cost, Layer1 unbounded fetch, token budget compliance |

## Architecture

```
tests/benchmarks/
  conftest.py              # --bench-scale / --bench-report CLI options, fixtures, markers
  data_generator.py        # Deterministic data factory (seeded RNG, planted needles)
  report.py                # JSON report writer + regression checker
  test_*.py                # 9 test modules (106 tests total)
```

### Data Generator

`PalaceDataGenerator(seed=42, scale="small")` produces deterministic, realistic test data:

- **`generate_project_tree()`** — writes real files + `mempalace.yaml` for `mine()` to ingest
- **`populate_palace_directly()`** — bypasses mining, inserts directly into ChromaDB (10-100x faster for search/MCP benchmarks)
- **`generate_kg_triples()`** — entity-relationship triples with temporal validity
- **`generate_search_queries()`** — queries with known-good answers for recall measurement

**Planted needles**: Unique identifiable content (e.g., `NEEDLE_0042: PostgreSQL vacuum autovacuum threshold...`) seeded into specific wings/rooms. Search queries target these needles, enabling recall@k measurement without an LLM judge.

### JSON Reports

When run with `--bench-report=path.json`, produces machine-readable output:

```json
{
  "timestamp": "2026-04-07T...",
  "git_sha": "abc123",
  "scale": "small",
  "system": {"os": "linux", "cpu_count": 8},
  "results": {
    "mcp_status": {"latency_ms_at_1000": 45.2, "rss_delta_mb_at_5000": 12.3},
    "search": {"avg_latency_ms_at_5000": 23.1, "recall_at_5": 0.92},
    "chromadb_insert": {"sequential_ms": 8500, "batched_ms": 1200, "speedup_ratio": 7.1}
  }
}
```

### Regression Detection

```python
from tests.benchmarks.report import check_regression

regressions = check_regression("current.json", "baseline.json", threshold=0.2)
# Returns list of metric descriptions that degraded beyond 20%
```

## CI Integration

The GitHub Actions workflow runs benchmarks on PRs at small scale:

```yaml
benchmark:
  runs-on: ubuntu-latest
  if: github.event_name == 'pull_request'
  # Runs: pytest tests/benchmarks/ -m "benchmark and not stress and not slow" --bench-scale=small
```

Existing unit tests are isolated with `--ignore=tests/benchmarks`.

## Markers

- `@pytest.mark.benchmark` — all benchmark tests
- `@pytest.mark.slow` — tests taking >30s even at small scale
- `@pytest.mark.stress` — tests that should only run at large/stress scale

## Dependencies

Only one new dependency beyond the existing dev stack: `psutil` (for cross-platform RSS measurement). `tracemalloc` and `resource` are stdlib.
