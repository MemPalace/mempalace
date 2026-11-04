"""Provider sign-in rendering and app boot behavior."""

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


def test_signin_page_renders_and_handles_callback_errors(node):
    script = Path(__file__).with_name("commonplace_signin_render.mjs")
    result = subprocess.run(
        [node, "--test", "--test-reporter=tap", str(script)],
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
