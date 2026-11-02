"""Per-repo quick checks read from ``.commonplace/checks.toml``."""

from __future__ import annotations

import fnmatch
import math
import os
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

DEFAULT_BUDGET_SECONDS = 10.0
CONFIG_RELPATH = ".commonplace/checks.toml"

_TOP_KEYS = frozenset({"budget_seconds", "check"})
_CHECK_KEYS = frozenset({"name", "run", "globs", "fix"})
_FILES_PLACEHOLDER = "{files}"


class ChecksConfigError(ValueError):
    """A checks.toml file that cannot be used as written."""


def _error(source: str, location: str, message: str) -> ChecksConfigError:
    return ChecksConfigError(f"{source}: {location}: {message}")


def _matches(path: str, glob: str) -> bool:
    if "/" in glob:
        return fnmatch.fnmatchcase(path, glob)
    return fnmatch.fnmatchcase(PurePosixPath(path).name, glob)


def _render(template: str, matched: list[str]) -> str:
    if _FILES_PLACEHOLDER not in template:
        return template
    quoted = " ".join(shlex.quote(path) for path in matched)
    return template.replace(_FILES_PLACEHOLDER, quoted)


@dataclass(frozen=True)
class CheckSpec:
    """One named check: a command run on the changed files it matches."""

    name: str
    run: str
    globs: tuple[str, ...]
    fix: str | None

    def files_for(self, files: Iterable[str]) -> list[str]:
        """Return the matching repo-relative posix paths, in input order, deduplicated."""
        matched: list[str] = []
        seen: set[str] = set()
        for raw in files:
            path = PurePosixPath(raw).as_posix()
            if path in seen or not any(_matches(path, glob) for glob in self.globs):
                continue
            seen.add(path)
            matched.append(path)
        return matched

    def command_for(self, files: Iterable[str]) -> str | None:
        """Return the run command for the matching files, or None when none match."""
        matched = self.files_for(files)
        return _render(self.run, matched) if matched else None

    def fix_command_for(self, files: Iterable[str]) -> str | None:
        """Return the fix command for the matching files, or None."""
        if self.fix is None:
            return None
        matched = self.files_for(files)
        return _render(self.fix, matched) if matched else None


@dataclass(frozen=True)
class ChecksConfig:
    """The parsed checks.toml of one repo."""

    budget_seconds: float = DEFAULT_BUDGET_SECONDS
    checks: tuple[CheckSpec, ...] = ()
    source: str | None = None


def _reject_unknown(
    table: dict[str, Any], allowed: frozenset[str], source: str, prefix: str
) -> None:
    for key in table:
        if key not in allowed:
            raise _error(source, f"{prefix}{key}", "unknown key")


def _parse_budget(value: Any, source: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(source, "budget_seconds", "expected a number of seconds")
    if not math.isfinite(value) or value <= 0:
        raise _error(source, "budget_seconds", "must be a finite number greater than 0")
    return float(value)


def _text(table: dict[str, Any], key: str, where: str, source: str) -> str | None:
    if key not in table:
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise _error(source, f"{where}.{key}", "expected a non-empty string")
    return value


def _globs(table: dict[str, Any], where: str, source: str) -> tuple[str, ...]:
    if "globs" not in table:
        return ("*",)
    value = table["globs"]
    if not isinstance(value, list) or not all(
        isinstance(glob, str) and glob.strip() for glob in value
    ):
        raise _error(source, f"{where}.globs", "expected a list of strings")
    if not value:
        raise _error(source, f"{where}.globs", "expected at least one glob")
    return tuple(value)


def _parse_check(table: dict[str, Any], index: int, source: str) -> CheckSpec:
    where = f"check[{index}]"
    _reject_unknown(table, _CHECK_KEYS, source, f"{where}.")
    name = _text(table, "name", where, source)
    if name is None:
        raise _error(source, f"{where}.name", "required")
    run = _text(table, "run", where, source)
    if run is None:
        raise _error(source, f"{where}.run", "required")
    return CheckSpec(
        name=name,
        run=run,
        globs=_globs(table, where, source),
        fix=_text(table, "fix", where, source),
    )


def parse_checks_config(text: str, source: str = CONFIG_RELPATH) -> ChecksConfig:
    """Parse checks.toml text. ``source`` names the file in error messages."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ChecksConfigError(f"{source}: invalid TOML: {exc}") from exc
    _reject_unknown(data, _TOP_KEYS, source, "")
    budget = DEFAULT_BUDGET_SECONDS
    if "budget_seconds" in data:
        budget = _parse_budget(data["budget_seconds"], source)
    raw_checks = data.get("check", [])
    if not isinstance(raw_checks, list) or not all(isinstance(item, dict) for item in raw_checks):
        raise _error(source, "check", "expected an array of tables ([[check]])")
    checks: list[CheckSpec] = []
    names: set[str] = set()
    for index, table in enumerate(raw_checks):
        spec = _parse_check(table, index, source)
        if spec.name in names:
            raise _error(source, f"check[{index}].name", f"duplicate check name {spec.name!r}")
        names.add(spec.name)
        checks.append(spec)
    return ChecksConfig(budget_seconds=budget, checks=tuple(checks), source=source)


def load_checks_config(repo_root: str | os.PathLike[str]) -> ChecksConfig | None:
    """Load ``.commonplace/checks.toml`` from a repo root; None when the file is absent."""
    path = Path(repo_root) / CONFIG_RELPATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return parse_checks_config(text, source=CONFIG_RELPATH)
