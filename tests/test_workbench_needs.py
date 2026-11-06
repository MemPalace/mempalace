"""Needs me component contracts, isolated from shell wiring."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace" / "web" / "static"
HARNESS = Path(__file__).with_name("workbench_needs.mjs")
REDS = [
    "approve",
    "context",
    "filters",
    "waiting",
    "settling",
    "stopped",
    "too_late",
    "decline",
    "linked",
    "ask",
    "reload",
    "races",
    "errors",
    "decision",
    "focus",
]


@pytest.mark.parametrize("case", REDS)
def test_red_needs(case):
    assert (STATIC / "needs.js").is_file(), "Needs module missing"  # RED
    result = subprocess.run(
        ["node", str(HARNESS), case], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_node_available():
    assert shutil.which("node") is not None


def test_guard_json_write_contract():
    source = (STATIC / "api.js").read_text()
    assert '"Content-Type": "application/json"' in source
    assert "JSON.stringify(options.body)" in source


def test_guard_existing_reject_contract():
    source = (STATIC / "app.js").read_text()
    assert 'action === "need-reject"' in source
    assert "body: { approve, version }" in source
