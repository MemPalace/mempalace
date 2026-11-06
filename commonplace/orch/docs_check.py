"""Docs-check: keep ``MODULES.md`` and ``SYSTEM.md`` current with the code.

``commonplace orch docs-check --base <ref> --head <ref>`` compares ``head`` with the merge
base of ``base`` and ``head`` and exits 1 when:

- ``MODULES.md`` is missing at ``head``, or a block lacks one of the fields **Paths**,
  **Owns**, **Promises**, **How it fails**, **Tests**, or has one empty;
- an entry of the repository root or of ``commonplace/`` is not listed exactly once
  (``commonplace/`` itself is split into its entries), or a path is listed twice;
- a changed path is owned by no block (longest listed path wins; a directory ends in
  ``/``);
- a module whose paths changed kept its block unchanged, or ``SYSTEM.md`` did not change
  (a whitespace-only edit counts as unchanged; the check never judges whether the new
  text is accurate).

The last two obligations are waived by a commit trailer ``Docs-waiver: W<n>`` whose id is
listed under ``## Waivers`` in ``SYSTEM.md`` **at the merge base**; any other waiver id
fails the check. A bad ref or any git failure exits 2. ``--init`` prints a starter block
for every unlisted root and ``commonplace/`` entry.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MODULES = "MODULES.md"
SYSTEM = "SYSTEM.md"
FIELDS = ("Paths", "Owns", "Promises", "How it fails", "Tests")
EXEMPT = frozenset({MODULES, SYSTEM})
SPLIT_DIRS = ("commonplace/",)
WAIVER_KEY = "Docs-waiver"
DEFAULT_BASE = "origin/develop"
_BLOCK = re.compile(r"^## (.+?)\s*$")
_FIELD = re.compile(r"^- \*\*(.+?):\*\*\s*(.*)$")
_PATH = re.compile(r"`([^`]+)`")
_WAIVER_ID = re.compile(r"W[1-9][0-9]{0,5}")
_WAIVER_LINE = re.compile(r"^(?:[-*] )?(W[1-9][0-9]{0,5}):\s*\S")
_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


class DocsCheckError(Exception):
    """An infrastructure failure (bad ref, git error): exit 2, never a pass."""


@dataclass
class Block:
    name: str
    fields: dict = field(default_factory=dict)
    paths: tuple = ()
    text: str = ""


@dataclass
class Modules:
    blocks: dict  # name -> Block
    owners: dict  # listed path -> block name
    problems: list


def parse_modules(text: str) -> Modules:
    """Parse ``MODULES.md``; structural problems are returned, not raised."""
    blocks: dict[str, Block] = {}
    problems: list[str] = []
    current: Block | None = None
    lines: list[str] = []
    last_field = None

    def close():
        if current is not None:
            current.text = "\n".join(line.rstrip() for line in lines).strip()

    for line in text.splitlines():
        heading = _BLOCK.match(line)
        if heading:
            close()
            name = heading.group(1)
            if name in blocks:
                problems.append(f"block '{name}' appears twice")
            current = blocks[name] = Block(name)
            lines, last_field = [line], None
            continue
        if current is None:
            continue
        lines.append(line)
        match = _FIELD.match(line)
        if match:
            last_field = match.group(1)
            if last_field in current.fields:
                problems.append(f"block '{current.name}' has field {last_field} twice")
            current.fields[last_field] = match.group(2).strip()
        elif last_field and line.startswith((" ", "\t")) and line.strip():
            current.fields[last_field] = (current.fields[last_field] + " " + line.strip()).strip()
        elif not line.strip():
            last_field = None
    close()
    owners: dict[str, str] = {}
    for block in blocks.values():
        for name in FIELDS:
            if name not in block.fields:
                problems.append(f"block '{block.name}' has no {name} field")
            elif not block.fields[name]:
                problems.append(f"block '{block.name}' has an empty {name} field")
        paths = tuple(_PATH.findall(block.fields.get("Paths", "")))
        if "Paths" in block.fields and block.fields["Paths"] and not paths:
            problems.append(f"block '{block.name}' lists no paths in backticks")
        block.paths = paths
        for path in paths:
            if any(ch in path for ch in "*?[") or path.startswith("/") or ".." in path.split("/"):
                problems.append(f"block '{block.name}' lists '{path}', which is not a plain path")
            elif path in SPLIT_DIRS:
                problems.append(f"'{path}' must be split into its entries")
            if path in owners:
                problems.append(f"'{path}' is listed twice")
            else:
                owners[path] = block.name
    return Modules(blocks, owners, problems)


def owner(path: str, owners: dict) -> str | None:
    """The block of ``path``: the longest listed path equal to it or a directory above it."""
    best = None
    for entry, name in owners.items():
        if path == entry or (entry.endswith("/") and path.startswith(entry)):
            if best is None or len(entry) > len(best[0]):
                best = (entry, name)
    return best[1] if best else None


# ---------------------------------------------------------------- git


def _git(repo, *args, check=True) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key not in _GIT_ENV_DROP}
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            env=env,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocsCheckError(f"git failed: {exc.__class__.__name__}") from None
    if check and result.returncode != 0:
        raise DocsCheckError(f"git {args[0]} failed")
    return result


def resolve(repo, ref: str) -> str:
    if not isinstance(ref, str) or not ref or ref.startswith("-"):
        raise DocsCheckError(f"bad ref: {ref!r}")
    result = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if result.returncode != 0:
        raise DocsCheckError(f"bad ref: {ref}")
    return result.stdout.decode().strip()


def merge_base(repo, base: str, head: str) -> str:
    result = _git(repo, "merge-base", base, head, check=False)
    if result.returncode != 0:
        raise DocsCheckError("base and head have no merge base")
    return result.stdout.decode().strip()


def show(repo, commit: str, path: str) -> str | None:
    if _git(repo, "cat-file", "-e", f"{commit}:{path}", check=False).returncode != 0:
        return None
    return _git(repo, "show", f"{commit}:{path}").stdout.decode("utf-8", "replace")


def _ls(repo, commit: str, tree: str = "") -> list[str]:
    spec = f"{commit}:{tree}" if tree else commit
    out = _git(repo, "ls-tree", "-z", spec).stdout.decode("utf-8", "replace")
    entries = []
    for item in out.split("\0"):
        if not item:
            continue
        meta, _tab, name = item.partition("\t")
        kind = meta.split()[1]
        entries.append(f"{tree}{name}" + ("/" if kind == "tree" else ""))
    return entries


def required_entries(repo, commit: str) -> list[str]:
    """Every root entry (``commonplace/`` split) and every ``commonplace/`` entry."""
    entries = []
    for entry in _ls(repo, commit):
        if entry in SPLIT_DIRS:
            entries.extend(_ls(repo, commit, entry))
        else:
            entries.append(entry)
    return entries


def changed(repo, base: str, head: str) -> list[str]:
    out = _git(repo, "diff", "--name-only", "--no-renames", "-z", base, head).stdout
    return [path for path in out.decode("utf-8", "replace").split("\0") if path]


def waiver_trailers(repo, base: str, head: str) -> list[str]:
    out = _git(
        repo,
        "log",
        f"--format=%(trailers:key={WAIVER_KEY},valueonly,separator=%x00)%x00",
        f"{base}..{head}",
    ).stdout.decode("utf-8", "replace")
    return [item.strip() for item in out.replace("\n", "\0").split("\0") if item.strip()]


def waiver_list(system_text: str | None) -> set[str]:
    """Ids under ``## Waivers`` (one ``W<n>: <scope>`` per line)."""
    ids, inside = set(), False
    for line in (system_text or "").splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Waivers"
            continue
        match = _WAIVER_LINE.match(line.strip()) if inside else None
        if match:
            ids.add(match.group(1))
    return ids


# ---------------------------------------------------------------- the check


def _words(text: str | None) -> str | None:
    """``text`` with every whitespace run collapsed: a whitespace-only edit is no change."""
    return None if text is None else " ".join(text.split())


def check(repo, base: str = DEFAULT_BASE, head: str = "HEAD") -> list[str]:
    """Problems for ``merge_base(base, head)..head``; raises ``DocsCheckError`` on git errors."""
    head_sha = resolve(repo, head)
    base_sha = merge_base(repo, resolve(repo, base), head_sha)
    text = show(repo, head_sha, MODULES)
    if text is None:
        return [f"{MODULES} is missing"]
    modules = parse_modules(text)
    problems = list(modules.problems)
    for entry in required_entries(repo, head_sha):
        if entry not in modules.owners:
            problems.append(f"'{entry}' is not listed in {MODULES}")
    paths = changed(repo, base_sha, head_sha)
    if not paths:
        return problems
    base_text = show(repo, base_sha, MODULES)
    base_modules = parse_modules(base_text) if base_text is not None else None
    allowed = waiver_list(show(repo, base_sha, SYSTEM))
    waived = False
    for item in waiver_trailers(repo, base_sha, head_sha):
        if _WAIVER_ID.fullmatch(item) and item in allowed:
            waived = True
        else:
            problems.append(f"waiver '{item}' is not in the base {SYSTEM} waiver list")
    touched: dict[str, str] = {}
    for path in paths:
        if path in EXEMPT:
            continue
        name = owner(path, modules.owners)
        if name is None and base_modules is not None and show(repo, head_sha, path) is None:
            name = owner(path, base_modules.owners)  # a deleted path keeps its old owner
        if name is None:
            problems.append(f"'{path}' is owned by no {MODULES} block")
            continue
        touched.setdefault(name, path)
    if waived:
        return problems
    for name, path in sorted(touched.items()):
        before = base_modules.blocks.get(name) if base_modules is not None else None
        after = modules.blocks.get(name)
        if before is not None and after is not None and _words(before.text) == _words(after.text):
            problems.append(f"module '{name}' changed ('{path}') but its {MODULES} block did not")
    if SYSTEM not in paths:
        problems.append(f"{SYSTEM} did not change")
    elif _words(show(repo, base_sha, SYSTEM)) == _words(show(repo, head_sha, SYSTEM)):
        problems.append(f"{SYSTEM} changed only in whitespace")
    return problems


def init_text(repo, head: str = "HEAD") -> str:
    """A starter block listing every root and ``commonplace/`` entry not yet listed."""
    head_sha = resolve(repo, head)
    text = show(repo, head_sha, MODULES)
    listed = parse_modules(text).owners if text is not None else {}
    missing = [entry for entry in required_entries(repo, head_sha) if entry not in listed]
    if not missing:
        return ""
    lines = [] if text is not None else ["# Modules", ""]
    lines += ["## unassigned", "", "- **Paths:** " + ", ".join(f"`{p}`" for p in missing)]
    lines += [f"- **{name}:**" for name in FIELDS[1:]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- command line


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    parser = parser or argparse.ArgumentParser(
        prog="commonplace orch docs-check", description="check MODULES.md and SYSTEM.md"
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"base ref (default {DEFAULT_BASE})")
    parser.add_argument("--head", default="HEAD", help="head ref (default HEAD)")
    parser.add_argument("--repo", default=".", help="repository directory (default .)")
    parser.add_argument(
        "--init", action="store_true", help="print a starter block for unlisted entries"
    )
    return parser


def run(args) -> int:
    repo = Path(args.repo)
    try:
        if args.init:
            sys.stdout.write(init_text(repo, args.head))
            return 0
        problems = check(repo, args.base, args.head)
    except DocsCheckError as exc:
        print(f"docs-check: error: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"docs-check: {problem}")
    if problems:
        return 1
    print("docs-check: OK")
    return 0


def main(argv=None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
