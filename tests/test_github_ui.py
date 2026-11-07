"""Needs renders only locally reported connection state and safe PR links."""

import subprocess
from pathlib import Path


def test_needs_github_states():
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("github_render.mjs"))],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_needs_switch_and_logout_clear():
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("github_lifecycle.mjs"))],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
