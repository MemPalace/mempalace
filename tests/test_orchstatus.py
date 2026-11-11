"""Coordinator status and End lease in Project settings: the browser-module cases."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("orchstatus.mjs")
SOURCE = SCRIPT.read_text()
CASES = re.findall(r"^  (?:async )?(\w+)\(\) \{", SOURCE[SOURCE.index("const cases = {") :], re.M)


def test_the_case_list_is_complete():
    assert len(CASES) >= 15 and "end_lease_leaves_unfinished_work_listed" in CASES


@pytest.mark.parametrize("case", CASES)
def test_orch_status(case):
    node = shutil.which("node")
    assert node, "Node is required for the project settings checks"
    result = subprocess.run(
        [node, str(SCRIPT), case], capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
