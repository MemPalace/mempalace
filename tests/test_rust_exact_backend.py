import os

import pytest

from mempalace.backends import available_backends, get_backend
from mempalace.backends.base import PalaceRef
from mempalace.backends.rust_exact import RustExactBackend, RustExactCollection


def test_registry_exposes_rust_exact():
    assert "rust_exact" in available_backends()
    backend = get_backend("rust_exact")
    assert isinstance(backend, RustExactBackend)
    assert backend.name == "rust_exact"


def test_rust_exact_same_sqlite_database_interchangeable(tmp_path):
    """Data written by sqlite_exact must be readable by rust_exact and vice versa."""
    palace = PalaceRef(id="interchangeable", local_path=str(tmp_path))

    # 1. Write via sqlite_exact
    sqlite_backend = get_backend("sqlite_exact")
    col_sqlite = sqlite_backend.get_collection(
        palace=palace, collection_name="test_col", create=True
    )
    col_sqlite.add(
        ids=["doc1", "doc2"],
        documents=["hello world", "rust native speed"],
        metadatas=[{"wing": "wingA", "room": "room1"}, {"wing": "wingB", "room": "room2"}],
        embeddings=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
    )
    assert col_sqlite.count() == 2

    # 2. Read and query via rust_exact
    rust_backend = get_backend("rust_exact")
    col_rust = rust_backend.get_collection(palace=palace, collection_name="test_col")
    assert isinstance(col_rust, RustExactCollection)
    assert col_rust.count() == 2

    # Query doc1
    res = col_rust.query(query_embeddings=[[1.0, 0.0, 0.0, 0.0]], n_results=1)
    assert res.ids[0][0] == "doc1"
    assert abs(res.distances[0][0] - 0.0) < 1e-5

    # Query with wing filter
    res_filtered = col_rust.query(
        query_embeddings=[[1.0, 0.0, 0.0, 0.0]],
        n_results=2,
        where={"wing": "wingB"},
    )
    assert res_filtered.ids[0] == ["doc2"]

    # 3. Add via rust_exact, read via sqlite_exact
    col_rust.add(
        ids=["doc3"],
        documents=["third entry"],
        metadatas=[{"wing": "wingA", "room": "room3"}],
        embeddings=[[0.0, 0.0, 1.0, 0.0]],
    )
    assert col_sqlite.count() == 3


def test_rust_exact_complex_filter_fallback(tmp_path):
    """Filters not directly handled by native index fall back gracefully to base."""
    palace = PalaceRef(id="fallback_test", local_path=str(tmp_path))
    rust_backend = get_backend("rust_exact")
    col = rust_backend.get_collection(palace=palace, collection_name="col", create=True)

    col.add(
        ids=["a", "b", "c"],
        documents=["alpha", "beta", "gamma"],
        metadatas=[{"score": 10}, {"score": 20}, {"score": 30}],
        embeddings=[[1.0, 0.0], [0.5, 0.5], [0.0, 1.0]],
    )

    # Complex filter using $in
    res = col.query(
        query_embeddings=[[1.0, 0.0]],
        n_results=5,
        where={"score": {"$in": [10, 30]}},
    )
    assert "b" not in res.ids[0]
    assert set(res.ids[0]) == {"a", "c"}


@pytest.fixture
def native_backend(tmp_path):
    from mempalace.backends.rust_exact import _NativeVectorIndex

    if _NativeVectorIndex is None:
        if os.environ.get("MEMPALACE_REQUIRE_NATIVE") == "1":
            pytest.fail("CI requires the installed native extension")
        pytest.skip("native extension not installed")
    backend = RustExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    try:
        yield backend, palace
    finally:
        backend.close()


def test_native_mutations_invalidate_all_wrappers(native_backend):
    backend, palace = native_backend
    col = backend.get_collection(palace=palace, collection_name="test", create=True)
    sibling = backend.get_collection(palace=palace, collection_name="test")
    col.add(ids=["a"], documents=["alpha"], embeddings=[[1.0, 0.0]])
    assert col.query(query_embeddings=[[1.0, 0.0]]).ids == [["a"]]
    sibling.add(ids=["b"], documents=["beta"], embeddings=[[0.0, 1.0]])
    assert col.query(query_embeddings=[[0.0, 1.0]], n_results=1).ids == [["b"]]
    sibling.update(ids=["b"], embeddings=[[-1.0, 0.0]])
    assert col.query(query_embeddings=[[1.0, 0.0]], n_results=1).ids == [["a"]]
    sibling.upsert(ids=["b"], documents=["new beta"], embeddings=[[1.0, 0.0]])
    assert col.query(query_embeddings=[[1.0, 0.0]], n_results=2).ids == [["a", "b"]]
    sibling.delete(ids=["a"])
    result = col.query(query_embeddings=[[1.0, 0.0]])
    assert result.ids == [["b"]]
    assert result.documents == [["new beta"]]
    assert col._native_index is not None


def test_native_external_commit_refresh(native_backend):
    backend, palace = native_backend
    col = backend.get_collection(palace=palace, collection_name="test", create=True)
    col.add(ids=["a"], documents=["alpha"], embeddings=[[1.0, 0.0]])
    col.query(query_embeddings=[[1.0, 0.0]])
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    other = SQLiteExactBackend()
    try:
        writer = other.get_collection(palace=palace, collection_name="test")
        writer.add(ids=["b"], documents=["beta"], embeddings=[[0.0, 1.0]])
        assert col.query(query_embeddings=[[0.0, 1.0]], n_results=1).ids == [["b"]]
    finally:
        other.close()


def test_empty_collection_survives_close(tmp_path):
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    first = RustExactBackend()
    first.get_collection(palace=palace, collection_name="empty", create=True)
    first.close()
    second = RustExactBackend()
    try:
        assert second.get_collection(palace=palace, collection_name="empty").count() == 0
    finally:
        second.close()


def test_native_format_detection_is_sqlite_only(tmp_path, monkeypatch):
    from mempalace.backends import detect_backends_for_path
    from mempalace.palace import resolve_backend_name

    monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
    monkeypatch.setattr("mempalace.palace._config_backend_value", lambda _: None)
    backend = RustExactBackend()
    try:
        backend.get_collection(str(tmp_path), "test", create=True)
        (tmp_path / ".rust_exact").touch()
        assert detect_backends_for_path(str(tmp_path)) == ["sqlite_exact"]
        assert resolve_backend_name(str(tmp_path), explicit="rust_exact") == "rust_exact"
        assert resolve_backend_name(str(tmp_path), explicit="sqlite_exact") == "sqlite_exact"
    finally:
        backend.close()


@pytest.mark.parametrize("count", [31, 10001])
def test_native_matches_python_ranking_and_filters(native_backend, count):
    import numpy as np
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend, palace = native_backend
    col = backend.get_collection(palace=palace, collection_name="test", create=True)
    vectors = np.random.default_rng(42).normal(size=(count, 4)).tolist()
    col.add(
        ids=[str(i) for i in range(count)],
        documents=["fixture"] * count,
        metadatas=[{"wing": "a" if i % 2 else "b"} for i in range(count)],
        embeddings=vectors,
    )
    python_backend = SQLiteExactBackend()
    try:
        reference = python_backend.get_collection(palace=palace, collection_name="test")
        for where in [None, {"wing": "a"}, {"wing": "missing"}]:
            for k in [0, 1, 10]:
                options = dict(query_embeddings=[[1.0, -0.3, 0.5, 0.1]], n_results=k, where=where)
                actual = col.query(**options)
                expected = reference.query(**options)
                assert actual.ids == expected.ids
                assert actual.distances[0] == pytest.approx(expected.distances[0], abs=1e-6)
        assert col._native_index is not None
    finally:
        python_backend.close()


def test_migrated_null_dimension_uses_native_index(native_backend):
    backend, palace = native_backend
    col = backend.get_collection(palace=palace, collection_name="test", create=True)
    col.add(ids=["a"], documents=["alpha"], embeddings=[[1.0, 0.0]])
    # A pre-dimension palace receives a nullable column on writable open.
    with col._cursor(write=True) as cur:
        cur.execute("UPDATE collections SET dimension=NULL WHERE name='test'")
    result = col.query(query_embeddings=[[1.0, 0.0]])
    assert result.ids == [["a"]]
    assert col._native_index is not None
    assert col._native_index.dim() == 2


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("boundary", ["load", "before_hydrate", "after_hydrate"])
def test_native_retries_when_writer_commits_during_query(
    native_backend, monkeypatch, operation, boundary
):
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend, palace = native_backend
    col = backend.get_collection(palace=palace, collection_name="test", create=True)
    col.add(ids=["a", "b"], documents=["alpha", "beta"], embeddings=[[1.0, 0.0], [0.0, 1.0]])
    peer = SQLiteExactBackend()
    writer = peer.get_collection(palace=palace, collection_name="test")
    changed = False

    def change_once():
        nonlocal changed
        if changed:
            return
        changed = True
        if operation == "delete":
            writer.delete(ids=["a"])
        else:
            writer.update(ids=["a"], documents=["changed alpha"], embeddings=[[-1.0, 0.0]])

    original_hydrate = col._hydrate
    original_load = col._ensure_native_index

    def hydrate(*args):
        if boundary == "before_hydrate":
            change_once()
        result = original_hydrate(*args)
        if boundary == "after_hydrate":
            change_once()
        return result

    def load(*args):
        index = original_load(*args)
        if boundary == "load":
            change_once()
        return index

    monkeypatch.setattr(col, "_hydrate", hydrate)
    monkeypatch.setattr(col, "_ensure_native_index", load)
    try:
        result = col.query(query_embeddings=[[1.0, 0.0]], n_results=2)
        assert changed
        assert result.ids == ([["b"]] if operation == "delete" else [["b", "a"]])
        assert result.documents == (
            [["beta"]] if operation == "delete" else [["beta", "changed alpha"]]
        )
        assert result.distances[0] == pytest.approx([1.0] if operation == "delete" else [1.0, 2.0])
    finally:
        peer.close()


def test_native_continuous_writes_fail_explicitly(native_backend, monkeypatch):
    from mempalace.backends.base import BackendError
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend, palace = native_backend
    col = backend.get_collection(palace=palace, collection_name="test", create=True)
    col.add(ids=["a"], documents=["alpha"], embeddings=[[1.0, 0.0]])
    peer = SQLiteExactBackend()
    writer = peer.get_collection(palace=palace, collection_name="test")
    original = col._hydrate
    calls = []

    def hydrate(*args):
        calls.append(1)
        writer.update(ids=["a"], documents=[str(len(calls))])
        return original(*args)

    monkeypatch.setattr(col, "_hydrate", hydrate)
    try:
        with pytest.raises(BackendError, match="changed repeatedly"):
            col.query(query_embeddings=[[1.0, 0.0]])
        assert len(calls) == 3
    finally:
        peer.close()


def test_native_immutable_snapshot_reopens_after_mid_query_commit(native_backend, monkeypatch):
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend, palace = native_backend
    writer = backend.get_collection(palace=palace, collection_name="test", create=True)
    writer.add(ids=["a"], documents=["alpha"], embeddings=[[1.0, 0.0]])
    backend.close_palace(palace)
    col = backend.get_collection(palace=palace, collection_name="test", options={"read_only": True})
    assert col._handle.immutable
    original = col._hydrate
    changed = False

    def hydrate(*args):
        nonlocal changed
        if not changed:
            changed = True
            peer = SQLiteExactBackend()
            try:
                writer = peer.get_collection(palace=palace, collection_name="test")
                writer.update(ids=["a"], documents=["changed"], embeddings=[[-1.0, 0.0]])
            finally:
                peer.close()
        return original(*args)

    monkeypatch.setattr(col, "_hydrate", hydrate)
    result = col.query(query_embeddings=[[1.0, 0.0]])
    assert result.documents == [["changed"]]
    assert result.distances[0] == pytest.approx([2.0])


@pytest.mark.parametrize("read_only", [False, True])
def test_native_index_is_shared_across_fresh_wrappers(native_backend, monkeypatch, read_only):
    import mempalace.backends.rust_exact as rust_module

    backend, palace = native_backend
    writer = backend.get_collection(palace=palace, collection_name="test", create=True)
    writer.add(ids=["a"], documents=["alpha"], embeddings=[[1.0, 0.0]])
    original = rust_module._NativeVectorIndex
    loads = []

    class CountingIndex:
        @staticmethod
        def load_from_sqlite(*args):
            loads.append(args)
            return original.load_from_sqlite(*args)

    monkeypatch.setattr(rust_module, "_NativeVectorIndex", CountingIndex)

    def search():
        col = backend.get_collection(
            palace=palace, collection_name="test", options={"read_only": read_only}
        )
        return col.query(query_embeddings=[[1.0, 0.0]]).ids

    assert search() == [["a"]]
    cold_loads = len(loads)
    assert cold_loads > 0
    assert search() == [["a"]]
    assert len(loads) == cold_loads, "warm wrapper unexpectedly reloaded the index"
    writer.add(ids=["b"], documents=["beta"], embeddings=[[0.0, 1.0]])
    assert search() == [["a", "b"]]
    refreshed_loads = len(loads)
    assert refreshed_loads > cold_loads
    assert search() == [["a", "b"]]
    assert len(loads) == refreshed_loads, "warm wrapper unexpectedly reloaded after mutation"


@pytest.mark.parametrize("magnitude", [1e20, 1e-30, 2**-149])
@pytest.mark.parametrize("count", [4, 10004])
def test_native_finite_extremes_match_python_cosine(native_backend, magnitude, count):
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend, palace = native_backend
    col = backend.get_collection(palace=palace, collection_name="extremes", create=True)
    vectors = [[magnitude, 0.0], [0.0, magnitude], [0.0, 0.0]]
    vectors.extend([[-magnitude, 0.0]] * (count - 3))
    ids = [str(i) for i in range(count)]
    col.add(ids=ids, documents=ids, embeddings=vectors)
    python_backend = SQLiteExactBackend()
    try:
        reference = python_backend.get_collection(palace=palace, collection_name="extremes")
        for query, distances in [
            ([magnitude, 0.0], [0.0, 1.0, 1.0, 2.0]),
            ([0.0, 0.0], [1.0, 1.0, 1.0, 1.0]),
        ]:
            actual = col.query(query_embeddings=[query], n_results=4)
            expected = reference.query(query_embeddings=[query], n_results=4)
            assert actual.ids == expected.ids == [["0", "1", "2", "3"]]
            assert actual.distances[0] == pytest.approx(distances, abs=1e-6)
            assert expected.distances[0] == pytest.approx(distances, abs=1e-6)
            assert col._native_index is not None
            for method in [col._native_index.query, col._native_index.query_parallel]:
                hits = method(query, 4)
                assert [hit[0] for hit in hits] == ["0", "1", "2", "3"]
                assert [hit[1] for hit in hits] == pytest.approx(distances, abs=1e-6)
    finally:
        python_backend.close()


def test_native_load_failure_after_commit_falls_back_after_cursor_exit(native_backend, monkeypatch):
    import mempalace.backends.rust_exact as rust_module
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend, palace = native_backend
    writer = backend.get_collection(palace=palace, collection_name="test", create=True)
    writer.add(ids=["a"], documents=["alpha"], embeddings=[[1.0, 0.0]])
    backend.close_palace(palace)
    col = backend.get_collection(palace=palace, collection_name="test", options={"read_only": True})
    assert col._handle.immutable

    class FailingLoader:
        @staticmethod
        def load_from_sqlite(*args):
            peer = SQLiteExactBackend()
            try:
                changed = peer.get_collection(palace=palace, collection_name="test")
                changed.update(ids=["a"], documents=["changed"], embeddings=[[-1.0, 0.0]])
            finally:
                peer.close()
            raise RuntimeError("native loader unavailable")

    monkeypatch.setattr(rust_module, "_NativeVectorIndex", FailingLoader)
    result = col.query(query_embeddings=[[1.0, 0.0]])
    assert result.documents == [["changed"]]
    assert result.distances[0] == pytest.approx([2.0])
