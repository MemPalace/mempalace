"""Performance budget regression tests.

CLAUDE.md lists two non-negotiable latency targets under "Design Principles":

    - Hooks under 500ms.
    - Startup injection under 100ms.

Prior to this module those were prose claims with no enforcement. A new
contributor could eagerly import chromadb from mempalace/__init__.py and
the design promise would silently regress — a lean package import is the
concrete lower bound for both claims, so the first thing we protect is
the import path.

Tests live in tests/benchmarks/ so they are excluded from the default
``pytest tests/ --ignore=tests/benchmarks`` run and do not slow the main
CI loop. Invoke them explicitly when validating performance-sensitive
changes::

    pytest tests/benchmarks/test_performance_budgets.py

Each measurement runs in a fresh Python subprocess so the timing is a
true cold-start number, not polluted by anything pytest imported first.
"""

import os
import subprocess
import sys

# Budgets declared in CLAUDE.md.
STARTUP_BUDGET_MS = 100  # "Startup injection under 100ms"

# CI multiplier — shared runners are noticeably slower than local machines
# and share CPU with other jobs. Local dev stays on the tight budget;
# CI gets headroom so genuine regressions still stand out but runner
# jitter does not flake the suite.
CI_MULTIPLIER = 3 if os.environ.get("CI") else 1


def _measure_cold_import_ms(import_line: str) -> float:
    """Time ``import_line`` inside a fresh Python interpreter.

    Running in a subprocess guarantees a cold import — no modules inherited
    from the test runner, no warm bytecode cache interference beyond what a
    real startup would see.
    """
    code = (
        "import time\n"
        "_t = time.perf_counter()\n"
        f"{import_line}\n"
        "print((time.perf_counter() - _t) * 1000)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


class TestStartupBudget:
    """Lock the import path under the 100ms startup-injection budget."""

    def test_package_import_under_startup_budget(self):
        budget_ms = STARTUP_BUDGET_MS * CI_MULTIPLIER
        elapsed_ms = _measure_cold_import_ms("import mempalace")
        assert elapsed_ms < budget_ms, (
            f"'import mempalace' took {elapsed_ms:.1f}ms — "
            f"budget is {budget_ms}ms ({STARTUP_BUDGET_MS}ms × {CI_MULTIPLIER}× CI). "
            "CLAUDE.md promises startup injection under 100ms; a regression in "
            "mempalace/__init__.py (typically a new eager import) will break "
            "that claim. Lazy-import the heavy dependency or move the work "
            "into a function."
        )

    def test_cli_import_under_startup_budget(self):
        """``from mempalace import cli`` is the path hooks take on invocation.

        If this regresses, every hook invocation pays the cost before any
        user work happens, which directly violates the 500ms hook budget
        and the 100ms startup injection budget.
        """
        budget_ms = STARTUP_BUDGET_MS * CI_MULTIPLIER
        elapsed_ms = _measure_cold_import_ms("from mempalace import cli")
        assert elapsed_ms < budget_ms, (
            f"'from mempalace import cli' took {elapsed_ms:.1f}ms — "
            f"budget is {budget_ms}ms. This is the import path hooks use, so "
            "any regression here adds latency to every Stop/PreCompact hook "
            "invocation before any real work begins."
        )
