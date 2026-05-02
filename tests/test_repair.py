"""Tests for mempalace.repair — scan, prune, and rebuild HNSW index."""

import os
import sqlite3
import struct
from contextlib import closing
from datetime import datetime
from unittest.mock import ANY, MagicMock, call, patch

import pytest

from mempalace import repair


# ── _get_palace_path ──────────────────────────────────────────────────


@patch("mempalace.repair.MempalaceConfig", create=True)
def test_get_palace_path_from_config(mock_config_cls):
    mock_config_cls.return_value.palace_path = "/configured/palace"
    with patch.dict("sys.modules", {}):
        # Force reimport to pick up the mock
        result = repair._get_palace_path()
    assert isinstance(result, str)


def test_get_palace_path_fallback():
    with patch("mempalace.repair._get_palace_path") as mock_get:
        mock_get.return_value = os.path.join(os.path.expanduser("~"), ".mempalace", "palace")
        result = mock_get()
        assert ".mempalace" in result


def test_get_collection_name_from_config():
    from mempalace.config import get_configured_collection_name

    get_configured_collection_name.cache_clear()
    with patch("mempalace.config.MempalaceConfig") as mock_config_cls:
        mock_config_cls.return_value.collection_name = "custom_drawers"
        assert repair._drawers_collection_name() == "custom_drawers"
    get_configured_collection_name.cache_clear()


# ── _paginate_ids ─────────────────────────────────────────────────────


def test_paginate_ids_single_batch():
    col = MagicMock()
    col.get.return_value = {"ids": ["id1", "id2", "id3"]}
    ids = repair._paginate_ids(col)
    assert ids == ["id1", "id2", "id3"]


def test_paginate_ids_empty():
    col = MagicMock()
    col.get.return_value = {"ids": []}
    ids = repair._paginate_ids(col)
    assert ids == []


def test_paginate_ids_with_where():
    col = MagicMock()
    col.get.return_value = {"ids": ["id1"]}
    repair._paginate_ids(col, where={"wing": "test"})
    col.get.assert_called_with(where={"wing": "test"}, include=[], limit=1000, offset=0)


def test_paginate_ids_offset_exception_fallback():
    col = MagicMock()
    # First call raises, fallback returns ids, second fallback returns empty
    col.get.side_effect = [
        Exception("offset bug"),
        {"ids": ["id1", "id2"]},
        Exception("offset bug"),
        {"ids": ["id1", "id2"]},  # same ids = no new = break
    ]
    ids = repair._paginate_ids(col)
    assert "id1" in ids


# ── _extract_drawers ──────────────────────────────────────────────────


def test_extract_drawers_preserves_valid_metadata():
    """Non-empty dict metadata passes through unchanged."""
    col = MagicMock()
    col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a", "room": "1"}, {"wing": "b", "room": "2"}],
    }
    all_ids, all_docs, all_metas = repair._extract_drawers(col, total=2, batch_size=2)
    assert all_ids == ["id1", "id2"]
    assert all_docs == ["doc1", "doc2"]
    assert all_metas == [{"wing": "a", "room": "1"}, {"wing": "b", "room": "2"}]


def test_extract_drawers_sanitizes_none_metadata():
    """None entries in metadatas are coerced to the sentinel dict.

    chromadb 1.5.x's `validate_metadata` raises `ValueError: Expected metadata
    to be a non-empty dict, got 0 metadata attributes in add.` if it sees a
    None entry; the sanitizer keeps the rebuild upsert from crashing.
    """
    col = MagicMock()
    col.get.return_value = {
        "ids": ["id1", "id2", "id3"],
        "documents": ["doc1", "doc2", "doc3"],
        "metadatas": [{"wing": "a"}, None, {"wing": "c"}],
    }
    _, _, all_metas = repair._extract_drawers(col, total=3, batch_size=3)
    assert all_metas[0] == {"wing": "a"}
    assert all_metas[1] == {"_repaired_empty_meta": True}
    assert all_metas[2] == {"wing": "c"}


def test_extract_drawers_sanitizes_empty_dict_metadata():
    """Empty dict {} entries are coerced to the sentinel dict.

    chromadb 1.5.x rejects `{}` the same way it rejects `None`. The comment
    in the previous code path mistakenly assumed otherwise.
    """
    col = MagicMock()
    col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{}, {"wing": "b"}],
    }
    _, _, all_metas = repair._extract_drawers(col, total=2, batch_size=2)
    assert all_metas[0] == {"_repaired_empty_meta": True}
    assert all_metas[1] == {"wing": "b"}


def test_extract_drawers_sanitization_preserves_alignment():
    """Sanitized output keeps the same length and ordering as input.

    Critical invariant: ids[i] / documents[i] / metadatas[i] must stay in
    lockstep through the sanitizer; otherwise the rebuild upsert mis-pairs
    documents with metadata.
    """
    col = MagicMock()
    col.get.return_value = {
        "ids": ["id1", "id2", "id3", "id4"],
        "documents": ["d1", "d2", "d3", "d4"],
        "metadatas": [None, {"k": "v"}, {}, None],
    }
    all_ids, all_docs, all_metas = repair._extract_drawers(col, total=4, batch_size=4)
    assert len(all_ids) == len(all_docs) == len(all_metas) == 4
    assert all_ids == ["id1", "id2", "id3", "id4"]
    assert all_metas[0] == {"_repaired_empty_meta": True}
    assert all_metas[1] == {"k": "v"}
    assert all_metas[2] == {"_repaired_empty_meta": True}
    assert all_metas[3] == {"_repaired_empty_meta": True}


def test_extract_drawers_multiple_batches():
    """Pagination handles batch boundaries without losing/duplicating rows."""
    col = MagicMock()
    col.get.side_effect = [
        {"ids": ["id1", "id2"], "documents": ["d1", "d2"], "metadatas": [{"a": 1}, None]},
        {"ids": ["id3"], "documents": ["d3"], "metadatas": [{}]},
        {"ids": [], "documents": [], "metadatas": []},
    ]
    all_ids, all_docs, all_metas = repair._extract_drawers(col, total=3, batch_size=2)
    assert all_ids == ["id1", "id2", "id3"]
    assert all_metas == [{"a": 1}, {"_repaired_empty_meta": True}, {"_repaired_empty_meta": True}]


# ── scan_palace ───────────────────────────────────────────────────────


def _install_mock_backend(mock_backend_cls, collection):
    """Wire mock_backend_cls so ChromaBackend().get_collection(...) returns *collection*."""
    mock_backend = MagicMock()
    mock_backend.get_collection.return_value = collection
    mock_backend_cls.return_value = mock_backend
    return mock_backend


@patch("mempalace.repair.ChromaBackend")
def test_scan_palace_no_ids(mock_backend_cls, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 0
    mock_col.get.return_value = {"ids": []}
    _install_mock_backend(mock_backend_cls, mock_col)

    good, bad = repair.scan_palace(palace_path=str(tmp_path))
    assert good == set()
    assert bad == set()


@patch("mempalace.repair.ChromaBackend")
def test_scan_palace_all_good(mock_backend_cls, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 2
    # _paginate_ids call
    mock_col.get.side_effect = [
        {"ids": ["id1", "id2"]},  # paginate
        {"ids": ["id1", "id2"]},  # probe batch — both returned
    ]
    _install_mock_backend(mock_backend_cls, mock_col)

    good, bad = repair.scan_palace(palace_path=str(tmp_path))
    assert "id1" in good
    assert "id2" in good
    assert len(bad) == 0


@patch("mempalace.repair.ChromaBackend")
def test_scan_palace_with_bad_ids(mock_backend_cls, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 2

    def get_side_effect(**kwargs):
        ids = kwargs.get("ids", None)
        if ids is None:
            # paginate call
            return {"ids": ["good1", "bad1"]}
        if "bad1" in ids and len(ids) == 1:
            raise Exception("corrupt")
        if "good1" in ids and len(ids) == 1:
            return {"ids": ["good1"]}
        # batch probe — raise to force per-id
        raise Exception("batch fail")

    mock_col.get.side_effect = get_side_effect
    _install_mock_backend(mock_backend_cls, mock_col)

    good, bad = repair.scan_palace(palace_path=str(tmp_path))
    assert "good1" in good
    assert "bad1" in bad


@patch("mempalace.repair.ChromaBackend")
def test_scan_palace_with_wing_filter(mock_backend_cls, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 1
    mock_col.get.side_effect = [
        {"ids": ["id1"]},  # paginate
        {"ids": ["id1"]},  # probe
    ]
    _install_mock_backend(mock_backend_cls, mock_col)

    repair.scan_palace(palace_path=str(tmp_path), only_wing="test_wing")
    # Verify where filter was passed
    first_call = mock_col.get.call_args_list[0]
    assert first_call.kwargs.get("where") == {"wing": "test_wing"}


# ── prune_corrupt ─────────────────────────────────────────────────────


@patch("mempalace.repair.ChromaBackend")
def test_prune_corrupt_no_file(mock_backend_cls, tmp_path):
    # Should print message and return without error
    repair.prune_corrupt(palace_path=str(tmp_path))


@patch("mempalace.repair.ChromaBackend")
def test_prune_corrupt_dry_run(mock_backend_cls, tmp_path):
    bad_file = tmp_path / "corrupt_ids.txt"
    bad_file.write_text("bad1\nbad2\n")
    repair.prune_corrupt(palace_path=str(tmp_path), confirm=False)
    # No backend calls in dry run
    mock_backend_cls.assert_not_called()


@patch("mempalace.repair.ChromaBackend")
def test_prune_corrupt_confirmed(mock_backend_cls, tmp_path):
    bad_file = tmp_path / "corrupt_ids.txt"
    bad_file.write_text("bad1\nbad2\n")

    mock_col = MagicMock()
    mock_col.count.side_effect = [10, 8]
    _install_mock_backend(mock_backend_cls, mock_col)

    repair.prune_corrupt(palace_path=str(tmp_path), confirm=True)
    mock_col.delete.assert_called_once()


@patch("mempalace.repair.ChromaBackend")
def test_prune_corrupt_delete_failure_fallback(mock_backend_cls, tmp_path):
    bad_file = tmp_path / "corrupt_ids.txt"
    bad_file.write_text("bad1\nbad2\n")

    mock_col = MagicMock()
    mock_col.count.side_effect = [10, 8]
    # Batch delete fails, per-id succeeds
    mock_col.delete.side_effect = [Exception("batch fail"), None, None]
    _install_mock_backend(mock_backend_cls, mock_col)

    repair.prune_corrupt(palace_path=str(tmp_path), confirm=True)
    assert mock_col.delete.call_count == 3  # 1 batch + 2 individual


# ── rebuild_index ─────────────────────────────────────────────────────


@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_no_palace(mock_backend_cls, tmp_path):
    nonexistent = str(tmp_path / "nope")
    repair.rebuild_index(palace_path=nonexistent)
    mock_backend_cls.assert_not_called()


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_empty_palace(mock_backend_cls, mock_shutil, tmp_path):
    mock_col = MagicMock()
    mock_col.count.return_value = 0
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)

    repair.rebuild_index(palace_path=str(tmp_path))
    mock_backend.delete_collection.assert_not_called()


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_success(mock_backend_cls, mock_shutil, tmp_path):
    # Create a valid sqlite file so the repair preflight can run quick_check.
    sqlite_path = tmp_path / "chroma.sqlite3"
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute("CREATE TABLE dummy(id INTEGER PRIMARY KEY)")
        conn.commit()

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }

    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_temp_col

    repair.rebuild_index(palace_path=str(tmp_path))

    # Verify: backed up sqlite only, not copytree.
    mock_shutil.copy2.assert_called_once()
    assert "chroma.sqlite3" in str(mock_shutil.copy2.call_args)

    # Verify: deleted and recreated (cosine is the backend default)
    assert mock_backend.create_collection.call_args_list == [
        call(str(tmp_path), ANY),
    ]
    assert mock_backend.delete_collection.call_args_list == [
        call(str(tmp_path), ANY),
        call(str(tmp_path), "mempalace_drawers"),
        call(str(tmp_path), ANY),
    ]
    # Verify: used upsert not add
    mock_temp_col.upsert.assert_called_once()
    mock_temp_col.modify.assert_called_once_with(name="mempalace_drawers")
    mock_temp_col.add.assert_not_called()


@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_releases_write_lock_on_backup_failure(mock_backend_cls, tmp_path):
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite_path.write_text("fake")
    lock = MagicMock()
    mock_col = MagicMock()
    mock_col.count.return_value = 2
    _install_mock_backend(mock_backend_cls, mock_col)

    with (
        patch("mempalace.repair.sqlite_integrity_errors", return_value=[]),
        patch("mempalace.repair.palace_write_lock", return_value=lock),
        patch("mempalace.repair.shutil.copy2", side_effect=OSError("copy failed")),
        pytest.raises(OSError, match="copy failed"),
    ):
        repair.rebuild_index(palace_path=str(tmp_path))

    lock.__enter__.assert_called_once_with()
    lock.__exit__.assert_called_once_with(None, None, None)


def test_extract_drawers_includes_embeddings_when_available():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }

    ids, docs, metas, embeddings = repair._extract_drawers(
        col, total=2, batch_size=10, include_embeddings=True
    )

    assert ids == ["id1", "id2"]
    assert docs == ["doc1", "doc2"]
    assert metas == [{"wing": "a"}, {"wing": "b"}]
    assert embeddings == [[0.1, 0.2], [0.3, 0.4]]
    col.get.assert_called_once_with(
        limit=10,
        offset=0,
        include=["documents", "metadatas", "embeddings"],
    )


def test_extract_drawers_can_skip_embeddings_for_reembed():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["id1"],
        "documents": ["doc1"],
        "metadatas": [{"wing": "a"}],
    }

    ids, docs, metas, embeddings = repair._extract_drawers(
        col, total=1, batch_size=10, include_embeddings=False
    )

    assert ids == ["id1"]
    assert docs == ["doc1"]
    assert metas == [{"wing": "a"}]
    assert embeddings is None
    col.get.assert_called_once_with(
        limit=10,
        offset=0,
        include=["documents", "metadatas"],
    )


def test_extract_drawers_skips_embeddings_by_default():
    col = MagicMock()
    col.get.return_value = {
        "ids": ["id1"],
        "documents": ["doc1"],
        "metadatas": [{"wing": "a"}],
    }

    ids, docs, metas, embeddings = repair._extract_drawers(col, total=1, batch_size=10)

    assert ids == ["id1"]
    assert docs == ["doc1"]
    assert metas == [{"wing": "a"}]
    assert embeddings is None
    col.get.assert_called_once_with(
        limit=10,
        offset=0,
        include=["documents", "metadatas"],
    )


@patch("mempalace.repair.ChromaBackend")
def test_rebuild_collection_via_temp_reuses_extracted_embeddings(mock_backend_cls):
    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_temp_col

    repair._rebuild_collection_via_temp(
        mock_backend,
        "/palace",
        ["id1", "id2"],
        ["doc1", "doc2"],
        [{"wing": "a"}, {"wing": "b"}],
        batch_size=10,
        all_embeddings=[[0.1, 0.2], [0.3, 0.4]],
        progress=lambda *args, **kwargs: None,
    )

    expected_kwargs = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }
    mock_temp_col.upsert.assert_called_once_with(**expected_kwargs)
    mock_temp_col.modify.assert_called_once_with(name="mempalace_drawers")


def test_stage_collection_restarts_without_embeddings_when_batch_incomplete():
    backend = MagicMock()
    first_temp = MagicMock()
    second_temp = MagicMock()
    second_temp.count.return_value = 2
    backend.create_collection.side_effect = [first_temp, second_temp]

    source_col = MagicMock()
    source_col.get.side_effect = [
        {
            "ids": ["id1", "id2"],
            "documents": ["doc1", "doc2"],
            "metadatas": [{"wing": "a"}, {"wing": "b"}],
        },
        {
            "ids": ["id1", "id2"],
            "documents": ["doc1", "doc2"],
            "metadatas": [{"wing": "a"}, {"wing": "b"}],
        },
    ]

    temp_col, temp_name, staged = repair._stage_collection_from_source(
        backend,
        "/palace",
        source_col,
        total=2,
        batch_size=10,
        collection_name="mempalace_drawers",
        include_embeddings=True,
        progress=lambda *args, **kwargs: None,
    )

    assert temp_col is second_temp
    assert temp_name.startswith("mempalace_drawers__repair_tmp__")
    assert staged == 2
    first_temp.upsert.assert_not_called()
    second_temp.upsert.assert_called_once_with(
        ids=["id1", "id2"],
        documents=["doc1", "doc2"],
        metadatas=[{"wing": "a"}, {"wing": "b"}],
    )
    assert source_col.get.call_args_list == [
        call(
            limit=10,
            offset=0,
            include=["documents", "metadatas", "embeddings"],
        ),
        call(limit=10, offset=0, include=["documents", "metadatas"]),
    ]


def test_stage_collection_cleans_temp_when_source_read_fails():
    backend = MagicMock()
    temp_col = MagicMock()
    backend.create_collection.return_value = temp_col
    source_col = MagicMock()
    source_col.get.side_effect = RuntimeError("source read failed")

    with pytest.raises(RuntimeError, match="source read failed"):
        repair._stage_collection_from_source(
            backend,
            "/palace",
            source_col,
            total=2,
            batch_size=10,
            collection_name="mempalace_drawers",
            include_embeddings=False,
            temp_name="mempalace_drawers__repair_tmp__test",
            progress=lambda *args, **kwargs: None,
        )

    backend.delete_collection.assert_any_call("/palace", "mempalace_drawers__repair_tmp__test")


def test_stage_collection_cleans_temp_when_upsert_fails():
    backend = MagicMock()
    temp_col = MagicMock()
    temp_col.upsert.side_effect = RuntimeError("upsert failed")
    backend.create_collection.return_value = temp_col
    source_col = MagicMock()
    source_col.get.return_value = {
        "ids": ["id1"],
        "documents": ["doc1"],
        "metadatas": [{"wing": "a"}],
    }

    with pytest.raises(RuntimeError, match="upsert failed"):
        repair._stage_collection_from_source(
            backend,
            "/palace",
            source_col,
            total=1,
            batch_size=10,
            collection_name="mempalace_drawers",
            include_embeddings=False,
            temp_name="mempalace_drawers__repair_tmp__test",
            progress=lambda *args, **kwargs: None,
        )

    backend.delete_collection.assert_any_call("/palace", "mempalace_drawers__repair_tmp__test")


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_ignores_missing_temp_collection_at_start(
    mock_backend_cls, mock_shutil, tmp_path
):
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite3.connect(str(sqlite_path)).close()

    def _fake_copy2(src, dst):
        with open(dst, "w") as handle:
            handle.write("backup")

    mock_shutil.copy2.side_effect = _fake_copy2

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }

    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_temp_col
    mock_backend.delete_collection.side_effect = [
        ValueError("Collection [mempalace_drawers__repair_tmp] does not exist"),
        None,
        None,
    ]

    repair.rebuild_index(palace_path=str(tmp_path))

    assert mock_shutil.copy2.call_count == 1
    assert mock_backend.delete_collection.call_args_list == [
        call(str(tmp_path), ANY),
        call(str(tmp_path), "mempalace_drawers"),
        call(str(tmp_path), ANY),
    ]


def test_delete_collection_if_exists_reraises_unexpected_value_error():
    mock_backend = MagicMock()
    mock_backend.delete_collection.side_effect = ValueError("invalid collection name")

    with pytest.raises(ValueError, match="invalid collection name"):
        repair._delete_collection_if_exists(mock_backend, "/palace", "bad/name")


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_error_reading(mock_backend_cls, mock_shutil, tmp_path):
    mock_backend = MagicMock()
    mock_backend.get_collection.side_effect = Exception("corrupt")
    mock_backend_cls.return_value = mock_backend

    repair.rebuild_index(palace_path=str(tmp_path))
    mock_backend.delete_collection.assert_not_called()


# ── #1208 truncation safety ───────────────────────────────────────────


def test_check_extraction_safety_passes_when_counts_match(tmp_path):
    """SQLite reports same count as extracted → no exception."""
    with patch("mempalace.repair.sqlite_drawer_count", return_value=500):
        repair.check_extraction_safety(str(tmp_path), 500)


def test_check_extraction_safety_uses_configured_collection(tmp_path):
    with patch("mempalace.repair.sqlite_drawer_count", return_value=500) as count:
        repair.check_extraction_safety(str(tmp_path), 500, collection_name="custom_drawers")
    count.assert_called_once_with(str(tmp_path), "custom_drawers")


def test_check_extraction_safety_default_uses_configured_collection(tmp_path):
    with (
        patch("mempalace.repair._drawers_collection_name", return_value="custom_drawers"),
        patch("mempalace.repair.sqlite_drawer_count", return_value=500) as count,
    ):
        repair.check_extraction_safety(str(tmp_path), 500)
    count.assert_called_once_with(str(tmp_path), "custom_drawers")


def test_check_extraction_safety_passes_when_sqlite_unreadable_and_under_cap(tmp_path):
    """SQLite check fails (None) but extraction is well under the cap → safe."""
    with patch("mempalace.repair.sqlite_drawer_count", return_value=None):
        repair.check_extraction_safety(str(tmp_path), 5_000)


def test_check_extraction_safety_aborts_when_sqlite_higher(tmp_path):
    """SQLite reports more than extracted — the user-reported #1208 case."""
    with patch("mempalace.repair.sqlite_drawer_count", return_value=67_580):
        try:
            repair.check_extraction_safety(str(tmp_path), 10_000)
        except repair.TruncationDetected as e:
            assert e.sqlite_count == 67_580
            assert e.extracted == 10_000
            assert "67,580" in e.message
            assert "10,000" in e.message
            assert "57,580" in e.message  # the loss number
        else:
            raise AssertionError("expected TruncationDetected")


def test_check_extraction_safety_aborts_when_unreadable_and_at_cap(tmp_path):
    """SQLite unreadable but extraction == default get() cap → suspicious."""
    with patch("mempalace.repair.sqlite_drawer_count", return_value=None):
        try:
            repair.check_extraction_safety(str(tmp_path), repair.CHROMADB_DEFAULT_GET_LIMIT)
        except repair.TruncationDetected as e:
            assert e.sqlite_count is None
            assert e.extracted == repair.CHROMADB_DEFAULT_GET_LIMIT
            assert "10,000" in e.message
        else:
            raise AssertionError("expected TruncationDetected")


def test_check_extraction_safety_override_skips_check(tmp_path):
    """``confirm_truncation_ok=True`` short-circuits both signals."""
    with patch("mempalace.repair.sqlite_drawer_count", return_value=99_999):
        # Would normally abort — override allows through
        repair.check_extraction_safety(str(tmp_path), 10_000, confirm_truncation_ok=True)


def test_sqlite_drawer_count_returns_none_on_missing_file(tmp_path):
    """Palace dir exists but no chroma.sqlite3 → None, not crash."""
    assert repair.sqlite_drawer_count(str(tmp_path)) is None


def test_sqlite_drawer_count_returns_none_on_unreadable_schema(tmp_path):
    """File exists but isn't a chromadb sqlite → None, not crash."""
    sqlite_path = os.path.join(str(tmp_path), "chroma.sqlite3")
    with open(sqlite_path, "wb") as f:
        f.write(b"not a sqlite file at all")
    assert repair.sqlite_drawer_count(str(tmp_path)) is None


def test_sqlite_drawer_count_uses_metadata_segment_count(tmp_path):
    """The truncation guard should count the metadata rows Chroma can
    direct-extract, not scan every embedding row through collection joins.
    """
    sqlite_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                scope TEXT NOT NULL
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL,
                seq_id BLOB NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (segment_id, embedding_id)
            );
            """
        )
        conn.execute("INSERT INTO collections (id, name) VALUES ('c1', 'mempalace_drawers')")
        conn.executemany(
            "INSERT INTO segments (id, collection, scope) VALUES (?, 'c1', ?)",
            [("seg-meta", "METADATA"), ("seg-vector", "VECTOR")],
        )
        conn.executemany(
            "INSERT INTO embeddings (segment_id, embedding_id, seq_id) VALUES (?, ?, X'01')",
            [
                ("seg-meta", "drawer_1"),
                ("seg-meta", "drawer_2"),
                ("seg-vector", "vector_sidecar"),
            ],
        )
        conn.commit()

        segment_id = repair._sqlite_metadata_segment_id(conn, "mempalace_drawers")
        plan = "\n".join(
            row[-1]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM embeddings WHERE segment_id = ?",
                (segment_id,),
            )
        )
    finally:
        conn.close()

    assert repair.sqlite_drawer_count(str(tmp_path), "mempalace_drawers") == 2
    assert "sqlite_autoindex_embeddings_1" in plan


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_default_uses_configured_collection(mock_backend_cls, mock_shutil, tmp_path):
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite3.connect(str(sqlite_path)).close()
    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_temp_col

    with (
        patch("mempalace.repair._drawers_collection_name", return_value="custom_drawers"),
        patch("mempalace.repair.sqlite_drawer_count", return_value=2) as count,
    ):
        repair.rebuild_index(palace_path=str(tmp_path))

    assert mock_backend.get_collection.call_args_list[0] == call(str(tmp_path), "custom_drawers")
    count.assert_called_once_with(str(tmp_path), "custom_drawers")
    assert call(str(tmp_path), "custom_drawers") in mock_backend.delete_collection.call_args_list
    assert call(str(tmp_path), ANY) in mock_backend.create_collection.call_args_list


def test_status_default_uses_configured_drawer_collection(tmp_path):
    with (
        patch("mempalace.repair._drawers_collection_name", return_value="custom_drawers"),
        patch("mempalace.repair.hnsw_capacity_status") as capacity_status,
    ):
        capacity_status.side_effect = [
            {
                "sqlite_count": 1,
                "hnsw_count": 1,
                "divergence": 0,
                "diverged": False,
                "status": "ok",
                "message": "",
            },
            {
                "sqlite_count": 1,
                "hnsw_count": 1,
                "divergence": 0,
                "diverged": False,
                "status": "ok",
                "message": "",
            },
        ]
        result = repair.status(palace_path=str(tmp_path))

    assert capacity_status.call_args_list[0].args == (str(tmp_path), "custom_drawers")
    assert capacity_status.call_args_list[1].args == (str(tmp_path), "mempalace_closets")
    assert result["status"] == "ok"
    assert result["message"] == ""

def _status_info():
    return {
        "sqlite_count": 1,
        "hnsw_count": 1,
        "divergence": 0,
        "diverged": False,
        "status": "ok",
        "message": "",
    }


def _seed_status_collection_sqlite(palace_path, collection_name, rows):
    sqlite_path = palace_path / "chroma.sqlite3"
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                scope TEXT NOT NULL
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL
            );
            """
        )
        for collection_index, (name, count) in enumerate(rows):
            collection_id = f"collection-{collection_index}"
            segment_id = f"segment-{collection_index}"
            conn.execute("INSERT INTO collections (id, name) VALUES (?, ?)", (collection_id, name))
            conn.execute(
                "INSERT INTO segments (id, collection, scope) VALUES (?, ?, 'METADATA')",
                (segment_id, collection_id),
            )
            conn.executemany(
                "INSERT INTO embeddings (segment_id, embedding_id) VALUES (?, ?)",
                [(segment_id, f"{name}-{i}") for i in range(count)],
            )
        conn.commit()
    finally:
        conn.close()


def test_status_reports_stale_repair_temp_collections(tmp_path, capsys):
    unique_temp = "mempalace_drawers__repair_tmp__20260502120000000000__123__abcdef12"
    _seed_status_collection_sqlite(
        tmp_path,
        "mempalace_drawers",
        [
            ("mempalace_drawers", 3),
            (unique_temp, 2),
        ],
    )

    with patch(
        "mempalace.repair.hnsw_capacity_status", side_effect=[_status_info(), _status_info()]
    ):
        result = repair.status(palace_path=str(tmp_path))

    out = capsys.readouterr().out
    assert result["status"] == "artifacts"
    assert result["repair_artifacts"] == {unique_temp: 2}
    assert f"{unique_temp}: 2 rows" in out
    assert f"mempalace --palace {str(tmp_path)} repair-status --cleanup-temp --yes" in out


def test_status_does_not_treat_user_collection_with_temp_substring_as_artifact(tmp_path, capsys):
    _seed_status_collection_sqlite(
        tmp_path,
        "mempalace_drawers",
        [
            ("mempalace_drawers", 3),
            ("mempalace_drawers__repair_tmp", 2),
        ],
    )

    with patch(
        "mempalace.repair.hnsw_capacity_status", side_effect=[_status_info(), _status_info()]
    ):
        result = repair.status(palace_path=str(tmp_path))

    out = capsys.readouterr().out
    assert result["repair_artifacts"] == {}
    assert "[repair artifacts]" in out
    assert "none found" in out


def test_status_reports_unique_repair_temp_collections(tmp_path, capsys):
    unique_temp = "mempalace_drawers__repair_tmp__20260502120000000000__123__abcdef12"
    _seed_status_collection_sqlite(
        tmp_path,
        "mempalace_drawers",
        [
            ("mempalace_drawers", 3),
            (unique_temp, 4),
        ],
    )

    with patch(
        "mempalace.repair.hnsw_capacity_status", side_effect=[_status_info(), _status_info()]
    ):
        result = repair.status(palace_path=str(tmp_path))

    out = capsys.readouterr().out
    assert result["repair_artifacts"] == {unique_temp: 4}
    assert f"{unique_temp}: 4 rows" in out


def test_status_does_not_mask_live_divergence_with_temp_artifacts(tmp_path, capsys):
    unique_temp = "mempalace_drawers__repair_tmp__20260502120000000000__123__abcdef12"
    _seed_status_collection_sqlite(
        tmp_path,
        "mempalace_drawers",
        [
            ("mempalace_drawers", 3),
            (unique_temp, 2),
        ],
    )
    diverged = {
        "sqlite_count": 3,
        "hnsw_count": 1,
        "divergence": 2,
        "diverged": True,
        "status": "needs_repair",
        "message": "HNSW behind sqlite",
    }

    with patch("mempalace.repair.hnsw_capacity_status", side_effect=[diverged, _status_info()]):
        result = repair.status(palace_path=str(tmp_path))

    capsys.readouterr()
    assert result["status"] == "needs_repair"
    assert result["repair_artifacts"] == {unique_temp: 2}


@patch("mempalace.repair.ChromaBackend")
def test_status_cleanup_temp_requires_yes(mock_backend_cls, tmp_path, capsys):
    unique_temp = "mempalace_drawers__repair_tmp__20260502121212121212__999__feedface"
    _seed_status_collection_sqlite(
        tmp_path,
        "mempalace_drawers",
        [(unique_temp, 2)],
    )

    with patch(
        "mempalace.repair.hnsw_capacity_status", side_effect=[_status_info(), _status_info()]
    ):
        result = repair.status(palace_path=str(tmp_path), cleanup_temp=True, assume_yes=False)

    out = capsys.readouterr().out
    assert result["cleanup"] == {}
    assert "cleanup skipped" in out
    mock_backend_cls.assert_not_called()


@patch("mempalace.repair.ChromaBackend")
def test_status_cleanup_temp_deletes_reported_artifacts(mock_backend_cls, tmp_path, capsys):
    unique_temp = "mempalace_drawers__repair_tmp__20260502121212121212__999__feedface"
    _seed_status_collection_sqlite(
        tmp_path,
        "mempalace_drawers",
        [(unique_temp, 2)],
    )
    mock_backend = MagicMock()
    mock_backend_cls.return_value = mock_backend

    with patch(
        "mempalace.repair.hnsw_capacity_status", side_effect=[_status_info(), _status_info()]
    ):
        result = repair.status(palace_path=str(tmp_path), cleanup_temp=True, assume_yes=True)

    out = capsys.readouterr().out
    assert result["cleanup"] == {unique_temp: "deleted"}
    assert result["repair_artifacts"] == {}
    assert f"cleanup {unique_temp}: deleted" in out
    mock_backend.delete_collection.assert_called_once_with(str(tmp_path), unique_temp)
    mock_backend.close.assert_called_once_with()


@patch("mempalace.repair.ChromaBackend")
def test_status_cleanup_temp_refuses_when_write_lock_active(mock_backend_cls, tmp_path, capsys):
    unique_temp = "mempalace_drawers__repair_tmp__20260502121212121212__999__feedface"
    _seed_status_collection_sqlite(
        tmp_path,
        "mempalace_drawers",
        [(unique_temp, 2)],
    )

    def locked(*args, **kwargs):
        raise repair.PalaceWriteAlreadyRunning("locked by repair")

    with (
        patch("mempalace.repair.palace_write_lock", locked),
        patch(
            "mempalace.repair.hnsw_capacity_status",
            side_effect=[_status_info(), _status_info()],
        ),
    ):
        result = repair.status(palace_path=str(tmp_path), cleanup_temp=True, assume_yes=True)

    out = capsys.readouterr().out
    assert result["cleanup"] == {unique_temp: "refused: locked by repair"}
    assert f"cleanup {unique_temp}: refused: locked by repair" in out
    mock_backend_cls.assert_not_called()


@patch("mempalace.repair.ChromaBackend")
def test_status_cleanup_temp_deletes_unique_temp_artifacts(mock_backend_cls, tmp_path, capsys):
    unique_temp = "mempalace_drawers__repair_tmp__20260502121212121212__999__feedface"
    _seed_status_collection_sqlite(
        tmp_path,
        "mempalace_drawers",
        [(unique_temp, 2)],
    )
    mock_backend = MagicMock()
    mock_backend_cls.return_value = mock_backend

    with patch(
        "mempalace.repair.hnsw_capacity_status", side_effect=[_status_info(), _status_info()]
    ):
        result = repair.status(palace_path=str(tmp_path), cleanup_temp=True, assume_yes=True)

    out = capsys.readouterr().out
    assert result["cleanup"] == {unique_temp: "deleted"}
    assert f"cleanup {unique_temp}: deleted" in out
    mock_backend.delete_collection.assert_called_once_with(str(tmp_path), unique_temp)
    mock_backend.close.assert_called_once_with()


@patch("mempalace.repair.ChromaBackend")
def test_status_cleanup_temp_reports_delete_failures(mock_backend_cls, tmp_path, capsys):
    unique_temp = "mempalace_drawers__repair_tmp__20260502121212121212__999__feedface"
    _seed_status_collection_sqlite(
        tmp_path,
        "mempalace_drawers",
        [(unique_temp, 2)],
    )
    mock_backend = MagicMock()
    mock_backend.delete_collection.side_effect = RuntimeError("locked")
    mock_backend_cls.return_value = mock_backend

    with patch(
        "mempalace.repair.hnsw_capacity_status", side_effect=[_status_info(), _status_info()]
    ):
        result = repair.status(palace_path=str(tmp_path), cleanup_temp=True, assume_yes=True)

    out = capsys.readouterr().out
    assert result["cleanup"] == {unique_temp: "failed: locked"}
    assert result["repair_artifacts"] == {unique_temp: 2}
    assert f"cleanup {unique_temp}: failed: locked" in out
    mock_backend.close.assert_called_once_with()


def test_status_missing_palace_returns_standard_shape(tmp_path, capsys):
    missing = tmp_path / "missing"

    result = repair.status(palace_path=str(missing))

    assert result["status"] == "unknown"
    assert result["drawers"]["status"] == "unknown"
    assert result["closets"]["status"] == "unknown"
    assert result["repair_artifacts"] == {}
    assert result["cleanup"] == {}


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_aborts_on_truncation_signal(mock_backend_cls, mock_shutil, tmp_path):
    """rebuild_index honors the safety guard: SQLite says 67k, get() returns
    10k → no delete_collection, no upsert, no backup."""
    mock_backend = MagicMock()
    mock_col = MagicMock()
    mock_col.count.return_value = 10_000
    # Single page comes back with 10_000 ids
    mock_col.get.side_effect = [
        {
            "ids": [f"id{i}" for i in range(10_000)],
            "documents": ["x"] * 10_000,
            "metadatas": [{}] * 10_000,
            "embeddings": [[0.1, 0.2]] * 10_000,
        },
        {"ids": [], "documents": [], "metadatas": []},
    ]
    mock_backend.get_collection.return_value = mock_col
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 10_000
    mock_backend.create_collection.return_value = mock_temp_col
    mock_backend_cls.return_value = mock_backend

    with patch("mempalace.repair.sqlite_drawer_count", return_value=67_580):
        repair.rebuild_index(palace_path=str(tmp_path))

    # Guard fired after staging: live collection was never deleted.
    assert mock_backend.delete_collection.call_args_list == [
        call(str(tmp_path), ANY),
        call(str(tmp_path), ANY),
    ]
    mock_backend.create_collection.assert_called_once_with(str(tmp_path), ANY)
    mock_shutil.copy2.assert_not_called()


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_proceeds_with_override(mock_backend_cls, mock_shutil, tmp_path):
    """Override flag lets repair proceed even when the guard would fire."""
    mock_backend = MagicMock()
    mock_col = MagicMock()
    mock_col.count.return_value = 10_000
    mock_col.get.side_effect = [
        {
            "ids": [f"id{i}" for i in range(10_000)],
            "documents": ["x"] * 10_000,
            "metadatas": [{}] * 10_000,
            "embeddings": [[0.1, 0.2]] * 10_000,
        },
        {"ids": [], "documents": [], "metadatas": []},
    ]
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 10_000
    mock_backend.get_collection.return_value = mock_col
    mock_backend.create_collection.return_value = mock_temp_col
    mock_backend_cls.return_value = mock_backend

    with patch("mempalace.repair.sqlite_drawer_count", return_value=67_580):
        repair.rebuild_index(palace_path=str(tmp_path), confirm_truncation_ok=True)

    assert mock_backend.delete_collection.call_count == 3
    assert mock_backend.create_collection.call_count == 1
    mock_temp_col.upsert.assert_called()
    mock_temp_col.modify.assert_called_once_with(name="mempalace_drawers")


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_stage_failure_leaves_live_collection_untouched(
    mock_backend_cls, mock_shutil, tmp_path
):
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite3.connect(str(sqlite_path)).close()

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 1
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_temp_col

    with pytest.raises(repair.RebuildCollectionError) as excinfo:
        repair.rebuild_index(palace_path=str(tmp_path))

    assert excinfo.value.live_replaced is False
    assert mock_shutil.copy2.call_count == 1
    assert mock_backend.delete_collection.call_args_list == [
        call(str(tmp_path), ANY),
        call(str(tmp_path), ANY),
    ]


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_live_failure_restores_backup(mock_backend_cls, mock_shutil, tmp_path):
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite3.connect(str(sqlite_path)).close()

    def _fake_copy2(src, dst):
        with open(dst, "w") as handle:
            handle.write("backup")

    mock_shutil.copy2.side_effect = _fake_copy2

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_temp_col.modify.side_effect = RuntimeError("live rename failed")
    active_backend = MagicMock()
    active_backend.get_collection.return_value = mock_col
    active_backend.create_collection.return_value = mock_temp_col
    helper_backend = MagicMock()
    mock_backend_cls.side_effect = [active_backend, helper_backend]

    with pytest.raises(repair.RebuildCollectionError) as excinfo:
        repair.rebuild_index(palace_path=str(tmp_path))

    assert excinfo.value.live_replaced is True
    assert mock_shutil.copy2.call_count == 2
    assert active_backend.delete_collection.call_args_list == [
        call(str(tmp_path), ANY),
        call(str(tmp_path), "mempalace_drawers"),
        call(str(tmp_path), ANY),
        call(str(tmp_path), "mempalace_drawers"),
    ]
    active_backend.close_palace.assert_called_once_with(str(tmp_path))
    helper_backend.close_palace.assert_not_called()


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_pre_swap_delete_failure_does_not_restore_backup(
    mock_backend_cls, mock_shutil, tmp_path, capsys
):
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite_path.write_text("fake")

    def _fake_copy2(src, dst):
        with open(dst, "w") as handle:
            handle.write("backup")

    mock_shutil.copy2.side_effect = _fake_copy2

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_temp_col
    mock_backend.delete_collection.side_effect = [
        None,
        RuntimeError("delete failed before live replacement"),
    ]

    with (
        patch("mempalace.repair.sqlite_integrity_errors", return_value=[]),
        pytest.raises(repair.RebuildCollectionError) as excinfo,
    ):
        repair.rebuild_index(palace_path=str(tmp_path))

    out = capsys.readouterr().out
    assert excinfo.value.live_replaced is False
    assert "Restoring from backup" not in out
    assert "Live collection was not replaced" in out
    mock_backend.close_palace.assert_not_called()
    assert mock_shutil.copy2.call_count == 1
    assert mock_backend.delete_collection.call_args_list == [
        call(str(tmp_path), ANY),
        call(str(tmp_path), "mempalace_drawers"),
        call(str(tmp_path), ANY),
    ]


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_rejects_extra_live_rows_after_recreate(
    mock_backend_cls, mock_shutil, tmp_path, capsys
):
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite_path.write_text("fake")

    def _fake_copy2(src, dst):
        with open(dst, "w") as handle:
            handle.write("backup")

    mock_shutil.copy2.side_effect = _fake_copy2

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_new_col = MagicMock()
    mock_new_col.count.return_value = 4
    mock_backend = MagicMock()
    mock_backend.get_collection.side_effect = [mock_col, mock_new_col]
    mock_backend.create_collection.return_value = mock_temp_col
    mock_backend_cls.return_value = mock_backend

    with (
        patch("mempalace.repair.sqlite_integrity_errors", return_value=[]),
        pytest.raises(repair.RebuildCollectionError) as excinfo,
    ):
        repair.rebuild_index(palace_path=str(tmp_path))

    out = capsys.readouterr().out
    assert excinfo.value.live_replaced is True
    assert "count mismatch" in str(excinfo.value)
    assert "Restoring from backup" in out
    assert mock_shutil.copy2.call_count == 2


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_live_delete_missing_still_restores_backup(
    mock_backend_cls, mock_shutil, tmp_path
):
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite3.connect(str(sqlite_path)).close()

    def _fake_copy2(src, dst):
        with open(dst, "w") as handle:
            handle.write("backup")

    mock_shutil.copy2.side_effect = _fake_copy2

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_temp_col.modify.side_effect = RuntimeError("rename failed")
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_temp_col
    mock_backend.delete_collection.side_effect = [
        None,
        None,
        None,
        repair.ChromaNotFoundError("missing"),
    ]

    with pytest.raises(repair.RebuildCollectionError) as excinfo:
        repair.rebuild_index(palace_path=str(tmp_path))

    assert excinfo.value.live_replaced is True
    assert mock_shutil.copy2.call_count == 2
    assert mock_backend.delete_collection.call_args_list == [
        call(str(tmp_path), ANY),
        call(str(tmp_path), "mempalace_drawers"),
        call(str(tmp_path), ANY),
        call(str(tmp_path), "mempalace_drawers"),
    ]


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_restore_failure_preserves_original_error(
    mock_backend_cls, mock_shutil, tmp_path, capsys
):
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite3.connect(str(sqlite_path)).close()

    def _copy2_side_effect(src, dst):
        if str(src).endswith(".backup"):
            raise PermissionError("locked sqlite")
        with open(dst, "w") as handle:
            handle.write("backup")

    mock_shutil.copy2.side_effect = _copy2_side_effect

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_temp_col.modify.side_effect = RuntimeError("live rename failed")
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_temp_col

    with pytest.raises(repair.RebuildCollectionError) as excinfo:
        repair.rebuild_index(palace_path=str(tmp_path))

    out = capsys.readouterr().out
    assert "locked sqlite" in out
    assert "Manual restore required" in out
    assert "live rename failed" in str(excinfo.value)


@patch("mempalace.repair.ChromaBackend")
def test_rebuild_collection_via_temp_keeps_original_error_when_cleanup_fails(
    mock_backend_cls,
):
    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_temp_col
    mock_temp_col.modify.side_effect = RuntimeError("live rename failed")
    mock_backend.delete_collection.side_effect = [
        None,
        None,
        RuntimeError("cleanup failed"),
    ]

    with pytest.raises(repair.RebuildCollectionError) as excinfo:
        repair._rebuild_collection_via_temp(
            mock_backend,
            "/palace",
            ["id1", "id2"],
            ["doc1", "doc2"],
            [{"wing": "a"}, {"wing": "b"}],
            batch_size=5000,
            progress=lambda *args, **kwargs: None,
        )

    assert "live rename failed" in str(excinfo.value)
    assert excinfo.value.live_replaced is True
    assert mock_backend.delete_collection.call_args_list == [
        call("/palace", ANY),
        call("/palace", "mempalace_drawers"),
        call("/palace", ANY),
    ]


def test_swap_temp_collection_cleans_temp_when_live_delete_fails():
    backend = MagicMock()
    temp_col = MagicMock()
    backend.delete_collection.side_effect = [
        RuntimeError("live delete failed"),
        None,
    ]

    with pytest.raises(repair.RebuildCollectionError) as excinfo:
        repair._swap_temp_collection_into_live(
            backend,
            "/palace",
            temp_col,
            "mempalace_drawers__repair_tmp",
            "mempalace_drawers",
            2,
            progress=lambda *args, **kwargs: None,
        )

    assert excinfo.value.live_replaced is False
    assert backend.delete_collection.call_args_list == [
        call("/palace", "mempalace_drawers"),
        call("/palace", "mempalace_drawers__repair_tmp"),
    ]
    temp_col.modify.assert_not_called()


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_ignores_temp_cleanup_failure_after_success(
    mock_backend_cls, mock_shutil, tmp_path
):
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite3.connect(str(sqlite_path)).close()

    def _fake_copy2(src, dst):
        with open(dst, "w") as handle:
            handle.write("backup")

    mock_shutil.copy2.side_effect = _fake_copy2

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
    }
    mock_temp_col = MagicMock()
    mock_temp_col.count.return_value = 2
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_temp_col
    mock_backend.delete_collection.side_effect = [
        None,
        None,
        RuntimeError("cleanup failed"),
    ]

    repair.rebuild_index(palace_path=str(tmp_path))

    assert mock_shutil.copy2.call_count == 1
    assert mock_backend.delete_collection.call_args_list == [
        call(str(tmp_path), ANY),
        call(str(tmp_path), "mempalace_drawers"),
        call(str(tmp_path), ANY),
    ]


# ── repair_max_seq_id ─────────────────────────────────────────────────


# Realistic poisoned values from the 2026-04-20 incident — from the sysdb-10
# b'\x11\x11' + 6 ASCII digit format being misread as big-endian u64.
_POISON_VAL = 1_229_822_654_365_970_487


def _seed_poisoned_max_seq_id(
    palace_path: str,
    *,
    drawers_meta_max: int = 502607,
    closets_meta_max: int = 501418,
    drawers_vec_poison: int = _POISON_VAL,
    drawers_meta_poison: int = _POISON_VAL + 1,
    closets_vec_poison: int = _POISON_VAL + 2,
    closets_meta_poison: int = _POISON_VAL + 3,
):
    """Build a minimal palace with poisoned max_seq_id rows.

    Returns a dict with segment UUIDs and the expected clean values.
    """
    os.makedirs(palace_path, exist_ok=True)
    db_path = os.path.join(palace_path, "chroma.sqlite3")

    drawers_coll = "coll-drawers-0000-1111-2222-333344445555"
    closets_coll = "coll-closets-0000-1111-2222-333344445555"
    drawers_vec = "seg-drawers-vec-0000-1111-2222-333344445555"
    drawers_meta = "seg-drawers-meta-0000-1111-2222-33334444555"
    closets_vec = "seg-closets-vec-0000-1111-2222-333344445555"
    closets_meta = "seg-closets-meta-0000-1111-2222-33334444555"

    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE segments(
                id TEXT PRIMARY KEY, type TEXT, scope TEXT, collection TEXT
            );
            CREATE TABLE max_seq_id(segment_id TEXT PRIMARY KEY, seq_id);
            CREATE TABLE embeddings(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_id TEXT,
                embedding_id TEXT,
                seq_id
            );
            CREATE TABLE embeddings_queue(seq_id INTEGER PRIMARY KEY, topic TEXT, id TEXT);
            CREATE TABLE collection_metadata(collection_id TEXT, key TEXT, str_value TEXT);
            """
        )
        conn.executemany(
            "INSERT INTO segments VALUES (?, ?, ?, ?)",
            [
                (drawers_vec, "urn:vector", "VECTOR", drawers_coll),
                (drawers_meta, "urn:metadata", "METADATA", drawers_coll),
                (closets_vec, "urn:vector", "VECTOR", closets_coll),
                (closets_meta, "urn:metadata", "METADATA", closets_coll),
            ],
        )
        conn.executemany(
            "INSERT INTO max_seq_id(segment_id, seq_id) VALUES (?, ?)",
            [
                (drawers_vec, drawers_vec_poison),
                (drawers_meta, drawers_meta_poison),
                (closets_vec, closets_vec_poison),
                (closets_meta, closets_meta_poison),
            ],
        )
        # Populate embeddings so the collection-MAX heuristic has data to work with.
        # drawers METADATA owns the max at drawers_meta_max; closets likewise.
        for i in range(1, drawers_meta_max + 1, max(drawers_meta_max // 5, 1)):
            conn.execute(
                "INSERT INTO embeddings(segment_id, embedding_id, seq_id) VALUES (?, ?, ?)",
                (drawers_meta, f"d-{i}", i),
            )
        conn.execute(
            "INSERT INTO embeddings(segment_id, embedding_id, seq_id) VALUES (?, ?, ?)",
            (drawers_meta, "d-max", drawers_meta_max),
        )
        for i in range(1, closets_meta_max + 1, max(closets_meta_max // 5, 1)):
            conn.execute(
                "INSERT INTO embeddings(segment_id, embedding_id, seq_id) VALUES (?, ?, ?)",
                (closets_meta, f"c-{i}", i),
            )
        conn.execute(
            "INSERT INTO embeddings(segment_id, embedding_id, seq_id) VALUES (?, ?, ?)",
            (closets_meta, "c-max", closets_meta_max),
        )
        conn.commit()
    return {
        "drawers_vec": drawers_vec,
        "drawers_meta": drawers_meta,
        "closets_vec": closets_vec,
        "closets_meta": closets_meta,
        "drawers_meta_max": drawers_meta_max,
        "closets_meta_max": closets_meta_max,
        "poisoned_values": {
            drawers_vec: drawers_vec_poison,
            drawers_meta: drawers_meta_poison,
            closets_vec: closets_vec_poison,
            closets_meta: closets_meta_poison,
        },
    }


def test_max_seq_id_detects_poison_rows(tmp_path):
    palace = str(tmp_path / "palace")
    seg = _seed_poisoned_max_seq_id(palace)
    db_path = os.path.join(palace, "chroma.sqlite3")

    # Add one clean row to confirm the threshold actually filters.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO segments VALUES ('seg-clean', 'urn:vector', 'VECTOR', 'coll-clean')"
        )
        conn.execute("INSERT INTO max_seq_id VALUES ('seg-clean', 1234)")
        conn.commit()

    found = repair._detect_poisoned_max_seq_ids(db_path)
    ids = {sid for sid, _ in found}
    assert ids == {
        seg["drawers_vec"],
        seg["drawers_meta"],
        seg["closets_vec"],
        seg["closets_meta"],
    }
    for sid, val in found:
        assert val > repair.MAX_SEQ_ID_SANITY_THRESHOLD
    assert "seg-clean" not in ids


def test_max_seq_id_heuristic_uses_collection_max(tmp_path):
    palace = str(tmp_path / "palace")
    seg = _seed_poisoned_max_seq_id(palace)

    result = repair.repair_max_seq_id(palace, dry_run=True)
    # Both drawers segments (VECTOR + METADATA) get the drawers collection max.
    assert result["after"][seg["drawers_vec"]] == seg["drawers_meta_max"]
    assert result["after"][seg["drawers_meta"]] == seg["drawers_meta_max"]
    # Both closets segments get the closets collection max.
    assert result["after"][seg["closets_vec"]] == seg["closets_meta_max"]
    assert result["after"][seg["closets_meta"]] == seg["closets_meta_max"]


def test_max_seq_id_from_sidecar_exact_restore(tmp_path):
    palace = str(tmp_path / "palace")
    seg = _seed_poisoned_max_seq_id(palace)

    # Craft a sidecar with known clean values that differ from the heuristic's
    # collection-max, so we can prove the sidecar path is preferred.
    sidecar_path = str(tmp_path / "chroma.sqlite3.sidecar")
    clean = {
        seg["drawers_vec"]: 499001,
        seg["drawers_meta"]: 499002,
        seg["closets_vec"]: 498001,
        seg["closets_meta"]: 498002,
    }
    with sqlite3.connect(sidecar_path) as conn:
        conn.execute("CREATE TABLE max_seq_id(segment_id TEXT PRIMARY KEY, seq_id INTEGER)")
        conn.executemany(
            "INSERT INTO max_seq_id VALUES (?, ?)",
            list(clean.items()),
        )
        conn.commit()

    result = repair.repair_max_seq_id(palace, from_sidecar=sidecar_path, assume_yes=True)
    assert result["segment_repaired"]
    db_path = os.path.join(palace, "chroma.sqlite3")
    with sqlite3.connect(db_path) as conn:
        rows = dict(conn.execute("SELECT segment_id, seq_id FROM max_seq_id").fetchall())
    for sid, val in clean.items():
        assert rows[sid] == val


def test_max_seq_id_dry_run_no_mutation(tmp_path):
    palace = str(tmp_path / "palace")
    seg = _seed_poisoned_max_seq_id(palace)
    db_path = os.path.join(palace, "chroma.sqlite3")

    with sqlite3.connect(db_path) as conn:
        before = dict(conn.execute("SELECT segment_id, seq_id FROM max_seq_id").fetchall())

    result = repair.repair_max_seq_id(palace, dry_run=True)
    assert result["dry_run"] is True
    assert result["segment_repaired"] == []

    with sqlite3.connect(db_path) as conn:
        after = dict(conn.execute("SELECT segment_id, seq_id FROM max_seq_id").fetchall())
    assert before == after
    # Nothing dropped into the palace dir either (no backup on dry-run).
    assert not any(fn.startswith("chroma.sqlite3.max-seq-id-backup-") for fn in os.listdir(palace))
    assert seg["drawers_vec"] in before  # sanity


def test_max_seq_id_segment_filter(tmp_path):
    palace = str(tmp_path / "palace")
    seg = _seed_poisoned_max_seq_id(palace)

    result = repair.repair_max_seq_id(palace, segment=seg["drawers_meta"], assume_yes=True)
    assert result["segment_repaired"] == [seg["drawers_meta"]]

    db_path = os.path.join(palace, "chroma.sqlite3")
    with sqlite3.connect(db_path) as conn:
        rows = dict(conn.execute("SELECT segment_id, seq_id FROM max_seq_id").fetchall())
    # Filtered segment is fixed; the other three remain poisoned.
    assert rows[seg["drawers_meta"]] == seg["drawers_meta_max"]
    for other in (seg["drawers_vec"], seg["closets_vec"], seg["closets_meta"]):
        assert rows[other] > repair.MAX_SEQ_ID_SANITY_THRESHOLD


def test_max_seq_id_heuristic_decodes_blob_embeddings_seq_id(tmp_path):
    """`embeddings.seq_id` rows can be BLOB-typed on palaces where chromadb
    1.5.x has been writing seq_ids natively (8-byte big-endian uint64).
    `_compute_heuristic_seq_id` must decode those rather than crashing on
    `int(bytes)` — the recovery feature is meaningless if it can't read
    the storage format it was designed to repair.
    """
    palace = str(tmp_path / "palace")
    seg = _seed_poisoned_max_seq_id(palace)
    db_path = os.path.join(palace, "chroma.sqlite3")

    drawers_meta_max = seg["drawers_meta_max"]
    blob_max = drawers_meta_max + 7
    blob_value = blob_max.to_bytes(8, "big")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO embeddings(segment_id, embedding_id, seq_id) VALUES (?, ?, ?)",
            (seg["drawers_meta"], "d-blob-max", blob_value),
        )
        conn.commit()

    result = repair.repair_max_seq_id(palace, dry_run=True)
    assert result["after"][seg["drawers_vec"]] == blob_max
    assert result["after"][seg["drawers_meta"]] == blob_max


def test_max_seq_id_no_poison_is_noop(tmp_path):
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    db_path = os.path.join(palace, "chroma.sqlite3")
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE segments(
                id TEXT PRIMARY KEY, type TEXT, scope TEXT, collection TEXT
            );
            CREATE TABLE max_seq_id(segment_id TEXT PRIMARY KEY, seq_id);
            CREATE TABLE embeddings(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                segment_id TEXT, embedding_id TEXT, seq_id
            );
            INSERT INTO segments VALUES ('s1', 'urn:vector', 'VECTOR', 'coll');
            INSERT INTO max_seq_id VALUES ('s1', 12345);
            """
        )
        conn.commit()

    result = repair.repair_max_seq_id(palace, assume_yes=True)
    assert result["segment_repaired"] == []
    assert result["backup"] is None
    with sqlite3.connect(db_path) as conn:
        rows = dict(conn.execute("SELECT segment_id, seq_id FROM max_seq_id").fetchall())
    assert rows == {"s1": 12345}


def test_max_seq_id_backup_created(tmp_path):
    palace = str(tmp_path / "palace")
    seg = _seed_poisoned_max_seq_id(palace)

    result = repair.repair_max_seq_id(palace, assume_yes=True)
    assert result["backup"] is not None
    assert os.path.isfile(result["backup"])

    with sqlite3.connect(result["backup"]) as conn:
        rows = dict(conn.execute("SELECT segment_id, seq_id FROM max_seq_id").fetchall())
    # Backup preserves the poisoned values from before the repair.
    assert rows[seg["drawers_vec"]] == seg["poisoned_values"][seg["drawers_vec"]]
    assert rows[seg["drawers_meta"]] == seg["poisoned_values"][seg["drawers_meta"]]


def test_max_seq_id_rollback_on_verification_failure(tmp_path, monkeypatch):
    """If the post-update detector still sees poison, raise and leave a backup."""
    palace = str(tmp_path / "palace")
    _seed_poisoned_max_seq_id(palace)

    real_detect = repair._detect_poisoned_max_seq_ids
    calls = {"n": 0}

    def flaky_detect(*args, **kwargs):
        calls["n"] += 1
        # First call (pre-repair) returns the real set so the repair proceeds.
        if calls["n"] == 1:
            return real_detect(*args, **kwargs)
        # Second call (post-repair verification) claims poison still exists.
        return [("seg-fake-still-poisoned", repair.MAX_SEQ_ID_SANITY_THRESHOLD + 1)]

    monkeypatch.setattr(repair, "_detect_poisoned_max_seq_ids", flaky_detect)

    with pytest.raises(repair.MaxSeqIdVerificationError):
        repair.repair_max_seq_id(palace, assume_yes=True)

    # A backup file is still present — caller can roll back from it.
    leftover = [fn for fn in os.listdir(palace) if "max-seq-id-backup-" in fn]
    assert leftover


def test_sqlite_integrity_errors_returns_empty_for_healthy_db(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    db_path = palace / "chroma.sqlite3"

    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE dummy(id INTEGER PRIMARY KEY)")
        conn.commit()

    assert repair.sqlite_integrity_errors(str(palace)) == []


def test_sqlite_integrity_errors_reports_unreadable_sqlite_file(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    db_path = palace / "chroma.sqlite3"
    db_path.write_bytes(b"not a sqlite database")

    errors = repair.sqlite_integrity_errors(str(palace))

    assert errors
    assert "quick_check failed" in errors[0]


@patch("mempalace.repair.shutil")
@patch("mempalace.repair.ChromaBackend")
def test_rebuild_index_aborts_on_sqlite_integrity_errors_before_delete_collection(
    mock_backend_cls,
    mock_shutil,
    tmp_path,
    capsys,
):
    """Regression for #1362: fail before Chroma delete_collection on sqlite corruption."""

    sqlite_path = tmp_path / "chroma.sqlite3"
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute("CREATE TABLE dummy(id INTEGER PRIMARY KEY)")
        conn.commit()

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
    }

    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)

    with patch(
        "mempalace.repair.sqlite_integrity_errors",
        return_value=[
            "Page 4 of B-tree 12345: database disk image is malformed",
            "Page 8 of B-tree 67890: database disk image is malformed",
        ],
    ):
        repair.rebuild_index(palace_path=str(tmp_path))

    out = capsys.readouterr().out

    assert "SQLite-layer corruption detected before repair rebuild" in out
    assert "PRAGMA quick_check" in out
    assert "delete_collection" in out
    assert "Page 4 of B-tree" in out

    mock_backend.delete_collection.assert_not_called()
    mock_backend.create_collection.assert_not_called()
    mock_shutil.copy2.assert_not_called()


def test_rebuild_index_runs_sqlite_preflight_before_chromadb_open(tmp_path, capsys):
    """The SQLite integrity preflight must run BEFORE backend.get_collection.

    chromadb's rust binding raises pyo3_runtime.PanicException (which is not
    a regular Exception subclass) on a malformed page, so any get_collection
    call against a corrupt SQLite propagates past `except Exception` handlers
    and produces a 30-line stack trace instead of the friendly abort message.
    Regression test for the ordering bug where the preflight was placed after
    the chromadb client open and therefore never reached on the cases it was
    designed to catch (#1364 follow-up).
    """
    palace = tmp_path / "palace"
    palace.mkdir()

    # Build a real chromadb palace with one drawer so chroma.sqlite3 exists
    # at full schema size, then mangle several middle pages so PRAGMA
    # quick_check fails with "disk image is malformed". This matches the
    # production failure mode users hit in #1362 / #1364.
    from mempalace.backends.chroma import ChromaBackend

    backend = ChromaBackend()
    try:
        col = backend.create_collection(str(palace), "mempalace_drawers")
        col.upsert(
            ids=["d1"],
            documents=["doc"],
            metadatas=[{"wing": "w", "room": "r"}],
        )
    finally:
        backend.close()

    sqlite_path = palace / "chroma.sqlite3"
    pre_size = sqlite_path.stat().st_size

    # Compute a page-aligned corruption offset that's always inside the
    # existing file. SQLite uses 4 KB pages by default; we mangle 4 pages
    # somewhere in the middle, skipping at least the first 2 pages
    # (header + root) so the file still opens. Without clamping to the
    # actual file size, a seek past EOF on r+b mode would silently
    # extend the file with zero-padding and leave the original pages
    # intact — quick_check would still pass, and the regression guard
    # would skip the bug.
    PAGE = 4096
    CORRUPT_BYTES = 16384  # 4 pages
    HEADER_GUARD = PAGE * 2  # leave header + root pages intact
    assert (
        pre_size >= HEADER_GUARD + CORRUPT_BYTES
    ), f"sqlite db too small to mangle without truncating: {pre_size} bytes"
    # Round (pre_size - CORRUPT_BYTES) down to a page boundary so we
    # mangle whole pages. Cap at offset 40960 (page 10) for stable
    # diagnostics across SQLite versions that may grow the file.
    max_offset = (pre_size - CORRUPT_BYTES) & ~(PAGE - 1)
    corrupt_offset = min(40960, max_offset)
    assert corrupt_offset >= HEADER_GUARD, f"corruption offset {corrupt_offset} too close to header"

    with open(sqlite_path, "r+b") as f:
        f.seek(corrupt_offset)
        f.write(b"\xde\xad\xbe\xef" * (CORRUPT_BYTES // 4))

    # No chromadb mocks: rebuild_index must reach sqlite_integrity_errors
    # before any code path that opens a chromadb client. If the preflight
    # comes too late, the test fails with pyo3_runtime.PanicException
    # instead of returning cleanly.
    repair.rebuild_index(palace_path=str(palace))

    out = capsys.readouterr().out
    assert "SQLite-layer corruption detected before repair rebuild" in out
    assert "PRAGMA quick_check" in out
    assert "disk image is malformed" in out


def test_max_seq_id_preflight_preserves_embeddings_queue(tmp_path):
    """#1295: default repair preflight must not drop queued writes."""

    palace = str(tmp_path / "palace")
    seg = _seed_poisoned_max_seq_id(
        palace,
        drawers_meta_max=102,
        closets_meta_max=11,
    )
    db_path = os.path.join(palace, "chroma.sqlite3")

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO embeddings_queue(seq_id, topic, id) VALUES (?, ?, ?)",
            [
                (seq_id, "persistent://default/default/mempalace_drawers", f"queued-{seq_id}")
                for seq_id in range(103, 123)
            ],
        )
        conn.commit()

    result = repair.maybe_repair_poisoned_max_seq_id_before_rebuild(
        palace,
        assume_yes=True,
    )

    assert result is not None
    assert result["segment_repaired"]

    with sqlite3.connect(db_path) as conn:
        max_seq_rows = dict(conn.execute("SELECT segment_id, seq_id FROM max_seq_id"))
        queue_count = conn.execute("SELECT COUNT(*) FROM embeddings_queue").fetchone()[0]

    assert max_seq_rows[seg["drawers_vec"]] == seg["drawers_meta_max"]
    assert max_seq_rows[seg["drawers_meta"]] == seg["drawers_meta_max"]
    assert max_seq_rows[seg["closets_vec"]] == seg["closets_meta_max"]
    assert max_seq_rows[seg["closets_meta"]] == seg["closets_meta_max"]

    # The old legacy rebuild path can discard queued writes. The preflight
    # repair must leave them on disk for Chroma to drain after the bookmark is
    # unpoisoned.
    assert queue_count == 20


def test_rebuild_index_repairs_poisoned_max_seq_id_before_collection_rebuild(tmp_path, capsys):
    """A poisoned bookmark should short-circuit before the legacy rebuild path."""

    palace = str(tmp_path / "palace")
    _seed_poisoned_max_seq_id(palace)

    with patch("mempalace.repair.ChromaBackend") as mock_backend:
        repair.rebuild_index(palace)

    out = capsys.readouterr().out
    backend = mock_backend.return_value

    # repair_max_seq_id may instantiate ChromaBackend to close cached clients
    # after editing sqlite directly. That is safe. The important thing is that
    # rebuild_index must not continue into the legacy Chroma collection read /
    # count / rebuild path after the max_seq_id preflight handles the issue.
    backend.get_collection.assert_not_called()

    assert "Detected poisoned max_seq_id rows" in out
    assert "non-destructive max_seq_id repair" in out


def test_rebuild_index_releases_lock_when_max_seq_id_preflight_short_circuits(tmp_path, monkeypatch):
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    lock = MagicMock()
    monkeypatch.setattr(repair, "palace_write_lock", MagicMock(return_value=lock))
    monkeypatch.setattr(
        repair,
        "maybe_repair_poisoned_max_seq_id_before_rebuild",
        MagicMock(return_value={"segment_repaired": True}),
    )

    repair.rebuild_index(palace)

    lock.__enter__.assert_called_once_with()
    lock.__exit__.assert_called_once_with(None, None, None)


def test_rebuild_index_releases_lock_when_sqlite_integrity_preflight_aborts(tmp_path, monkeypatch):
    palace = str(tmp_path / "palace")
    os.makedirs(palace)
    lock = MagicMock()
    monkeypatch.setattr(repair, "palace_write_lock", MagicMock(return_value=lock))
    monkeypatch.setattr(repair, "sqlite_integrity_errors", MagicMock(return_value=["bad page"]))
    monkeypatch.setattr(repair, "print_sqlite_integrity_abort", MagicMock())

    repair.rebuild_index(palace)

    lock.__enter__.assert_called_once_with()
    lock.__exit__.assert_called_once_with(None, None, None)


# ── extract_via_sqlite + rebuild_from_sqlite (#1308) ──────────────────
#
# These tests build real chromadb palaces in tmp_path rather than mocking
# the SQLite layer. The bug class they guard against is "extraction sees
# different rows than chromadb stored" — the only honest check is to let
# chromadb actually write rows and then read them back via the SQLite
# bypass. Mocking the SQLite cursor would defeat the test.


def _seed_palace(palace_path, collection_name, rows):
    """Build a real chromadb palace at ``palace_path`` and add ``rows``.

    ``rows`` is a list of ``(id, document, metadata)`` tuples.
    """
    from mempalace.backends.chroma import ChromaBackend

    backend = ChromaBackend()
    try:
        col = backend.create_collection(str(palace_path), collection_name)
        col.upsert(
            ids=[r[0] for r in rows],
            documents=[r[1] for r in rows],
            metadatas=[r[2] for r in rows],
        )
    finally:
        # Release chromadb's rust-side SQLite/HNSW file locks before the
        # caller proceeds. Without this, an in-place rebuild on Windows
        # fails with WinError 32 on data_level0.bin during the archive
        # rename (cf. PR #1310 test-windows job).
        backend.close()


def test_extract_via_sqlite_returns_all_rows_with_metadata(tmp_path):
    """Round-trip: a chromadb palace with N upserted rows returns those
    same N rows when read via the SQLite bypass.

    Catches: anyone who breaks the segments/embeddings/embedding_metadata
    JOIN, swaps the metadata vs vector segment, or changes how the
    document is stored under the ``chroma:document`` key.

    Also asserts every embedding row underlying the extraction lives in
    a ``segments.scope = 'METADATA'`` segment. Document + metadata rows
    are stored under METADATA in Chroma's segment layout while HNSW
    files live under ``VECTOR``; locking that assumption in here means a
    future refactor that accidentally points the JOIN at ``VECTOR``
    fails this test instead of silently regressing the recovery path.
    """
    rows = [
        (f"drawer_{i:03d}", f"document body {i}", {"wing": "test_wing", "room": f"r{i % 3}"})
        for i in range(25)
    ]
    _seed_palace(tmp_path, "mempalace_drawers", rows)

    extracted = list(repair.extract_via_sqlite(str(tmp_path), "mempalace_drawers"))

    assert len(extracted) == 25
    by_id = {emb_id: (doc, meta) for emb_id, doc, meta in extracted}
    assert set(by_id) == {r[0] for r in rows}
    for emb_id, doc, meta in rows:
        got_doc, got_meta = by_id[emb_id]
        assert got_doc == doc, f"document mangled for {emb_id}"
        assert got_meta == meta, f"metadata mangled for {emb_id}: {got_meta!r}"

    # Lock the segment-scope assumption directly against Chroma's on-disk
    # layout so a future change that points the extraction JOIN at the
    # VECTOR segment cannot pass this test. Query each extracted row's
    # backing segment scope via the same SQLite tables ``extract_via_sqlite``
    # reads from.
    sqlite_path = os.path.join(str(tmp_path), "chroma.sqlite3")
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        scopes = {
            scope
            for (scope,) in conn.execute(
                """
                SELECT DISTINCT s.scope
                FROM embeddings e
                JOIN segments s ON e.segment_id = s.id
                JOIN collections c ON s.collection = c.id
                WHERE c.name = ? AND e.embedding_id IN ({})
                """.format(",".join("?" * len(extracted))),
                ("mempalace_drawers", *(emb_id for emb_id, _, _ in extracted)),
            )
        }
    finally:
        conn.close()
    assert scopes == {"METADATA"}, (
        f"extraction is reading from segments scoped {scopes!r}; only "
        "'METADATA' should back the document/metadata rows. If Chroma's "
        "segment layout changed, update extract_via_sqlite's WHERE clause."
    )


def test_extract_via_sqlite_preserves_typed_metadata(tmp_path):
    """Chromadb stores int / float / bool / string in distinct typed
    columns. Extraction must round-trip the original type, not coerce
    everything to string.

    Catches: a regression where the SELECT order changes and ints come
    back as None, or where the column-resolution rule prefers the wrong
    column.
    """
    rows = [
        (
            "drawer_typed",
            "doc",
            {
                "wing": "w",
                "chunk_index": 7,  # int
                "score": 0.42,  # float
                "is_active": True,  # bool
            },
        ),
    ]
    _seed_palace(tmp_path, "mempalace_drawers", rows)

    extracted = list(repair.extract_via_sqlite(str(tmp_path), "mempalace_drawers"))
    assert len(extracted) == 1
    _, _, meta = extracted[0]

    assert meta["chunk_index"] == 7 and isinstance(meta["chunk_index"], int)
    assert meta["score"] == 0.42 and isinstance(meta["score"], float)
    assert meta["is_active"] is True
    assert meta["wing"] == "w"


def test_extract_via_sqlite_unknown_collection_yields_nothing(tmp_path):
    """Asking for a collection that isn't in the palace must return an
    empty iterator, not silently fall back to another collection's
    metadata segment. Seeds two real collections and queries for a third
    name so a regression that drops the WHERE c.name=? filter would leak
    rows from the seeded collections rather than passing.
    """
    _seed_palace(tmp_path, "mempalace_drawers", [("d1", "doc", {"wing": "w"})])
    _seed_palace(tmp_path, "mempalace_closets", [("c1", "abbrev", {"wing": "w"})])
    assert list(repair.extract_via_sqlite(str(tmp_path), "not_a_real_collection")) == []


def test_extract_via_sqlite_missing_palace_yields_nothing(tmp_path):
    """No chroma.sqlite3 → empty iterator, no exception. Callers depend
    on this when probing speculatively."""
    empty = tmp_path / "no_palace_here"
    empty.mkdir()
    assert list(repair.extract_via_sqlite(str(empty), "mempalace_drawers")) == []


def test_extract_via_sqlite_skips_rows_missing_document_metadata(tmp_path):
    sqlite_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT NOT NULL, scope TEXT NOT NULL);
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL
            );
            CREATE TABLE embedding_metadata (
                id INTEGER,
                key TEXT NOT NULL,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER,
                PRIMARY KEY (id, key)
            );
            """
        )
        conn.execute("INSERT INTO collections (id, name) VALUES ('c1', 'mempalace_drawers')")
        conn.execute(
            "INSERT INTO segments (id, collection, scope) VALUES ('seg1', 'c1', 'METADATA')"
        )
        conn.execute(
            "INSERT INTO embeddings (id, segment_id, embedding_id) VALUES (1, 'seg1', 'd1')"
        )
        conn.execute(
            """
            INSERT INTO embedding_metadata
                (id, key, string_value, int_value, float_value, bool_value)
            VALUES (1, 'wing', 'w', NULL, NULL, NULL)
            """
        )
        conn.commit()
    finally:
        conn.close()

    assert list(repair.extract_via_sqlite(str(tmp_path), "mempalace_drawers")) == []


def test_extract_via_sqlite_uses_existing_chroma_indexes(tmp_path):
    """The SQLite recovery scan should not need a temp sort. Chroma already
    creates a unique index on ``embeddings(segment_id, embedding_id)`` and a
    primary-key index on ``embedding_metadata(id, key)``; the extractor query
    should follow those indexes so large palaces stream from disk instead of
    building a temp B-tree.
    """
    sqlite_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE segments (
                id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                scope TEXT NOT NULL
            );
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL,
                seq_id BLOB NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (segment_id, embedding_id)
            );
            CREATE TABLE embedding_metadata (
                id INTEGER REFERENCES embeddings(id),
                key TEXT NOT NULL,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER,
                PRIMARY KEY (id, key)
            );
            """
        )
        conn.execute("INSERT INTO collections (id, name) VALUES ('c1', 'mempalace_drawers')")
        conn.execute(
            "INSERT INTO segments (id, collection, scope) VALUES ('seg1', 'c1', 'METADATA')"
        )
        conn.execute(
            "INSERT INTO embeddings (id, segment_id, embedding_id, seq_id) "
            "VALUES (1, 'seg1', 'drawer_z', X'01'), (2, 'seg1', 'drawer_a', X'02')"
        )
        conn.executemany(
            """
            INSERT INTO embedding_metadata
                (id, key, string_value, int_value, float_value, bool_value)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "chroma:document", "body z", None, None, None),
                (1, "wing", "w", None, None, None),
                (2, "chroma:document", "body a", None, None, None),
                (2, "wing", "w", None, None, None),
            ],
        )
        plan = "\n".join(
            row[-1]
            for row in conn.execute(
                "EXPLAIN QUERY PLAN " + repair._SQLITE_EXTRACT_ROWS_SQL, ("seg1",)
            )
        )
        conn.commit()
    finally:
        conn.close()

    assert "USE TEMP B-TREE" not in plan
    assert list(repair.extract_via_sqlite(str(tmp_path), "mempalace_drawers")) == [
        ("drawer_a", "body a", {"wing": "w"}),
        ("drawer_z", "body z", {"wing": "w"}),
    ]


def test_load_sqlite_vectors_reads_latest_float32_vectors(tmp_path):
    sqlite_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE embeddings_queue (
                seq_id INTEGER PRIMARY KEY,
                operation INTEGER NOT NULL,
                topic TEXT NOT NULL,
                id TEXT NOT NULL,
                vector BLOB,
                encoding TEXT,
                metadata TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        topic = "persistent://default/default/col-drawers"
        conn.execute(
            "INSERT INTO collections (id, name) VALUES ('col-drawers', 'mempalace_drawers')"
        )
        conn.executemany(
            """
            INSERT INTO embeddings_queue
                (seq_id, operation, topic, id, vector, encoding, metadata)
            VALUES (?, 2, ?, ?, ?, 'FLOAT32', '{}')
            """,
            [
                (1, topic, "drawer_1", struct.pack("<ff", 1.0, 2.0)),
                (2, topic, "drawer_2", struct.pack("<ff", 3.0, 4.0)),
                (3, topic, "drawer_1", struct.pack("<ff", 5.0, 6.0)),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    vectors = repair._load_sqlite_vectors(str(tmp_path), "mempalace_drawers")

    assert set(vectors) == {"drawer_1", "drawer_2"}
    assert vectors["drawer_1"].tolist() == [5.0, 6.0]
    assert vectors["drawer_2"].tolist() == [3.0, 4.0]


def test_load_sqlite_vectors_raises_on_unreadable_vector_tables(tmp_path):
    sqlite_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(sqlite_path)
    conn.execute("CREATE TABLE unrelated (id TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.OperationalError):
        repair._load_sqlite_vectors(str(tmp_path), "mempalace_drawers")


def test_rebuild_one_collection_reuses_complete_stored_vectors(tmp_path):
    sqlite_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE embeddings_queue (
                seq_id INTEGER PRIMARY KEY,
                operation INTEGER NOT NULL,
                topic TEXT NOT NULL,
                id TEXT NOT NULL,
                vector BLOB,
                encoding TEXT,
                metadata TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            "INSERT INTO collections (id, name) VALUES ('col-drawers', 'mempalace_drawers')"
        )
        conn.execute(
            """
            INSERT INTO embeddings_queue
                (seq_id, operation, topic, id, vector, encoding, metadata)
            VALUES (1, 2, 'persistent://default/default/col-drawers',
                    'drawer_with_vector', ?, 'FLOAT32', '{}')
            """,
            (struct.pack("<ff", 0.25, 0.75),),
        )
        conn.commit()
    finally:
        conn.close()

    backend = MagicMock()
    col = MagicMock()
    col.count.return_value = 1
    backend.create_collection.return_value = col

    with (
        patch(
            "mempalace.repair.extract_via_sqlite",
            return_value=iter([("drawer_with_vector", "doc 1", {"wing": "w"})]),
        ),
        patch(
            "mempalace.repair._sqlite_embedding_ids",
            return_value=["drawer_with_vector"],
        ),
    ):
        upserted = repair._rebuild_one_collection(
            backend=backend,
            source_palace=str(tmp_path),
            dest_palace=str(tmp_path / "dest"),
            collection_name="mempalace_drawers",
            batch_size=10,
            archive_path=None,
            counts_so_far={},
        )

    assert upserted == 1
    col.upsert.assert_called_once()
    assert col.upsert.call_args.kwargs["ids"] == ["drawer_with_vector"]
    assert col.upsert.call_args.kwargs["embeddings"][0].tolist() == [0.25, 0.75]


def test_rebuild_one_collection_reembeds_when_stored_vectors_incomplete(tmp_path):
    backend = MagicMock()
    col = MagicMock()
    col.count.return_value = 2
    backend.create_collection.return_value = col

    with (
        patch(
            "mempalace.repair._load_sqlite_vectors",
            return_value={"drawer_with_vector": [0.25, 0.75]},
        ),
        patch(
            "mempalace.repair._sqlite_embedding_ids",
            return_value=["drawer_with_vector", "drawer_without_vector"],
        ),
        patch(
            "mempalace.repair.extract_via_sqlite",
            return_value=iter(
                [
                    ("drawer_with_vector", "doc 1", {"wing": "w"}),
                    ("drawer_without_vector", "doc 2", {"wing": "w"}),
                ]
            ),
        ),
    ):
        upserted = repair._rebuild_one_collection(
            backend=backend,
            source_palace=str(tmp_path),
            dest_palace=str(tmp_path / "dest"),
            collection_name="mempalace_drawers",
            batch_size=10,
            archive_path=None,
            counts_so_far={},
            reembed=True,
        )

    assert upserted == 2
    col.upsert.assert_called_once_with(
        ids=["drawer_with_vector", "drawer_without_vector"],
        documents=["doc 1", "doc 2"],
        metadatas=[{"wing": "w"}, {"wing": "w"}],
    )


def test_rebuild_one_collection_fails_when_extraction_skips_source_rows(tmp_path):
    backend = MagicMock()
    col = MagicMock()
    col.count.return_value = 1
    backend.create_collection.return_value = col

    with (
        patch("mempalace.repair._load_sqlite_vectors", return_value={}),
        patch(
            "mempalace.repair._sqlite_embedding_ids",
            return_value=["drawer_with_metadata", "drawer_missing_metadata"],
        ),
        patch(
            "mempalace.repair.extract_via_sqlite",
            return_value=iter([("drawer_with_metadata", "doc 1", {"wing": "w"})]),
        ),
    ):
        with pytest.raises(repair.RebuildPartialError) as excinfo:
            repair._rebuild_one_collection(
                backend=backend,
                source_palace=str(tmp_path),
                dest_palace=str(tmp_path / "dest"),
                collection_name="mempalace_drawers",
                batch_size=10,
                archive_path=None,
                counts_so_far={},
            )

    assert excinfo.value.failed_collection == "mempalace_drawers"
    assert "source/extracted count mismatch" in str(excinfo.value)


def test_rebuild_one_collection_fails_when_source_document_metadata_missing(tmp_path):
    sqlite_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.executescript(
            """
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT NOT NULL, scope TEXT NOT NULL);
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id TEXT NOT NULL,
                embedding_id TEXT NOT NULL
            );
            CREATE TABLE embedding_metadata (
                id INTEGER,
                key TEXT NOT NULL,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER,
                PRIMARY KEY (id, key)
            );
            """
        )
        conn.execute("INSERT INTO collections (id, name) VALUES ('c1', 'mempalace_drawers')")
        conn.execute(
            "INSERT INTO segments (id, collection, scope) VALUES ('seg1', 'c1', 'METADATA')"
        )
        conn.execute(
            "INSERT INTO embeddings (id, segment_id, embedding_id) VALUES (1, 'seg1', 'd1')"
        )
        conn.execute(
            """
            INSERT INTO embedding_metadata
                (id, key, string_value, int_value, float_value, bool_value)
            VALUES (1, 'wing', 'w', NULL, NULL, NULL)
            """
        )
        conn.commit()
    finally:
        conn.close()

    backend = MagicMock()
    col = MagicMock()
    col.count.return_value = 0
    backend.create_collection.return_value = col

    with pytest.raises(repair.RebuildPartialError) as excinfo:
        repair._rebuild_one_collection(
            backend=backend,
            source_palace=str(tmp_path),
            dest_palace=str(tmp_path / "dest"),
            collection_name="mempalace_drawers",
            batch_size=10,
            archive_path=None,
            counts_so_far={},
            reembed=True,
        )

    assert "source/extracted count mismatch" in str(excinfo.value)
    col.upsert.assert_not_called()


def test_rebuild_one_collection_reembed_skips_stored_vectors(tmp_path):
    backend = MagicMock()
    col = MagicMock()
    col.count.return_value = 1
    backend.create_collection.return_value = col

    with (
        patch(
            "mempalace.repair.extract_via_sqlite",
            return_value=iter([("drawer_with_vector", "doc 1", {"wing": "w"})]),
        ),
        patch("mempalace.repair._load_sqlite_vectors") as load_vectors,
    ):
        upserted = repair._rebuild_one_collection(
            backend=backend,
            source_palace=str(tmp_path),
            dest_palace=str(tmp_path / "dest"),
            collection_name="mempalace_drawers",
            batch_size=10,
            archive_path=None,
            counts_so_far={},
            reembed=True,
        )

    assert upserted == 1
    load_vectors.assert_not_called()
    col.upsert.assert_called_once_with(
        ids=["drawer_with_vector"],
        documents=["doc 1"],
        metadatas=[{"wing": "w"}],
    )


def test_rebuild_one_collection_wraps_create_collection_failure(tmp_path):
    backend = MagicMock()
    backend.create_collection.side_effect = RuntimeError("create failed")

    with pytest.raises(repair.RebuildPartialError) as excinfo:
        repair._rebuild_one_collection(
            backend=backend,
            source_palace=str(tmp_path),
            dest_palace=str(tmp_path / "dest"),
            collection_name="mempalace_drawers",
            batch_size=10,
            archive_path=str(tmp_path / "archive"),
            counts_so_far={},
        )

    assert "during creating destination collection" in excinfo.value.message
    assert "create failed" in excinfo.value.message
    assert excinfo.value.archive_path == str(tmp_path / "archive")
    assert excinfo.value.partial_counts == {"mempalace_drawers": 0}


def test_rebuild_one_collection_recovery_hint_includes_dest_and_source(tmp_path):
    backend = MagicMock()
    col = MagicMock()
    col.upsert.side_effect = RuntimeError("upsert failed")
    backend.create_collection.return_value = col
    archive_path = str(tmp_path / "archive")
    dest_path = str(tmp_path / "dest")

    with (
        patch("mempalace.repair._load_sqlite_vectors", return_value={}),
        patch("mempalace.repair._sqlite_embedding_ids", return_value=["d1"]),
        patch(
            "mempalace.repair.extract_via_sqlite",
            return_value=iter([("d1", "doc 1", {"wing": "w"})]),
        ),
        pytest.raises(repair.RebuildPartialError) as excinfo,
    ):
        repair._rebuild_one_collection(
            backend=backend,
            source_palace=str(tmp_path / "source"),
            dest_palace=dest_path,
            collection_name="mempalace_drawers",
            batch_size=10,
            archive_path=archive_path,
            counts_so_far={},
        )

    assert f"mempalace --palace {dest_path} repair --mode from-sqlite" in excinfo.value.message
    assert f"--source {archive_path}" in excinfo.value.message
    assert "during extracting and upserting rows" in excinfo.value.message


def test_rebuild_one_collection_validates_destination_count(tmp_path):
    backend = MagicMock()
    col = MagicMock()
    col.count.return_value = 1
    backend.create_collection.return_value = col

    with (
        patch(
            "mempalace.repair.extract_via_sqlite",
            return_value=iter(
                [
                    ("d1", "doc 1", {"wing": "w"}),
                    ("d2", "doc 2", {"wing": "w"}),
                ]
            ),
        ),
        pytest.raises(repair.RebuildPartialError) as excinfo,
    ):
        repair._rebuild_one_collection(
            backend=backend,
            source_palace=str(tmp_path),
            dest_palace=str(tmp_path / "dest"),
            collection_name="mempalace_drawers",
            batch_size=10,
            archive_path=None,
            counts_so_far={},
        )

    assert "count mismatch" in excinfo.value.message
    assert excinfo.value.partial_counts == {"mempalace_drawers": 2}


def test_rebuild_from_sqlite_roundtrips_via_real_chromadb(tmp_path):
    """End-to-end: seed source palace, rebuild into a fresh dest, then
    open dest with a fresh ChromaBackend and verify ``count()`` and
    metadata filters return the original rows. Also asserts a closet
    document round-trips so a future regression that re-embeds with the
    wrong EF or swaps drawer/closet content would fail here.

    This is the single most important regression guard. If
    ``rebuild_from_sqlite`` silently drops rows or mangles metadata, no
    other test in this file would catch it because they all stop at the
    extraction layer.
    """
    from mempalace.backends.chroma import ChromaBackend

    source = tmp_path / "source"
    dest = tmp_path / "dest"

    rows = [
        (f"drawer_{i:03d}", f"body {i}", {"wing": "alpha" if i % 2 else "beta", "room": "r0"})
        for i in range(40)
    ]
    _seed_palace(source, "mempalace_drawers", rows)
    _seed_palace(
        source,
        "mempalace_closets",
        [("closet_x", "abbrev pointer →drawer_001", {"wing": "alpha"})],
    )

    counts = repair.rebuild_from_sqlite(str(source), str(dest))
    assert counts == {"mempalace_drawers": 40, "mempalace_closets": 1}

    backend = ChromaBackend()
    drawers = backend.get_collection(str(dest), "mempalace_drawers")
    assert drawers.count() == 40
    alpha = drawers.get(where={"wing": "alpha"})
    assert len(alpha["ids"]) == 20

    # Spot-check that document text round-trips for one specific drawer
    # — protects against a regression where extraction or upsert order
    # silently swaps document bodies between IDs.
    one = drawers.get(ids=["drawer_007"], include=["documents", "metadatas"])
    assert one["documents"] == ["body 7"]
    assert one["metadatas"][0]["wing"] == "alpha"

    # Closets: the AAAK index layer. Re-embedded with the same EF so a
    # known closet ID and its document body must come back intact.
    closets = backend.get_collection(str(dest), "mempalace_closets")
    assert closets.count() == 1
    closet_row = closets.get(ids=["closet_x"], include=["documents", "metadatas"])
    assert closet_row["documents"] == ["abbrev pointer →drawer_001"]
    assert closet_row["metadatas"][0] == {"wing": "alpha"}


def test_rebuild_from_sqlite_uses_configured_drawer_collection(tmp_path):
    from mempalace.backends.chroma import ChromaBackend

    source = tmp_path / "source"
    dest = tmp_path / "dest"

    _seed_palace(source, "custom_drawers", [("custom_1", "body", {"wing": "alpha"})])
    _seed_palace(source, "mempalace_closets", [("closet_x", "ptr", {"wing": "alpha"})])

    counts = repair.rebuild_from_sqlite(
        str(source),
        str(dest),
        collection_name="custom_drawers",
    )

    assert counts == {"custom_drawers": 1, "mempalace_closets": 1}
    backend = ChromaBackend()
    custom = backend.get_collection(str(dest), "custom_drawers")
    assert custom.get(ids=["custom_1"], include=["documents"])["documents"] == ["body"]
    with pytest.raises(Exception):
        backend.get_collection(str(dest), "mempalace_drawers")


def test_rebuild_from_sqlite_preserves_extra_first_party_collections(tmp_path):
    from mempalace.backends.chroma import ChromaBackend

    source = tmp_path / "source"
    dest = tmp_path / "dest"

    _seed_palace(source, "mempalace_drawers", [("d1", "drawer", {"wing": "w"})])
    _seed_palace(source, "mempalace_compressed", [("c1", "compressed", {"wing": "w"})])

    counts = repair.rebuild_from_sqlite(str(source), str(dest))

    assert counts["mempalace_drawers"] == 1
    assert counts["mempalace_compressed"] == 1
    compressed = ChromaBackend().get_collection(str(dest), "mempalace_compressed")
    row = compressed.get(ids=["c1"], include=["documents", "metadatas"])
    assert row["documents"] == ["compressed"]
    assert row["metadatas"] == [{"wing": "w"}]


def test_unique_temp_collection_names_include_internal_marker():
    first = repair._unique_temp_collection_name("mempalace_drawers")
    second = repair._unique_temp_collection_name("mempalace_drawers")

    assert first != second
    assert first.startswith("mempalace_drawers__repair_tmp__")
    assert repair._is_repair_internal_collection(first)
    assert not repair._is_repair_internal_collection("mempalace_drawers__repair_tmp")


def test_rebuild_from_sqlite_fails_on_missing_chroma_document(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _seed_palace(source, "mempalace_drawers", [("d1", "body", {"wing": "w"})])

    conn = sqlite3.connect(source / "chroma.sqlite3")
    try:
        row = conn.execute(
            """
            SELECT e.id
            FROM embeddings e
            JOIN embedding_metadata em ON em.id = e.id
            WHERE em.key = 'chroma:document'
            LIMIT 1
            """
        ).fetchone()
        conn.execute(
            "DELETE FROM embedding_metadata WHERE id = ? AND key = 'chroma:document'",
            (row[0],),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="missing chroma:document"):
        repair.rebuild_from_sqlite(str(source), str(dest))
    assert not dest.exists()


def test_rebuild_from_sqlite_content_audit_is_opt_in(tmp_path, monkeypatch):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _seed_palace(source, "mempalace_drawers", [("d1", "body", {"wing": "w"})])

    audit = MagicMock()
    monkeypatch.setattr(repair, "_audit_collection_content", audit)

    repair.rebuild_from_sqlite(str(source), str(dest))
    audit.assert_not_called()

    source2 = tmp_path / "source2"
    dest2 = tmp_path / "dest2"
    _seed_palace(source2, "mempalace_drawers", [("d1", "body", {"wing": "w"})])
    repair.rebuild_from_sqlite(str(source2), str(dest2), audit_content=True)
    audit.assert_called_once_with(str(source2.resolve()), str(dest2.resolve()), "mempalace_drawers")


def test_validate_destination_inventory_rejects_extra_rows():
    expected = repair.CollectionInventory("mempalace_drawers", 1, frozenset({"d1"}), frozenset())
    actual = repair.CollectionInventory(
        "mempalace_drawers", 2, frozenset({"d1", "d2"}), frozenset()
    )

    with pytest.raises(RuntimeError, match="count mismatch"):
        repair._validate_destination_inventory(expected, actual)


def test_validate_destination_inventory_rejects_same_count_id_mismatch():
    expected = repair.CollectionInventory("mempalace_drawers", 1, frozenset({"d1"}), frozenset())
    actual = repair.CollectionInventory("mempalace_drawers", 1, frozenset({"d2"}), frozenset())

    with pytest.raises(RuntimeError, match="ID set mismatch"):
        repair._validate_destination_inventory(expected, actual)


def test_validate_destination_inventory_rejects_missing_destination_documents():
    expected = repair.CollectionInventory("mempalace_drawers", 1, frozenset({"d1"}), frozenset())
    actual = repair.CollectionInventory(
        "mempalace_drawers", 1, frozenset({"d1"}), frozenset({"d1"})
    )

    with pytest.raises(RuntimeError, match="missing chroma:document"):
        repair._validate_destination_inventory(expected, actual)


def test_sqlite_inventories_skips_generated_internal_temp_collections(monkeypatch):
    seen = []

    def fake_inventory(palace_path, collection_name):
        seen.append((palace_path, collection_name))
        return repair.CollectionInventory(collection_name, 0, frozenset(), frozenset())

    monkeypatch.setattr(repair, "_sqlite_collection_inventory", fake_inventory)

    result = repair._sqlite_inventories(
        "/palace",
        (
            "mempalace_drawers",
            "mempalace_drawers__repair_tmp",
            "mempalace_drawers__repair_tmp__20260502121212121212__999__feedface",
        ),
    )

    assert list(result) == ["mempalace_drawers", "mempalace_drawers__repair_tmp"]
    assert seen == [
        ("/palace", "mempalace_drawers"),
        ("/palace", "mempalace_drawers__repair_tmp"),
    ]


def test_audit_collection_content_detects_changed_document(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _seed_palace(source, "mempalace_drawers", [("d1", "original", {"wing": "w"})])
    _seed_palace(dest, "mempalace_drawers", [("d1", "changed", {"wing": "w"})])

    with pytest.raises(RuntimeError, match="content audit failed"):
        repair._audit_collection_content(str(source), str(dest), "mempalace_drawers")


def test_audit_collection_content_detects_missing_document(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _seed_palace(source, "mempalace_drawers", [("d1", "original", {"wing": "w"})])
    _seed_palace(dest, "mempalace_drawers", [("d2", "extra", {"wing": "w"})])

    with pytest.raises(RuntimeError, match="missing docs: d1"):
        repair._audit_collection_content(str(source), str(dest), "mempalace_drawers")


def test_audit_collection_content_detects_extra_document(tmp_path):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _seed_palace(source, "mempalace_drawers", [("d1", "original", {"wing": "w"})])
    _seed_palace(
        dest,
        "mempalace_drawers",
        [("d1", "original", {"wing": "w"}), ("d2", "extra", {"wing": "w"})],
    )

    with pytest.raises(RuntimeError, match="extra docs: d2"):
        repair._audit_collection_content(str(source), str(dest), "mempalace_drawers")


def test_rebuild_from_sqlite_restores_archive_on_post_rebuild_validation_failure(
    tmp_path, monkeypatch
):
    palace = tmp_path / "palace"
    _seed_palace(palace, "mempalace_drawers", [("d1", "body", {"wing": "w"})])

    calls = {"count": 0}
    real_validate = repair._validate_destination_inventory

    def fail_once(expected, actual):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated validation failure")
        return real_validate(expected, actual)

    monkeypatch.setattr(repair, "_validate_destination_inventory", fail_once)

    with pytest.raises(RuntimeError, match="simulated validation failure"):
        repair.rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)

    assert (palace / "chroma.sqlite3").exists()
    assert len(list(repair.extract_via_sqlite(str(palace), "mempalace_drawers"))) == 1
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("palace.pre-rebuild-")]


def test_rebuild_from_sqlite_restores_archive_on_dest_setup_failure(tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    _seed_palace(palace, "mempalace_drawers", [("d1", "body", {"wing": "w"})])
    real_makedirs = os.makedirs

    def fail_makedirs(path, *args, **kwargs):
        if path == os.path.realpath(str(palace)):
            raise OSError("simulated setup failure")
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(repair.os, "makedirs", fail_makedirs)

    with pytest.raises(OSError, match="simulated setup failure"):
        repair.rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)

    assert (palace / "chroma.sqlite3").exists()
    assert len(list(repair.extract_via_sqlite(str(palace), "mempalace_drawers"))) == 1
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("palace.pre-rebuild-")]


def test_rebuild_from_sqlite_restores_archive_on_backend_setup_failure(tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    _seed_palace(palace, "mempalace_drawers", [("d1", "body", {"wing": "w"})])

    def fail_backend():
        raise RuntimeError("simulated backend setup failure")

    monkeypatch.setattr(repair, "ChromaBackend", fail_backend)

    with pytest.raises(RuntimeError, match="simulated backend setup failure"):
        repair.rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)

    assert (palace / "chroma.sqlite3").exists()
    assert len(list(repair.extract_via_sqlite(str(palace), "mempalace_drawers"))) == 1
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("palace.pre-rebuild-")]


def test_rebuild_from_sqlite_refuses_when_write_lock_active(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _seed_palace(source, "mempalace_drawers", [("d1", "body", {"wing": "w"})])

    def locked(*args, **kwargs):
        raise repair.PalaceWriteAlreadyRunning("locked by test")

    monkeypatch.setattr(repair, "palace_write_lock", locked)

    counts = repair.rebuild_from_sqlite(str(source), str(dest))

    out = capsys.readouterr().out
    assert counts == {}
    assert "ABORT: locked by test" in out
    assert not dest.exists()


def test_rebuild_from_sqlite_releases_lock_on_existing_dest_refusal(tmp_path, monkeypatch):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    dest.mkdir()
    _seed_palace(source, "mempalace_drawers", [("d1", "body", {"wing": "w"})])
    dest_lock = MagicMock()
    source_lock = MagicMock()
    lock_factory = MagicMock(side_effect=[dest_lock, source_lock])
    monkeypatch.setattr(repair, "palace_write_lock", lock_factory)

    counts = repair.rebuild_from_sqlite(str(source), str(dest))

    assert counts == {}
    assert lock_factory.call_args_list == [
        call(os.path.realpath(str(dest)), operation="repair"),
        call(os.path.realpath(str(source)), operation="repair source"),
    ]
    dest_lock.__enter__.assert_called_once_with()
    source_lock.__enter__.assert_called_once_with()
    source_lock.__exit__.assert_called_once_with(None, None, None)
    dest_lock.__exit__.assert_called_once_with(None, None, None)


def test_rebuild_from_sqlite_releases_dest_lock_when_source_lock_active(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _seed_palace(source, "mempalace_drawers", [("d1", "body", {"wing": "w"})])
    dest_lock = MagicMock()

    def lock_factory(path, *, operation):
        if operation == "repair source":
            raise repair.PalaceWriteAlreadyRunning("source busy")
        return dest_lock

    monkeypatch.setattr(repair, "palace_write_lock", lock_factory)

    counts = repair.rebuild_from_sqlite(str(source), str(dest))

    out = capsys.readouterr().out
    assert counts == {}
    assert "ABORT: source busy" in out
    dest_lock.__enter__.assert_called_once_with()
    dest_lock.__exit__.assert_called_once_with(None, None, None)


def test_recoverable_collection_names_orders_primary_and_ignores_empty(tmp_path):
    from mempalace.backends.chroma import ChromaBackend

    palace = tmp_path / "source"
    _seed_palace(palace, "custom_drawers", [("d1", "drawer", {"wing": "w"})])
    _seed_palace(palace, "mempalace_closets", [("c1", "closet", {"wing": "w"})])
    _seed_palace(palace, "mempalace_compressed", [("z1", "compressed", {"wing": "w"})])
    _seed_palace(palace, "custom_drawers__repair_tmp", [("tmp1", "tmp", {"wing": "w"})])
    ChromaBackend().create_collection(str(palace), "empty_collection")

    assert repair._recoverable_collection_names(str(palace), "custom_drawers") == (
        "custom_drawers",
        "mempalace_closets",
        "custom_drawers__repair_tmp",
        "mempalace_compressed",
    )


def test_rebuild_from_sqlite_refuses_zero_primary_collection_before_archive(tmp_path):
    palace = tmp_path / "palace"
    _seed_palace(palace, "mempalace_drawers", [("d1", "doc", {"wing": "w"})])
    sqlite_before = (palace / "chroma.sqlite3").stat().st_size

    counts = repair.rebuild_from_sqlite(
        str(palace),
        str(palace),
        archive_existing_dest=True,
        collection_name="custom_drawers",
    )

    assert counts == {}
    assert palace.exists()
    assert (palace / "chroma.sqlite3").stat().st_size == sqlite_before
    archives = [p for p in tmp_path.iterdir() if "pre-rebuild" in p.name]
    assert archives == []


def test_rebuild_from_sqlite_refuses_existing_dest(tmp_path):
    """Refuse to write into a directory that already exists when source
    and dest differ. Without this, an unattended re-run would silently
    interleave a partial rebuild with whatever's already at dest.
    """
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    _seed_palace(source, "mempalace_drawers", [("d1", "doc", {"wing": "w"})])
    dest.mkdir()
    # Drop a marker file so we can prove the dir wasn't touched.
    (dest / "marker.txt").write_text("preexisting")

    counts = repair.rebuild_from_sqlite(str(source), str(dest))
    assert counts == {}
    assert (dest / "marker.txt").read_text() == "preexisting"
    assert not (dest / "chroma.sqlite3").exists()


def test_rebuild_from_sqlite_in_place_archives_when_opted_in(tmp_path):
    """In-place rebuild (source == dest) with ``archive_existing_dest=True``
    must move the original aside to ``<dest>.pre-rebuild-<ts>`` and read
    from the archive — the original drawer rows must survive in the new
    palace, AND the archive itself must still contain the original rows.

    Catches: a refactor that moves the original out but then reads from
    the now-empty original location, producing an empty rebuild; also
    catches a swap that empties the archive after reading.
    """
    palace = tmp_path / "palace"
    rows = [(f"d{i}", f"body {i}", {"wing": "w", "room": "r"}) for i in range(15)]
    _seed_palace(palace, "mempalace_drawers", rows)

    counts = repair.rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)
    assert counts["mempalace_drawers"] == 15

    archives = [p for p in tmp_path.iterdir() if p.name.startswith("palace.pre-rebuild-")]
    assert len(archives) == 1
    assert (archives[0] / "chroma.sqlite3").exists()
    # Archive must still hold the same row count via the SQLite bypass —
    # proves the archive wasn't silently truncated as a side effect.
    archived_rows = list(repair.extract_via_sqlite(str(archives[0]), "mempalace_drawers"))
    assert len(archived_rows) == 15

    from mempalace.backends.chroma import ChromaBackend

    rebuilt = ChromaBackend().get_collection(str(palace), "mempalace_drawers")
    assert rebuilt.count() == 15


def test_rebuild_from_sqlite_treats_symlinked_dest_as_in_place(tmp_path):
    """A symlink alias to the same palace must take the in-place archive path.

    Without canonicalization, repair sees source != dest, then refuses because
    the destination path already exists. That blocks the safe archive/rebuild
    path for users invoking repair through a symlinked palace location.
    """
    palace = tmp_path / "palace"
    palace_link = tmp_path / "palace-link"
    rows = [(f"d{i}", f"body {i}", {"wing": "w", "room": "r"}) for i in range(3)]
    _seed_palace(palace, "mempalace_drawers", rows)
    try:
        palace_link.symlink_to(palace, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    counts = repair.rebuild_from_sqlite(
        str(palace),
        str(palace_link),
        archive_existing_dest=True,
    )

    assert counts["mempalace_drawers"] == 3
    archives = [p for p in tmp_path.iterdir() if p.name.startswith("palace.pre-rebuild-")]
    assert len(archives) == 1
    assert len(list(repair.extract_via_sqlite(str(palace), "mempalace_drawers"))) == 3
    assert len(list(repair.extract_via_sqlite(str(archives[0]), "mempalace_drawers"))) == 3


def test_unique_archive_path_skips_existing_collision(tmp_path, monkeypatch):
    class FixedDatetime:
        @staticmethod
        def now():
            return datetime.strptime("20260502-155501-123456", "%Y%m%d-%H%M%S-%f")

    palace = tmp_path / "palace"
    first = tmp_path / "palace.pre-rebuild-20260502-155501-123456"
    first.mkdir()
    monkeypatch.setattr(repair, "datetime", FixedDatetime)

    assert repair._unique_archive_path(str(palace)) == str(first) + "-2"


def test_rebuild_from_sqlite_in_place_skips_archive_collision(tmp_path, monkeypatch):
    class FixedDatetime:
        @staticmethod
        def now():
            return datetime.strptime("20260502-155501-123456", "%Y%m%d-%H%M%S-%f")

    palace = tmp_path / "palace"
    first_archive = tmp_path / "palace.pre-rebuild-20260502-155501-123456"
    first_archive.mkdir()
    (first_archive / "marker.txt").write_text("preexisting archive")
    rows = [(f"d{i}", f"body {i}", {"wing": "w", "room": "r"}) for i in range(3)]
    _seed_palace(palace, "mempalace_drawers", rows)
    monkeypatch.setattr(repair, "datetime", FixedDatetime)

    counts = repair.rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)

    second_archive = tmp_path / "palace.pre-rebuild-20260502-155501-123456-2"
    assert counts["mempalace_drawers"] == 3
    assert (first_archive / "marker.txt").read_text() == "preexisting archive"
    assert not (first_archive / "palace").exists()
    assert (second_archive / "chroma.sqlite3").exists()
    assert len(list(repair.extract_via_sqlite(str(second_archive), "mempalace_drawers"))) == 3


def test_rebuild_from_sqlite_in_place_refuses_without_archive_flag(tmp_path):
    """Source == dest without archive flag must abort untouched. The
    most catastrophic possible regression of this code path is silently
    deleting the only copy of the user's data."""
    palace = tmp_path / "palace"
    _seed_palace(palace, "mempalace_drawers", [("d1", "doc", {"wing": "w"})])
    sqlite_before = (palace / "chroma.sqlite3").stat().st_size

    counts = repair.rebuild_from_sqlite(str(palace), str(palace))
    assert counts == {}
    # Same file, untouched.
    assert (palace / "chroma.sqlite3").stat().st_size == sqlite_before
    archives = [p for p in tmp_path.iterdir() if "pre-rebuild" in p.name]
    assert archives == []


def test_rebuild_from_sqlite_source_missing_chroma_db(tmp_path):
    """Source dir exists but has no chroma.sqlite3 → returns empty,
    leaves dest untouched."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "stray_file").write_text("not a palace")
    dest = tmp_path / "dest"

    counts = repair.rebuild_from_sqlite(str(source), str(dest))
    assert counts == {}
    assert not dest.exists()


def test_rebuild_from_sqlite_in_place_validates_source_before_archiving(tmp_path):
    """In-place + archive_existing_dest=True with a dir that lacks
    chroma.sqlite3 must NOT rename the dir before bailing. An earlier
    revision archived first and validated second, leaving the user with
    a renamed empty dir to manually undo. Catches that ordering bug.
    """
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "marker.txt").write_text("not a real palace")

    counts = repair.rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)
    assert counts == {}
    # No archive created — original dir still in place with its marker.
    assert palace.exists()
    assert (palace / "marker.txt").read_text() == "not a real palace"
    archives = [p for p in tmp_path.iterdir() if "pre-rebuild" in p.name]
    assert archives == []


def test_rebuild_from_sqlite_raises_on_upsert_failure(tmp_path, monkeypatch):
    """Mid-batch in-place upsert failure must restore the archived palace
    and raise ``RebuildPartialError``. Without this, an unattended script
    gets exit-code-zero on a partial rebuild and the user discovers the
    data loss only when search starts returning fewer hits.
    """
    palace = tmp_path / "palace"
    rows = [(f"d{i}", f"body {i}", {"wing": "w", "room": "r"}) for i in range(5)]
    _seed_palace(palace, "mempalace_drawers", rows)

    # Make the very first upsert raise so we don't depend on batch
    # boundary behavior. Patching ChromaCollection.upsert (the wrapper
    # mempalace's backend returns) keeps the failure path realistic.
    # ``monkeypatch`` is pytest's built-in fixture that auto-restores
    # the original attribute when the test exits, so we don't need to
    # undo this manually.
    from mempalace.backends.chroma import ChromaCollection

    def boom(self, **kwargs):
        raise RuntimeError("simulated chromadb upsert failure")

    monkeypatch.setattr(ChromaCollection, "upsert", boom)

    with pytest.raises(repair.RebuildPartialError) as excinfo:
        repair.rebuild_from_sqlite(str(palace), str(palace), archive_existing_dest=True)

    err = excinfo.value
    assert err.failed_collection == "mempalace_drawers"
    assert err.partial_counts.get("mempalace_drawers") == 0
    assert err.archive_path is None
    assert os.path.isfile(palace / "chroma.sqlite3")
    restored_rows = list(repair.extract_via_sqlite(str(palace), "mempalace_drawers"))
    assert len(restored_rows) == 5
    assert err.dest_palace == os.path.abspath(str(palace))
    assert "Original palace restored" in err.message
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("palace.pre-rebuild-")]


def test_rebuild_from_sqlite_honors_configured_drawer_collection_name(tmp_path, monkeypatch):
    """A user with a non-default drawers collection name (set via
    ``MempalaceConfig().collection_name``) must have THAT collection
    rebuilt — not the hardcoded ``mempalace_drawers``.

    Catches: a regression where the recovery path silently rebuilds the
    default-name collection on a custom-named palace, leaving the user's
    actual data unrebuilt while reporting "rebuild complete." This is
    the failure mode reviewer mjc flagged on PR #1310 as needing to line
    up with the configured-collection-name work in #1312. Closets stay
    fixed (``mempalace_closets``) by design — the AAAK index references
    drawer IDs by string and is not per-deployment configurable.

    Strategy: monkeypatch the lazy resolver so the test is hermetic and
    does not depend on the global config file or env state.
    """
    from mempalace.backends.chroma import ChromaBackend

    custom_drawers = "custom_drawers_xyz"
    monkeypatch.setattr(repair, "_drawers_collection_name", lambda: custom_drawers)

    source = tmp_path / "source"
    dest = tmp_path / "dest"

    drawer_rows = [(f"d{i}", f"body {i}", {"wing": "alpha"}) for i in range(3)]
    closet_rows = [("closet_a", "abbrev →d0", {"wing": "alpha"})]
    _seed_palace(source, custom_drawers, drawer_rows)
    _seed_palace(source, "mempalace_closets", closet_rows)

    counts = repair.rebuild_from_sqlite(str(source), str(dest))

    # Rebuilt under the custom name, not under the default "mempalace_drawers".
    assert counts == {custom_drawers: 3, "mempalace_closets": 1}

    backend = ChromaBackend()
    rebuilt_drawers = backend.get_collection(str(dest), custom_drawers)
    assert rebuilt_drawers.count() == 3

    # Default-name collection must NOT exist in dest — proves we did not
    # silently fall back to the hardcoded name during rebuild.
    try:
        rebuilt_default = backend.get_collection(str(dest), "mempalace_drawers")
        # If get_collection returns without raising, count() should be 0
        # (chromadb may auto-create on get with some EFs); a non-zero
        # count would mean we wrote rows to the wrong collection.
        assert rebuilt_default.count() == 0, (
            "rebuild leaked rows into the default-name collection on a "
            "custom-name palace — recovery wrote to the wrong collection."
        )
    except Exception:
        pass  # Expected: collection wasn't created.
