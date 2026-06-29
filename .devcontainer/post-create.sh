#!/usr/bin/env bash
set -euo pipefail

echo "=== MemPalace Dev Container Setup ==="

pip install -e ".[dev]"

# Match CI's ruff pin (pyproject only sets a floor; without this contributors
# get a newer ruff locally than CI runs, causing phantom lint failures).
pip install "ruff>=0.4.0,<0.5"

pip install pre-commit
pre-commit install

echo ""
echo "=== Verification ==="
echo "python: $(python --version)"
echo "pytest: $(python -m pytest --version 2>&1 | head -1)"
echo "ruff:   $(python -m ruff --version 2>&1 | head -1)"
echo ""
echo "Ready. Run: pytest tests/ -v --ignore=tests/benchmarks"
