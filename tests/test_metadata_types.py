"""Declared drawer metadata fields are stored and compared in their types."""

from datetime import datetime, timezone

import pytest

from mempalace.metadata_types import coerce_metadata_types, is_sentinel


def test_numeric_and_flag_text_becomes_its_declared_type():
    meta = {
        "chunk_index": "3",
        "chunk_total": 4.0,
        "source_mtime": "1700000000.25",
        "is_sentinel": "true",
        "normalize_version": " 2 ",
    }
    assert coerce_metadata_types(meta) == {
        "chunk_index": 3,
        "chunk_total": 4,
        "source_mtime": 1700000000.25,
        "is_sentinel": True,
        "normalize_version": 2,
    }


def test_a_datetime_timestamp_is_stored_as_iso_text():
    when = datetime(2026, 10, 17, 9, 30, tzinfo=timezone.utc)
    assert coerce_metadata_types({"filed_at": when}) == {"filed_at": when.isoformat()}


@pytest.mark.parametrize(
    "meta",
    [
        {"chunk_index": "first"},
        {"source_mtime": "nan"},
        {"source_mtime": "inf"},
        {"is_sentinel": "maybe"},
        {"chunk_index": True},
        {"room": "42"},
        {"chunk_total": None},
    ],
)
def test_values_that_do_not_parse_or_are_not_declared_are_kept(meta):
    assert coerce_metadata_types(meta) is meta


def test_an_unchanged_record_is_not_copied():
    meta = {"chunk_index": 1, "source_mtime": 1.5, "wing": "w"}
    assert coerce_metadata_types(meta) is meta


@pytest.mark.parametrize("backend", ["chroma", "sqlite_exact"])
def test_a_write_stores_declared_types_so_range_filters_compare_numbers(tmp_path, backend):
    from mempalace.backends import get_backend
    from mempalace.backends.base import PalaceRef

    palace = tmp_path / "palace"
    palace.mkdir()
    col = get_backend(backend).get_collection(
        palace=PalaceRef(id=str(palace), local_path=str(palace)),
        collection_name="mempalace_drawers",
        create=True,
    )
    col.upsert(
        ids=["a", "b"],
        documents=["first chunk", "tenth chunk"],
        metadatas=[{"chunk_index": "9"}, {"chunk_index": "10"}],
        embeddings=[[0.1, 0.2, 0.3], [0.3, 0.2, 0.1]],
    )
    got = col.get(ids=["a", "b"], include=["metadatas"])
    assert sorted(m["chunk_index"] for m in got["metadatas"]) == [9, 10]
    # As text, "9" > "10"; as numbers only the tenth chunk passes.
    hits = col.get(where={"chunk_index": {"$gt": 9}}, include=[])
    assert hits["ids"] == ["b"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        ("true", True),
        (1, True),
        ("false", False),
        ("0", False),
        (0, False),
        (None, False),
        ("", False),
        ("yes", True),
    ],
)
def test_is_sentinel_reads_legacy_text_flags(value, expected):
    assert is_sentinel({"is_sentinel": value}) is expected
    assert is_sentinel(None) is False


def test_an_integer_beyond_64_bits_is_kept_as_given():
    big = str(2**63)
    assert coerce_metadata_types({"chunk_index": big})["chunk_index"] == big
    assert coerce_metadata_types({"chunk_index": str(2**63 - 1)})["chunk_index"] == 2**63 - 1
    huge = float(2**70)
    assert coerce_metadata_types({"chunk_index": huge})["chunk_index"] == huge
