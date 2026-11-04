"""A9: browser-free project header, Connect and model rendering."""

import shutil
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"


def test_project_header_and_connect_view():
    assert (STATIC / "projects.js").is_file() and (STATIC / "projects.js").stat().st_size > 0  # RED
    node = shutil.which("node")
    assert node, "Node is required for the project view checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("projects_view.mjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
