import json
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from mempalace.hooks import hook_path, hooks_dir


def test_hooks_dir_exists():
    assert hooks_dir().is_dir()


def test_hooks_dir_contains_scripts():
    scripts = {f.name for f in hooks_dir().iterdir() if f.suffix == ".sh"}
    assert "mempal_save_hook.sh" in scripts
    assert "mempal_precompact_hook.sh" in scripts
    assert "mempal_session_end_hook.sh" in scripts


def test_hook_path_returns_existing_file():
    p = hook_path("mempal_save_hook.sh")
    assert p.is_file()
    assert p.name == "mempal_save_hook.sh"


def test_hook_path_returns_nested_file():
    p = hook_path("cursor/install.sh")
    assert p.is_file()
    assert p.name == "install.sh"


def test_hook_path_raises_for_missing():
    with pytest.raises(FileNotFoundError):
        hook_path("nonexistent_hook.sh")


def test_hook_path_raises_for_path_traversal():
    with pytest.raises(FileNotFoundError):
        hook_path("../pyproject.toml")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX executable bit")
def test_hook_scripts_are_executable():
    for name in ("mempal_save_hook.sh", "mempal_precompact_hook.sh", "mempal_session_end_hook.sh"):
        p = hook_path(name)
        mode = p.stat().st_mode
        assert mode & stat.S_IXUSR, f"{name} should be executable"


def test_cli_hooks_path():
    result = subprocess.run(
        [sys.executable, "-m", "mempalace", "hooks", "path"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert Path(result.stdout.strip()).parts[-2:] == ("mempalace", "hooks")


def test_cli_hooks_install_claude():
    result = subprocess.run(
        [sys.executable, "-m", "mempalace", "hooks", "install"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    config = json.loads(result.stdout)
    assert "hooks" in config
    assert "Stop" in config["hooks"]
    assert "SessionEnd" in config["hooks"]
    assert "PreCompact" in config["hooks"]


def test_cli_hooks_install_codex():
    result = subprocess.run(
        [sys.executable, "-m", "mempalace", "hooks", "install", "--format", "codex"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    config = json.loads(result.stdout)
    assert "Stop" in config
    assert "PreCompact" in config
