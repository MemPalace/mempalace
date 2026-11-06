"""Browser-free route resolution contracts."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"


@pytest.mark.parametrize("case", ["root", "empty", "chat", "space", "needs"])
def test_routes(case):
    assert (STATIC / "routes.js").is_file(), "Route module missing"
    node = shutil.which("node")
    assert node, "Node is required for route checks"
    result = subprocess.run(
        [node, str(Path(__file__).with_name("workbench_routes.mjs")), case],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
