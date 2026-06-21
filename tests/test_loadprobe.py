"""Tests for mempalace._loadprobe and the probe_palace_loadable / validate_palace_health
helpers in mempalace.repair.
"""

import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from mempalace import repair
from mempalace.repair import (
    PalaceCorruptError,
    PalaceSegmentUnloadableError,
    PalaceSqliteCorruptError,
    probe_palace_loadable,
    validate_palace_health,
)


# ── Exception hierarchy ───────────────────────────────────────────────────────


def test_palace_sqlite_corrupt_error_is_palace_corrupt_error():
    """PalaceSqliteCorruptError must be a subclass of PalaceCorruptError."""
    err = PalaceSqliteCorruptError("/some/palace", ["page corrupt"])
    assert isinstance(err, PalaceCorruptError)
    assert isinstance(err, Exception)


def test_palace_segment_unloadable_error_is_palace_corrupt_error():
    """PalaceSegmentUnloadableError must be a subclass of PalaceCorruptError."""
    err = PalaceSegmentUnloadableError("/some/palace", ["HNSW unloadable"])
    assert isinstance(err, PalaceCorruptError)
    assert isinstance(err, Exception)


def test_palace_segment_unloadable_error_attributes():
    errors = ["HNSW segment unloadable: native crash"]
    err = PalaceSegmentUnloadableError("/my/palace", errors)
    assert err.palace_path == "/my/palace"
    assert err.errors == errors


def test_palace_segment_unloadable_error_str_contains_guidance():
    """__str__ must mention the from-sqlite recovery command."""
    err = PalaceSegmentUnloadableError("/p", ["Error loading hnsw index"])
    msg = str(err)
    assert "from-sqlite" in msg
    assert "/p" in msg


def test_palace_corrupt_error_base_catchable():
    """Both subclasses are caught by except PalaceCorruptError."""
    caught = []
    for exc in [
        PalaceSqliteCorruptError("/p", ["btree"]),
        PalaceSegmentUnloadableError("/p", ["hnsw"]),
    ]:
        try:
            raise exc
        except PalaceCorruptError as e:
            caught.append(e)
    assert len(caught) == 2


# ── probe_palace_loadable — monkeypatched subprocess ─────────────────────────


def _make_completed(returncode=0, stdout="", stderr=""):
    """Build a fake CompletedProcess-like object."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def test_probe_returns_empty_on_success(tmp_path):
    """returncode 0 → [] (palace is loadable)."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    # Create fake chroma.sqlite3 so the early-exit guard doesn't skip the probe.
    open(os.path.join(palace, "chroma.sqlite3"), "w").close()

    with patch("subprocess.run", return_value=_make_completed(returncode=0, stdout="OK")):
        result = probe_palace_loadable(palace)
    assert result == []


def test_probe_returns_sigsegv_message_on_139(tmp_path):
    """returncode 139 (SIGSEGV) → error message containing 'SIGSEGV'."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    open(os.path.join(palace, "chroma.sqlite3"), "w").close()

    with patch("subprocess.run", return_value=_make_completed(returncode=139)):
        result = probe_palace_loadable(palace)
    assert len(result) == 1
    assert "SIGSEGV" in result[0]
    assert "unloadable" in result[0].lower()


def test_probe_returns_sigsegv_message_on_minus_11(tmp_path):
    """returncode -11 (SIGSEGV on Unix) → same SIGSEGV message."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    open(os.path.join(palace, "chroma.sqlite3"), "w").close()

    with patch("subprocess.run", return_value=_make_completed(returncode=-11)):
        result = probe_palace_loadable(palace)
    assert len(result) == 1
    assert "SIGSEGV" in result[0]


def test_probe_detects_hnsw_error_in_stderr(tmp_path):
    """'Error loading hnsw index' in stderr → 'unloadable' message."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    open(os.path.join(palace, "chroma.sqlite3"), "w").close()

    err_line = "InternalError: Error loading hnsw index: segment corrupt"
    with patch(
        "subprocess.run",
        return_value=_make_completed(returncode=2, stderr=err_line),
    ):
        result = probe_palace_loadable(palace)
    assert len(result) == 1
    assert "unloadable" in result[0].lower()
    assert "hnsw" in result[0].lower()


def test_probe_detects_hnsw_error_in_stdout(tmp_path):
    """'Error loading hnsw index' in stdout → 'unloadable' message."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    open(os.path.join(palace, "chroma.sqlite3"), "w").close()

    err_line = "error loading hnsw index"
    with patch(
        "subprocess.run",
        return_value=_make_completed(returncode=2, stdout=err_line),
    ):
        result = probe_palace_loadable(palace)
    assert len(result) == 1
    assert "unloadable" in result[0].lower()


def test_probe_other_nonzero_returncode(tmp_path):
    """Non-zero returncode not matching segfault or hnsw → generic failure message."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    open(os.path.join(palace, "chroma.sqlite3"), "w").close()

    with patch(
        "subprocess.run",
        return_value=_make_completed(returncode=1, stderr="ImportError: no module named foo"),
    ):
        result = probe_palace_loadable(palace)
    assert len(result) == 1
    assert "rc=1" in result[0]
    assert "ImportError" in result[0] or "no module" in result[0].lower()


def test_probe_timeout(tmp_path):
    """TimeoutExpired → timeout message."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    open(os.path.join(palace, "chroma.sqlite3"), "w").close()

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=30.0)):
        result = probe_palace_loadable(palace, timeout=30.0)
    assert len(result) == 1
    assert "timed out" in result[0]
    assert "30" in result[0]


def test_probe_absent_palace_returns_empty(tmp_path):
    """No palace directory → [] (nothing to probe)."""
    absent = str(tmp_path / "nonexistent_palace")
    result = probe_palace_loadable(absent)
    assert result == []


def test_probe_absent_sqlite_returns_empty(tmp_path):
    """Palace directory exists but no chroma.sqlite3 → []."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    # No chroma.sqlite3 file.
    result = probe_palace_loadable(palace)
    assert result == []


def test_probe_spawn_failure_returns_empty(tmp_path):
    """OSError spawning subprocess → [] (best-effort; don't block caller)."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    open(os.path.join(palace, "chroma.sqlite3"), "w").close()

    with patch("subprocess.run", side_effect=FileNotFoundError("python not found")):
        result = probe_palace_loadable(palace)
    assert result == []


# ── validate_palace_health ────────────────────────────────────────────────────


def test_validate_health_both_clean_clears_sentinel(tmp_path):
    """Both sqlite and probe clean → clears sentinel, returns []."""
    from mempalace.corruption_sentinel import read_corruption_sentinel, write_corruption_sentinel

    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    write_corruption_sentinel(palace, ["stale error"])

    with (
        patch.object(repair, "sqlite_integrity_errors", return_value=[]),
        patch.object(repair, "probe_palace_loadable", return_value=[]),
    ):
        result = validate_palace_health(palace)

    assert result == []
    assert read_corruption_sentinel(palace) is None


def test_validate_health_probe_fails_writes_sentinel_and_raises(tmp_path):
    """Clean sqlite + unloadable probe → writes sentinel + raises PalaceSegmentUnloadableError."""
    from mempalace.corruption_sentinel import read_corruption_sentinel

    palace = str(tmp_path / "palace")
    os.makedirs(palace)

    probe_errors = ["HNSW segment unloadable: native crash (SIGSEGV)"]
    with (
        patch.object(repair, "sqlite_integrity_errors", return_value=[]),
        patch.object(repair, "probe_palace_loadable", return_value=probe_errors),
    ):
        with pytest.raises(PalaceSegmentUnloadableError) as exc_info:
            validate_palace_health(palace)

    assert exc_info.value.errors == probe_errors
    assert exc_info.value.palace_path == palace
    sentinel = read_corruption_sentinel(palace)
    assert sentinel is not None
    assert sentinel["errors"] == probe_errors


def test_validate_health_probe_not_called_when_btree_corrupt(tmp_path):
    """B-tree corruption → raises PalaceSqliteCorruptError WITHOUT calling probe."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)

    btree_errors = ["2nd reference to page 711020"]
    probe_mock = MagicMock(return_value=[])

    with (
        patch.object(repair, "sqlite_integrity_errors", return_value=btree_errors),
        patch.object(repair, "probe_palace_loadable", probe_mock),
    ):
        with pytest.raises(PalaceSqliteCorruptError):
            validate_palace_health(palace)

    probe_mock.assert_not_called()


def test_validate_health_btree_errors_writes_sentinel(tmp_path):
    """B-tree corruption → sentinel is written before raising."""
    from mempalace.corruption_sentinel import read_corruption_sentinel

    palace = str(tmp_path / "palace")
    os.makedirs(palace)

    btree_errors = ["database disk image is malformed"]
    with (
        patch.object(repair, "sqlite_integrity_errors", return_value=btree_errors),
    ):
        with pytest.raises(PalaceSqliteCorruptError):
            validate_palace_health(palace)

    sentinel = read_corruption_sentinel(palace)
    assert sentinel is not None
    assert sentinel["errors"] == btree_errors


def test_validate_health_fts_only_falls_through_to_probe(tmp_path):
    """FTS-only errors fall through; if probe is clean → no raise, return []."""
    palace = str(tmp_path / "palace")
    os.makedirs(palace)

    fts_errors = ["malformed inverted index for fts5 table main.embedding_fulltext_search"]
    with (
        patch.object(repair, "sqlite_integrity_errors", return_value=fts_errors),
        patch.object(repair, "probe_palace_loadable", return_value=[]),
    ):
        # Should NOT raise — FTS-only does not block, and probe is clean.
        result = validate_palace_health(palace)
    assert result == []


def test_validate_health_no_sentinel_written_when_set_sentinel_false(tmp_path):
    """set_sentinel=False → sentinel not written even when probe finds errors."""
    from mempalace.corruption_sentinel import read_corruption_sentinel

    palace = str(tmp_path / "palace")
    os.makedirs(palace)

    probe_errors = ["HNSW segment unloadable: native crash (SIGSEGV)"]
    with (
        patch.object(repair, "sqlite_integrity_errors", return_value=[]),
        patch.object(repair, "probe_palace_loadable", return_value=probe_errors),
    ):
        with pytest.raises(PalaceSegmentUnloadableError):
            validate_palace_health(palace, set_sentinel=False)

    assert read_corruption_sentinel(palace) is None


# ── _loadprobe happy-path integration test ────────────────────────────────────


@pytest.mark.slow
def test_loadprobe_real_collection(tmp_path):
    """Build a tiny real ChromaDB collection and verify the probe exits 0.

    Marked slow — spawns a subprocess that cold-loads chromadb + ONNX.
    Run with: pytest -m slow tests/test_loadprobe.py::test_loadprobe_real_collection
    """
    import chromadb
    from mempalace.embedding import get_embedding_function

    palace = str(tmp_path / "palace")
    os.makedirs(palace)

    client = chromadb.PersistentClient(path=palace)
    ef = get_embedding_function()
    col = client.create_collection("mempalace_drawers", embedding_function=ef)
    col.add(
        ids=["doc1", "doc2"],
        documents=["hello world", "test document"],
        metadatas=[{"wing": "test"}, {"wing": "test"}],
    )
    # Close the client before spawning a subprocess that opens the same path.
    del col
    del client

    result = subprocess.run(
        [sys.executable, "-m", "mempalace._loadprobe", palace],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"_loadprobe failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
