"""The fact readers must not reach ``commonplace.rules``.

The quick affected-test selector follows imports, function-level ones included. A
single ``from commonplace.rules import checks`` inside ``knowledge_graph`` made
nearly every change reach most of the suite, so quick checks fell back to the
full run. The read deadline lives in the stdlib-only ``mempalace.deadline``;
``commonplace.rules.checks`` re-uses it, so the two never drift.
"""

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
READERS = ("mempalace/knowledge_graph.py", "mempalace/fact_index.py", "mempalace/searcher/facts.py")

# A fresh interpreter, so modules loaded by other tests cannot hide the edge.
PROBE = textwrap.dedent(
    """
    import os, sys, tempfile
    from mempalace.knowledge_graph import KnowledgeGraph
    from mempalace.searcher.facts import search_facts

    kg = KnowledgeGraph(os.path.join(tempfile.mkdtemp(), "kg.sqlite3"))
    kg.add_triple("Max", "likes", "chess", wing="wing_a")
    allow = kg.read_candidates()["facts"]
    hits = kg.read_projection(allow, lambda: search_facts(kg, "chess", limit=3), search=True)
    assert len(allow) == 1 and len(hits) == 1, (allow, hits)
    print("\\n".join(sorted(m for m in sys.modules if m.split(".")[0] == "commonplace")))
    """
)


def test_reading_and_searching_facts_never_loads_commonplace():
    done = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": "", "HOME": str(ROOT)},
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    loaded = done.stdout.split()
    assert "commonplace.rules" not in loaded
    assert loaded == []


@pytest.mark.parametrize("path", READERS)
def test_fact_readers_have_no_commonplace_import_anywhere(path):
    tree = ast.parse((ROOT / path).read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            found.append(node.module or "")
    assert [name for name in found if name.split(".")[0] == "commonplace"] == []


def test_rules_checks_and_fact_readers_share_one_deadline(monkeypatch):
    from commonplace.rules import checks
    from mempalace import deadline

    assert checks._deadline is deadline._deadline
    with checks.deadline(5):
        assert 0 < deadline.remaining(100) <= 5
    assert deadline.remaining(100) == 100
    # checks keeps its own clock, which the rules tests freeze.
    monkeypatch.setattr(checks, "monotonic", lambda: 0.0)
    with checks.deadline(5):
        assert checks.remaining(100) == 5
