"""Provider sign-in rendering and app boot behavior."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def node():
    executable = shutil.which("node")
    assert executable, "node is required for the sign-in render check"
    return executable


@pytest.mark.parametrize("selection", ["guards", "dialog", "modal", "create"])
def test_signin_page_renders_and_handles_callback_errors(node, selection):
    script = Path(__file__).with_name("commonplace_signin_render.mjs")
    if selection != "guards":
        static = Path(__file__).parents[1] / "commonplace" / "web" / "static"
        assert (static / "onboarding.js").exists(), "onboarding module missing"  # RED
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", str(script)],
        env={**os.environ, "SIGNIN_CASE": selection},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    cases = re.findall(r"^\s*(ok|not ok) \d+ - (.+)$", result.stdout, re.MULTILINE)
    failed = [name for status, name in cases if status == "not ok"]
    assert result.returncode == 0, (
        f"Failed Node cases: {', '.join(failed)}\n{result.stdout}\n{result.stderr}"
    )
    assert cases, result.stdout
