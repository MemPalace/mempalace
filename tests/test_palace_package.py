"""The palace ops module is a package; the public import path is unchanged."""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS = (
    "collection",
    "backend",
    "closets",
    "mine_lock",
    "palace_lock",
    "mined",
)


def test_palace_is_a_package():
    import mempalace.palace as palace

    assert hasattr(palace, "__path__")
    assert callable(palace.get_collection)
    assert callable(palace.get_closets_collection)
    assert callable(palace.mine_lock)
    assert callable(palace.mine_palace_lock)
    assert issubclass(palace.MineAlreadyRunning, RuntimeError)
    assert palace.NORMALIZE_VERSION == 2


@pytest.mark.parametrize("name", FRAGMENTS)
def test_fragments_refuse_direct_import(name):
    with pytest.raises(ImportError, match="implementation fragment"):
        importlib.import_module(f"mempalace.palace.{name}")


def test_fragment_files_exist():
    pkg = REPO_ROOT / "mempalace" / "palace"
    missing = [name for name in FRAGMENTS if not (pkg / f"{name}.py").is_file()]
    assert missing == []
    assert not (REPO_ROOT / "mempalace" / "palace.py").exists()
