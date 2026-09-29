"""__init__-level guards that must take effect before transitive imports."""

import os
import subprocess
import sys
import sysconfig
import tempfile

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
    without crashing.

    The subprocess runs from a neutral directory (the temp dir) rather
    than inheriting pytest's CWD. When the CWD is the repo checkout,
    the local ``mempalace/`` source directory shadows the installed
    package, so ``mempalace.__file__`` resolves to the checkout and its
    parent directory is only reachable through the empty-string CWD
    marker on sys.path -- which the ``if p`` filter excludes. Running
    from the temp dir makes the parent resolve to the installed
    location consistently, so the over-strip assertion below is
    layout-agnostic (a check on the actual filter behavior, not on
    whatever happens to be in the CWD).
    """
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
        cwd=tempfile.gettempdir(),
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
    assert f"ENV: {expected_env}" in out, (
        f"PYTHONPATH should be preserved by package import: {diag}"
    )
    assert "SENTINEL_IN_PATH: False" in out, f"sentinel-prefix leak: {diag}"
    # Filter must not over-strip: the mempalace package itself must remain
    # importable, so its parent directory must survive on sys.path.
    assert "MEMPALACE_PARENT_PRESENT: True" in out, (
        f"filter over-stripped sys.path (mempalace parent gone): {diag}"
    )


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
        cwd=tempfile.gettempdir(),
        capture_output=True,
        text=True,
        check=False,
    )
    diag = f"rc={result.returncode}; stdout={result.stdout!r}; stderr={result.stderr!r}"
    assert result.returncode == 0, f"subprocess failed: {diag}"
    assert "CWD_IN_PATH: True" in result.stdout, f"cwd marker dropped: {diag}"
    assert "DOT_IN_PATH: False" in result.stdout, f"dot leak survived: {diag}"


@pytest.mark.parametrize("path_name", ["purelib", "platlib"])
def test_init_keeps_the_running_environments_own_site_packages(path_name):
    """A PYTHONPATH entry naming THIS interpreter's own site-packages is
    redundant, not foreign, and must survive.

    Embedding hosts routinely launch a venv with PYTHONPATH pointing at that
    same venv's site-packages -- Electron backends, IDE language servers,
    ``python -m`` wrappers. Stripping it leaves ``import mempalace`` working
    (it resolved before the guard ran) and every dependency import after it
    failing with a ModuleNotFoundError raised from inside mempalace while the
    same import from the same interpreter succeeds (#2484).

    The foreign sentinel in the same PYTHONPATH is the control: the wrong-ABI
    protection must still strip it.
    """
    own_site = sysconfig.get_paths()[path_name]
    env = os.environ.copy()
    env["PYTHONPATH"] = own_site + os.pathsep + f"{_LEAK_PREFIX}/foreign"
    code = (
        "import mempalace, os, sys; "
        f"own = {own_site!r}; prefix = {_LEAK_PREFIX!r}; "
        "norm = lambda p: os.path.normcase(os.path.normpath(os.path.realpath(p))); "
        "print('OWN_PRESENT:', any(norm(p) == norm(own) for p in sys.path if p)); "
        "print('FOREIGN_PRESENT:', any(prefix in (p or '') for p in sys.path))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tempfile.gettempdir(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "OWN_PRESENT: True" in result.stdout, result.stdout
    assert "FOREIGN_PRESENT: False" in result.stdout, result.stdout


@pytest.mark.parametrize("location", ["other-python", "nested-venv", "site-packages-child"])
def test_init_strips_foreign_paths_beneath_the_running_prefix(location):
    """A shared prefix or nested venv does not make a foreign path our own."""
    own_site = sysconfig.get_paths()["purelib"]
    foreign_sites = {
        "other-python": os.path.join(sys.prefix, "lib", "python0.0", "site-packages"),
        "nested-venv": os.path.join(sys.prefix, "other-venv", "Lib", "site-packages"),
        "site-packages-child": os.path.join(own_site, "foreign"),
    }
    foreign_site = foreign_sites[location]
    env = os.environ.copy()
    env["PYTHONPATH"] = foreign_site + os.pathsep + own_site
    code = (
        "import mempalace, os, sys; "
        f"own = {own_site!r}; foreign = {foreign_site!r}; "
        "norm = lambda p: os.path.normcase(os.path.normpath(os.path.realpath(p))); "
        "print('OWN_PRESENT:', any(norm(p) == norm(own) for p in sys.path if p)); "
        "print('FOREIGN_PRESENT:', any(norm(p) == norm(foreign) for p in sys.path if p)); "
        "import pydantic_core; print('DEPENDENCY_IMPORTED:', pydantic_core.__name__)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tempfile.gettempdir(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    diag = f"location={location!r}; stdout={result.stdout!r}; stderr={result.stderr!r}"
    assert result.returncode == 0, diag
    assert "OWN_PRESENT: True" in result.stdout, diag
    assert "FOREIGN_PRESENT: False" in result.stdout, diag
    assert "DEPENDENCY_IMPORTED: pydantic_core" in result.stdout, diag
