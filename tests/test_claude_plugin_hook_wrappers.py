"""Execution tests for Claude plugin hook wrapper scripts."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_HOOKS_DIR = REPO_ROOT / ".claude-plugin" / "hooks"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None,
    reason="bash required for Claude plugin hook wrapper tests",
)

SCRIPT_CASES = [
    ("mempal-stop-hook.sh", "stop"),
    ("mempal-precompact-hook.sh", "precompact"),
]


def _shell_path(path: Path) -> str:
    return path.as_posix()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _make_bin_dir(tmp_path: Path, executables: dict[str, str]) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, content in executables.items():
        _write_executable(bin_dir / name, content)
    return bin_dir


def _capture_stdin_to(output_path: Path) -> str:
    return (
        'stdin_payload=""\n'
        'while IFS= read -r line || [ -n "$line" ]; do\n'
        '  stdin_payload="${stdin_payload}${line}"\n'
        "done\n"
        f'printf \'%s\' "$stdin_payload" > "{_shell_path(output_path)}"\n'
    )


def _run_hook(
    script_name: str,
    payload: str,
    bin_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None

    env = os.environ.copy()
    env["PATH"] = str(bin_dir)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [BASH, _shell_path(PLUGIN_HOOKS_DIR / script_name)],
        input=payload,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=env,
    )


@pytest.mark.parametrize(("script_name", "hook_name"), SCRIPT_CASES)
def test_plugin_hook_wrapper_prefers_mempalace_cli(
    tmp_path: Path, script_name: str, hook_name: str
) -> None:
    """bare mempalace on PATH is preferred over $MEMPAL_PYTHON_BIN -m mempalace."""
    args_file = tmp_path / "args.txt"
    stdin_file = tmp_path / "stdin.json"

    # python3 stub must exist so MEMPAL_PYTHON_BIN resolves — but mempalace
    # stub should be preferred because it's on PATH and MEMPAL_PYTHON is unset.
    bin_dir = _make_bin_dir(
        tmp_path,
        {
            "mempalace": (
                "#!/bin/sh\n"
                f'printf \'%s\' "$*" > "{_shell_path(args_file)}"\n'
                f"{_capture_stdin_to(stdin_file)}"
                "printf '{}\\n'\n"
            ),
            "python3": "#!/bin/sh\nexit 99\n",
        },
    )

    payload = '{"session_id":"abc123"}'
    # Unset MEMPAL_PYTHON so the hook uses the PATH-based resolution
    result = _run_hook(script_name, payload, bin_dir, extra_env={"MEMPAL_PYTHON": ""})

    assert result.returncode == 0
    assert result.stdout == "{}\n"
    assert (
        args_file.read_text(encoding="utf-8")
        == f"hook run --hook {hook_name} --harness claude-code"
    )
    assert stdin_file.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize(("script_name", "hook_name"), SCRIPT_CASES)
def test_plugin_hook_wrapper_uses_mempal_python_when_set(
    tmp_path: Path, script_name: str, hook_name: str
) -> None:
    """$MEMPAL_PYTHON overrides PATH-based resolution and uses -m mempalace."""
    args_file = tmp_path / "args.txt"
    stdin_file = tmp_path / "stdin.json"

    explicit_python = tmp_path / "my_python"
    _write_executable(
        explicit_python,
        "#!/bin/sh\n"
        f'printf \'%s\' "$*" > "{_shell_path(args_file)}"\n'
        f"{_capture_stdin_to(stdin_file)}"
        "printf '{}\\n'\n",
    )

    bin_dir = _make_bin_dir(tmp_path, {"mempalace": "#!/bin/sh\nexit 99\n"})

    payload = '{"session_id":"explicit"}'
    result = _run_hook(
        script_name, payload, bin_dir, extra_env={"MEMPAL_PYTHON": str(explicit_python)}
    )

    assert result.returncode == 0
    assert result.stdout == "{}\n"
    assert args_file.read_text(encoding="utf-8").startswith(
        f"-m mempalace hook run --hook {hook_name} --harness claude-code"
    )
    assert stdin_file.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize(("script_name", "hook_name"), SCRIPT_CASES)
def test_plugin_hook_wrapper_falls_back_to_python3_minus_m(
    tmp_path: Path, script_name: str, hook_name: str
) -> None:
    """Falls back to python3 -m mempalace when mempalace is not on PATH."""
    args_file = tmp_path / "args.txt"
    stdin_file = tmp_path / "stdin.json"

    # No mempalace stub — hook must fall through to python3 -m mempalace
    bin_dir = _make_bin_dir(
        tmp_path,
        {
            "python3": (
                "#!/bin/sh\n"
                f'printf \'%s\' "$*" > "{_shell_path(args_file)}"\n'
                f"{_capture_stdin_to(stdin_file)}"
                "printf '{}\\n'\n"
            ),
        },
    )

    payload = '{"session_id":"fallback"}'
    result = _run_hook(script_name, payload, bin_dir, extra_env={"MEMPAL_PYTHON": ""})

    assert result.returncode == 0
    assert result.stdout == "{}\n"
    assert args_file.read_text(encoding="utf-8").startswith(
        f"-m mempalace hook run --hook {hook_name} --harness claude-code"
    )
    assert stdin_file.read_text(encoding="utf-8") == payload
