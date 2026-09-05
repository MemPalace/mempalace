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
