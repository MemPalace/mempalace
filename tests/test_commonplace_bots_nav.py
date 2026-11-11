"""S8: bots-first navigation (landing route, rail, account menu, phone tabs, deep links), run in node."""

import shutil
import subprocess
from pathlib import Path


def test_bots_first_navigation():
    node = shutil.which("node")
    assert node, "Node is required for the navigation checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("commonplace_bots_nav.mjs"))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
