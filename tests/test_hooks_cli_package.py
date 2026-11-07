"""The hook runtime is a package; the public import path is unchanged."""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS = (
    "process",
    "mine_spawn",
    "routing",
    "ingest",
    "harness",
    "handlers",
)


def test_hooks_cli_is_a_package():
    import mempalace.hooks_cli as hooks_cli

    assert hasattr(hooks_cli, "__path__")
    assert callable(hooks_cli.run_hook)
    assert callable(hooks_cli.hook_stop)
    assert callable(hooks_cli.hook_session_start)
    assert hooks_cli.PALACE_ROOT.name == ".mempalace"


@pytest.mark.parametrize("name", FRAGMENTS)
def test_fragments_refuse_direct_import(name):
    with pytest.raises(ImportError, match="implementation fragment"):
        importlib.import_module(f"mempalace.hooks_cli.{name}")


def test_fragment_files_exist():
    pkg = REPO_ROOT / "mempalace" / "hooks_cli"
    missing = [name for name in FRAGMENTS if not (pkg / f"{name}.py").is_file()]
    assert missing == []
    assert not (REPO_ROOT / "mempalace" / "hooks_cli.py").exists()
