import os
import shutil
import tempfile
from pathlib import Path

import yaml

from mempalace.miner import mine
from mempalace.exporter import export_palace


def write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _setup_palace(tmpdir):
    """Create a small palace with drawers across two wings for testing."""
    project_a = Path(tmpdir) / "project_a"
    project_b = Path(tmpdir) / "project_b"
    palace_path = str(Path(tmpdir) / "palace")

    # Project A: wing=alpha, rooms=backend,frontend
    os.makedirs(project_a / "backend")
    os.makedirs(project_a / "frontend")
    write_file(project_a / "backend" / "server.py", "def serve():\n    return 'ok'\n" * 20)
    write_file(project_a / "frontend" / "app.js", "function render() { return 'hi'; }\n" * 20)
    with open(project_a / "mempalace.yaml", "w") as f:
        yaml.dump(
            {
                "wing": "alpha",
                "rooms": [
                    {"name": "backend", "description": "Backend code"},
                    {"name": "frontend", "description": "Frontend code"},
                ],
            },
            f,
        )

    # Project B: wing=beta, rooms=docs
    os.makedirs(project_b / "docs")
    write_file(project_b / "docs" / "guide.md", "# Guide\n\nThis explains things.\n" * 20)
    with open(project_b / "mempalace.yaml", "w") as f:
        yaml.dump(
            {
                "wing": "beta",
                "rooms": [{"name": "docs", "description": "Documentation"}],
            },
            f,
        )

    mine(str(project_a), palace_path)
    mine(str(project_b), palace_path)

    return palace_path


def test_export_creates_structure():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        output_dir = os.path.join(tmpdir, "export")

        stats = export_palace(palace_path, output_dir)

        # Should have two wings
        assert stats["wings"] == 2
        assert stats["rooms"] >= 2
        assert stats["drawers"] >= 3

        # Directory structure
        assert os.path.isfile(os.path.join(output_dir, "index.md"))
        assert os.path.isdir(os.path.join(output_dir, "alpha"))
        assert os.path.isdir(os.path.join(output_dir, "beta"))

        # Room files exist
        assert os.path.isfile(os.path.join(output_dir, "alpha", "backend.md"))
        assert os.path.isfile(os.path.join(output_dir, "alpha", "frontend.md"))
        assert os.path.isfile(os.path.join(output_dir, "beta", "docs.md"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_markdown_content():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        output_dir = os.path.join(tmpdir, "export")

        export_palace(palace_path, output_dir)

        # Check that room files contain expected markdown elements
        backend_md = Path(output_dir) / "alpha" / "backend.md"
        content = backend_md.read_text(encoding="utf-8")

        assert content.startswith("# alpha / backend\n")
        assert "## drawer_" in content
        assert "| Field | Value |" in content
        assert "| Source |" in content
        assert "| Filed |" in content
        assert "| Added by |" in content
        assert "---" in content
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_index_content():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        output_dir = os.path.join(tmpdir, "export")

        export_palace(palace_path, output_dir)

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
        palace_path = os.path.join(tmpdir, "empty_palace")
        output_dir = os.path.join(tmpdir, "export")

        stats = export_palace(palace_path, output_dir)

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
        palace_path = _setup_palace(tmpdir)
        decoy_target = os.path.join(tmpdir, "decoy_target")
        os.makedirs(decoy_target)
        output_dir = os.path.join(tmpdir, "export")
        _try_symlink_or_skip(decoy_target, output_dir)

        with pytest.raises(ValueError, match="symbolic link"):
            export_palace(palace_path, output_dir)

        # Decoy target must remain empty — nothing followed the symlink.
        assert os.listdir(decoy_target) == []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_refuses_symlinked_wing_dir():
    """A symlink pre-placed at a wing subdirectory must also be refused."""
    import pytest

    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        decoy_target = os.path.join(tmpdir, "decoy_target")
        os.makedirs(decoy_target)
        output_dir = os.path.join(tmpdir, "export")
        os.makedirs(output_dir)
        _try_symlink_or_skip(decoy_target, os.path.join(output_dir, "alpha"))

        with pytest.raises(ValueError, match="symbolic link"):
            export_palace(palace_path, output_dir)

        assert os.listdir(decoy_target) == []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_refuses_symlinked_room_file():
    """A symlink pre-placed at a room file path must not be followed."""
    import pytest

    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        decoy_target = os.path.join(tmpdir, "decoy_target.md")
        Path(decoy_target).write_text("untouched\n", encoding="utf-8")
        output_dir = os.path.join(tmpdir, "export")
        os.makedirs(os.path.join(output_dir, "alpha"))
        _try_symlink_or_skip(decoy_target, os.path.join(output_dir, "alpha", "backend.md"))

        with pytest.raises(ValueError, match="symbolic link"):
            export_palace(palace_path, output_dir)

        # Decoy file must remain unchanged — open did not follow the symlink.
        assert Path(decoy_target).read_text(encoding="utf-8") == "untouched\n"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_refuses_symlinked_index_file():
    """A symlink pre-placed at output_dir/index.md must not be followed."""
    import pytest

    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        decoy_target = os.path.join(tmpdir, "decoy_index.md")
        Path(decoy_target).write_text("untouched\n", encoding="utf-8")
        output_dir = os.path.join(tmpdir, "export")
        os.makedirs(output_dir)
        _try_symlink_or_skip(decoy_target, os.path.join(output_dir, "index.md"))

        with pytest.raises(ValueError, match="symbolic link"):
            export_palace(palace_path, output_dir)

        assert Path(decoy_target).read_text(encoding="utf-8") == "untouched\n"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
