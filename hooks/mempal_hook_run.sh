#!/bin/bash
# Runs `mempalace hook run "$@"`, stdin included, through the first of
# $MEMPAL_PYTHON, `mempalace` on PATH, python3, or python that can import
# mempalace. Shared by the legacy hook wrappers in this directory.
#
# When none can, it prints the empty hook reply and exits 0 so the harness
# carries on, as the older scripts did.
set -uo pipefail

if [ -n "${MEMPAL_PYTHON:-}" ] && "${MEMPAL_PYTHON}" -c "import mempalace" </dev/null >/dev/null 2>&1; then
    exec "${MEMPAL_PYTHON}" -m mempalace hook run "$@"
fi
if command -v mempalace >/dev/null 2>&1; then
    exec mempalace hook run "$@"
fi
for python_bin in python3 python; do
    if command -v "${python_bin}" >/dev/null 2>&1 \
        && "${python_bin}" -c "import mempalace" </dev/null >/dev/null 2>&1; then
        exec "${python_bin}" -m mempalace hook run "$@"
    fi
done

echo "MemPalace hook: no interpreter can import mempalace; set MEMPAL_PYTHON" >&2
printf '{}'
