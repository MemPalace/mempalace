"""Browser-free U6 checks with fake API responses, timers and scroll containers."""

import shutil
import subprocess
from pathlib import Path


def test_console_view_and_controller():
    node = shutil.which("node")
    assert node, "Node is required for the console view checks"
    script = Path(__file__).with_name("commonplace_console_view.mjs")
    result = subprocess.run(
        [node, "--test", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
