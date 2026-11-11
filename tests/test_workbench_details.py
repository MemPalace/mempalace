"""Isolated Details contract; the shell wires this component separately."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"
HARNESS = Path(__file__).with_name("workbench_details.mjs")
REDS = ["origin", "blocked", "proposal", "actions", "tabs", "strips", "loading", "priority"]


@pytest.mark.parametrize("case", REDS)
def test_red_details(case):
    assert (STATIC / "details.js").is_file(), "Details module missing"
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_node_available():
    assert shutil.which("node") is not None


def test_guard_legacy_action_contract():
    source = (STATIC / "app.js").read_text()
    for marker in (
        'data-action="edit-card"',
        'data-action="panel-move"',
        'data-action="handoff"',
        'data-form="comment"',
        'if (status === "review") reviewForm()',
    ):
        assert marker in source
