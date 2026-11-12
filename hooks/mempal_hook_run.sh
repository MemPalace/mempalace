#!/bin/bash
# Runs `mempalace hook run "$@"`, stdin included, through the first of
# $MEMPAL_PYTHON, `mempalace` on PATH, python3, or python that can import
# mempalace.
#
# This file is shared by every hook wrapper. The Claude Code and Codex plugins
# install from their own folders, so each carries a byte-identical copy
# (.claude-plugin/hooks/, .codex-plugin/hooks/); tests/test_hook_resolver.py
# keeps the copies in step. Edit this file and copy it over.
#
# When no interpreter can import mempalace, the hook prints the empty reply
# and exits 0 so the harness carries on. A leading --on-missing=error makes it
# exit 1 with the message instead, for harnesses that should show the failure.
set -uo pipefail

on_missing=empty
if [ "${1:-}" = "--on-missing=error" ]; then
    on_missing=error
    shift
fi

# Hooks run in the user's project directory, which may be untrusted. Python puts
# that directory first on sys.path for `-c` and `-m`, so a project holding
# mempalace/ or mempalace.py would run instead of the installed package.
# PYTHONSAFEPATH stops that on Python 3.11+, for this hook and the mines it
# spawns; the probe and launch below also drop the empty entry `-c` adds, which
# covers 3.9 and 3.10. The working directory itself stays put.
export PYTHONSAFEPATH=1
drop_cwd='import sys
if sys.path and sys.path[0] == "": del sys.path[0]'
probe="${drop_cwd}
import mempalace"
launch="${drop_cwd}
import runpy
runpy.run_module(\"mempalace\", run_name=\"__main__\", alter_sys=True)"

if [ -n "${MEMPAL_PYTHON:-}" ] && "${MEMPAL_PYTHON}" -c "${probe}" </dev/null >/dev/null 2>&1; then
    exec "${MEMPAL_PYTHON}" -c "${launch}" hook run "$@"
fi
if command -v mempalace >/dev/null 2>&1; then
    exec mempalace hook run "$@"
fi
for python_bin in python3 python; do
    if command -v "${python_bin}" >/dev/null 2>&1 \
        && "${python_bin}" -c "${probe}" </dev/null >/dev/null 2>&1; then
        exec "${python_bin}" -c "${launch}" hook run "$@"
    fi
done

echo "MemPalace hook error: could not find a runnable mempalace command or module; set MEMPAL_PYTHON" >&2
if [ "$on_missing" = error ]; then
    exit 1
fi
printf '{}'
