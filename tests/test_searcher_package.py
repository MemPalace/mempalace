"""The searcher is a package; the public import path is unchanged."""

import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS = (
    "ranking",
    "filters",
    "sqlite_bm25",
    "candidates",
    "query",
    "cli_search",
    "render",
)


def test_searcher_is_a_package():
    import mempalace.searcher as searcher

    assert hasattr(searcher, "__path__")
    assert callable(searcher.search)
    assert callable(searcher.search_memories)
    assert issubclass(searcher.SearchError, Exception)
    assert callable(searcher.build_where_filter)
    assert callable(searcher.get_collection)


@pytest.mark.parametrize("name", FRAGMENTS)
def test_fragments_refuse_direct_import(name):
    with pytest.raises(ImportError, match="implementation fragment"):
        importlib.import_module(f"mempalace.searcher.{name}")


def test_fragment_files_exist():
    pkg = REPO_ROOT / "mempalace" / "searcher"
    missing = [name for name in FRAGMENTS if not (pkg / f"{name}.py").is_file()]
    assert missing == []
    assert not (REPO_ROOT / "mempalace" / "searcher.py").exists()
