import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _coverage_fail_under() -> str:
    pyproject = PYPROJECT_PATH.read_text(encoding="utf-8")
    match = re.search(r"^fail_under = (\d+)$", pyproject, re.MULTILINE)
    assert match, "Could not parse coverage fail_under from pyproject.toml"
    return match.group(1)


def _job_block(name: str) -> str:
    ci = CI_PATH.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(?P<body>(?:    .*\n?)*)", ci, re.MULTILINE)
    assert match, f"Could not find CI job {name}"
    return match.group("body")


def test_linux_and_macos_coverage_match_pyproject_threshold():
    threshold = _coverage_fail_under()

    for job_name in ("test-linux", "test-macos"):
        job = _job_block(job_name)
        assert f"--cov-fail-under={threshold}" in job, (
            f"{job_name} must use --cov-fail-under={threshold} to match "
            "pyproject.toml coverage policy."
        )


def test_windows_keeps_documented_lower_coverage_threshold():
    job = _job_block("test-windows")
    assert "--cov-fail-under=80" in job
    assert "Windows" in job
