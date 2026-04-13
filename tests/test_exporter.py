import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from mempalace.exporter import export_palace, export_snapshot


class FakeCollection:
    def __init__(self, rows):
        self._rows = rows

    def count(self):
        return len(self._rows)

    def get(self, limit=1000, offset=0, include=None, where=None):
        rows = self._rows
        if where and "wing" in where:
            rows = [row for row in rows if row["metadata"].get("wing") == where["wing"]]

        batch = rows[offset : offset + limit]
        return {
            "ids": [row["id"] for row in batch],
            "documents": [row["document"] for row in batch],
            "metadatas": [row["metadata"] for row in batch],
        }


def _seed_rows():
    return [
        {
            "id": "drawer_alpha_backend_1",
            "document": "def serve():\n    return 'ok'\n",
            "metadata": {
                "wing": "alpha",
                "room": "backend",
                "source_file": "server.py",
                "filed_at": "2026-04-13T10:00:00",
                "added_by": "miner",
            },
        },
        {
            "id": "drawer_alpha_frontend_1",
            "document": "function render() { return 'hi'; }\n",
            "metadata": {
                "wing": "alpha",
                "room": "frontend",
                "source_file": "app.js",
                "filed_at": "2026-04-13T10:05:00",
                "added_by": "miner",
            },
        },
        {
            "id": "drawer_beta_docs_1",
            "document": "# Guide\n\nThis explains things.\n",
            "metadata": {
                "wing": "beta",
                "room": "docs",
                "source_file": "guide.md",
                "filed_at": "2026-04-13T10:10:00",
                "added_by": "miner",
            },
        },
    ]


def test_export_creates_structure():
    tmpdir = tempfile.mkdtemp()
    try:
        output_dir = os.path.join(tmpdir, "export")
        with patch("mempalace.exporter.get_collection", return_value=FakeCollection(_seed_rows())):
            stats = export_palace("/fake/palace", output_dir)

        assert stats["wings"] == 2
        assert stats["rooms"] == 3
        assert stats["drawers"] == 3
        assert os.path.isfile(os.path.join(output_dir, "index.md"))
        assert os.path.isdir(os.path.join(output_dir, "alpha"))
        assert os.path.isdir(os.path.join(output_dir, "beta"))
        assert os.path.isfile(os.path.join(output_dir, "alpha", "backend.md"))
        assert os.path.isfile(os.path.join(output_dir, "alpha", "frontend.md"))
        assert os.path.isfile(os.path.join(output_dir, "beta", "docs.md"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_markdown_content():
    tmpdir = tempfile.mkdtemp()
    try:
        output_dir = os.path.join(tmpdir, "export")
        with patch("mempalace.exporter.get_collection", return_value=FakeCollection(_seed_rows())):
            export_palace("/fake/palace", output_dir)

        backend_md = Path(output_dir) / "alpha" / "backend.md"
        content = backend_md.read_text(encoding="utf-8")

        assert content.startswith("# alpha / backend\n")
        assert "## drawer_alpha_backend_1" in content
        assert "| Field | Value |" in content
        assert "| Source | server.py |" in content
        assert "| Filed | 2026-04-13T10:00:00 |" in content
        assert "| Added by | miner |" in content
        assert "---" in content
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_index_content():
    tmpdir = tempfile.mkdtemp()
    try:
        output_dir = os.path.join(tmpdir, "export")
        with patch("mempalace.exporter.get_collection", return_value=FakeCollection(_seed_rows())):
            export_palace("/fake/palace", output_dir)

        index_md = Path(output_dir) / "index.md"
        content = index_md.read_text(encoding="utf-8")

        assert "# Palace Export" in content
        assert "| Wing | Rooms | Drawers |" in content
        assert "[alpha](alpha/)" in content
        assert "[beta](beta/)" in content
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_empty_palace():
    tmpdir = tempfile.mkdtemp()
    try:
        output_dir = os.path.join(tmpdir, "export")
        with patch("mempalace.exporter.get_collection", return_value=FakeCollection([])):
            stats = export_palace("/fake/palace", output_dir)

        assert stats == {"wings": 0, "rooms": 0, "drawers": 0}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _try_symlink_or_skip(target: str, link: str):
    """Create a symlink, skipping the test if the runtime forbids it.

    Windows without Developer Mode/admin and some restricted CI sandboxes
    refuse os.symlink with OSError or NotImplementedError. The exporter
    hardening is meaningful only where symlinks can be created at all, so
    skipping is preferable to a hard failure.
    """
    import pytest

    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"symlink creation not supported in this environment: {e}")


def test_export_refuses_symlinked_output_dir():
    """A symlink at the output path must not be followed (defense-in-depth)."""
    import pytest

    tmpdir = tempfile.mkdtemp()
    try:
        decoy_target = os.path.join(tmpdir, "decoy_target")
        os.makedirs(decoy_target)
        output_dir = os.path.join(tmpdir, "export")
        _try_symlink_or_skip(decoy_target, output_dir)

        with patch("mempalace.exporter.get_collection", return_value=FakeCollection(_seed_rows())):
            with pytest.raises(ValueError, match="symbolic link"):
                export_palace("/fake/palace", output_dir)

        # Decoy target must remain empty — nothing followed the symlink.
        assert os.listdir(decoy_target) == []


def test_export_snapshot_creates_snapshot_artifacts():
    tmpdir = tempfile.mkdtemp()
    try:
        output_dir = os.path.join(tmpdir, "exports")
        with patch("mempalace.exporter.get_collection", return_value=FakeCollection(_seed_rows())):
            result = export_snapshot(
                palace_path="/fake/palace",
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


def test_export_refuses_symlinked_wing_dir():
    """A symlink pre-placed at a wing subdirectory must also be refused."""
    import pytest

    tmpdir = tempfile.mkdtemp()
    try:
        decoy_target = os.path.join(tmpdir, "decoy_target")
        os.makedirs(decoy_target)
        output_dir = os.path.join(tmpdir, "export")
        os.makedirs(output_dir)
        _try_symlink_or_skip(decoy_target, os.path.join(output_dir, "alpha"))

        with patch("mempalace.exporter.get_collection", return_value=FakeCollection(_seed_rows())):
            with pytest.raises(ValueError, match="symbolic link"):
                export_palace("/fake/palace", output_dir)

        assert os.listdir(decoy_target) == []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_refuses_symlinked_room_file():
    """A symlink pre-placed at a room file path must not be followed."""
    import pytest

    tmpdir = tempfile.mkdtemp()
    try:
        decoy_target = os.path.join(tmpdir, "decoy_target.md")
        Path(decoy_target).write_text("untouched\n", encoding="utf-8")
        output_dir = os.path.join(tmpdir, "export")
        os.makedirs(os.path.join(output_dir, "alpha"))
        _try_symlink_or_skip(decoy_target, os.path.join(output_dir, "alpha", "backend.md"))

        with patch("mempalace.exporter.get_collection", return_value=FakeCollection(_seed_rows())):
            with pytest.raises(ValueError, match="symbolic link"):
                export_palace("/fake/palace", output_dir)

        # Decoy file must remain unchanged — open did not follow the symlink.
        assert Path(decoy_target).read_text(encoding="utf-8") == "untouched\n"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_refuses_symlinked_index_file():
    """A symlink pre-placed at output_dir/index.md must not be followed."""
    import pytest

    tmpdir = tempfile.mkdtemp()
    try:
        decoy_target = os.path.join(tmpdir, "decoy_index.md")
        Path(decoy_target).write_text("untouched\n", encoding="utf-8")
        output_dir = os.path.join(tmpdir, "export")
        os.makedirs(output_dir)
        _try_symlink_or_skip(decoy_target, os.path.join(output_dir, "index.md"))

        with patch("mempalace.exporter.get_collection", return_value=FakeCollection(_seed_rows())):
            with pytest.raises(ValueError, match="symbolic link"):
                export_palace("/fake/palace", output_dir)

        assert Path(decoy_target).read_text(encoding="utf-8") == "untouched\n"
def test_export_snapshot_manifest_and_overview_are_scoped_to_wing():
    tmpdir = tempfile.mkdtemp()
    try:
        output_dir = os.path.join(tmpdir, "exports")
        with patch("mempalace.exporter.get_collection", return_value=FakeCollection(_seed_rows())):
            result = export_snapshot(
                palace_path="/fake/palace",
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
