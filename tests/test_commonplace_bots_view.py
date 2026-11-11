"""S5: stream parser, sender and bots screen contracts, run in node with a fake DOM and fake API."""

import shutil
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"


def test_bots_stream_and_view():
    for name in ("botstream.js", "bots.js", "bots.css"):
        assert (STATIC / name).exists(), name
    node = shutil.which("node")
    assert node, "Node is required for the bots view checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("commonplace_bots_view.mjs"))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-2000:]
