# Export Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-class `mempalace export` CLI command that creates timestamped markdown snapshot exports with overview and manifest files.

**Architecture:** Extend `mempalace/exporter.py` with a new snapshot-oriented export path while preserving the existing room markdown format and `export_palace()` behavior. Wire the new capability into `mempalace/cli.py` and validate it with focused exporter and CLI tests.

**Tech Stack:** Python 3.9+, argparse, filesystem I/O, Chroma-backed exporter helpers, pytest

---

### Task 1: Add exporter snapshot tests first

**Files:**
- Modify: `tests/test_exporter.py`
- Test: `tests/test_exporter.py`

- [ ] **Step 1: Write the failing tests**

```python
import json

from mempalace.exporter import export_palace, export_snapshot


def test_export_snapshot_creates_snapshot_artifacts():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        output_dir = os.path.join(tmpdir, "exports")

        result = export_snapshot(
            palace_path=palace_path,
            output_dir=output_dir,
            snapshot_name="snapshot-1",
        )

        snapshot_dir = Path(result["snapshot_path"])
        assert snapshot_dir.name == "snapshot-1"
        assert (snapshot_dir / "overview.md").is_file()
        assert (snapshot_dir / "manifest.json").is_file()
        assert (snapshot_dir / "index.md").is_file()
        assert (snapshot_dir / "alpha" / "index.md").is_file()
        assert (snapshot_dir / "alpha" / "backend.md").is_file()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_snapshot_manifest_and_overview_are_scoped_to_wing():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        output_dir = os.path.join(tmpdir, "exports")

        result = export_snapshot(
            palace_path=palace_path,
            output_dir=output_dir,
            snapshot_name="alpha-only",
            wing="alpha",
        )

        snapshot_dir = Path(result["snapshot_path"])
        manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        overview = (snapshot_dir / "overview.md").read_text(encoding="utf-8")
        wing_index = (snapshot_dir / "alpha" / "index.md").read_text(encoding="utf-8")

        assert manifest["filters"] == {"wing": "alpha"}
        assert manifest["stats"]["wings"] == 1
        assert manifest["wings"][0]["name"] == "alpha"
        assert not (snapshot_dir / "beta").exists()
        assert "# Palace Snapshot" in overview
        assert "alpha" in overview
        assert "beta" not in overview
        assert "# Wing Export — alpha" in wing_index
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_exporter.py -v`
Expected: FAIL because `export_snapshot` does not exist yet

- [ ] **Step 3: Write minimal implementation**

Implement snapshot export in `mempalace/exporter.py` by:

```python
def export_snapshot(...):
    snapshot_path = _build_snapshot_path(...)
    stats = _export_markdown_tree(...)
    _write_snapshot_root_index(...)
    _write_snapshot_overview(...)
    _write_snapshot_manifest(...)
    return {**stats, "snapshot_path": snapshot_path}
```

Keep `export_palace()` working by routing it through shared helpers rather than changing room markdown output.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_exporter.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_exporter.py mempalace/exporter.py
git commit -m "feat: add export snapshot files"
```

### Task 2: Add CLI tests first

**Files:**
- Modify: `tests/test_cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
from mempalace.cli import cmd_export


@patch("mempalace.cli.MempalaceConfig")
def test_cmd_export_calls_export_snapshot(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/fake/palace"
    args = argparse.Namespace(
        palace=None,
        output_dir="/tmp/export",
        snapshot_name="snapshot-1",
        wing="alpha",
    )
    with patch("mempalace.exporter.export_snapshot") as mock_export:
        mock_export.return_value = {"snapshot_path": "/tmp/export/snapshot-1", "drawers": 2}
        cmd_export(args)
        mock_export.assert_called_once_with(
            palace_path="/fake/palace",
            output_dir="/tmp/export",
            snapshot_name="snapshot-1",
            wing="alpha",
        )


def test_main_export_dispatches():
    with (
        patch("sys.argv", ["mempalace", "export", "/tmp/export"]),
        patch("mempalace.cli.cmd_export") as mock_cmd,
    ):
        main()
        mock_cmd.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL because `cmd_export` and the `export` subcommand do not exist yet

- [ ] **Step 3: Write minimal implementation**

Implement in `mempalace/cli.py`:

```python
def cmd_export(args):
    from .exporter import export_snapshot

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    result = export_snapshot(
        palace_path=palace_path,
        output_dir=args.output_dir,
        snapshot_name=args.snapshot_name,
        wing=args.wing,
    )
    print(f"  Snapshot: {result['snapshot_path']}")
```

Add the `export` argparse subcommand and include it in the dispatch table.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_cli.py mempalace/cli.py
git commit -m "feat: add export snapshot cli command"
```

### Task 3: Run focused regression verification

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `tests/test_exporter.py`
- Modify: `mempalace/cli.py`
- Modify: `mempalace/exporter.py`

- [ ] **Step 1: Run focused regression tests**

Run: `python -m pytest tests/test_exporter.py tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 2: Run formatter-safe validation**

Run: `python -m ruff check mempalace/exporter.py mempalace/cli.py tests/test_exporter.py tests/test_cli.py`
Expected: PASS

- [ ] **Step 3: Commit final verification-ready changes**

```bash
git add mempalace/cli.py mempalace/exporter.py tests/test_exporter.py tests/test_cli.py docs/superpowers/plans/2026-04-13-export-snapshot.md
git commit -m "feat: add snapshot export workflow"
```
