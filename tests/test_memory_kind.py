import chromadb
import pytest
import yaml

from mempalace.config import MEMORY_KINDS, validate_memory_kind
from mempalace.convo_miner import mine_convos
from mempalace.miner import mine


def _disable_project_derivations(monkeypatch):
    monkeypatch.setattr("mempalace.miner._compute_topic_tunnels_for_wing", lambda *a, **k: 0)
    monkeypatch.setattr("mempalace.miner._compute_entity_tunnels_for_wing", lambda *a, **k: 0)
    monkeypatch.setattr("mempalace.miner.compute_hallways_for_wing", lambda *a, **k: [])
    monkeypatch.setattr("mempalace.miner._validate_palace_fts5_after_mine", lambda *a, **k: None)


def test_memory_kind_validation():
    assert MEMORY_KINDS == ("archive", "curated", "reference")
    assert validate_memory_kind(" CURATED ") == "curated"
    with pytest.raises(ValueError, match="memory_kind must be one of"):
        validate_memory_kind("summary")


def test_project_memory_kind_default_config_override_and_idempotency(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _disable_project_derivations(monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    source = project / "notes.md"
    source.write_text("Verbatim project reference. " * 20)
    config_path = project / "mempalace.yaml"
    config = {"wing": "project", "rooms": [{"name": "general", "description": "all"}]}
    config_path.write_text(yaml.safe_dump(config))
    palace = tmp_path / "palace"

    mine(str(project), str(palace))
    client = chromadb.PersistentClient(path=str(palace))
    col = client.get_collection("mempalace_drawers")
    first = col.get(where={"source_file": str(source.resolve())}, include=["metadatas"])
    first_ids = first["ids"]
    assert first_ids
    assert {meta["memory_kind"] for meta in first["metadatas"]} == {"reference"}

    config["memory_kind"] = "curated"
    config_path.write_text(yaml.safe_dump(config))
    mine(str(project), str(palace))
    configured = col.get(where={"source_file": str(source.resolve())}, include=["metadatas"])
    assert configured["ids"] == first_ids
    assert {meta["memory_kind"] for meta in configured["metadatas"]} == {"curated"}

    mine(str(project), str(palace), memory_kind="archive")
    overridden = col.get(where={"source_file": str(source.resolve())}, include=["metadatas"])
    assert overridden["ids"] == first_ids
    assert {meta["memory_kind"] for meta in overridden["metadatas"]} == {"archive"}


def test_conversation_kind_defaults_override_and_stamps_sentinel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "mempalace.convo_miner._compute_hallways_for_wing_safe", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "mempalace.convo_miner._validate_palace_fts5_after_mine", lambda *a, **k: None
    )
    convos = tmp_path / "convos"
    convos.mkdir()
    chat = convos / "chat.txt"
    chat.write_text(
        "> What is memory?\nMemory is persistence.\n\n"
        "> Why?\nIt preserves continuity.\n\n"
        "> How?\nStore exact words locally.\n"
    )
    tiny = convos / "tiny.txt"
    tiny.write_text("hi")
    palace = tmp_path / "palace"

    mine_convos(str(convos), str(palace), wing="convos")
    client = chromadb.PersistentClient(path=str(palace))
    col = client.get_collection("mempalace_drawers")
    archive_rows = col.get(include=["metadatas"])
    assert archive_rows["ids"]
    assert {meta["memory_kind"] for meta in archive_rows["metadatas"]} == {"archive"}

    mine_convos(str(convos), str(palace), wing="convos", memory_kind="curated")
    curated_rows = col.get(include=["metadatas"])
    assert {meta["memory_kind"] for meta in curated_rows["metadatas"]} == {"curated"}
    sentinel = col.get(where={"source_file": str(tiny.resolve())}, include=["metadatas"])
    assert sentinel["metadatas"][0]["memory_kind"] == "curated"
