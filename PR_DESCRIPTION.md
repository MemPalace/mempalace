## What does this PR do?

Adds two deduplication features to prevent **embedding reinforcement** — the silent accumulation of near-identical content that degrades semantic search quality over time.

### 1. Diary write dedup guard (`tool_diary_write`)

`tool_diary_write` now performs a cosine similarity check (`≥ 0.92`) before inserting a new entry. If a near-identical document already exists in the palace, the write is rejected with a structured response:

```json
{
  "success": false,
  "reason": "duplicate_diary_entry",
  "message": "A near-identical diary entry already exists.",
  "matches": [...]
}
```

**Why this matters:** Diary entries are written automatically by hooks and agents. Without dedup, repeated saves or session summaries with trivial variations (e.g. `★★★` vs `★★`) create N copies of the same embedding. ChromaDB's default distance metric treats these as equally relevant results, flooding search with redundant content instead of diverse matches.

The threshold of `0.92` was chosen to be strict enough to catch semantically identical content while allowing genuinely different entries about similar topics to coexist. This is consistent with the existing `0.9` threshold in `tool_add_drawer`.

### 2. Palace-wide duplicate report (`mempalace_dedup_report`)

New read-only diagnostic tool that scans the palace for near-duplicate clusters:

```
mempalace_dedup_report(threshold=0.92, wing="optional", limit=1000)
```

Returns structured output:
- **Clusters**: Each cluster has an "anchor" document and its near-duplicates
- **Similarity scores**: Per-pair cosine similarity
- **Metadata**: Wing, room, `filed_at` timestamps for triage
- **Previews**: First 150 chars of each document

This is intentionally **read-only** — it reports but does not delete. Users can review clusters and decide which drawers to remove via `mempalace_delete_drawer`.

Supports optional `wing` filter to scope scans to a specific wing (useful for large palaces), and configurable `threshold` for sensitivity control.

## Technical details

- **No new dependencies.** Uses existing `tool_check_duplicate` internally.
- **Performance:** `tool_dedup_report` issues one `col.query()` per unique document. For a 1000-drawer palace this is ~1000 queries against the local ChromaDB — typically completes in under 5 seconds on modern hardware. The `limit` parameter caps scan scope.
- **Clustering is greedy:** Once a document is assigned to a cluster (as anchor or duplicate), it's excluded from further matching via a `seen` set. This prevents O(n²) blowup and avoids reporting the same pair in multiple clusters.

## How to test

```bash
uv run pytest tests/test_dedup.py -v        # 9 new tests
uv run pytest tests/ -v                      # 110 total, 0 regressions
uv run ruff check mempalace/mcp_server.py tests/test_dedup.py   # clean
```

## Test coverage

| Test | What it verifies |
|------|-----------------|
| `test_exact_duplicate_rejected` | Identical diary content blocked on second write |
| `test_near_duplicate_rejected` | Trivially varied content (e.g. `★★★` → `★★`) blocked |
| `test_distinct_entry_accepted` | Genuinely different content passes dedup |
| `test_different_agents_can_write_similar` | Cross-agent dedup works (same fact = same embedding) |
| `test_report_on_clean_palace` | No false positives on distinct documents |
| `test_report_finds_duplicates` | Exact duplicates detected and clustered |
| `test_report_wing_filter` | Wing scoping restricts scan correctly |
| `test_report_empty_palace` | Empty palace returns zeros without error |
| `test_report_threshold_sensitivity` | Lower threshold catches more near-duplicates |

## Checklist
- [x] Tests pass (`python -m pytest tests/ -v`)
- [x] No hardcoded paths
- [x] Linter passes (`ruff check .`)

---

> **A note of thanks.** We came across MemPalace while researching memory architectures for our own project and were genuinely impressed by the engineering quality — the 4-layer token loading, the temporal KG, the entity registry. While auditing the codebase, we noticed that diary writes lacked the same dedup guard that `tool_add_drawer` already has, and that there was no way to diagnose accumulated redundancy across the palace. Consider this PR a *señal de pago* — a down payment for the ideas we've borrowed. Good engineering deserves to be repaid in kind. 🤝
