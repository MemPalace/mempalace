"""__init__-level guards that must take effect before transitive imports."""

import os
import subprocess
import sys

import pytest


_LEAK_PREFIX = "/__mempalace_leak_test_sentinel__"


@pytest.mark.parametrize(
    "pythonpath",
    [
        f"{_LEAK_PREFIX}/single",
        f"{_LEAK_PREFIX}/a{os.pathsep}{_LEAK_PREFIX}/b",
        f"{_LEAK_PREFIX}/with-trailing{os.sep}",
        f"{os.pathsep}{_LEAK_PREFIX}/leading-sep",
        ".",
        "",
        None,
    ],
    ids=["single", "multi", "trailing-sep", "leading-pathsep", "dot", "empty", "unset"],
)
def test_init_filters_sys_path_from_leaked_pythonpath(pythonpath):
    """Package init must remove sentinel-prefixed entries from sys.path
    so transitive imports do not pull compiled extensions from the
    leaked PYTHONPATH. os.environ['PYTHONPATH'] is left intact so host
    applications embedding mempalace as a library keep their env for
    their own subprocesses; the env strip lives in the CLI/MCP entry
    points (see test_cli.py / test_mcp_server.py).

    Asserts on the sentinel substring directly so the test does not
    couple to the production normalization logic. The dot/empty/unset
    cases additionally exercise the early-return / collision paths
    without crashing."""
    env = os.environ.copy()
    if pythonpath is None:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = pythonpath
    code = (
        "import mempalace, os, sys; "
        f"prefix = {_LEAK_PREFIX!r}; "
        "mempalace_parent = os.path.dirname(os.path.dirname(mempalace.__file__)); "
        "print('ENV:', repr(os.environ.get('PYTHONPATH'))); "
        "print('SENTINEL_IN_PATH:', any(prefix in (p or '') for p in sys.path)); "
        "print('MEMPALACE_PARENT_PRESENT:', any("
        "os.path.normcase(os.path.normpath(p)) == os.path.normcase(os.path.normpath(mempalace_parent)) "
        "for p in sys.path if p))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    diag = (
        f"input={pythonpath!r}; rc={result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    assert result.returncode == 0, f"subprocess failed: {diag}"
    out = result.stdout
    # Env must be preserved verbatim: embedded callers may need it.
    expected_env = repr(pythonpath) if pythonpath is not None else repr(None)
    assert (
        f"ENV: {expected_env}" in out
    ), f"PYTHONPATH should be preserved by package import: {diag}"
    assert "SENTINEL_IN_PATH: False" in out, f"sentinel-prefix leak: {diag}"
    # Filter must not over-strip: the mempalace package itself must remain
    # importable, so its parent directory must survive on sys.path.
    assert (
        "MEMPALACE_PARENT_PRESENT: True" in out
    ), f"filter over-stripped sys.path (mempalace parent gone): {diag}"


def test_init_preserves_cwd_marker_when_pythonpath_collides():
    """PYTHONPATH='.' normalizes to the same value as the empty-string
    CWD marker on sys.path. The strip must remove '.' from sys.path
    without collapsing the implicit current-directory entry."""
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    code = (
        "import mempalace, sys; "
        "print('CWD_IN_PATH:', '' in sys.path); "
        "print('DOT_IN_PATH:', '.' in sys.path)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    diag = f"rc={result.returncode}; stdout={result.stdout!r}; " f"stderr={result.stderr!r}"
    assert result.returncode == 0, f"subprocess failed: {diag}"
    assert "CWD_IN_PATH: True" in result.stdout, f"cwd marker dropped: {diag}"
    assert "DOT_IN_PATH: False" in result.stdout, f"dot leak survived: {diag}"
