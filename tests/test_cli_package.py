"""The CLI is a package; the public import path is unchanged."""

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS = (
    "_common",
    "cmd_init",
    "_hub",
    "cmd_mine",
    "cmd_sync",
    "cmd_query",
    "cmd_update",
    "cmd_coord",
    "cmd_repair",
    "cmd_serve",
    "parser",
)


def test_cli_is_a_package():
    import mempalace.cli as cli

    assert hasattr(cli, "__path__")
    assert callable(cli.main)
    assert callable(cli.cmd_mine)
    assert callable(cli.cmd_init)
    assert callable(cli.cmd_logstream)
    assert callable(cli.mine_source_adapter)


@pytest.mark.parametrize("name", FRAGMENTS)
def test_fragments_refuse_direct_import(name):
    with pytest.raises(ImportError, match="implementation fragment"):
        importlib.import_module(f"mempalace.cli.{name}")


def test_python_m_cli_version():
    proc = subprocess.run(
        [sys.executable, "-m", "mempalace.cli", "--version"],
        capture_output=True,
        timeout=60,
        cwd=str(REPO_ROOT),
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "MemPalace" in (proc.stdout + proc.stderr)


def test_fragment_files_exist():
    pkg = REPO_ROOT / "mempalace" / "cli"
    missing = [name for name in FRAGMENTS if not (pkg / f"{name}.py").is_file()]
    assert missing == []
    assert not (REPO_ROOT / "mempalace" / "cli.py").exists()
