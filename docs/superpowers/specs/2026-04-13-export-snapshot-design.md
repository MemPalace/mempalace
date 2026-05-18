# Export Snapshot Design

## Summary

MemPalace now includes a first-class snapshot export workflow for human-readable, archive-friendly markdown output.

The feature is exposed through:

```bash
mempalace export <output_dir>
```

It builds on the existing exporter and preserves verbatim room content while adding snapshot-level navigation and metadata files.

## Implemented Scope

The implemented snapshot export supports:

- `mempalace export <output_dir>`
- `--palace <path>`
- `--snapshot-name <name>`
- `--wing <name>`
- Timestamped snapshot directories when `--snapshot-name` is omitted
- `overview.md`
- `manifest.json`
- Snapshot root `index.md`
- Per-wing `index.md`
- Existing room-level verbatim markdown files

The feature is additive. It does not change storage layout, drawer contents, search behavior, mining behavior, MCP behavior, or knowledge graph behavior.

## Command Behavior

### CLI

`mempalace/cli.py` provides an `export` subcommand that resolves the palace path using the same pattern as the other CLI commands and delegates the work to `export_snapshot()` in `mempalace/exporter.py`.

### Output structure

Running:

```bash
mempalace export ./exports
```

creates a timestamped snapshot directory like:

```text
./exports/
  2026-04-13_145200/
    overview.md
    manifest.json
    index.md
    alpha/
      index.md
      backend.md
      frontend.md
    beta/
      index.md
      docs.md
```

When `--snapshot-name` is passed, that name is used directly as the snapshot directory.

When `--wing` is passed, only that wing is exported and the generated statistics are scoped to the filtered output.

## Exporter Design

`mempalace/exporter.py` now has two public entry points:

- `export_palace()` for the original markdown tree export
- `export_snapshot()` for snapshot exports

Shared logic is kept inside internal helpers so both export styles reuse the same room-writing behavior.

### Shared export flow

The exporter:

1. Opens the palace collection
2. Streams drawers in paginated batches
3. Groups drawers by wing and room
4. Writes room markdown files incrementally
5. Accumulates summary counts for wings and rooms

This keeps memory use bounded while preserving the existing room markdown format.

### Snapshot-specific files

`export_snapshot()` adds:

- `overview.md`
  Human-readable summary with snapshot identity, filters, totals, and links to each wing
- `manifest.json`
  Machine-readable metadata including snapshot name, export timestamp, version, filters, and aggregated counts
- Snapshot root `index.md`
  Navigation into exported wings
- Per-wing `index.md`
  Navigation into exported room files

### Room markdown compatibility

Room markdown files keep the existing export format:

- `# wing / room` heading
- one section per drawer
- blockquoted verbatim content
- metadata table for source, filed time, and added-by

This keeps existing exported room content stable while allowing richer snapshot output above it.

## Error Handling

The implemented behavior is:

- empty palace exports return zero counts
- explicit `--snapshot-name` collisions raise `FileExistsError`
- generated timestamp collisions are resolved by appending a numeric suffix
- path components continue to be sanitized before writing directories or files

## Files Changed

The snapshot export feature is implemented through changes in:

- `mempalace/cli.py`
- `mempalace/exporter.py`
- `tests/test_cli.py`
- `tests/test_exporter.py`

## Validation

This implementation was verified with:

```bash
C:\Users\SZGF\AppData\Local\Programs\Python\Python311\python.exe -m pytest tests -v
C:\Users\SZGF\AppData\Local\Programs\Python\Python311\python.exe -m ruff check mempalace/exporter.py mempalace/cli.py tests/test_exporter.py tests/test_cli.py
```

The branch-level verification completed successfully at the time of writing, including full-suite test coverage.
