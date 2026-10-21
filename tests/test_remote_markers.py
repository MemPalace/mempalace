"""Remote-backend metadata must survive one bad value, and markers must not tear."""

import json
from datetime import date

import pytest

from mempalace.backends._remote_common import jsonable_metadata
from mempalace.backends.base import BackendMismatchError, PalaceRef
from mempalace.backends.milvus import MilvusBackend, _MilvusConfig
from mempalace.backends.pgvector import PgVectorBackend, _PgVectorConfig
from mempalace.backends.qdrant import QdrantBackend, _QdrantConfig

_META = {
    "wing": "w",
    "room": "r",
    "source_file": "a.md",
    "when": date(2026, 1, 1),
}


def test_one_unserializable_value_does_not_wipe_metadata():
    cleaned = jsonable_metadata(dict(_META))
    assert cleaned == {"wing": "w", "room": "r", "source_file": "a.md", "when": "2026-01-01"}


def test_nan_is_stored_as_text_not_a_token_jsonb_rejects():
    assert jsonable_metadata({"score": float("nan")}) == {"score": "nan"}


def test_reserved_metadata_keys_are_refused():
    with pytest.raises(ValueError, match="reserved"):
        jsonable_metadata({"id": "x"}, reserved=frozenset({"id"}))


def test_marker_rewrite_keeps_created_at_and_skips_an_unchanged_target(tmp_path):
    backend = QdrantBackend()
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    cfg = _QdrantConfig(url="http://127.0.0.1:6333", namespace="alpha")
    backend._write_marker(ref, cfg)
    path = tmp_path / "qdrant_backend.json"
    first_text = path.read_text(encoding="utf-8")
    created = json.loads(first_text)["created_at"]

    backend._write_marker(ref, cfg)
    assert path.read_text(encoding="utf-8") == first_text

    changed = _QdrantConfig(url="http://127.0.0.1:6333", namespace="beta")
    backend._write_marker(ref, changed)
    second = backend._read_marker(ref)
    assert second["created_at"] == created
    assert second["qdrant"]["namespace"] == "beta"


def test_crash_mid_marker_write_leaves_a_readable_marker(tmp_path, monkeypatch):
    backend = PgVectorBackend()
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    cfg = _PgVectorConfig(dsn="postgresql://db.example/palace", namespace="alpha")
    backend._write_marker(ref, cfg)
    path = tmp_path / "pgvector_backend.json"
    original = path.read_bytes()

    def killed(payload, handle, **kwargs):
        handle.write("{")
        handle.flush()
        raise RuntimeError("killed mid-write")

    monkeypatch.setattr(json, "dump", killed)
    changed = _PgVectorConfig(dsn="postgresql://db.example/palace", namespace="beta")
    with pytest.raises(RuntimeError, match="killed mid-write"):
        backend._write_marker(ref, changed)

    assert path.read_bytes() == original
    marker = backend._read_marker(ref)
    assert marker["pgvector"]["namespace"] == "alpha"


def test_milvus_marker_publish_is_atomic(tmp_path, monkeypatch):
    backend = MilvusBackend()
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    cfg = _MilvusConfig(uri="http://127.0.0.1:19530", namespace="alpha")
    backend._write_marker(ref, cfg)
    path = tmp_path / "milvus_backend.json"
    original = path.read_bytes()

    def killed(payload, handle, **kwargs):
        raise RuntimeError("killed mid-write")

    monkeypatch.setattr(json, "dump", killed)
    with pytest.raises(RuntimeError, match="killed mid-write"):
        backend._write_marker(ref, _MilvusConfig(uri="http://127.0.0.1:19530", namespace="beta"))

    assert path.read_bytes() == original
    assert backend._read_marker(ref)["milvus"]["namespace"] == "alpha"


def test_validate_marker_names_the_mismatched_target_key(tmp_path):
    backend = QdrantBackend()
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    alpha = _QdrantConfig(url="http://127.0.0.1:6333", namespace="alpha")
    backend._write_marker(ref, alpha)
    backend._validate_marker_target(ref, alpha)
    beta = _QdrantConfig(url="http://127.0.0.1:6333", namespace="beta")
    with pytest.raises(BackendMismatchError, match=r"remote target .*namespace.*QDRANT_URL"):
        backend._validate_marker_target(ref, beta)


def test_unreadable_marker_is_a_mismatch_and_is_replaced_on_write(tmp_path):
    backend = MilvusBackend()
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    (tmp_path / "milvus_backend.json").write_text("{", encoding="utf-8")
    with pytest.raises(BackendMismatchError, match="unreadable"):
        backend._read_marker(ref)
    backend._write_marker(ref, _MilvusConfig(uri="http://127.0.0.1:19530", namespace="alpha"))
    assert backend._read_marker(ref)["milvus"]["namespace"] == "alpha"
