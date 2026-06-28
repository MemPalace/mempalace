"""Backend isolation conformance suite (RFC 001 isolation contract).

Runs the shared isolation assertions from ``_backend_conformance`` against the
built-in local backends. Server-mode backends (qdrant) run the same assertions
under their own fake/live client in ``test_qdrant_backend.py``.
"""

import pytest

from _backend_conformance import assert_partition_isolation

from mempalace.backends import PalaceRef
from mempalace.backends.chroma import ChromaBackend
from mempalace.backends.sqlite_exact import SQLiteExactBackend

_LOCAL_BACKENDS = [
    pytest.param(ChromaBackend, id="chroma"),
    pytest.param(SQLiteExactBackend, id="sqlite_exact"),
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


def test_local_backends_do_not_claim_namespace_isolation():
    """Local backends isolate by on-disk path, not namespace; they must not
    advertise the namespace-isolation capability (RFC 001 isolation contract)."""
    assert "supports_namespace_isolation" not in ChromaBackend.capabilities
    assert "supports_namespace_isolation" not in SQLiteExactBackend.capabilities
