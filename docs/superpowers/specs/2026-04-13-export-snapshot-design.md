# Export Snapshot Design

## Summary

Add a first-class export snapshot feature that produces a human-readable, archive-friendly view of a MemPalace palace without changing stored memory contents or storage layout.

The feature will expose a new CLI command, `mempalace export <output_dir>`, and build on the existing markdown exporter in `mempalace/exporter.py`. The output will be a timestamped snapshot directory containing:

- `overview.md` for human-first summary and navigation
- `manifest.json` for machine-readable snapshot metadata
- `index.md` at the snapshot root
- `index.md` inside each exported wing
- Existing room-level markdown files containing verbatim drawer content

This is an observability feature, not a retrieval or storage feature. It must preserve MemPalace's verbatim guarantee and local-first model.

## Goals

- Make palace exports easier for humans to read, browse, and archive
- Preserve verbatim drawer content exactly as exported today
- Reuse the current exporter implementation and file layout as much as possible
- Add a stable machine-readable snapshot manifest for future tooling
- Keep the first version additive and backward compatible

## Non-Goals

- No changes to ChromaDB or SQLite schema
- No HTML export
- No snapshot diffing against previous exports
- No AI-generated summaries or lossy condensation
- No changes to mining, search, MCP tools, or knowledge graph behavior

## Current State

`mempalace/exporter.py` already exports the palace as markdown files grouped by wing and room and writes a root `index.md`. `tests/test_exporter.py` covers this basic behavior.

The CLI does not currently expose export as a formal command, so export is available as code but not as a stable user-facing workflow. The current output is useful as raw data, but it lacks:

- A snapshot identity for archiving
- A human-first overview page
- Wing-level navigation pages
- A machine-readable manifest for future comparison or automation

## User Experience

### Command

```bash
mempalace export <output_dir>
```

### Optional arguments

- `--palace <path>`: export from a non-default palace path
- `--snapshot-name <name>`: use a caller-provided snapshot directory name
- `--wing <name>`: export only one wing

### Default behavior

Running `mempalace export ./exports` creates:

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

If `--snapshot-name` is omitted, the command generates a timestamp-based name in local time using `YYYY-MM-DD_HHMMSS`.

If `--wing` is provided, the snapshot contains only that wing and all summary statistics are scoped to the filtered export, not the whole palace.

## Detailed Design

### CLI changes

`mempalace/cli.py` will gain:

- A new `cmd_export(args)` handler
- A new `export` subcommand in argument parsing

The handler will:

1. Resolve `palace_path` using existing CLI conventions
2. Resolve `output_dir`
3. Pass `palace_path`, `output_dir`, `snapshot_name`, and optional `wing` filter into a new snapshot export function
4. Print the generated snapshot path and summary stats

The command should follow the style of existing CLI commands such as `status`, `search`, and `split`.

### Exporter changes

`mempalace/exporter.py` will be extended with a new high-level function:

```python
export_snapshot(
    palace_path: str,
    output_dir: str,
    snapshot_name: str | None = None,
    wing: str | None = None,
) -> dict
```

This function will:

1. Build the final snapshot directory path
2. Query the palace collection
3. Stream drawers in batches, as the current exporter already does
4. Write room-level markdown files with the current verbatim format
5. Write wing-level `index.md` files
6. Write snapshot root `index.md`
7. Write `overview.md`
8. Write `manifest.json`
9. Return structured stats including the final snapshot path

The existing `export_palace()` function should remain available. To minimize duplication, shared batching and write logic should be factored into small internal helpers inside `exporter.py`, but only where that meaningfully reduces duplication for snapshot export. This is a targeted extraction, not a general refactor.

### Output file responsibilities

#### `manifest.json`

Machine-readable metadata for scripting and future diff support.

Proposed structure:

```json
{
  "snapshot_name": "2026-04-13_145200",
  "exported_at": "2026-04-13T14:52:00+08:00",
  "mempalace_version": "3.1.0",
  "palace_path": "/abs/path/to/palace",
  "format": "markdown_snapshot",
  "filters": {
    "wing": null
  },
  "stats": {
    "wings": 2,
    "rooms": 3,
    "drawers": 42
  },
  "wings": [
    {
      "name": "alpha",
      "rooms": 2,
      "drawers": 30
    },
    {
      "name": "beta",
      "rooms": 1,
      "drawers": 12
    }
  ]
}
```

Field expectations:

- `snapshot_name`: final directory name
- `exported_at`: local ISO-8601 timestamp
- `mempalace_version`: from `mempalace.version`
- `palace_path`: absolute path used for export
- `format`: fixed identifier for this output shape
- `filters`: currently only wing filtering
- `stats`: counts scoped to the exported data set
- `wings`: sorted wing summary list

#### `overview.md`

Human-first entry point with:

- Snapshot identity
- Export time
- Palace path
- Active filters
- Total counts
- A wing summary table with links

This file should not summarize drawer content. It should only describe exported structure and navigation.

#### Root `index.md`

Compact navigation page that lists exported wings and links into each wing directory. This remains close to the existing root index behavior, but in the snapshot layout rather than directly under the caller's output directory.

#### Wing `index.md`

A new per-wing index with:

- Wing name
- Room counts
- Drawer counts per room
- Links to room markdown files

#### Room markdown files

Continue using the current exporter format:

- Header for `wing / room`
- One section per drawer
- Blockquoted verbatim content
- Metadata table

This protects backward compatibility for anyone already consuming exported room files.

## Data Flow

1. CLI resolves palace and output arguments
2. Exporter opens the Chroma collection
3. Exporter streams drawers in paginated batches
4. Each batch is grouped by wing and room
5. Room markdown files are appended incrementally
6. In-memory counters accumulate summary stats per wing and room
7. After streaming completes, exporter writes overview and index files from accumulated stats
8. Exporter writes `manifest.json`
9. CLI prints final snapshot path and stats

This keeps memory usage proportional to batch size plus summary metadata, not total drawer volume.

## Error Handling

- Empty palace: preserve current behavior and return zero stats without writing a misleading partial snapshot
- Missing palace: surface the same style of error the existing exporter and CLI already use
- Invalid wing filter: export succeeds with zero stats if no matching drawers exist, but the overview and manifest must make the filter explicit
- Existing snapshot directory collision:
  - If the generated timestamp directory already exists, append a numeric suffix
  - If the user supplies `--snapshot-name` and the target exists, fail clearly rather than silently merging content
- Unsafe filenames: continue using the existing path component sanitizer

## Compatibility

This design is additive:

- Existing `export_palace()` callers continue to work
- Existing room markdown shape remains intact
- No storage or schema changes
- No behavior changes to search, mine, repair, MCP, or KG modules

The only new user-facing surface is the CLI `export` command and the richer snapshot output layout when that command is used.

## Risks

### Large palace exports

`overview.md` and wing indexes may become long in very large palaces. This is acceptable for v1 because they remain navigational documents, not full-content dumps.

### Manifest stability

Once `manifest.json` ships, downstream scripts may start depending on it. The initial field set should therefore stay small and deliberate.

### Path disclosure

Including absolute `palace_path` helps debugging and archival provenance, but it also exposes local paths inside the snapshot. This is acceptable for local-first storage and human archiving, but future work may add optional path redaction.

## Testing

### Unit and integration coverage

Update tests to cover:

- CLI dispatch for `mempalace export`
- Snapshot directory creation with generated timestamp
- Snapshot directory creation with explicit `--snapshot-name`
- Wing-filtered export
- `overview.md` content
- `manifest.json` content and scoped stats
- Wing-level `index.md` generation
- Existing room markdown content remains unchanged
- Collision handling for generated snapshot names

### Regression protection

Existing exporter tests should remain and continue to validate the base markdown room format.

## Implementation Plan Preview

Expected files to change:

- `mempalace/cli.py`
- `mempalace/exporter.py`
- `tests/test_cli.py`
- `tests/test_exporter.py`

No new runtime dependencies are required.

## Future Extensions

The snapshot structure intentionally leaves room for later additions without rethinking the model:

- Snapshot diff reports
- JSON-only export mode
- Optional path redaction
- Source file distribution tables
- Last-filed-at summaries when reliable metadata is present
