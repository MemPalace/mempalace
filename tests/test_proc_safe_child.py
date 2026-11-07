"""Child interpreters never import from the working directory.

The daemon, the HTTP hub and the SQLite lock holder are started from whatever
directory the user stands in, which may be a repository they have only just
cloned. ``python -m`` and ``python -c`` both put that directory first on
``sys.path``, so a ``mempalace/`` package planted there would run instead of
the installed one. ``_proc.safe_module_argv`` and ``_proc.safe_child_env``
are the one way children are started.
"""

import os
import subprocess
import sys

from mempalace import _proc
from mempalace.backends import _inproc_sqlite

HIJACK = 'raise SystemExit("HIJACKED")\n'


def _plant_hostile_package(tmp_path):
    pkg = tmp_path / "mempalace"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(HIJACK, encoding="utf-8")
    (pkg / "__main__.py").write_text(HIJACK, encoding="utf-8")
    return tmp_path


def _without_safepath():
    env = dict(os.environ)
    env.pop("PYTHONSAFEPATH", None)
    return env


def test_safe_module_argv_shape():
    argv = _proc.safe_module_argv("mempalace.daemon", "serve", "--palace", "p")
    assert argv[0] == sys.executable
    assert argv[1] == "-c"
    assert "runpy.run_module('mempalace.daemon'" in argv[2]
    assert "del sys.path[0]" in argv[2]
    assert argv[3:] == ["serve", "--palace", "p"]
    assert "-m" not in argv


def test_safe_module_argv_runs_the_installed_package_from_a_hostile_cwd(tmp_path):
    cwd = _plant_hostile_package(tmp_path)
    # Control: the hazard is real. Without the guard, ``-m`` runs the planted
    # package (on 3.11+ only when PYTHONSAFEPATH is unset, hence the env).
    plain = subprocess.run(
        [sys.executable, "-m", "mempalace", "--version"],
        cwd=cwd,
        env=_without_safepath(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "HIJACKED" in plain.stdout + plain.stderr

    safe = subprocess.run(
        _proc.safe_module_argv("mempalace", "--version"),
        cwd=cwd,
        env=_without_safepath(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "HIJACKED" not in safe.stdout + safe.stderr
    assert safe.returncode == 0, safe.stderr
    assert "MemPalace" in safe.stdout


def test_safe_module_argv_passes_arguments_through(tmp_path):
    script_pkg = tmp_path / "probe_pkg"
    script_pkg.mkdir()
    (script_pkg / "__init__.py").write_text("", encoding="utf-8")
    (script_pkg / "__main__.py").write_text(
        "import sys; print('|'.join(sys.argv[1:])); print('' in sys.path)",
        encoding="utf-8",
    )
    env = _without_safepath()
    env["PYTHONPATH"] = str(tmp_path)
    out = subprocess.run(
        _proc.safe_module_argv("probe_pkg", "a", "--flag", "b c"),
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0] == "a|--flag|b c"
    assert lines[1] == "False"  # the empty (cwd) entry is gone


def test_safe_child_env_sets_safepath_and_copies():
    base = {"A": "1"}
    env = _proc.safe_child_env(base)
    assert env == {"A": "1", "PYTHONSAFEPATH": "1"}
    assert base == {"A": "1"}
    assert _proc.safe_child_env()["PYTHONSAFEPATH"] == "1"


def test_hold_script_drops_the_cwd_before_importing_sqlite():
    script = _inproc_sqlite._HOLD_SCRIPT
    drop = script.index("del sys.path[0]")
    assert script.index("sqlite3") > drop
    assert script.index("pathname2url") > drop


def test_hold_script_ignores_a_planted_sqlite3_module(tmp_path):
    (tmp_path / "sqlite3.py").write_text(HIJACK, encoding="utf-8")
    (tmp_path / "urllib").mkdir()
    (tmp_path / "urllib" / "__init__.py").write_text(HIJACK, encoding="utf-8")
    db = tmp_path / "x.sqlite3"
    import sqlite3

    sqlite3.connect(db).close()
    proc = subprocess.Popen(
        [sys.executable, "-c", _inproc_sqlite._HOLD_SCRIPT, str(db)],
        cwd=tmp_path,
        env=_without_safepath(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The holder reports its state on one stdout line once the real
        # sqlite3 opened the database; a hijacked import exits with the
        # marker on stderr instead.
        first = proc.stdout.readline().strip()
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            _out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate(timeout=10)
    assert "HIJACKED" not in err
    assert first in {"ready", "busy", "idle", "failed"}, (first, err)
