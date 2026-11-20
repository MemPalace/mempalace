# Upgrade compatibility & `DIVERGED`-index recovery (v3.0.x → v3.9.x)

What is and isn't affected by the upgrade, and how to recover a palace whose
vector index reports `DIVERGED` right after you upgrade.

## `search … --wing` is stable across v3.0.x → v3.9.x

`mempalace search "<query>" --wing <name>` (and `--room`) keeps the same
shape and meaning from v3.0.0 through the current v3.9.x line. External
callers and legacy CLI wrappers that shell out to these commands need **no
source changes**: the flags are still accepted, still filter to one project,
and still return hybrid results (vector similarity re-ranked with BM25).

## A `DIVERGED` index right after upgrading is expected, not a bug

Immediately after a package upgrade, a large palace can briefly report
`repair-status` = `DIVERGED` — the SQLite row count is ahead of the flushed
HNSW vector count (flush-lag plus a rebuild boundary). In the reporter's case
that was 119,862 SQLite rows → 119,000 HNSW vectors → an 862-row gap (~0.7%).
A small residual gap like this is **within flush-lag tolerance and safe**; the
missing vectors are recent writes that haven't hit the next `sync_threshold`
flush yet. During that window `search` stays correct by falling back to
BM25-only SQLite results (the vector leg is fenced off to protect ChromaDB).

## Recommended recovery, if you want the vector leg back

Rebuild from SQLite — do **not** re-mine (re-mining drops MCP-added drawers
and diary entries, which have no source file):

```bash
mempalace repair --mode from-sqlite --archive-existing --yes
mempalace repair-status   # should now show the gap cleared
```

`--mode from-sqlite` reads rows straight from `chroma.sqlite3` (never opening
a ChromaDB client against the diverged palace), re-embeds under your configured
embedding function, rebuilds FTS5, VACUUMs, and requires a clean final
`PRAGMA quick_check`. `--archive-existing` renames the current palace to
`<palace>.pre-rebuild-<timestamp>` first, so nothing is lost.

## Cross-references

- `CHANGELOG.md` (3.9.0) — "Diverged-index recovery now points at
  `repair --mode from-sqlite`, not a re-mine"; "SQLite recovery is bounded,
  atomic, and honest."
- `.claude-plugin/skills/mempalace-recall/SKILL.md` — "Palace index corrupt /
  compactor error" (the same command, in the agent-facing skill).
- `docs/recovery/index-metadata-recovery.md` — the *distinct* `dimensionality:
  None` segment-quarantine case (a different corruption shape, not this one).
