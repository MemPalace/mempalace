"""Tests for mempalace.repair — scan, prune, and rebuild HNSW index."""

import os
import sqlite3
from unittest.mock import MagicMock, patch

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
    # Create a fake sqlite file
    sqlite_path = tmp_path / "chroma.sqlite3"
    sqlite_path.write_text("fake")

    mock_col = MagicMock()
    mock_col.count.return_value = 2
    mock_col.get.return_value = {
        "ids": ["id1", "id2"],
        "documents": ["doc1", "doc2"],
        "metadatas": [{"wing": "a"}, {"wing": "b"}],
    }

    mock_new_col = MagicMock()
    mock_backend = _install_mock_backend(mock_backend_cls, mock_col)
    mock_backend.create_collection.return_value = mock_new_col

    repair.rebuild_index(palace_path=str(tmp_path))

    # Verify: backed up sqlite only (not copytree)
    mock_shutil.copy2.assert_called_once()
    assert "chroma.sqlite3" in str(mock_shutil.copy2.call_args)

    # Verify: deleted and recreated (cosine is the backend default)
    mock_backend.delete_collection.assert_called_once_with(str(tmp_path), "mempalace_drawers")
    mock_backend.create_collection.assert_called_once_with(str(tmp_path), "mempalace_drawers")

    # Verify: used upsert not add
    mock_new_col.upsert.assert_called_once()
    mock_new_col.add.assert_not_called()


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
        },
        {"ids": [], "documents": [], "metadatas": []},
    ]
    mock_backend.get_collection.return_value = mock_col
    mock_backend_cls.return_value = mock_backend

    with patch("mempalace.repair.sqlite_drawer_count", return_value=67_580):
        repair.rebuild_index(palace_path=str(tmp_path))

    # Guard fired: nothing destructive happened
    mock_backend.delete_collection.assert_not_called()
    mock_backend.create_collection.assert_not_called()
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
        },
        {"ids": [], "documents": [], "metadatas": []},
    ]
    mock_new_col = MagicMock()
    mock_backend.get_collection.return_value = mock_col
    mock_backend.create_collection.return_value = mock_new_col
    mock_backend_cls.return_value = mock_backend

    with patch("mempalace.repair.sqlite_drawer_count", return_value=67_580):
        repair.rebuild_index(palace_path=str(tmp_path), confirm_truncation_ok=True)

    mock_backend.delete_collection.assert_called_once()
    mock_backend.create_collection.assert_called_once()
    mock_new_col.upsert.assert_called()


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

    conn = sqlite3.connect(db_path)
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
    conn.close()
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
