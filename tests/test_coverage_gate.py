"""The coverage threshold has one source: pyproject.toml."""

import re
from pathlib import Path
from types import SimpleNamespace

from coverage.config import read_coverage_config
from coverage_gate import (
    apply_platform_threshold,
    cli_requested_fail_under,
    coverage_thresholds,
    threshold_for_platform,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _plugin(fail_under: float) -> SimpleNamespace:
    cov_config = SimpleNamespace(fail_under=fail_under)
    return SimpleNamespace(
        options=SimpleNamespace(cov_source=["mempalace"], cov_fail_under=fail_under),
        cov_controller=SimpleNamespace(cov=SimpleNamespace(config=cov_config)),
    )


def _config(plugin: SimpleNamespace, args: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        invocation_params=SimpleNamespace(args=args),
        pluginmanager=SimpleNamespace(getplugin=lambda name: plugin if name == "_cov" else None),
    )


def test_pyproject_holds_both_platform_gates():
    non_windows, windows = coverage_thresholds()
    assert non_windows == 85
    assert windows == 80
    assert threshold_for_platform("linux", non_windows, windows) == non_windows
    assert threshold_for_platform("win32", non_windows, windows) == windows
    assert threshold_for_platform("darwin", non_windows, windows) == non_windows


def test_default_coverage_config_omits_milvus_and_uses_pyproject_gate():
    warnings: list[str] = []
    config = read_coverage_config(
        str(_REPO_ROOT / "pyproject.toml"),
        warn=warnings.append,
    )
    assert warnings == []
    assert config.fail_under == 85
    assert config.precision == 2
    assert any(pattern.endswith("mempalace/backends/milvus.py") for pattern in config.run_omit)


def test_milvus_measurement_config_does_not_restate_the_package_gate():
    warnings: list[str] = []
    config = read_coverage_config(
        str(_REPO_ROOT / ".github" / "coverage-milvus.toml"),
        warn=warnings.append,
    )
    assert warnings == []
    assert config.fail_under == 0
    assert not config.source
    assert any(pattern.endswith("mempalace/backends/milvus.py") for pattern in config.run_include)


def test_ci_reads_the_pyproject_gate_instead_of_hardcoding_it():
    workflow = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    code_lines = [line for line in workflow.splitlines() if not line.lstrip().startswith("#")]
    assert not any("--cov-fail-under" in line for line in code_lines)
    assert re.search(r"fail_under\s*[=:]\s*\d+", workflow) is None
    assert 'pip install -e ".[dev,milvus]" "pymilvus==3.0.0" "milvus-lite==3.0"' in workflow
    assert "--cov-config=.github/coverage-milvus.toml" in workflow
    milvus_job = workflow.split("test-milvus:", 1)[1].split("\n  lint:", 1)[0]
    assert "\n          --cov\n" in milvus_job


def test_windows_hook_applies_pyproject_windows_gate():
    plugin = _plugin(85)
    config = _config(plugin, ["--cov=mempalace"])
    applied = apply_platform_threshold(config, platform="win32")
    assert applied == 80
    assert plugin.options.cov_fail_under == 80
    assert plugin.cov_controller.cov.config.fail_under == 80


def test_non_windows_hook_leaves_the_coverage_config_value():
    plugin = _plugin(85)
    config = _config(plugin, ["--cov=mempalace"])
    assert apply_platform_threshold(config, platform="linux") is None
    assert plugin.options.cov_fail_under == 85


def test_explicit_cli_threshold_is_not_overwritten():
    plugin = _plugin(70)
    config = _config(plugin, ["--cov=mempalace", "--cov-fail-under=70"])
    assert apply_platform_threshold(config, platform="win32") is None
    assert plugin.options.cov_fail_under == 70
    assert cli_requested_fail_under(["--cov-fail-under", "80"])


def test_measurement_config_with_no_gate_is_left_alone():
    plugin = _plugin(0)
    config = _config(plugin, ["--cov=mempalace/backends/milvus.py"])
    assert apply_platform_threshold(config, platform="win32") is None
    assert plugin.options.cov_fail_under == 0
