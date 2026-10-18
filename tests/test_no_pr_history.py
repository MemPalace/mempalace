"""Comments, docstrings and CLI help state constraints, not PR history.

A reference like ``(#1234)``, ``PR 1234`` or a plan label like ``W1-12`` tells
a reader where a line came from, not why it is there, and it goes stale the
moment the code moves on. This scans the package and the shell hooks and fails
on any such reference in a comment, a docstring, or an argparse ``help=`` /
``description=`` / ``epilog=`` string. Ordinary string literals are not
scanned: they are data, not commentary. Tests are not scanned either; a test
may cite the issue it guards as regression context.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_HISTORY = re.compile(
    r"(?<![\w/])#\d{3,5}\b"  # (#1234), see #1234 — but not owner/repo#1234 or chroma#6975
    r"|\b(?:PR|pull request|issue)s?\s*#?\d{3,5}\b"
    r"|\bfixed in\s+(?:PR\s*)?#?\d{3,5}\b"
    r"|\b(?:W1B|W[1-5])-\d{2}\b"  # refactor-plan item labels
    r"|\bD-\d{2}\b",  # refactor-plan decision labels
    re.IGNORECASE,
)

_HELP_KEYWORDS = {"help", "description", "epilog"}


def _python_texts(path: Path):
    source = path.read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            yield tok.start[0], tok.string
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                yield node.body[0].lineno, doc
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in _HELP_KEYWORDS:
                    for sub in ast.walk(kw.value):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            yield sub.lineno, sub.value


def _shell_texts(path: Path):
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#") and not stripped.startswith("#!"):
            yield lineno, stripped


def _offenders():
    found = []
    targets = [(p, _python_texts) for p in sorted((ROOT / "mempalace").rglob("*.py"))]
    targets += [(p, _shell_texts) for p in sorted((ROOT / "hooks").rglob("*.sh"))]
    for path, reader in targets:
        rel = path.relative_to(ROOT).as_posix()
        for lineno, text in reader(path):
            for match in _HISTORY.finditer(text):
                found.append(f"{rel}:{lineno}: {match.group(0)!r}")
    return found


def test_comments_docstrings_and_help_cite_no_pr_history():
    offenders = _offenders()
    assert not offenders, "PR/issue/plan references in commentary:\n" + "\n".join(offenders)


def test_the_pattern_spares_upstream_links_and_rfcs():
    assert not _HISTORY.search("see chroma-core/chroma#6975 upstream")
    assert not _HISTORY.search("RFC 002 section 5.6")
    assert _HISTORY.search("guards the regression from (#1234)")
    assert _HISTORY.search("follows plan item W1-12")
