"""Invariant tests: every ChromaDB collection-creation path must set
``hnsw:space=cosine``.

Reason: ChromaDB's default HNSW distance is L2 (Euclidean). Under L2,
the searcher's ``max(0, 1 - distance)`` similarity formula systematically
floors to 0 because L2 distances on normalized 384-dim vectors routinely
exceed 1.0 — users then see flat ``Match: 0.0`` across every result and
have no signal that their palace is broken.

This test file locks the invariant so a future refactor that drops the
``metadata={"hnsw:space": "cosine"}`` parameter from any creation path
gets caught at test time rather than silently degrading search quality.
"""

from mempalace.backends.chroma import ChromaBackend
from mempalace.palace import get_collection


EXPECTED_METRIC = "cosine"


def _assert_cosine(col, where: str) -> None:
    meta = col.metadata if hasattr(col, "metadata") else col._collection.metadata
    assert isinstance(meta, dict), f"{where}: expected metadata dict, got {meta!r}"
    assert meta.get("hnsw:space") == EXPECTED_METRIC, (
        f"{where}: expected hnsw:space={EXPECTED_METRIC!r}, got {meta!r}. "
        "A collection without cosine metric will silently break the "
        "similarity formula used by the searcher."
    )


def test_legacy_get_or_create_collection_sets_cosine(tmp_path):
    backend = ChromaBackend()
    col = backend.get_or_create_collection(str(tmp_path), "mempalace_drawers")
    _assert_cosine(col, "legacy get_or_create_collection")


def test_legacy_create_collection_sets_cosine(tmp_path):
    backend = ChromaBackend()
    col = backend.create_collection(str(tmp_path), "mempalace_drawers")
    _assert_cosine(col, "legacy create_collection")


def test_new_get_collection_with_create_sets_cosine(tmp_path):
    """RFC 001 typed surface — ``get_collection(..., create=True)`` is the
    path the miner + init flow take. Must also set cosine."""
    backend = ChromaBackend()
    col = backend.get_collection(str(tmp_path), "mempalace_drawers", create=True)
    _assert_cosine(col, "get_collection(create=True)")


def test_palace_module_get_collection_sets_cosine(tmp_path):
    """The public ``mempalace.palace.get_collection`` is what most callers
    use. Must produce cosine palaces."""
    col = get_collection(str(tmp_path), "mempalace_drawers", create=True)
    _assert_cosine(col, "palace.get_collection(create=True)")


def test_reopening_cosine_palace_preserves_metric(tmp_path):
    """Opening a previously-created cosine palace (create=False) must
    still expose the cosine metadata — catches any regression where
    reopening drops or overwrites metadata."""
    backend = ChromaBackend()
    backend.create_collection(str(tmp_path), "mempalace_drawers")
    # Fresh backend simulates a process restart
    backend2 = ChromaBackend()
    col = backend2.get_collection(str(tmp_path), "mempalace_drawers", create=False)
    _assert_cosine(col, "re-opened palace")


def test_fresh_palace_via_full_stack_gets_cosine(tmp_path):
    """End-to-end: build a palace with the public API the way a new user
    would, confirm the resulting collection uses cosine distance.

    Uses the ``tmp_path`` fixture rather than ``tempfile.TemporaryDirectory``
    so ChromaDB's persistent SQLite file handles aren't asked to release
    during the test body — pytest cleans the path at session end, by which
    point the process is exiting and Windows' file-lock contention is
    moot. Matches the cleanup strategy used by the rest of this file and
    the project's 80% Windows coverage note in CLAUDE.md.
    """
    col = get_collection(str(tmp_path), "mempalace_drawers", create=True)
    _assert_cosine(col, "full-stack new palace")

    # And the closets collection too
    closets = get_collection(str(tmp_path), "mempalace_closets", create=True)
    _assert_cosine(closets, "full-stack new closets")
