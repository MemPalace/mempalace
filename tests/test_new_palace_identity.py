"""A brand-new qdrant / pgvector palace records its embedder identity.

These backends create the palace folder on their first upsert, but the
identity is recorded on the first (empty) open, before it. The sidecar
write used to fail on the missing folder and swallow the error, so a palace
built by ``mempalace mine`` stayed unrecorded for good and every later
same-dimension model swap wrote silently (matrix F3 / S4b).
"""

import json

import pytest
import yaml

from mempalace.backends.base import EmbedderIdentity, EmbedderIdentityRecordError

from test_pgvector_backend import fake_pgvector  # noqa: F401  (fixture)
from test_qdrant_backend import fake_qdrant  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    from mempalace import palace
    from mempalace.backends import reset_backends

    reset_backends()
    monkeypatch.setattr(palace, "_VALIDATED_IDENTITY", set())
    for key in ("MEMPALACE_EMBEDDING_MODEL", "MEMPALACE_BACKEND", "MEMPALACE_PALACE_PATH"):
        monkeypatch.delenv(key, raising=False)
    yield
    reset_backends()


def _project(tmp_path):
    project = tmp_path / "project"
    (project / "notes").mkdir(parents=True)
    (project / "notes" / "garden.md").write_text(
        "The greenhouse tomatoes need watering every morning before nine.\n" * 8
    )
    with open(project / "mempalace.yaml", "w") as fh:
        yaml.dump({"wing": "garden", "rooms": [{"name": "notes", "description": "Notes"}]}, fh)
    return project


@pytest.mark.usefixtures("fake_qdrant", "fake_pgvector")
@pytest.mark.parametrize("backend", ["qdrant", "pgvector"])
def test_mine_into_a_new_palace_records_the_identity(backend, tmp_path, monkeypatch):
    from mempalace.miner import mine

    palace = tmp_path / "does" / "not" / "exist" / "palace"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "palace_path": str(palace),
                "backend": backend,
                "embedding_model": "minilm",
                "pgvector_dsn": "postgresql://example/db",
            }
        )
    )
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config_dir))
    project = _project(tmp_path)

    mine(str(project), str(palace))

    sidecar = json.loads((palace / "mempalace_embedder.json").read_text())
    assert sidecar["mempalace_drawers"]["model_name"] == "minilm", sidecar
    assert (palace / f"{backend}_backend.json").is_file()


@pytest.mark.parametrize("backend", ["qdrant", "pgvector"])
def test_identity_on_a_missing_palace_folder_is_recorded(backend, tmp_path):
    from test_embedder_identity import _pgvector_collection, _qdrant_collection

    palace = tmp_path / "new-palace"
    make = _qdrant_collection if backend == "qdrant" else _pgvector_collection
    col = make(palace, write_marker=False)
    assert not palace.exists()

    col.set_embedder_identity(EmbedderIdentity("minilm", 384))

    assert col.get_stored_embedder_identity() == EmbedderIdentity("minilm", 384)
    assert not col._marker_exists()  # the folder alone does not initialize the palace


def test_a_failed_identity_write_raises(tmp_path, monkeypatch):
    from mempalace.backends import _sidecar

    blocker = tmp_path / "palace"
    blocker.write_text("a file where the palace folder should be")
    with pytest.raises(EmbedderIdentityRecordError, match="could not record"):
        _sidecar.write_embedder_sidecar(
            str(blocker / "mempalace_embedder.json"),
            "mempalace_drawers",
            EmbedderIdentity("minilm", 384),
        )


def test_a_write_open_that_cannot_record_refuses(tmp_path, monkeypatch):
    """A brand-new collection whose identity cannot be recorded refuses the
    write open instead of filing unprotected rows; a read open never records,
    so it is unaffected."""
    from mempalace import palace as P
    from test_embedder_identity import _qdrant_collection

    monkeypatch.setenv("MEMPALACE_EMBEDDING_MODEL", "minilm")
    col = _qdrant_collection(tmp_path, write_marker=False)
    monkeypatch.setattr(P, "_collection_has_rows", lambda *_args: False)

    def _fail(path, data):
        raise OSError(30, "Read-only file system")

    from mempalace.backends import _sidecar

    monkeypatch.setattr(_sidecar, "_write_atomically", _fail)
    P._enforce_embedder_identity(col, str(tmp_path), "mempalace_drawers", create=False)
    with pytest.raises(EmbedderIdentityRecordError, match="Read-only file system"):
        P._enforce_embedder_identity(col, str(tmp_path), "mempalace_drawers", create=True)


def test_identity_record_failures_are_mcp_tool_errors():
    from mempalace import embedding
    from mempalace.mcp_server import _tool_result_is_error

    result = embedding.model_error_result(EmbedderIdentityRecordError("could not record ..."))
    assert result["error"] == "Embedder identity not recorded"
    assert result["error_class"] == "EmbedderIdentityRecordError"
    assert _tool_result_is_error(result)
