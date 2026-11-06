"""Project settings consent, autosave, and return navigation."""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "commonplace/web/static"
REDS = [
    "hosted_panel",
    "hosted_turn_on",
    "local_400",
    "disclosure",
    "phone_escape",
    "invalid_local_provider",
    "basic",
    "local_urls",
    "missing_model",
    "not_loopback",
    "inherited",
    "single_flight",
    "limits",
    "limits_failure",
    "phases",
    "settings_screen",
]
GUARDS = ["guard_own_memory", "guard_fixed_errors", "guard_shared_consent"]


@pytest.mark.parametrize("case", REDS + GUARDS)
def test_project_settings(case):
    assert (STATIC / "projects.js").is_file()
    node = shutil.which("node")
    assert node, "Node is required for project settings checks"
    result = subprocess.run(
        [node, str(Path(__file__).with_name("workbench_project_settings.mjs")), case],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr  # RED
