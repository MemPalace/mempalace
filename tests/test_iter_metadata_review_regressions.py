"""Regression tests for the #2745 review: sqlite_exact iter_metadata and the
mined-file fast paths. Adapted from Astra's review probes."""

import os

import pytest

from mempalace.backends.base import PalaceRef
from mempalace.backends.embedding_wrapper import EmbeddingCollection
from mempalace.backends.sqlite_exact import SQLiteExactBackend
from mempalace.palace import (
    NORMALIZE_VERSION,
    file_already_mined,
    prefetch_complete_mtimes,
    prefetch_mined_set,
)


@pytest.fixture
def col(tmp_path):
    backend = SQLiteExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    yield backend.get_collection(palace=palace, collection_name="mempalace_drawers", create=True)
    backend.close()


def _add(col, rows):
    col.add(
        ids=[str(i) for i in range(len(rows))],
        documents=["d"] * len(rows),
        metadatas=rows,
        embeddings=[[1.0, 0.0]] * len(rows),
    )


def _source(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("content")
    os.utime(source, (100, 100))
    return str(source)


@pytest.mark.parametrize("wrapped", [False, True])
def test_null_normalize_version_is_never_proven_by_the_fast_path(col, tmp_path, wrapped):
    src = _source(tmp_path)
    _add(
        col,
        [{"source_file": src, "normalize_version": None, "source_mtime": 100.0, "chunk_total": 1}],
    )
    target = EmbeddingCollection(col) if wrapped else col
    assert file_already_mined(target, src, check_mtime=True) is False
    fast = prefetch_complete_mtimes(target, [src])
    assert fast is None or src not in fast


@pytest.mark.parametrize("wrapped", [False, True])
def test_fast_path_defers_null_version_source_to_the_per_file_check(col, tmp_path, wrapped):
    # A null-version drawer next to a complete current group: the fast path must
    # not decide; the per-file check (null = stale drawer) decides.
    src = _source(tmp_path)
    _add(
        col,
        [
            {
                "source_file": src,
                "normalize_version": None,
                "source_mtime": 100.0,
                "chunk_total": 1,
            },
            {
                "source_file": src,
                "normalize_version": NORMALIZE_VERSION,
                "source_mtime": 100.0,
                "chunk_total": 1,
            },
        ],
    )
    target = EmbeddingCollection(col) if wrapped else col
    fast = prefetch_complete_mtimes(target, [src])
    assert fast is None or src not in fast
    assert file_already_mined(target, src, check_mtime=True) is True


def test_prefetch_mined_set_treats_null_version_as_stale_instead_of_aborting(col, tmp_path):
    src = _source(tmp_path)
    _add(col, [{"source_file": src, "normalize_version": None, "source_mtime": 100.0}])
    assert prefetch_mined_set(col) == {}
    assert prefetch_mined_set(col, source_files=[src]) == {}


def test_iter_metadata_keeps_explicit_null_values(col):
    _add(
        col,
        [
            {"source_file": "/a.md", "normalize_version": None},
            {"source_file": None, "normalize_version": None},
        ],
    )
    assert list(col.iter_metadata(("source_file", "normalize_version"))) == [
        {"source_file": "/a.md", "normalize_version": None}
    ]


def test_source_only_projection_applies_unpushable_equality(col):
    _add(
        col,
        [{"source_file": "/a.md", "tag.name": "yes"}, {"source_file": "/b.md", "tag.name": "no"}],
    )
    assert list(
        col.iter_metadata(("source_file",), require_key="source_file", equals={"tag.name": "yes"})
    ) == [{"source_file": "/a.md"}]
    assert list(col.iter_metadata(("source_file",), equals={"tag.name": "yes"})) == [
        {"source_file": "/a.md"}
    ]


@pytest.mark.parametrize("require_key", [None, "source_file"])
def test_empty_membership_list_matches_nothing(col, require_key):
    _add(col, [{"source_file": "/a.md", "wing": "yes"}])
    assert (
        list(col.iter_metadata(("source_file",), require_key=require_key, equals={"wing": []}))
        == []
    )


def test_membership_values_repeated_across_chunks_yield_one_row(col):
    _add(col, [{"source_file": "/a.md", "wing": "yes"}])
    assert list(col.iter_metadata(("source_file",), equals={"wing": ["yes"] * 901})) == [
        {"source_file": "/a.md"}
    ]


def test_source_only_projection_keeps_non_string_values_without_require_key(col):
    _add(col, [{"source_file": 42}, {"source_file": False}])
    assert list(col.iter_metadata(("source_file",))) == [
        {"source_file": 42},
        {"source_file": False},
    ]
