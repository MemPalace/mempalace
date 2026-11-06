"""Isolated Memory contracts; the application wires the component separately."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"
HARNESS = Path(__file__).with_name("workbench_memory.mjs")
REDS = [
    "wings",
    "exact",
    "authors",
    "fanout",
    "filters",
    "neighbours",
    "reply",
    "link",
    "render",
    "controller",
    "stale",
    "failures",
]


@pytest.mark.parametrize("case", REDS)
def test_red_memory(case):
    assert (STATIC / "memory.js").is_file(), "Memory module missing"  # RED
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_node_available():
    assert shutil.which("node") is not None


def test_guard_existing_api_contract():
    source = (STATIC / "api.js").read_text()
    assert "export async function api(" in source
    assert 'credentials: "same-origin"' in source
    assert "JSON.stringify(options.body)" in source
