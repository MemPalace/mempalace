"""Docs-check: keep the module map and ``SYSTEM.md`` current with the code.

The module map is one block per module. Each block lives in its own file
``docs/modules/<module>.md``; ``MODULES.md`` keeps only the format rules (blocks written
directly in ``MODULES.md`` are still read). ``SYSTEM.md`` may be extended by one ``### ``
section per file in ``docs/system/``. Parallel changes then edit different files and merge
without a git conflict. The fragment files themselves are exempt from the module-block
obligation (they *are* the blocks), and a change under ``docs/system/`` counts as a change
to ``SYSTEM.md``.

``commonplace orch docs-check --base <ref> --head <ref>`` compares ``head`` with the merge
base of ``base`` and ``head`` and exits 1 when:

- ``MODULES.md`` is missing at ``head``, a fragment is malformed (a module file that is
  not exactly one block named like the file, a ``docs/system/`` file that does not start
  with a ``### `` heading), or a block lacks one of the fields **Paths**,
  **Owns**, **Promises**, **How it fails**, **Tests**, or has one empty (including
  empty indented list items); fields accept inline text or blank-separated sub-bullets;
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
MODULES_DIR = "docs/modules/"
SYSTEM_DIR = "docs/system/"
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
            item = line.strip()
            if item in {"-", "*", "+"}:
                problems.append(f"block '{current.name}' has an empty {last_field} list item")
            else:
                current.fields[last_field] = (current.fields[last_field] + " " + item).strip()
        elif line.strip():
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


def is_exempt(path: str) -> bool:
    """Paths that *are* the docs: they never demand a module block of their own."""
    return path in EXEMPT or path.startswith((MODULES_DIR, SYSTEM_DIR))


def is_system_path(path: str) -> bool:
    return path == SYSTEM or path.startswith(SYSTEM_DIR)


def module_fragment_problems(entry: str, text: str) -> list[str]:
    """Problems of one ``docs/modules/`` entry: one ``## <name>`` block, named like the file."""
    stem = entry[len(MODULES_DIR) :]
    if entry.endswith("/") or not stem.endswith(".md"):
        return [f"'{entry}' is not a module file ({MODULES_DIR}<module>.md)"]
    stem = stem[: -len(".md")]
    names = list(parse_modules(text).blocks)
    if names != [stem]:
        found = ", ".join(f"'{name}'" for name in names) or "none"
        return [f"'{entry}' must hold exactly one '## {stem}' block (found {found})"]
    return []


def system_fragment_problems(entry: str, text: str) -> list[str]:
    """Problems of one ``docs/system/`` entry: a Markdown file starting with a ``### `` heading."""
    if entry.endswith("/") or not entry.endswith(".md"):
        return [f"'{entry}' is not a system section file ({SYSTEM_DIR}<section>.md)"]
    first = next((line for line in text.splitlines() if line.strip()), "")
    if not first.startswith("### ") or not first[4:].strip():
        return [f"'{entry}' must start with a '### <title>' heading"]
    return []


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


def _fragments(repo, commit: str, directory: str) -> list[tuple[str, str]]:
    """``(entry, text)`` of every entry of ``directory`` at ``commit``, sorted by name."""
    if _git(repo, "cat-file", "-e", f"{commit}:{directory}", check=False).returncode != 0:
        return []
    return [
        (entry, "" if entry.endswith("/") else show(repo, commit, entry) or "")
        for entry in sorted(_ls(repo, commit, directory))
    ]


def module_texts(repo, commit: str) -> list[str] | None:
    """``MODULES.md`` followed by every module file; None when ``MODULES.md`` is missing."""
    legacy = show(repo, commit, MODULES)
    if legacy is None:
        return None
    return [legacy] + [text for _entry, text in _fragments(repo, commit, MODULES_DIR)]


def load_modules(repo, commit: str) -> Modules | None:
    """The module map at ``commit`` from ``MODULES.md`` plus ``docs/modules/``."""
    texts = module_texts(repo, commit)
    if texts is None:
        return None
    modules = parse_modules("\n".join(texts))
    modules.problems[:0] = [
        problem
        for entry, text in _fragments(repo, commit, MODULES_DIR)
        for problem in module_fragment_problems(entry, text)
    ]
    return modules


def system_text(repo, commit: str) -> str | None:
    """``SYSTEM.md`` followed by every ``docs/system/`` section; None when there is neither."""
    base = show(repo, commit, SYSTEM)
    parts = _fragments(repo, commit, SYSTEM_DIR)
    if base is None and not parts:
        return None
    return "\n".join([base or ""] + [text for _entry, text in parts])


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
    modules = load_modules(repo, head_sha)
    if modules is None:
        return [f"{MODULES} is missing"]
    problems = list(modules.problems)
    for entry, text in _fragments(repo, head_sha, SYSTEM_DIR):
        problems += system_fragment_problems(entry, text)
    for entry in required_entries(repo, head_sha):
        if entry not in modules.owners:
            problems.append(f"'{entry}' is not listed in {MODULES}")
    paths = changed(repo, base_sha, head_sha)
    if not paths:
        return problems
    base_modules = load_modules(repo, base_sha)
    allowed = waiver_list(show(repo, base_sha, SYSTEM))
    waived = False
    for item in waiver_trailers(repo, base_sha, head_sha):
        if _WAIVER_ID.fullmatch(item) and item in allowed:
            waived = True
        else:
            problems.append(f"waiver '{item}' is not in the base {SYSTEM} waiver list")
    touched: dict[str, str] = {}
    for path in paths:
        if is_exempt(path):
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
    if not any(is_system_path(path) for path in paths):
        problems.append(f"{SYSTEM} did not change")
    elif _words(system_text(repo, base_sha)) == _words(system_text(repo, head_sha)):
        problems.append(f"{SYSTEM} changed only in whitespace")
    return problems


def init_text(repo, head: str = "HEAD") -> str:
    """A starter block (save it as ``docs/modules/<module>.md``) for every unlisted entry."""
    head_sha = resolve(repo, head)
    modules = load_modules(repo, head_sha)
    listed = modules.owners if modules is not None else {}
    missing = [entry for entry in required_entries(repo, head_sha) if entry not in listed]
    if not missing:
        return ""
    lines = [] if modules is not None else ["# Modules", ""]
    lines += ["## unassigned", "", "- **Paths:** " + ", ".join(f"`{p}`" for p in missing)]
    lines += [f"- **{name}:**" for name in FIELDS[1:]]
    return "\n".join(lines) + "\n"


def assembled(repo, head: str, what: str) -> str:
    """The module map (``MODULES.md`` plus every module file) or ``SYSTEM.md`` plus sections."""
    head_sha = resolve(repo, head)
    if what == "system":
        text = system_text(repo, head_sha)
        parts = [] if text is None else [text]
    else:
        parts = module_texts(repo, head_sha) or []
    return "\n".join(part.rstrip("\n") + "\n" for part in parts)


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
    parser.add_argument(
        "--print",
        dest="print_what",
        choices=("modules", "system"),
        help="print the whole module map or SYSTEM.md with its docs/system/ sections",
    )
    return parser


def run(args) -> int:
    repo = Path(args.repo)
    try:
        if args.init:
            sys.stdout.write(init_text(repo, args.head))
            return 0
        if args.print_what:
            sys.stdout.write(assembled(repo, args.head, args.print_what))
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
