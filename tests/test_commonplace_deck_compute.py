"""Compute geometry, machine states and screen lifecycle in the real ES modules."""

import shutil
import subprocess
from pathlib import Path


def test_deck_compute():
    node = shutil.which("node")
    assert node, "Node is required for the Compute checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("commonplace_deck_compute.mjs"))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-2000:]
