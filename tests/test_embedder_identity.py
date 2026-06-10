"""Embedder-identity persistence and three-state enforcement (RFC 001).

A same-dimension model swap (e.g. two 384-d models) silently corrupts
retrieval on the explicit-embedding backends, which have no native model
check. The contract records the model name and refuses a swap on open. These
tests avoid loading any embedding model: the enforcement *check* path needs
only the configured model name (cheap), and persistence is exercised with
``EmbedderIdentity`` objects and explicit vectors.
"""

import os
import warnings

import pytest

from mempalace.backends.base import (
    DimensionMismatchError,
    EmbedderIdentity,
    EmbedderIdentityMismatchError,
    EmbedderIdentityUnknownWarning,
    PalaceRef,
    check_embedder_identity,
)


# ---------------------------------------------------------------------------
# Three-state helper
# ---------------------------------------------------------------------------


def test_unknown_when_nothing_stored():
    assert check_embedder_identity(None, EmbedderIdentity("minilm", 384)) == "unknown"


def test_unknown_when_current_is_nameless():
    stored = EmbedderIdentity("minilm", 384)
    assert check_embedder_identity(stored, EmbedderIdentity("", 384)) == "unknown"
    assert check_embedder_identity(stored, None) == "unknown"


def test_known_match():
    a = EmbedderIdentity("minilm", 384)
    assert check_embedder_identity(a, a) == "known_match"


def test_match_skips_unknown_dimension():
    # dimension 0 means "not probed" and must not be treated as a real conflict.
    assert (
        check_embedder_identity(EmbedderIdentity("minilm", 384), EmbedderIdentity("minilm", 0))
        == "known_match"
    )


def test_model_swap_raises_identity_error():
    with pytest.raises(EmbedderIdentityMismatchError):
        check_embedder_identity(EmbedderIdentity("minilm", 384), EmbedderIdentity("gemma", 384))


def test_dimension_change_raises_dimension_error_first():
    # Width change is physically unusable — checked before the name swap.
    with pytest.raises(DimensionMismatchError):
        check_embedder_identity(EmbedderIdentity("a", 384), EmbedderIdentity("b", 768))


def test_force_returns_mismatch_without_raising():
    assert (
        check_embedder_identity(
            EmbedderIdentity("minilm", 384),
            EmbedderIdentity("gemma", 384),
            force_model_swap=True,
        )
        == "known_mismatch"
    )


# ---------------------------------------------------------------------------
# Per-backend persistence roundtrip (no model loads)
# ---------------------------------------------------------------------------


def _sqlite_collection(tmp_path):
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend = SQLiteExactBackend()
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)


def _chroma_collection(tmp_path):
    from mempalace.backends.chroma import ChromaBackend

    backend = ChromaBackend()
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)


def test_sqlite_identity_roundtrip(tmp_path):
    col = _sqlite_collection(tmp_path)
    assert col.get_stored_embedder_identity() is None
    col.add(documents=["x"], ids=["a"], metadatas=[{}], embeddings=[[0.1, 0.2, 0.3, 0.4]])
    col.set_embedder_identity(EmbedderIdentity("minilm", 4))
    got = col.get_stored_embedder_identity()
    assert got is not None and got.model_name == "minilm" and got.dimension == 4


def test_sqlite_set_identity_ignores_nameless(tmp_path):
    col = _sqlite_collection(tmp_path)
    col.add(documents=["x"], ids=["a"], metadatas=[{}], embeddings=[[0.1, 0.2, 0.3, 0.4]])
    col.set_embedder_identity(EmbedderIdentity("", 4))
    assert col.get_stored_embedder_identity() is None


def test_chroma_identity_roundtrip_via_sidecar(tmp_path):
    col = _chroma_collection(tmp_path)
    assert col.get_stored_embedder_identity() is None
    col.set_embedder_identity(EmbedderIdentity("minilm", 384))
    got = col.get_stored_embedder_identity()
    assert got is not None and got.model_name == "minilm" and got.dimension == 384
    assert os.path.isfile(os.path.join(str(tmp_path), "mempalace_embedder.json"))


def test_pgvector_identity_survives_marker_rewrite(tmp_path):
    # Identity lives in a sidecar, separate from the mismatch marker, so a
    # marker rebuild (which happens on every write) must not affect it.
    from mempalace.backends.pgvector import PgVectorBackend, _PgVectorConfig

    backend = PgVectorBackend()
    cfg = _PgVectorConfig(dsn="postgresql://example", namespace=None)
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    # No marker needed to record identity — the sidecar is unguarded.
    backend._set_embedder_identity(ref, "mempalace_drawers", EmbedderIdentity("minilm", 384))
    backend._write_marker(ref, cfg)
    got = backend._get_embedder_identity(ref, "mempalace_drawers")
    assert got is not None and got.model_name == "minilm" and got.dimension == 384


def test_embeddingcollection_delegates_identity_not_shadowed():
    # BaseCollection defines these as concrete methods, so __getattr__ never
    # delegates them — the wrapper needs explicit forwarding or it silently
    # reports the no-op default and masks the wrapped backend's identity.
    from mempalace.backends.base import BaseCollection
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    class _Inner(BaseCollection):
        def __init__(self):
            self._ident = None

        def add(self, **k): ...
        def upsert(self, **k): ...
        def query(self, **k): ...
        def get(self, **k): ...
        def delete(self, **k): ...
        def count(self):
            return 0

        def get_stored_embedder_identity(self):
            return self._ident

        def set_embedder_identity(self, identity):
            self._ident = identity

    inner = _Inner()
    wrapped = EmbeddingCollection(inner)
    wrapped.set_embedder_identity(EmbedderIdentity("minilm", 384))
    assert inner._ident is not None and inner._ident.model_name == "minilm"
    assert wrapped.get_stored_embedder_identity().model_name == "minilm"


# ---------------------------------------------------------------------------
# Enforcement via palace.get_collection (sqlite_exact, no model load)
# ---------------------------------------------------------------------------


@pytest.fixture
def clear_identity_cache():
    from mempalace import palace

    palace._VALIDATED_IDENTITY.clear()
    yield
    palace._VALIDATED_IDENTITY.clear()


def _seed_sqlite_with_identity(tmp_path, model):
    col = _sqlite_collection(tmp_path)
    col.add(documents=["x"], ids=["a"], metadatas=[{}], embeddings=[[0.1, 0.2, 0.3, 0.4]])
    if model is not None:
        col.set_embedder_identity(EmbedderIdentity(model, 4))
    return col


def test_enforcement_match_does_not_raise(tmp_path, monkeypatch, clear_identity_cache):
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    from mempalace import palace as P

    _seed_sqlite_with_identity(tmp_path, "minilm")
    P._VALIDATED_IDENTITY.clear()
    # Should not raise.
    P.get_collection(str(tmp_path), collection_name="mempalace_drawers", create=False)


def test_enforcement_model_swap_raises(tmp_path, monkeypatch, clear_identity_cache):
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    from mempalace import palace as P

    _seed_sqlite_with_identity(tmp_path, "minilm")
    P._VALIDATED_IDENTITY.clear()
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma")
    with pytest.raises(EmbedderIdentityMismatchError):
        P.get_collection(str(tmp_path), collection_name="mempalace_drawers", create=False)


def test_enforcement_brand_new_records_current_model(tmp_path, monkeypatch, clear_identity_cache):
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    from mempalace import palace as P

    col = P.get_collection(str(tmp_path), collection_name="mempalace_drawers", create=True)
    got = col.get_stored_embedder_identity()
    assert got is not None and got.model_name == "minilm"


def test_enforcement_legacy_with_data_warns(tmp_path, monkeypatch, clear_identity_cache):
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    from mempalace import palace as P

    _seed_sqlite_with_identity(tmp_path, None)  # data, but no recorded identity
    P._VALIDATED_IDENTITY.clear()
    with pytest.warns(EmbedderIdentityUnknownWarning):
        P.get_collection(str(tmp_path), collection_name="mempalace_drawers", create=False)


def test_enforcement_nameless_model_is_a_noop(tmp_path, monkeypatch, clear_identity_cache):
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    from mempalace import palace as P

    _seed_sqlite_with_identity(tmp_path, None)
    P._VALIDATED_IDENTITY.clear()
    # A nameless current embedder cannot enforce — no raise, no warning.
    monkeypatch.setattr("mempalace.embedding.current_model_name", lambda model=None: "")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        P.get_collection(str(tmp_path), collection_name="mempalace_drawers", create=False)


# ---------------------------------------------------------------------------
# set_palace_embedder_identity override path
# ---------------------------------------------------------------------------


def test_set_palace_identity_override_requires_force(tmp_path, monkeypatch, clear_identity_cache):
    monkeypatch.setenv("MEMPALACE_BACKEND", "sqlite_exact")
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    from mempalace import palace as P

    _seed_sqlite_with_identity(tmp_path, "minilm")
    # Recording a different model without force is refused.
    with pytest.raises(EmbedderIdentityMismatchError):
        P.set_palace_embedder_identity(str(tmp_path), model="embeddinggemma", force=False)
    # With force it goes through, recording the name only (no foreign load).
    old, new = P.set_palace_embedder_identity(str(tmp_path), model="embeddinggemma", force=True)
    assert old.model_name == "minilm" and new.model_name == "embeddinggemma"


def test_set_palace_identity_empty_target_raises(tmp_path, monkeypatch):
    # No model given and none configured: recording is a no-op in every backend,
    # so refuse rather than claim a phantom success.
    monkeypatch.setattr("mempalace.config.MempalaceConfig.embedding_model", property(lambda s: ""))
    from mempalace import palace as P

    with pytest.raises(ValueError):
        P.set_palace_embedder_identity(str(tmp_path), model=None)


def test_enforcement_prefers_effective_identity(monkeypatch, clear_identity_cache):
    # A server_embedder collection reports its own effective identity; the
    # configured model must be ignored in favor of it. Here effective and
    # stored disagree, so enforcement raises even though config says "minilm".
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    from mempalace import palace as P

    class _ServerCol:
        def effective_embedder_identity(self):
            return EmbedderIdentity("server-model", 768)

        def get_stored_embedder_identity(self):
            return EmbedderIdentity("other-model", 768)

        def count(self):
            return 5

        def set_embedder_identity(self, identity):
            raise AssertionError("must not record on a mismatch")

    with pytest.raises(EmbedderIdentityMismatchError):
        P._enforce_embedder_identity(_ServerCol(), "/tmp/x", "c", create=False)


def test_chroma_corrupt_sidecar_returns_none(tmp_path):
    # A malformed sidecar (non-dict JSON) must not raise — degrade to unknown.
    col = _chroma_collection(tmp_path)
    path = os.path.join(str(tmp_path), "mempalace_embedder.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('["not", "a", "dict"]')
    assert col.get_stored_embedder_identity() is None
    # And a subsequent set still works (overwrites the junk).
    col.set_embedder_identity(EmbedderIdentity("minilm", 384))
    assert col.get_stored_embedder_identity().model_name == "minilm"


# ---------------------------------------------------------------------------
# qdrant: identity persisted in the local marker (no live qdrant needed)
# ---------------------------------------------------------------------------


def _qdrant_collection(tmp_path, *, write_marker=True):
    from mempalace.backends.qdrant import QdrantBackend, QdrantCollection, _QdrantConfig

    backend = QdrantBackend()
    config = _QdrantConfig(url="http://localhost:6333", api_key=None, namespace=None)
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    if write_marker:
        backend._write_marker(ref, config)
    # The identity methods read/write the local marker only; the client is
    # never touched, so a placeholder stands in for a live REST connection.
    return QdrantCollection(
        backend=backend,
        client=object(),
        config=config,
        palace=ref,
        collection_name="mempalace_drawers",
        remote_collection="mp_drawers_remote",
    )


def test_qdrant_identity_survives_marker_rewrite(tmp_path):
    from mempalace.backends.qdrant import QdrantBackend, _QdrantConfig

    backend = QdrantBackend()
    config = _QdrantConfig(url="http://localhost:6333", api_key=None, namespace=None)
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    backend._write_marker(ref, config)
    backend._set_embedder_identity(ref, "mempalace_drawers", EmbedderIdentity("minilm", 384))
    backend._write_marker(ref, config)  # rebuild must not wipe embedders
    got = backend._get_embedder_identity(ref, "mempalace_drawers")
    assert got is not None and got.model_name == "minilm" and got.dimension == 384


def test_qdrant_collection_delegates_identity(tmp_path):
    col = _qdrant_collection(tmp_path)
    assert col.get_stored_embedder_identity() is None
    col.set_embedder_identity(EmbedderIdentity("minilm", 384))
    got = col.get_stored_embedder_identity()
    assert got is not None and got.model_name == "minilm" and got.dimension == 384


def test_qdrant_set_identity_creates_sidecar_when_missing(tmp_path):
    # Brand-new palace whose first write hasn't created the marker yet:
    # recording identity must create it, not silently no-op into permanent
    # "unknown" (the marker-on-write vs record-on-open timing gap).
    col = _qdrant_collection(tmp_path, write_marker=False)
    assert not col._marker_exists()
    col.set_embedder_identity(EmbedderIdentity("minilm", 384))
    got = col.get_stored_embedder_identity()
    assert got is not None and got.model_name == "minilm"


def _pgvector_collection(tmp_path, *, write_marker=True):
    from mempalace.backends.pgvector import PgVectorBackend, PgVectorCollection, _PgVectorConfig

    backend = PgVectorBackend()
    config = _PgVectorConfig(dsn="postgresql://example", namespace=None)
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    if write_marker:
        backend._write_marker(ref, config)
    return PgVectorCollection(
        backend=backend,
        client=object(),
        config=config,
        palace=ref,
        collection_name="mempalace_drawers",
        table="mp_drawers_t",
    )


def test_pgvector_set_identity_creates_sidecar_when_missing(tmp_path):
    # Same brand-new-palace timing gap as qdrant: recording must create the
    # marker rather than no-op.
    col = _pgvector_collection(tmp_path, write_marker=False)
    assert not col._marker_exists()
    col.set_embedder_identity(EmbedderIdentity("minilm", 384))
    got = col.get_stored_embedder_identity()
    assert got is not None and got.model_name == "minilm"


def test_qdrant_enforcement_model_swap_raises(tmp_path, monkeypatch, clear_identity_cache):
    # The enforcement check reads the marker (no server) and compares to the
    # configured model — a swap raises just like the local backends.
    from mempalace import palace as P

    col = _qdrant_collection(tmp_path)
    col.set_embedder_identity(EmbedderIdentity("minilm", 384))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "embeddinggemma")
    with pytest.raises(EmbedderIdentityMismatchError):
        P._enforce_embedder_identity(col, str(tmp_path), "mempalace_drawers", create=False)
