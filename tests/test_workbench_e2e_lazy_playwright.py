"""The browser e2e modules must import without the playwright package.

Without CP_BROWSER_E2E=1 every test in tests/workbench_e2e/ is skipped, but pytest still imports
the modules to collect them. The dev install has no playwright, so a module-level
``import playwright`` / ``from playwright...`` there is a collection error that fails the full suite.
"""

import ast
from pathlib import Path

import pytest

E2E = Path(__file__).parent / "workbench_e2e"


def _module_level_playwright_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "playwright"]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "playwright":
            found.append(node.module)
    return found


@pytest.mark.parametrize("path", sorted(E2E.glob("*.py")), ids=lambda p: p.name)
def test_no_module_level_playwright_import(path):
    assert _module_level_playwright_imports(path) == []
