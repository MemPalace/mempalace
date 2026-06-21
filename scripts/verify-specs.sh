#!/usr/bin/env bash
set -euo pipefail
find specs -name '*.md' | while read -r f; do
  n="$(grep -coE '\([^)]+:L[0-9]+' "$f" || true)"
  printf '%4s  %s\n' "${n:-0}" "$f"
done | sort -n | tee /tmp/spec-citation-report.txt
echo "--- coverage ---"
echo "source modules: $(git ls-files 'mempalace/*.py' | wc -l)   source specs: $(find specs/src -name '*.md' 2>/dev/null | wc -l)"
echo "test files:     $(git ls-files 'tests/*.py'     | wc -l)   test specs:   $(find specs/tests -name '*.md' 2>/dev/null | wc -l)"
echo "--- specs with ZERO citations (must be empty) ---"
awk '$1==0{print $2}' /tmp/spec-citation-report.txt
