"""Command palette and shortcuts, exercised through isolated Node cases."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "commonplace" / "web" / "static"
REDS = [
    "new",
    "forbidden",
    "navigation",
    "ids",
    "context",
    "render",
    "keys",
    "dispatch",
    "switch",
    "lifecycle",
]


@pytest.mark.parametrize("case", REDS)
def test_red_palette(case):
    assert (STATIC / "palette.js").is_file(), "Palette module missing"  # RED
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "workbench_palette.mjs"), case],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr  # RED


def test_guard_single_key_shell():
    assert shutil.which("node") is not None
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "workbench_shell_wiring.mjs"), "switch"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_navigation_routes():
    source = (STATIC / "app.js").read_text()
    for route in ("#/rules", "#/console", "#/settings", "#/space/", "#/decide"):
        assert route in source
