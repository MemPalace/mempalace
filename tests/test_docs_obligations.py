"""docs-check against the merge base with the development branch.

Every change must keep ``MODULES.md`` and ``SYSTEM.md`` current (see ``SYSTEM.md``,
"Keeping this file current"). Outside a git checkout (an installed package) there is
nothing to compare and the test skips; inside one it fails closed when the base ref
cannot be resolved.
"""

from pathlib import Path

import pytest

from commonplace.orch import docs_check

REPO = Path(__file__).resolve().parents[1]
BASE = "origin/develop"


def test_docs_obligations_hold_against_the_development_branch(capsys):
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout")
    code = docs_check.main(["--repo", str(REPO), "--base", BASE, "--head", "HEAD"])
    out = capsys.readouterr()
    assert code == 0, out.out + out.err
