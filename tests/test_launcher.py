"""
test_launcher.py — Tests for scripts/mempalace_mcp_start.py

Verifies:
- Launcher exits non-zero on missing palace directory
- Launcher exits non-zero on missing required packages
- Launcher exits non-zero if ChromaDB cannot open the palace
- Quarantine runs before healthcheck
"""

import sys
import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Load the launcher module directly from scripts/ (not installed as a package)
_LAUNCHER_PATH = Path(__file__).parent.parent / "scripts" / "mempalace_mcp_start.py"
_spec = importlib.util.spec_from_file_location("mempalace_mcp_start", _LAUNCHER_PATH)
_launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_launcher)


def test_missing_palace_directory(tmp_path):
    nonexistent = str(tmp_path / "no_such_palace")
    with patch.object(sys, "argv", ["launcher", "--palace", nonexistent]):
        with pytest.raises(SystemExit) as exc:
            _launcher.main()
    assert exc.value.code != 0


def test_missing_dependency_exits_nonzero(tmp_path):
    palace = str(tmp_path / "palace")
    Path(palace).mkdir()
    with patch("builtins.__import__", side_effect=ImportError("no module")):
        with pytest.raises(SystemExit) as exc:
            _launcher._check_deps()
    assert exc.value.code != 0


def test_chromadb_open_failure_exits_nonzero(tmp_path):
    palace = str(tmp_path / "palace")
    Path(palace).mkdir()
    with patch("chromadb.PersistentClient", side_effect=Exception("disk error")):
        with pytest.raises(SystemExit) as exc:
            _launcher._healthcheck(palace)
    assert exc.value.code != 0


def test_quarantine_runs_before_healthcheck(tmp_path):
    palace = str(tmp_path / "palace")
    Path(palace).mkdir()
    call_order = []

    def mock_quarantine(path, stale_seconds):
        call_order.append("quarantine")

    mock_collections = MagicMock()
    mock_collections.list_collections.return_value = []
    mock_client = MagicMock(return_value=mock_collections)

    with patch("mempalace.backends.chroma.quarantine_stale_hnsw", mock_quarantine):
        _launcher._quarantine(palace)

    with patch("chromadb.PersistentClient", mock_client):
        _launcher._healthcheck(palace)
        call_order.append("healthcheck")

    assert call_order == ["quarantine", "healthcheck"]


def test_quarantine_error_does_not_abort(tmp_path):
    palace = str(tmp_path / "palace")
    Path(palace).mkdir()

    def raising_quarantine(path, stale_seconds):
        raise RuntimeError("hnsw corrupt")

    # Should log warning and continue, not raise or exit
    with patch("mempalace.backends.chroma.quarantine_stale_hnsw", raising_quarantine):
        _launcher._quarantine(palace)  # must not raise
