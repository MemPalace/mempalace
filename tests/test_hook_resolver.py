"""The shared hook resolver and the plugin copies of it.

``hooks/mempal_hook_run.sh`` picks the interpreter for every hook wrapper.
The Claude Code and Codex plugins install from their own folders, so each
ships a copy; these tests keep the copies byte-identical and check the
Codex wrapper and the resolver's failure modes.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOLVER = REPO_ROOT / "hooks" / "mempal_hook_run.sh"
RESOLVER_COPIES = [
    REPO_ROOT / ".claude-plugin" / "hooks" / "mempal_hook_run.sh",
    REPO_ROOT / ".codex-plugin" / "hooks" / "mempal_hook_run.sh",
]
CODEX_HOOK = REPO_ROOT / ".codex-plugin" / "hooks" / "mempal-hook.sh"
BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(BASH is None, reason="bash required for hook script tests")


@pytest.mark.parametrize("copy", RESOLVER_COPIES, ids=lambda p: p.parent.parent.name)
def test_plugin_copy_matches_the_shared_resolver(copy: Path) -> None:
    assert copy.read_bytes() == RESOLVER.read_bytes(), (
        f"{copy.relative_to(REPO_ROOT)} drifted from hooks/mempal_hook_run.sh; copy it over"
    )


def _write_executable(path: Path, content: str) -> None:
    # Path.write_text(newline=) only exists from Python 3.10.
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    path.chmod(0o755)


def _recorder(args_file: Path, stdin_file: Path, *, python: bool) -> str:
    # A python stub answers the `-c` import check and records only the launch,
    # whose code calls run_module, minus the `-c <code>` pair.
    probe = (
        'if [ "$1" = "-c" ]; then\n  case "$2" in *run_module*) shift 2 ;; *) exit 0 ;; esac\nfi\n'
        if python
        else ""
    )
    return (
        "#!/bin/sh\n"
        f"{probe}"
        f'printf \'%s\' "$*" > "{args_file.as_posix()}"\n'
        'payload=""\n'
        'while IFS= read -r line || [ -n "$line" ]; do\n'
        '  payload="${payload}${line}"\n'
        "done\n"
        f'printf \'%s\' "$payload" > "{stdin_file.as_posix()}"\n'
        "printf '{}\\n'\n"
    )


def _run(script: Path, args: list, payload: str, bin_dir: Path) -> subprocess.CompletedProcess:
    assert BASH is not None
    env = os.environ.copy()
    env.pop("MEMPAL_PYTHON", None)
    env["PATH"] = str(bin_dir)
    return subprocess.run(
        [BASH, script.as_posix(), *args],
        input=payload,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=env,
    )


@needs_bash
@pytest.mark.parametrize("runner", ["mempalace", "python3", "python"])
def test_codex_hook_forwards_through_the_resolver(tmp_path, runner):
    args_file = tmp_path / "args.txt"
    stdin_file = tmp_path / "stdin.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / runner, _recorder(args_file, stdin_file, python=runner != "mempalace")
    )

    payload = '{"session_id":"codex-1"}'
    result = _run(CODEX_HOOK, ["stop"], payload, bin_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "{}\n"
    assert args_file.read_text(encoding="utf-8") == "hook run --hook stop --harness codex"
    assert stdin_file.read_text(encoding="utf-8") == payload


@needs_bash
def test_codex_hook_reports_a_missing_mempalace(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    result = _run(CODEX_HOOK, ["stop"], "{}", bin_dir)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "could not find a runnable mempalace command or module" in result.stderr


_EVIL = "import pathlib\npathlib.Path(__file__).with_name('HIJACKED').write_text('x')\n"


def _plant_mempalace(project: Path, layout: str) -> Path:
    """A project directory whose ``mempalace`` would hijack an import from the cwd."""
    project.mkdir()
    if layout == "module":
        (project / "mempalace.py").write_text(_EVIL, encoding="utf-8")
        return project / "HIJACKED"
    pkg = project / "mempalace"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(_EVIL, encoding="utf-8")
    return pkg / "HIJACKED"


@needs_bash
@pytest.mark.skipif(os.name == "nt", reason="runs the real interpreter through bash; POSIX only")
@pytest.mark.parametrize("layout", ["module", "package"])
def test_resolver_ignores_a_mempalace_in_the_project_directory(tmp_path, layout):
    import sys

    project = tmp_path / "project"
    marker = _plant_mempalace(project, layout)
    env = os.environ.copy()
    env.pop("PYTHONSAFEPATH", None)
    env["MEMPAL_PYTHON"] = sys.executable
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    result = subprocess.run(
        [BASH, str(RESOLVER), "--hook", "stop", "--harness", "claude-code"],
        input="{}",
        text=True,
        capture_output=True,
        cwd=project,
        env=env,
        timeout=120,
    )
    assert not marker.exists(), f"the project's mempalace ran: {result.stderr!r}"
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("layout", ["module", "package"])
def test_child_mine_launcher_ignores_a_mempalace_in_the_cwd(tmp_path, layout):
    """The mines a hook spawns start in the project directory too. With
    PYTHONSAFEPATH unset, as on Python 3.9 and 3.10, the launcher itself must
    keep the cwd off sys.path."""
    import sys

    from mempalace.hooks_cli import _SAFE_MAIN

    project = tmp_path / "project"
    marker = _plant_mempalace(project, layout)
    env = os.environ.copy()
    env.pop("PYTHONSAFEPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", _SAFE_MAIN, "--version"],
        text=True,
        capture_output=True,
        cwd=project,
        env=env,
        timeout=120,
    )
    assert not marker.exists(), f"the project's mempalace ran: {result.stderr!r}"
    assert result.returncode == 0, result.stderr
    assert "mempalace" in (result.stdout + result.stderr).lower()


@needs_bash
def test_resolver_replies_empty_by_default_when_nothing_runs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    result = _run(RESOLVER, ["--hook", "stop", "--harness", "claude-code"], "{}", bin_dir)
    assert result.returncode == 0
    assert result.stdout == "{}"
    assert "MEMPAL_PYTHON" in result.stderr
