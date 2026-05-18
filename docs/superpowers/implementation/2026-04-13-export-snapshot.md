# Export Snapshot Implementation

## Overview

This document describes the implemented export snapshot feature in MemPalace.

The goal of the feature is to make palace exports easier for humans to read and archive without changing the underlying storage model or the verbatim drawer guarantee.

## User-facing behavior

The new CLI entry point is:

```bash
mempalace export <output_dir>
```

Supported options:

- `--palace <path>`
- `--snapshot-name <name>`
- `--wing <name>`

### What the command writes

The export command creates a snapshot directory that contains:

- `overview.md`
- `manifest.json`
- snapshot root `index.md`
- one `index.md` file per wing
- room markdown files with verbatim drawer content

Example structure:

```text
exports/
  2026-04-13_145200/
    overview.md
    manifest.json
    index.md
    alpha/
      index.md
      backend.md
      frontend.md
```

## Implementation details

### CLI integration

`mempalace/cli.py` adds:

- an `export` subcommand
- `cmd_export(args)`
- dispatch wiring into the main CLI command table

The CLI resolves the palace path using the same approach as other commands and calls `export_snapshot()`.

### Exporter structure

`mempalace/exporter.py` now separates shared export work from snapshot-specific file generation.

Shared helpers are responsible for:

- paginating collection reads
- grouping drawers by wing and room
- writing room markdown files
- collecting wing and room summary counts

Snapshot-specific helpers are responsible for:

- building the snapshot path
- writing `overview.md`
- writing `manifest.json`
- writing root and wing indexes

### Manifest contents

`manifest.json` includes:

- snapshot name
- export timestamp
- MemPalace version
- palace path
- active filters
- total counts
- per-wing counts

### Compatibility

The room markdown format is unchanged.

`export_palace()` remains available and continues to export the original markdown tree layout.

## Tests and verification

### Feature tests

The snapshot workflow is covered by:

- `tests/test_exporter.py`
- `tests/test_cli.py`

These tests verify:

- snapshot artifact creation
- wing-scoped exports
- manifest structure
- overview content
- CLI dispatch

### Test reliability fixes included in this branch

To make full-suite verification pass in the current Windows offline environment, this branch also includes test-only fixes in:

- `tests/test_onboarding.py`
- `tests/test_convo_miner.py`

Those changes do not alter the export snapshot runtime behavior.

### Verification commands

The branch was validated with:

```bash
C:\Users\SZGF\AppData\Local\Programs\Python\Python311\python.exe -m pytest tests -v
C:\Users\SZGF\AppData\Local\Programs\Python\Python311\python.exe -m ruff check tests/test_onboarding.py tests/test_convo_miner.py
C:\Users\SZGF\AppData\Local\Programs\Python\Python311\python.exe -m ruff check mempalace/exporter.py mempalace/cli.py tests/test_exporter.py tests/test_cli.py
```

## Files involved

Primary implementation files:

- `mempalace/cli.py`
- `mempalace/exporter.py`
- `tests/test_cli.py`
- `tests/test_exporter.py`

Supporting full-suite test stabilization files:

- `tests/test_onboarding.py`
- `tests/test_convo_miner.py`
