"""Apply the Windows coverage gate from pyproject.toml.

``[tool.coverage.report] fail_under`` is the non-Windows gate. pytest-cov
copies it when coverage starts. On Windows, ``[tool.mempalace.coverage]
fail_under_windows`` replaces that copy. CI and local pytest both go through
this hook; neither should pass ``--cov-fail-under``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.9
        import tomli as tomllib

    with path.open("rb") as handle:
        return tomllib.load(handle)


def coverage_thresholds(pyproject: Path | None = None) -> tuple[float, float]:
    """Return ``(fail_under, fail_under_windows)`` from pyproject.toml."""
    data = _load_toml(pyproject or _PYPROJECT)
    report = data["tool"]["coverage"]["report"]
    windows = data["tool"]["mempalace"]["coverage"]
    return float(report["fail_under"]), float(windows["fail_under_windows"])


def threshold_for_platform(platform: str, non_windows: float, windows: float) -> float:
    if platform == "win32":
        return windows
    return non_windows


def cli_requested_fail_under(args: list[str]) -> bool:
    return any(arg == "--cov-fail-under" or arg.startswith("--cov-fail-under=") for arg in args)


def _invocation_args(config: Any) -> list[str]:
    params = getattr(config, "invocation_params", None)
    args = getattr(params, "args", None)
    if args is None:
        return []
    return [str(arg) for arg in args]


def apply_platform_threshold(config: Any, *, platform: str | None = None) -> float | None:
    """Set pytest-cov's fail-under on Windows. Return the value written.

    Non-Windows runs keep the threshold coverage already read from
    pyproject. An explicit ``--cov-fail-under`` is left alone. A measurement
    config that leaves the gate at 0 (the Milvus extra job) is left alone.
    """
    if cli_requested_fail_under(_invocation_args(config)):
        return None

    pluginmanager = getattr(config, "pluginmanager", None)
    if pluginmanager is None:
        return None
    plugin = pluginmanager.getplugin("_cov")
    options = getattr(plugin, "options", None)
    if options is None or not getattr(options, "cov_source", None):
        return None

    current = getattr(options, "cov_fail_under", None)
    if current is not None and float(current) == 0:
        return None

    platform_name = sys.platform if platform is None else platform
    if platform_name != "win32":
        return None

    _non_windows, windows = coverage_thresholds()
    _write_fail_under(plugin, windows)
    return windows


def _write_fail_under(plugin: Any, value: float) -> None:
    plugin.options.cov_fail_under = value
    controller = getattr(plugin, "cov_controller", None)
    cov = getattr(controller, "cov", None)
    cov_config = getattr(cov, "config", None)
    if cov_config is not None and hasattr(cov_config, "fail_under"):
        cov_config.fail_under = value
