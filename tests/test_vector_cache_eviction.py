"""Regression tests for per-collection vector-cache eviction (#2907).

``sqlite_exact`` keeps each collection's embedding matrix warm in
``_vector_cache`` (keyed by collection id). A write through a collection's
cursor used to call ``clear()`` on the whole cache, so a one-row upsert into
``mempalace_closets`` also threw away the ``mempalace_drawers`` matrix and the
next search re-read and re-decoded every embedding in the palace.

A write may only invalidate the collection it actually wrote to.
"""

from mempalace.backends import PalaceRef, get_backend

META = {"wing": "w", "room": "r", "source_file": "f.md"}
VECTOR = [[0.1, 0.2, 0.3, 0.4]]


def _collection(backend, ref, name):
    return backend.get_collection(palace=ref, collection_name=name, create=True)


def _warm(collection, drawer_id):
    collection.upsert(
        ids=[drawer_id],
        documents=[f"doc {drawer_id}"],
        metadatas=[META],
        embeddings=VECTOR,
    )
    collection.query(query_embeddings=VECTOR, n_results=1, include=["distances"])


def test_write_evicts_only_the_collection_it_touched(tmp_path):
    path = str(tmp_path / "palace")
    ref = PalaceRef(id=path, local_path=path)
    backend = get_backend("sqlite_exact")

    drawers = _collection(backend, ref, "mempalace_drawers")
    closets = _collection(backend, ref, "mempalace_closets")
    _warm(drawers, "d0")
    _warm(closets, "c0")

    handle = drawers._handle
    assert len(handle._vector_cache) == 2, "both collections should be cached"

    closets.upsert(
        ids=["c1"],
        documents=["another closet"],
        metadatas=[META],
        embeddings=VECTOR,
    )

    assert len(handle._vector_cache) == 1, "the closet write must not evict the drawers matrix"
    # The surviving entry still answers without a reload.
    assert drawers.query(query_embeddings=VECTOR, n_results=1, include=["distances"]).ids == [
        ["d0"]
    ]
    assert len(handle._vector_cache) == 1


def test_write_evicts_its_own_collection(tmp_path):
    path = str(tmp_path / "palace")
    ref = PalaceRef(id=path, local_path=path)
    backend = get_backend("sqlite_exact")

    drawers = _collection(backend, ref, "mempalace_drawers")
    _warm(drawers, "d0")
    handle = drawers._handle
    assert len(handle._vector_cache) == 1

    drawers.upsert(
        ids=["d1"],
        documents=["a new drawer"],
        metadatas=[META],
        embeddings=VECTOR,
    )

    assert handle._vector_cache == {}, "the written collection's cache is dropped"

    # A fresh read sees the new row and re-warms the cache.
    result = drawers.query(query_embeddings=VECTOR, n_results=5, include=["distances"])
    assert set(result.ids[0]) == {"d0", "d1"}
    assert len(handle._vector_cache) == 1
