"""Pre-push quick check: changed files only, inside a time budget.

Runs what git is about to push through lint and format checks, ``docs-check``
against the base branch, and the fast tests that map to the changed files. It is
a quick check, never the full suite or CI, and says so in every run. When the
budget runs out it prints what it skipped instead of failing or passing silently.

Install it with ``python -m commonplace.guards.hooks install``; run it by hand with
``python -m commonplace.guards.prepush --manual``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TextIO

from commonplace.guards import push_range
from commonplace.guards.stacks import PlannedCommand, detect_stacks, plan_commands, run_command
from commonplace.orch import docs_check

DEFAULT_BUDGET_SECONDS = 60.0
BUDGET_ENV = "COMMONPLACE_PREPUSH_BUDGET"
TESTS_DIR = "tests"
# -v prints each test id before it runs, so a cut-off run shows where it stopped.
FAST_TEST_ARGS = ("-v", "-x", "-p", "no:cacheprovider")
MAX_LISTED = 5
MAX_OUTPUT_LINES = 20

_PASSED, _FAILED, _SKIPPED, _INCOMPLETE = "passed", "failed", "skipped", "incomplete"
_NA = "n/a"  # nothing to check here; unlike skipped, it leaves the run complete


# ── changed-file selection ─────────────────────────────────────────────


@dataclass(frozen=True)
class ChangedTests:
    """Test files chosen for a set of changed files, and the changes that map to none."""

    files: tuple[str, ...]
    unmapped: tuple[str, ...]


def _module_names(path: str) -> list[str]:
    """Test-name stems a source path can own, most qualified first: ``pkg/sub/mod.py`` -> ``sub_mod``, ``mod``."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if len(parts) <= 1:
        return list(parts)
    return ["_".join(parts[i:]) for i in range(1, len(parts))]


def select_tests(
    repo_root: Path | str, changed: Iterable[str], tests_dir: str = TESTS_DIR
) -> ChangedTests:
    """Map changed paths to ``tests/test_<name>.py`` and ``tests/test_<name>_*.py`` files.

    A changed test file selects itself. Anything else Python maps by module name;
    other changes (docs, assets, fixtures) select nothing and are returned as unmapped.
    """
    root = Path(repo_root)
    available = {
        f"{tests_dir}/{p.name}" for p in (root / tests_dir).glob("test_*.py") if p.is_file()
    }
    chosen: set[str] = set()
    unmapped: list[str] = []
    for path in dict.fromkeys(PurePosixPath(c).as_posix() for c in changed):
        if not path.endswith(".py"):
            unmapped.append(path)
            continue
        if path in available:
            chosen.add(path)
            continue
        if path.startswith(f"{tests_dir}/"):
            unmapped.append(path)
            continue
        # Most qualified name first; a bare module name (``stacks``) is only a fallback, so
        # ``guards/hooks.py`` does not pull in every unrelated ``test_hooks_*`` file.
        for name in _module_names(path):
            hits = {
                candidate
                for candidate in available
                if (stem := PurePosixPath(candidate).stem) == f"test_{name}"
                or stem.startswith(f"test_{name}_")
            }
            if hits:
                chosen |= hits
                break
        else:
            unmapped.append(path)
    return ChangedTests(tuple(sorted(chosen)), tuple(unmapped))


# ── budget and tools ───────────────────────────────────────────────────


class Budget:
    """A wall-clock allowance shared by every step of one run."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = seconds
        self._clock = clock
        self._start = clock()

    def elapsed(self) -> float:
        return self._clock() - self._start

    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed())


def parse_budget(value: str | None, warn: Callable[[str], None]) -> float:
    """Seconds from a flag or environment value; an unusable value warns and uses the default."""
    if value is None or not value.strip():
        return DEFAULT_BUDGET_SECONDS
    try:
        seconds = float(value)
    except ValueError:
        seconds = float("nan")
    if not seconds > 0 or seconds == float("inf"):
        warn(f"ignoring budget {value!r}: expected seconds greater than 0")
        return DEFAULT_BUDGET_SECONDS
    return seconds


def tool_path(repo_root: Path | str, current: str | None = None) -> str:
    """PATH with this work tree's own tool directories first.

    A linked worktree has its own ``.venv``; its ruff and pytest must win over
    whatever the shell that ran ``git push`` happens to have. The running
    interpreter's own directory comes next, for a hook pointed at another env.
    """
    root = Path(repo_root)
    own = [
        str(path)
        for path in (
            root / ".venv" / "bin",
            root / ".venv" / "Scripts",
            root / "node_modules" / ".bin",
            Path(sys.executable).parent,  # tools installed beside the interpreter in use
        )
        if path.is_dir()
    ]
    rest = os.environ.get("PATH", "") if current is None else current
    return os.pathsep.join([*own, rest]) if rest else os.pathsep.join(own)


# ── the run ────────────────────────────────────────────────────────────


@dataclass
class StepOutcome:
    """One step's result. ``status`` is passed, failed, skipped, incomplete or n/a."""

    name: str
    status: str
    seconds: float = 0.0
    detail: str = ""
    lines: list[str] = field(default_factory=list)
    rerun: str | None = None


@dataclass
class QuickReport:
    outcomes: list[StepOutcome]
    budget_seconds: float
    total_seconds: float

    def with_status(self, status: str) -> list[StepOutcome]:
        return [o for o in self.outcomes if o.status == status]

    @property
    def failed(self) -> bool:
        return bool(self.with_status(_FAILED))

    @property
    def complete(self) -> bool:
        return not self.with_status(_SKIPPED) and not self.with_status(_INCOMPLETE)

    @property
    def exit_code(self) -> int:
        """1 only when a check failed; skipped or cut-short work is reported, never a failure."""
        return 1 if self.failed else 0


def _say(stream: TextIO, text: str = "") -> None:
    print(text, file=stream, flush=True)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _tail(text: str, count: int = MAX_OUTPUT_LINES) -> list[str]:
    return [line for line in text.splitlines() if line.strip()][-count:]


def _listed(names: Iterable[str]) -> str:
    names = list(names)
    shown = ", ".join(names[:MAX_LISTED])
    return shown + (f" (+{len(names) - MAX_LISTED} more)" if len(names) > MAX_LISTED else "")


def _file_steps(top: Path, changed: list[str]) -> list[tuple[str, PlannedCommand]]:
    """Lint and format commands for the changed files, named ``lint <tool>`` / ``format <tool>``."""
    stacks = [stack for stack in detect_stacks(top) if stack.name != "tsc"]
    steps: list[tuple[str, PlannedCommand]] = []
    for command in plan_commands(top, stacks, changed):
        if command.name == "ruff":
            steps.append(("lint ruff", command))
            steps.append(
                (
                    "format ruff",
                    PlannedCommand(
                        name="ruff format",
                        argv=("ruff", "format", "--check", "--force-exclude", *command.files),
                        files=command.files,
                        fix="ruff format " + " ".join(shlex.quote(f) for f in command.files),
                    ),
                )
            )
        elif command.name == "cargo-fmt":
            steps.append(("format cargo-fmt", command))
        else:
            steps.append((f"lint {command.name}", command))
    return steps


def _run_command_step(
    name: str, command: PlannedCommand, top: Path, budget: Budget, env: dict[str, str]
) -> StepOutcome:
    remaining = budget.remaining()
    if remaining <= 0:
        return StepOutcome(name, _SKIPPED, detail="budget spent before it started")
    result = run_command(command, top, remaining, env=env)
    if result.status == "passed":
        return StepOutcome(name, _PASSED, result.seconds)
    if result.status == "missing":
        return StepOutcome(
            name, _SKIPPED, result.seconds, f"{command.argv[0]} is not installed in this worktree"
        )
    if result.status == "timeout":
        return StepOutcome(name, _INCOMPLETE, result.seconds, "budget ran out while it was running")
    lines = result.locations[:MAX_OUTPUT_LINES] or _tail(result.output)
    return StepOutcome(name, _FAILED, result.seconds, lines=lines, rerun=command.fix)


def _docs_step(top: Path, base: str, heads: list[str], budget: Budget) -> StepOutcome:
    if not (top / docs_check.MODULES).is_file():
        return StepOutcome("docs-check", _NA, detail=f"no {docs_check.MODULES} in this repo")
    if budget.remaining() <= 0:
        return StepOutcome("docs-check", _SKIPPED, detail="budget spent before it started")
    start = time.monotonic()
    problems: list[str] = []
    try:
        for head in heads:
            problems += docs_check.check(top, base, head)
    except docs_check.DocsCheckError as exc:
        return StepOutcome(
            "docs-check", _SKIPPED, detail=f"could not compare with {base}: {exc} (git fetch it)"
        )
    seconds = time.monotonic() - start
    if problems:
        return StepOutcome(
            "docs-check",
            _FAILED,
            seconds,
            lines=list(dict.fromkeys(problems)),
            rerun=f"python -m commonplace orch docs-check --base {base} --head HEAD",
        )
    return StepOutcome("docs-check", _PASSED, seconds)


def _started_files(output: str, files: Iterable[str]) -> list[str]:
    """Selected files pytest has begun, in run order (``-v`` prints ``file::test`` as each starts)."""
    return [f for f in files if re.search(rf"^{re.escape(f)}::", output, re.MULTILINE)]


def _test_step(top: Path, selection: ChangedTests, budget: Budget, env: dict[str, str]):
    rerun = " ".join(
        shlex.quote(part) for part in ("python", "-m", "pytest", *FAST_TEST_ARGS, *selection.files)
    )
    if not selection.files:
        return StepOutcome("tests", _NA, detail="no fast test maps to the changed files")
    if importlib.util.find_spec("pytest") is None:
        return StepOutcome("tests", _SKIPPED, detail="pytest is not installed in this worktree")
    if budget.remaining() <= 0:
        return StepOutcome("tests", _SKIPPED, detail="budget spent before it started")
    command = PlannedCommand(
        name="pytest",
        argv=(sys.executable, "-m", "pytest", *FAST_TEST_ARGS, *selection.files),
        files=selection.files,
    )
    result = run_command(command, top, budget.remaining(), env=env)
    count = _plural(len(selection.files), "test file")
    if result.status == "passed":
        return StepOutcome("tests", _PASSED, result.seconds, detail=count)
    if result.status == "timeout":
        started = _started_files(result.output, selection.files)
        done = started[:-1]  # the newest one may have been cut mid-file
        pending = [f for f in selection.files if f not in done]
        return StepOutcome(
            "tests",
            _INCOMPLETE,
            result.seconds,
            detail=(
                f"budget ran out: {len(done)} of {count} confirmed finished; "
                f"not confirmed: {_listed(pending)}"
            ),
            rerun=rerun,
        )
    return StepOutcome(
        "tests",
        _FAILED,
        result.seconds,
        detail=count,
        lines=_tail(result.output),
        rerun=rerun,
    )


def _print_outcome(stream: TextIO, outcome: StepOutcome) -> None:
    label = {
        _PASSED: "ok     ",
        _FAILED: "FAILED ",
        _SKIPPED: "SKIPPED",
        _INCOMPLETE: "CUT OFF",
        _NA: "n/a    ",
    }[outcome.status]
    timing = f" ({outcome.seconds:.1f}s)" if outcome.seconds else ""
    detail = f": {outcome.detail}" if outcome.detail else ""
    _say(stream, f"  [{label}] {outcome.name}{timing}{detail}")
    for line in outcome.lines:
        _say(stream, f"      {line}")
    if outcome.rerun and outcome.status != _PASSED:
        _say(stream, f"      run: {outcome.rerun}")


def run_quick_checks(
    repo_root: Path | str,
    changed: list[str],
    *,
    base: str,
    heads: list[str],
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    files_current: bool = True,
    out: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    env: dict[str, str] | None = None,
) -> QuickReport:
    """Run lint, format, docs-check and mapped tests for ``changed`` within the budget.

    ``files_current`` is False when the pushed commit is not the checked-out one: the
    file-based steps would then read the wrong files, so they are skipped and reported.
    """
    stream = sys.stderr if out is None else out
    top = Path(repo_root)
    budget = Budget(budget_seconds, clock)
    env = dict(os.environ if env is None else env)
    env["PATH"] = tool_path(top, env.get("PATH"))
    env.setdefault("PYTHONUNBUFFERED", "1")
    _say(
        stream,
        f"commonplace pre-push QUICK CHECK: {_plural(len(changed), 'changed file')}, "
        f"budget {budget_seconds:g}s",
    )
    _say(stream, "This is not the full test suite and not CI; a pass here is not a green build.")

    outcomes: list[StepOutcome] = []

    def record(outcome: StepOutcome) -> None:
        outcomes.append(outcome)
        _print_outcome(stream, outcome)

    if files_current:
        steps = _file_steps(top, changed)
        if not steps:
            record(StepOutcome("lint/format", _NA, detail="no changed file has a lint stack"))
        for name, command in steps:
            record(_run_command_step(name, command, top, budget, env))
    else:
        why = "the pushed commit is not the checked-out HEAD"
        record(StepOutcome("lint/format", _SKIPPED, detail=why))
    record(_docs_step(top, base, heads, budget))
    if files_current:
        selection = select_tests(top, changed)
        record(_test_step(top, selection, budget, env))
        if selection.unmapped:
            _say(
                stream,
                f"  note: no test mapped for {_plural(len(selection.unmapped), 'changed file')}: "
                f"{_listed(selection.unmapped)}",
            )
    else:
        record(
            StepOutcome("tests", _SKIPPED, detail="the pushed commit is not the checked-out HEAD")
        )

    report = QuickReport(outcomes, budget_seconds, budget.elapsed())
    _print_verdict(stream, report)
    return report


def _print_verdict(stream: TextIO, report: QuickReport) -> None:
    took = f"{report.total_seconds:.1f}s of {report.budget_seconds:g}s"
    if report.failed:
        names = ", ".join(o.name for o in report.with_status(_FAILED))
        _say(stream, f"QUICK CHECK FAILED ({took}): {names}. The push is blocked until it passes.")
    if report.complete and not report.failed:
        _say(
            stream,
            f"QUICK CHECK PASSED ({took}). Only the changed files and their mapped fast tests "
            "were checked; the full suite and CI still decide.",
        )
        return
    left = report.with_status(_SKIPPED) + report.with_status(_INCOMPLETE)
    if left:
        _say(
            stream,
            "QUICK CHECK INCOMPLETE: not checked: "
            + "; ".join(f"{o.name} ({o.detail})" for o in left)
            + ". This does not block the push, and it is not a pass for those.",
        )
        _say(
            stream,
            f"Raise the budget with --budget SECONDS or {BUDGET_ENV}, or run the commands above.",
        )


# ── command line ───────────────────────────────────────────────────────


def _git_out(top: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(top), *args], capture_output=True, text=True, check=False
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def _resolve_base(top: Path, wanted: str | None) -> str:
    """The comparison base: the flag, else the repo's development branch, else its default."""
    candidates = [wanted] if wanted else [docs_check.DEFAULT_BASE]
    for ref in candidates:
        if ref and _git_out(top, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            return ref
    if wanted:
        return wanted
    return push_range.default_branch(top) or docs_check.DEFAULT_BASE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m commonplace.guards.prepush",
        description="Quick pre-push check on the files a push changes, within a time budget.",
    )
    parser.add_argument("remote", nargs="?", default="origin", help="remote name (git passes it)")
    parser.add_argument("url", nargs="?", help="remote url (git passes it)")
    parser.add_argument(
        "--budget",
        metavar="SECONDS",
        help=f"time budget (default {DEFAULT_BUDGET_SECONDS:g}; env {BUDGET_ENV})",
    )
    parser.add_argument("--base", help=f"base ref (default {docs_check.DEFAULT_BASE})")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="check HEAD against the base instead of reading pushed refs from stdin",
    )
    return parser


def main(argv: list[str] | None = None, stdin: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    stream = sys.stderr

    def warn(message: str) -> None:
        _say(stream, f"commonplace pre-push: {message}")

    top_text = _git_out(Path.cwd(), "rev-parse", "--show-toplevel")
    if top_text is None:
        warn("not inside a git work tree")
        return 2
    top = Path(top_text)
    budget = parse_budget(args.budget or os.environ.get(BUDGET_ENV), warn)
    base = _resolve_base(top, args.base)
    source = sys.stdin if stdin is None else stdin
    head_sha = _git_out(top, "rev-parse", "HEAD") or ""
    try:
        if args.manual or source.isatty():
            updates = [
                push_range.PushUpdate("HEAD", head_sha, "refs/heads/HEAD", "0" * 40),
            ]
        else:
            updates = push_range.parse_pre_push_lines(source.read())
        base_exists = _git_out(top, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        changed = push_range.changed_files(
            top, updates, args.remote, default_ref=base if base_exists else None
        )
    except push_range.PushRangeError as exc:
        warn(f"cannot tell which files changed: {exc}. Not checked; the push is not blocked.")
        return 0
    pushed = [u for u in updates if not (u.is_delete or u.is_tag)]
    if not pushed:
        warn("nothing to check (only deletes or tags).")
        return 0
    heads = list(dict.fromkeys(u.local_sha for u in pushed))
    changed = [path for path in changed if (top / path).exists()]
    report = run_quick_checks(
        top,
        changed,
        base=base,
        heads=heads,
        budget_seconds=budget,
        files_current=heads == [head_sha],
    )
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
