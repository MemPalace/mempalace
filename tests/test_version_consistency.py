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


def test_ruff_pins_match():
    """CI's ruff and pyproject's ruff must be the same version.

    The lint job installs ruff by literal pin rather than from pyproject, so
    the two can drift with nothing to notice: CI went on linting with 0.15.14
    while pyproject asked for 0.15.20. A dependabot bump to pyproject then
    passes CI green without the new version ever running — which matters,
    because 0.16 started formatting Python inside markdown fences and would
    have landed a lint config that disagreed with every contributor's local
    `ruff format`.
    """
    pyproject_pins = _pyproject_ruff_pins()
    ci_pins = _ci_ruff_pins()

    assert pyproject_pins, "no ruff pin found in pyproject.toml"
    assert ci_pins, "no ruff pin found in .github/workflows/ci.yml"
    assert len(set(pyproject_pins)) == 1, (
        f"pyproject.toml pins ruff at more than one version: {sorted(set(pyproject_pins))}"
    )
    assert set(ci_pins) == set(pyproject_pins), (
        f"ruff pin drift — ci.yml has {sorted(set(ci_pins))}, "
        f"pyproject.toml has {sorted(set(pyproject_pins))}. "
        "Bump both together so CI and local runs format identically."
    )
