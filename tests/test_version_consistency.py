import re
from pathlib import Path

from mempalace import __version__
from mempalace.mcp_server import handle_request


def _expected_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
    assert match is not None, "Could not find project version in pyproject.toml"
    return match.group(1)


def test_package_version_matches_pyproject():
    assert __version__ == _expected_version()


def test_mcp_initialize_reports_package_version():
    response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert response["result"]["serverInfo"]["version"] == _expected_version()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _pyproject_ruff_pins() -> list[str]:
    content = (_repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    return re.findall(r'"ruff==([^"]+)"', content)


def _ci_ruff_pins() -> list[str]:
    content = (_repo_root() / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return re.findall(r'pip install "ruff==([^"]+)"', content)


def _precommit_ruff_revs(content: str) -> list[str]:
    ruff_repo = re.search(
        r"(?ms)^[ \t]*-[ \t]+repo:[ \t]+https://github\.com/astral-sh/ruff-pre-commit[ \t]*$.*?(?=^[ \t]*-[ \t]+repo:|\Z)",
        content,
    )
    if ruff_repo is None:
        return []
    return re.findall(r"^[ \t]*rev:[ \t]+v(\d+\.\d+\.\d+)[ \t]*$", ruff_repo.group(), re.MULTILINE)


def _precommit_ruff_rev() -> list[str]:
    content = (_repo_root() / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    return _precommit_ruff_revs(content)


def test_precommit_ruff_rev_ignores_other_repositories():
    content = """
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.1
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
"""
    assert _precommit_ruff_revs(content) == ["0.16.1"]


def test_ruff_pins_match():
    """CI's ruff, pyproject's ruff, and pre-commit's ruff rev must match.

    The lint job installs ruff by literal pin rather than from pyproject, so
    the pins can drift with nothing to notice: CI went on linting with 0.15.14
    while pyproject asked for 0.15.20. A dependabot bump to pyproject then
    passes CI green without the new version ever running — which matters,
    because 0.16 started formatting Python inside markdown fences and would
    have landed a lint config that disagreed with every contributor's local
    `ruff format`. The pre-commit config pins the same ruff in its own rev, so
    all three sources must stay in lock-step.
    """
    pyproject_pins = _pyproject_ruff_pins()
    ci_pins = _ci_ruff_pins()
    precommit_revs = _precommit_ruff_rev()

    assert pyproject_pins, "no ruff pin found in pyproject.toml"
    assert ci_pins, "no ruff pin found in .github/workflows/ci.yml"
    assert precommit_revs, "no ruff rev found in .pre-commit-config.yaml"
    assert len(set(pyproject_pins)) == 1, (
        f"pyproject.toml pins ruff at more than one version: {sorted(set(pyproject_pins))}"
    )
    assert set(ci_pins) == set(pyproject_pins), (
        f"ruff pin drift — ci.yml has {sorted(set(ci_pins))}, "
        f"pyproject.toml has {sorted(set(pyproject_pins))}. "
        "Bump both together so CI and local runs format identically."
    )
    assert set(precommit_revs) == set(pyproject_pins), (
        f"ruff pin drift — .pre-commit-config.yaml rev has "
        f"{sorted(set(precommit_revs))}, pyproject.toml has "
        f"{sorted(set(pyproject_pins))}. "
        "Bump the pre-commit rev to match so local hooks and CI format identically."
    )
