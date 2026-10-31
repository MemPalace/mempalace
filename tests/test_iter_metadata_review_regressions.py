"""Regression tests for the #2745 review: sqlite_exact iter_metadata and the
mined-file fast paths. Adapted from Astra's review probes."""

import os
from contextlib import contextmanager

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


@pytest.mark.parametrize("value", ["raw\0value", "raw\udcffvalue", "\x01mpesc:literal"])
@pytest.mark.parametrize("route", ["all", "projection", "equality", "source_only", "membership"])
def test_metadata_iterator_decodes_stored_text_and_encodes_scoped_values(col, value, route):
    metadata = {"source_file": value, "wing": value}
    _add(col, [metadata])
    if route == "all":
        rows = col.iter_metadata()
        expected = metadata
    elif route == "projection":
        rows = col.iter_metadata(("source_file",))
        expected = {"source_file": value}
    elif route == "equality":
        rows = col.iter_metadata(equals={"wing": value})
        expected = metadata
    elif route == "source_only":
        rows = col.iter_metadata(("source_file",), require_key="source_file")
        expected = {"source_file": value}
    else:
        rows = col.iter_metadata(("source_file",), equals={"wing": [value]})
        expected = {"source_file": value}
    assert list(rows) == [expected]


@pytest.mark.parametrize("route", ["all", "source_only", "membership"])
def test_metadata_iterator_does_not_materialize_the_full_collection(col, monkeypatch, route):
    _add(col, [{"source_file": "/a", "wing": "w"}, {"source_file": "/b", "wing": "w"}])
    original = col._cursor

    class StreamingCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def execute(self, *args):
            self.cursor.execute(*args)
            return self

        def fetchall(self):
            raise AssertionError("iter_metadata must stream rows instead of calling fetchall")

        def __iter__(self):
            return iter(self.cursor)

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    @contextmanager
    def cursor():
        with original() as raw:
            yield StreamingCursor(raw)

    monkeypatch.setattr(col, "_cursor", cursor)
    kwargs = {}
    if route == "source_only":
        kwargs = {"keys": ("source_file",), "require_key": "source_file"}
    elif route == "membership":
        kwargs = {"equals": {"wing": ["w"]}}
    assert len(list(col.iter_metadata(**kwargs))) == 2


def test_failed_prefetch_closes_its_metadata_stream(col, monkeypatch):
    closed = []

    def broken(*args, **kwargs):
        try:
            yield {"source_file": []}  # An unusable row makes the consumer fall back.
        finally:
            closed.append(True)

    monkeypatch.setattr(col, "iter_metadata", broken)
    assert prefetch_complete_mtimes(col, ["/a"]) is None
    assert closed == [True]
