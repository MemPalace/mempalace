"""Live chat states in the app: browser-free renderer and pane checks."""

import shutil
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"


def test_chat_live_view():
    assert (STATIC / "chatlive.js").exists()
    node = shutil.which("node")
    assert node, "Node is required for the chat view checks"
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("chat_live_view.mjs"))],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
