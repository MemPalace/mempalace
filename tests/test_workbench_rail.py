"""Rail render contract and phone navigation."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "commonplace" / "web" / "static"


@pytest.mark.parametrize("case", ["phone", "counts", "current", "menu", "escaping"])
def test_rail_render(case):
    assert (STATIC / "rail.js").is_file(), "Rail module missing"
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "workbench_rail.mjs"), case],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_g01_existing_navigation_routes():
    source = (STATIC / "app.js").read_text()
    for route in ("#/rules", "#/console", "#/settings", "#/board/"):
        assert route in source


def test_g02_shared_shell_tokens():
    css = (STATIC / "workbench.css").read_text()
    for token in ("--accent:", "--surface:", "--wait-bg:", "--line:"):
        assert token in css
