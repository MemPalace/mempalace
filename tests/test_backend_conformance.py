"""Backend isolation conformance suite (RFC 001 isolation contract).

Runs the shared isolation assertions from ``_backend_conformance`` against the
built-in local backends. Server-mode backends (qdrant) run the same assertions
under their own fake/live client in ``test_qdrant_backend.py``.
"""

import pytest
from _backend_conformance import (
    assert_partition_isolation,
    assert_update_skips_missing_ids,
    assert_where_filters_conform,
    assert_write_batch_validated,
)

from mempalace.backends import PalaceRef
from mempalace.backends.chroma import ChromaBackend
from mempalace.backends.rust_exact import RustExactBackend
from mempalace.backends.sqlite_exact import SQLiteExactBackend

_LOCAL_BACKENDS = [
    pytest.param(ChromaBackend, id="chroma"),
    pytest.param(SQLiteExactBackend, id="sqlite_exact"),
    pytest.param(RustExactBackend, id="rust_exact"),
]


@pytest.mark.parametrize("backend_cls", _LOCAL_BACKENDS)
def test_cross_palace_isolation(backend_cls, tmp_path):
    """One backend instance must isolate two distinct palaces (PalaceRef.id)."""
    backend = backend_cls()
    try:
        cols = []
        for label in ("alpha", "beta"):
            path = tmp_path / label
            ref = PalaceRef(id=str(path), local_path=str(path))
            cols.append(
                backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
            )
        assert_partition_isolation(backend, cols[0], cols[1])
    finally:
        backend.close()


@pytest.mark.parametrize("backend_cls", _LOCAL_BACKENDS)
def test_update_skips_missing_ids(backend_cls, tmp_path):
    """``update`` never creates a record for an id that does not exist."""
    backend = backend_cls()
    try:
        ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
        col = backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
        assert_update_skips_missing_ids(backend, col)
    finally:
        backend.close()


@pytest.mark.parametrize("backend_cls", _LOCAL_BACKENDS)
def test_where_filters_conform(backend_cls, tmp_path):
    """Every backend answers the same ``where`` filters with the same rows."""
    backend = backend_cls()
    try:
        ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
        col = backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
        assert_where_filters_conform(backend, col)
    finally:
        backend.close()


@pytest.mark.parametrize("backend_cls", _LOCAL_BACKENDS)
def test_write_batch_validated(backend_cls, tmp_path):
    """A write batch whose columns disagree in length is refused and writes nothing."""
    backend = backend_cls()
    try:
        ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
        col = backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
        assert_write_batch_validated(backend, col)
    finally:
        backend.close()


@pytest.mark.parametrize("backend_cls", _LOCAL_BACKENDS)
def test_default_update_skips_missing_ids(backend_cls, tmp_path, monkeypatch):
    """The ``BaseCollection.update`` default, which pgvector, qdrant, milvus and
    third-party backends inherit, holds the same contract as the overrides."""
    from mempalace.backends.base import BaseCollection

    backend = backend_cls()
    try:
        ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
        col = backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
        monkeypatch.setattr(type(col), "update", BaseCollection.update)
        assert_update_skips_missing_ids(backend, col)
    finally:
        backend.close()


def test_default_update_is_one_upsert_even_when_a_vector_is_missing():
    """A row without a stored vector re-embeds the batch in one atomic upsert."""
    from unittest.mock import MagicMock

    from mempalace.backends.base import BaseCollection, GetResult

    col = MagicMock()
    col.get.return_value = GetResult(
        ids=["a", "b"],
        documents=["alpha", "beta"],
        metadatas=[{"wing": "w"}, {"wing": "w"}],
        embeddings=[[1.0, 0.0], None],
    )
    BaseCollection.update(col, ids=["a", "b"], metadatas=[{"room": "x"}, {"room": "y"}])

    calls = [call.kwargs for call in col.upsert.call_args_list]
    assert [(c["ids"], c["embeddings"]) for c in calls] == [(["a", "b"], None)]


def test_local_backends_do_not_claim_namespace_isolation():
    """Local backends isolate by on-disk path, not namespace; they must not
    advertise the namespace-isolation capability (RFC 001 isolation contract)."""
    assert "supports_namespace_isolation" not in ChromaBackend.capabilities
    assert "supports_namespace_isolation" not in SQLiteExactBackend.capabilities
    assert "supports_namespace_isolation" not in RustExactBackend.capabilities


@pytest.mark.parametrize("backend_cls", _LOCAL_BACKENDS)
def test_local_backends_reject_populated_namespace(backend_cls, tmp_path):
    """RFC 001 section 4.4 no-silent-drop: non-advertising backends MUST raise when
    PalaceRef.namespace is set rather than accept-and-ignore."""
    from mempalace.backends.base import UnsupportedCapabilityError

    backend = backend_cls()
    try:
        path = tmp_path / "palace"
        path.mkdir()
        ref = PalaceRef(id=str(path), local_path=str(path), namespace="tenant-a")
        with pytest.raises(UnsupportedCapabilityError):
            backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
    finally:
        backend.close()
