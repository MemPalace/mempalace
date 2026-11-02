"""Lint and format stacks of a repo: detection, per-file command plans, budgeted runs.

Pure library: nothing here installs a hook or reads a commonplace config file.
A pre-push guard calls ``detect_stacks``, ``plan_commands`` and ``run_checks``.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

JS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
TS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts")
JS_TOOLS = frozenset({"oxlint", "eslint", "biome", "prettier", "tsc"})

_ESLINT_FILES = (
    tuple(f"eslint.config.{ext}" for ext in ("js", "mjs", "cjs", "ts", "mts", "cts"))
    + (".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml")
    + (".eslintrc.yaml",)
)
_PRETTIER_FILES = (
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.mjs",
    ".prettierrc.toml",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
    "prettier.config.ts",
)
_RUFF_TABLE = re.compile(r"^\s*\[tool\.ruff[.\]]", re.MULTILINE)


@dataclass(frozen=True)
class Stack:
    """A lint or format tool detected in a repo.

    ``marker`` is the repo-relative file that triggered detection.
    """

    name: str
    extensions: tuple[str, ...]
    marker: str


def _read_package_json(repo_root: Path) -> dict:
    """Parse the root package.json; anything unreadable or malformed counts as empty."""
    try:
        data = json.loads((repo_root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _declares(package: dict, dependency: str) -> bool:
    """True when the package.json lists the dependency in dependencies or devDependencies."""
    for section in ("dependencies", "devDependencies"):
        deps = package.get(section)
        if isinstance(deps, dict) and dependency in deps:
            return True
    return False


def _first_file(repo_root: Path, names: tuple[str, ...]) -> str | None:
    """Return the first name that exists as a file at the repo root."""
    for name in names:
        if (repo_root / name).is_file():
            return name
    return None


def _pyproject_has_ruff(repo_root: Path) -> bool:
    try:
        text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return _RUFF_TABLE.search(text) is not None


def detect_stacks(repo_root: Path | str) -> list[Stack]:
    """Detect the lint and format stacks at the repo root, in a stable order."""
    root = Path(repo_root)
    package = _read_package_json(root)
    stacks: list[Stack] = []

    ruff_marker = _first_file(root, ("ruff.toml", ".ruff.toml"))
    if ruff_marker is None and _pyproject_has_ruff(root):
        ruff_marker = "pyproject.toml"
    if ruff_marker:
        stacks.append(Stack("ruff", (".py", ".pyi"), ruff_marker))

    if (root / ".oxlintrc.json").is_file():
        stacks.append(Stack("oxlint", JS_EXTENSIONS, ".oxlintrc.json"))
    elif _declares(package, "oxlint"):
        stacks.append(Stack("oxlint", JS_EXTENSIONS, "package.json"))

    eslint_marker = _first_file(root, _ESLINT_FILES)
    if eslint_marker is None and "eslintConfig" in package:
        eslint_marker = "package.json"
    if eslint_marker:
        stacks.append(Stack("eslint", JS_EXTENSIONS, eslint_marker))

    biome_marker = _first_file(root, ("biome.json", "biome.jsonc"))
    if biome_marker:
        stacks.append(Stack("biome", JS_EXTENSIONS + (".json", ".jsonc", ".css"), biome_marker))

    prettier_marker = _first_file(root, _PRETTIER_FILES)
    if prettier_marker is None and "prettier" in package:
        prettier_marker = "package.json"
    if prettier_marker:
        stacks.append(
            Stack(
                "prettier",
                JS_EXTENSIONS + (".json", ".css", ".scss", ".md", ".yaml", ".yml", ".html"),
                prettier_marker,
            )
        )

    if (root / "tsconfig.json").is_file():
        stacks.append(Stack("tsc", TS_EXTENSIONS, "tsconfig.json"))
    if (root / "Cargo.toml").is_file():
        stacks.append(Stack("cargo-fmt", (".rs",), "Cargo.toml"))
    if (root / "go.mod").is_file():
        stacks.append(Stack("gofmt", (".go",), "go.mod"))
    return stacks


@dataclass(frozen=True)
class PlannedCommand:
    """One check to run. ``argv`` is run directly; ``shell`` (config-provided) runs via the shell."""

    name: str
    argv: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    fix: str | None = None
    fail_on_output: bool = False
    shell: str | None = None


# name -> (check argv before the files, fix argv before the files, fail_on_output)
_FILE_TOOLS: dict[str, tuple[tuple[str, ...], tuple[str, ...], bool]] = {
    "ruff": (("ruff", "check", "--force-exclude"), ("ruff", "check", "--fix"), False),
    "oxlint": (("oxlint",), ("oxlint", "--fix"), False),
    "eslint": (("eslint", "--no-warn-ignored"), ("eslint", "--fix"), False),
    "biome": (
        ("biome", "check", "--no-errors-on-unmatched"),
        ("biome", "check", "--write"),
        False,
    ),
    "prettier": (("prettier", "--check", "--ignore-unknown"), ("prettier", "--write"), False),
    "cargo-fmt": (("cargo", "fmt", "--check", "--"), ("cargo", "fmt", "--"), False),
    "gofmt": (("gofmt", "-l"), ("gofmt", "-w"), True),
}


def _repo_path(repo_root: Path, changed: str) -> str:
    """Repo-relative POSIX path for a changed file given relative or absolute."""
    path = Path(changed)
    if path.is_absolute():
        try:
            path = path.relative_to(repo_root)
        except ValueError:
            pass
    return path.as_posix()


def _quoted(argv: tuple[str, ...]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def plan_commands(
    repo_root: Path | str, stacks: list[Stack], changed_files: list[str]
) -> list[PlannedCommand]:
    """Plan one command per stack that has changed files, limited to those files.

    Stacks without a changed file of their extensions get no command; the caller
    passes only changed files, so nothing is skipped silently.
    """
    root = Path(repo_root)
    changed = list(dict.fromkeys(_repo_path(root, f) for f in changed_files))
    commands: list[PlannedCommand] = []
    for stack in stacks:
        files = tuple(f for f in changed if f.endswith(stack.extensions))
        if not files:
            continue
        if stack.name == "tsc":
            # tsc cannot be limited to files and still honour tsconfig, so it checks the
            # whole project once when any TS file changed. It has no fix command.
            argv = ("tsc", "--noEmit", "-p", stack.marker)
            commands.append(PlannedCommand(name="tsc", argv=argv, files=files))
            continue
        check, fix, fail_on_output = _FILE_TOOLS[stack.name]
        commands.append(
            PlannedCommand(
                name=stack.name,
                argv=(*check, *files),
                files=files,
                fix=_quoted((*fix, *files)),
                fail_on_output=fail_on_output,
            )
        )
    return commands


def resolve_executable(repo_root: Path | str, name: str) -> str | None:
    """Find a JS tool in the repo's node_modules/.bin first, then on PATH."""
    if name in JS_TOOLS:
        local = Path(repo_root) / "node_modules" / ".bin" / name
        if local.is_file():
            return str(local)
    return shutil.which(name)


# ── running ────────────────────────────────────────────────────────────

MAX_LOCATIONS = 20


@dataclass
class CommandResult:
    """Outcome of one planned command. ``status`` is passed, failed, missing or timeout."""

    command: PlannedCommand
    status: str
    seconds: float
    output: str
    locations: list[str]


@dataclass
class CheckReport:
    """Results of one run. A missing tool is printed but does not fail the run."""

    results: list[CommandResult]
    total_seconds: float
    budget_seconds: float

    @property
    def ok(self) -> bool:
        return not any(result.status in ("failed", "timeout") for result in self.results)

    @property
    def over_budget(self) -> bool:
        return self.total_seconds > self.budget_seconds


def _say(stream: TextIO, text: str) -> None:
    print(text, file=stream, flush=True)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _joined(*parts: str) -> str:
    return "\n".join(part.rstrip("\n") for part in parts if part.strip())


def _strip_dot(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def _parse_locations(files: tuple[str, ...], output: str) -> list[str]:
    """``path:line`` for each output line location that names one of the command's files."""
    if not files:
        return []
    alternatives = "|".join(re.escape(f) for f in sorted(set(files), key=len, reverse=True))
    # ``path:line`` (most linters) or ``path(line,col)`` (tsc).
    pattern = re.compile(rf"(?:^|(?<=\s))(?:\./)?({alternatives})(?::(\d+)|\((\d+),\d+\))")
    found = [
        f"{match.group(1)}:{match.group(2) or match.group(3)}"
        for line in output.splitlines()
        for match in pattern.finditer(line)
    ]
    return list(dict.fromkeys(found))


def _execute(
    command: PlannedCommand,
    cwd: Path,
    hard_timeout_seconds: float,
    env: dict[str, str] | None,
) -> tuple[str, str, list[str]]:
    """Run one command and return (status, output, locations)."""
    if command.shell is not None:
        argv: str | list[str] = command.shell
        use_shell = True
    else:
        if not command.argv:
            raise ValueError(f"{command.name}: planned command has neither argv nor shell")
        exe = resolve_executable(cwd, command.argv[0])
        if exe is None:
            return "missing", "", []
        argv = [exe, *command.argv[1:]]
        use_shell = False
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            shell=use_shell,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=hard_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return "timeout", _joined(_text(exc.stdout), _text(exc.stderr)), []
    except OSError as exc:
        return "failed", str(exc), []
    output = _joined(proc.stdout, proc.stderr)
    if command.fail_on_output and proc.returncode == 0:
        # Exit 0 but the tool lists offending files on stdout (gofmt -l).
        listed = [
            line
            for line in dict.fromkeys(_strip_dot(raw.strip()) for raw in output.splitlines())
            if line
        ]
        return ("failed" if listed else "passed"), output, listed
    if proc.returncode != 0:
        return "failed", output, _parse_locations(command.files, output)
    return "passed", output, []


def _run_one(
    command: PlannedCommand,
    cwd: Path,
    hard_timeout_seconds: float,
    clock: Callable[[], float],
    env: dict[str, str] | None,
) -> CommandResult:
    start = clock()
    status, output, locations = _execute(command, cwd, hard_timeout_seconds, env)
    return CommandResult(command, status, clock() - start, output, locations)


def _print_result(stream: TextIO, result: CommandResult, hard_timeout_seconds: float) -> None:
    name = result.command.name
    if result.status == "missing":
        _say(stream, f"{name}: not installed, not checked (install it or remove the stack)")
        return
    if result.status == "passed":
        _say(stream, f"{name}: passed in {result.seconds:.1f}s")
        return
    if result.status == "timeout":
        _say(stream, f"{name}: timed out after {hard_timeout_seconds:g}s (hard limit)")
    else:
        _say(stream, f"{name}: failed in {result.seconds:.1f}s")
    if result.command.fix:
        _say(stream, f"  fix: {result.command.fix}")
    if result.locations:
        for location in result.locations[:MAX_LOCATIONS]:
            _say(stream, f"  {location}")
        extra = len(result.locations) - MAX_LOCATIONS
        if extra > 0:
            _say(stream, f"  ... and {extra} more")
        return
    lines = [line for line in result.output.splitlines() if line.strip()]
    for line in lines[-MAX_LOCATIONS:]:
        _say(stream, f"  {line}")


def run_checks(
    commands: Iterable[PlannedCommand],
    cwd: Path | str,
    *,
    budget_seconds: float = 10.0,
    hard_timeout_seconds: float = 60.0,
    out: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    env: dict[str, str] | None = None,
) -> CheckReport:
    """Run the planned commands one after another and report against a soft time budget.

    Every command runs even when the budget is spent; the budget only decides
    whether the timing summary is printed. ``clock`` is read twice per command.
    """
    stream = sys.stderr if out is None else out
    root = Path(cwd)
    planned = list(commands)
    files = {f for command in planned for f in command.files}
    _say(
        stream,
        f"commonplace checks: {_plural(len(planned), 'command')} on "
        f"{_plural(len(files), 'file')}, budget {budget_seconds:g}s",
    )
    results = []
    for command in planned:
        result = _run_one(command, root, hard_timeout_seconds, clock, env)
        results.append(result)
        _print_result(stream, result, hard_timeout_seconds)
    report = CheckReport(
        results=results,
        total_seconds=sum(result.seconds for result in results),
        budget_seconds=budget_seconds,
    )
    if report.over_budget:
        _say(stream, f"over budget: {report.total_seconds:.1f}s of {budget_seconds:g}s")
        for result in sorted(results, key=lambda r: r.seconds, reverse=True):
            _say(stream, f"  {result.command.name}: {result.seconds:.1f}s")
    else:
        _say(
            stream,
            f"commonplace checks: done in {report.total_seconds:.1f}s, budget {budget_seconds:g}s",
        )
    return report
