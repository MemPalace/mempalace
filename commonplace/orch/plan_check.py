"""Plan-check: the design-before-code grammar for plan files and plan cards.

``commonplace orch plan-check FILE`` exits 1 at the first violation of:

- **G0** text inside fenced code blocks (lines starting with three backticks) is ignored;
- **G1** headings ``## 0.`` to ``## 7.`` are present, in order;
- **G2** section 0 has ``**a.`` to ``**f.``, in order, each with at least 40 non-space
  characters;
- **G3** every (a) line with a digit carries the word ``M``, ``T``, ``G``, ``MEASURED``,
  ``TARGET`` or ``GUESS``;
- **G4** (e) has a table whose header is exactly ``Dependency | Timeout | Bounded retry |
  Idempotency key | User sees | RED``; each row has six non-empty cells and a RED cell
  like ``R1, R2b``;
- **G5** (f) has a table whose header starts ``ID | Fault injected | Assertion``, with
  unique ids like ``R1`` or ``R2b``;
- **G6** every (e) id appears in (f);
- **G7** a section 6 line ``R-test files:`` maps every (f) id to exactly one file
  (``file: R1 R2; other_file: R3``).

On a board whose accepted live orchestrator config exists, the board service runs the
same check (``require_plan``) when a ``plan`` card is approved and before a card created
by a configured planner connection reaches ``todo`` or ``done`` (``board_gate``).
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from commonplace.extensions import ToolError

FENCE = "```"
SECTIONS = tuple(range(8))
PARTS = "abcdef"
MIN_PART_CHARS = 40
E_HEADER = "Dependency | Timeout | Bounded retry | Idempotency key | User sees | RED"
F_HEADER = "ID | Fault injected | Assertion"
TEST_FILES = "R-test files:"
RED_ID = re.compile(r"R\d+[a-z]?")
RED_CELL = re.compile(r"R\d+[a-z]?(, R\d+[a-z]?)*")
LABEL = re.compile(r"\b(?:M|T|G|MEASURED|TARGET|GUESS)\b")
_HEADING = re.compile(r"## (\d+)\.(?:\s|$)")
_PART = re.compile(r"\*\*([a-z])\.")
_SEPARATOR = re.compile(r":?-+:?")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
MAX_PLAN_BYTES = 1_000_000


class PlanCheckError(ValueError):
    """The first violation: ``rule`` is ``G0``..``G7``; ``str()`` is ``"G<n>: <message>"``."""

    def __init__(self, rule: str, message: str):
        self.rule = rule
        self.message = message
        super().__init__(f"{rule}: {message}")


def unfenced(text: str) -> list[str]:
    """G0: the lines outside fenced code blocks (an unclosed fence runs to the end)."""
    lines, inside = [], False
    for line in text.splitlines():
        if line.lstrip(" ").startswith(FENCE) and len(line) - len(line.lstrip(" ")) <= 3:
            inside = not inside
            continue
        if not inside:
            lines.append(line)
    return lines


def _level2(line: str) -> bool:
    return line.startswith("## ")


def _sections(lines: list[str]) -> dict[int, list[str]]:
    """G1. Section ``k`` runs from its heading to the next level-2 heading."""
    first: dict[int, int] = {}
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and int(match.group(1)) in SECTIONS:
            first.setdefault(int(match.group(1)), index)
    previous = -1
    for number in SECTIONS:
        if number not in first:
            raise PlanCheckError("G1", f"heading '## {number}.' is missing")
        if first[number] < previous:
            raise PlanCheckError("G1", f"heading '## {number}.' is out of order")
        previous = first[number]
    sections = {}
    for number, start in first.items():
        end = next(
            (i for i in range(start + 1, len(lines)) if _level2(lines[i])),
            len(lines),
        )
        sections[number] = lines[start:end]
    return sections


def _parts(section: list[str]) -> dict[str, list[str]]:
    """G2. Part ``x`` runs from its ``**x.`` line to the next part line."""
    starts = [(i, m.group(1)) for i, line in enumerate(section) if (m := _PART.match(line))]
    first: dict[str, int] = {}
    for index, letter in starts:
        first.setdefault(letter, index)
    previous = -1
    for letter in PARTS:
        if letter not in first:
            raise PlanCheckError("G2", f"section 0 has no '**{letter}.' part")
        if first[letter] < previous:
            raise PlanCheckError("G2", f"part '**{letter}.' is out of order")
        previous = first[letter]
    bounds = sorted(index for index, _letter in starts)
    parts = {}
    for letter in PARTS:
        start = first[letter]
        end = next((i for i in bounds if i > start), len(section))
        parts[letter] = section[start:end]
        body = "\n".join(parts[letter])[len(f"**{letter}.") :]
        if len("".join(body.split())) < MIN_PART_CHARS:
            raise PlanCheckError(
                "G2", f"part '**{letter}.' has fewer than {MIN_PART_CHARS} non-space characters"
            )
    return parts


def _cells(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [cell.strip() for cell in _CELL_SPLIT.split(text)]


def _table(part: list[str]) -> tuple[list[str], list[list[str]]] | None:
    """The first table in a part: (header cells, body rows)."""
    for index, line in enumerate(part):
        if line.strip().startswith("|"):
            rows = []
            for row in part[index:]:
                if not row.strip().startswith("|"):
                    break
                rows.append(_cells(row))
            if len(rows) < 2 or not all(_SEPARATOR.fullmatch(cell) for cell in rows[1]):
                return rows[0], []
            return rows[0], rows[2:]
    return None


def _check_numbers(part: list[str]) -> None:
    for line in part:
        if any(ch.isdigit() for ch in line) and not LABEL.search(line):
            raise PlanCheckError("G3", "a line with a number in (a) is not labelled M, T or G")


def _failure_ids(part: list[str]) -> list[str]:
    table = _table(part)
    if table is None:
        raise PlanCheckError("G4", "(e) has no failure table")
    header, rows = table
    if " | ".join(header) != E_HEADER:
        raise PlanCheckError("G4", f"the (e) table header must be exactly '{E_HEADER}'")
    if not rows:
        raise PlanCheckError("G4", "the (e) table has no rows")
    ids = []
    for number, row in enumerate(rows, 1):
        if len(row) != 6 or not all(row):
            raise PlanCheckError("G4", f"(e) row {number} needs six non-empty cells")
        if not RED_CELL.fullmatch(row[5]):
            raise PlanCheckError(
                "G4", f"(e) row {number} has a RED cell that is not like 'R1, R2b'"
            )
        ids.extend(item.strip() for item in row[5].split(","))
    return ids


def _test_ids(part: list[str]) -> list[str]:
    table = _table(part)
    if table is None:
        raise PlanCheckError("G5", "(f) has no RED test table")
    header, rows = table
    if not " | ".join(header).startswith(F_HEADER):
        raise PlanCheckError("G5", f"the (f) table header must start '{F_HEADER}'")
    if not rows:
        raise PlanCheckError("G5", "the (f) table has no rows")
    ids = []
    for number, row in enumerate(rows, 1):
        if not RED_ID.fullmatch(row[0]):
            raise PlanCheckError("G5", f"(f) row {number} has an id that is not like 'R1' or 'R2b'")
        if row[0] in ids:
            raise PlanCheckError("G5", f"(f) id {row[0]} is listed twice")
        ids.append(row[0])
    return ids


def _test_files(section: list[str], ids: list[str]) -> None:
    files: dict[str, set[str]] = {}
    found = False
    for line in section:
        text = line.strip()
        if not text.startswith(TEST_FILES):
            continue
        found = True
        for group in text[len(TEST_FILES) :].split(";"):
            if not group.strip():
                continue
            name, colon, listed = group.partition(":")
            name = name.strip()
            if not colon or not name or any(ch.isspace() for ch in name):
                raise PlanCheckError("G7", "an 'R-test files:' entry is not 'file: R1 R2'")
            for item in re.split(r"[\s,]+", listed.strip()):
                if not item:
                    continue
                if not RED_ID.fullmatch(item):
                    raise PlanCheckError("G7", f"'{name}' lists something that is not a RED id")
                files.setdefault(item, set()).add(name)
    if not found:
        raise PlanCheckError("G7", "section 6 has no 'R-test files:' line")
    for item in ids:
        if item not in files:
            raise PlanCheckError("G7", f"(f) id {item} is not mapped to a test file")
        if len(files[item]) != 1:
            raise PlanCheckError("G7", f"(f) id {item} is mapped to more than one file")


def check(text: str) -> None:
    """Raise ``PlanCheckError`` at the first violation of G0-G7; return when the plan passes."""
    if not isinstance(text, str):
        raise PlanCheckError("G1", "the plan must be text")
    lines = unfenced(text)
    sections = _sections(lines)
    parts = _parts(sections[0])
    _check_numbers(parts["a"])
    failures = _failure_ids(parts["e"])
    tests = _test_ids(parts["f"])
    for item in failures:
        if item not in tests:
            raise PlanCheckError("G6", f"(e) id {item} does not appear in (f)")
    _test_files(sections[6], tests)


def violation(text: str) -> PlanCheckError | None:
    """The first violation, or ``None``."""
    try:
        check(text)
    except PlanCheckError as exc:
        return exc
    return None


# ---------------------------------------------------------------- board plan gate


@dataclass(frozen=True)
class BoardGate:
    """A board with an accepted live orchestrator config.

    ``planners`` holds the ``created_by`` refs (``conn:<name>``) of its planner
    connections; empty when the stored body no longer parses (the label guard and the
    approval check still apply)."""

    board: str
    planners: frozenset[str]

    def is_planner(self, created_by) -> bool:
        return isinstance(created_by, str) and created_by in self.planners


def board_gate(workspace_dir, board: str) -> BoardGate | None:
    """Read-only: the plan gate of ``board`` or ``None`` when it has no accepted live config.

    Never creates ``runner.sqlite3`` or its tables."""
    path = Path(workspace_dir) / "runner.sqlite3"
    if not path.is_file():
        return None
    from mempalace.sqlite_crypto import connect, row_factory

    from . import schema, store

    conn = connect(str(path), timeout=10, isolation_level=None)
    try:
        conn.row_factory = row_factory
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orch_configs'"
        ).fetchone():
            return None
        live = store.accepted_in(conn, board)
    finally:
        conn.close()
    if live is None:
        return None
    try:
        names = live.config().planner_connections
    except schema.ConfigError:
        names = ()
    return BoardGate(board=board, planners=frozenset(f"conn:{name}" for name in names))


def require_plan(text, *, what: str = "plan") -> None:
    """Raise ``ToolError`` (``no: ...``) when ``text`` fails plan-check."""
    found = violation(text if isinstance(text, str) else "")
    if found is not None:
        raise ToolError(f"no: {what} fails plan-check ({found}); see docs/orchestrator.md")


# ---------------------------------------------------------------- command line


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(
        prog="commonplace orch plan-check", description="check a plan against G0-G7"
    )
    parser.add_argument("file", help="plan file (Markdown); '-' reads standard input")
    return parser


def run(args) -> int:
    try:
        if args.file == "-":
            text = sys.stdin.read(MAX_PLAN_BYTES + 1)
        else:
            with open(args.file, encoding="utf-8") as handle:
                text = handle.read(MAX_PLAN_BYTES + 1)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"plan-check: cannot read {args.file}: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    if len(text) > MAX_PLAN_BYTES:
        print("plan-check: plan is too large", file=sys.stderr)
        return 2
    found = violation(text)
    if found is not None:
        print(f"plan-check: {found}")
        return 1
    print("plan-check: OK")
    return 0


def main(argv=None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
