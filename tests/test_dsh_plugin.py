"""Run the DeepSeek Harness plugin's own Node test suite (``.dsh-plugin/test``).

The plugin is JavaScript, so its behaviour is tested with ``node --test``. This
wrapper puts that suite on the same ``pytest`` path CI already runs, so the
plugin cannot silently rot next to the Python it drives.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent / ".dsh-plugin"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_dsh_plugin_node_suite():
    suite = sorted(str(path) for path in (PLUGIN_DIR / "test").glob("*.test.mjs"))
    assert suite, "the DSH plugin ships a Node test suite"

    result = subprocess.run(
        [NODE, "--test", *suite],
        cwd=PLUGIN_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
