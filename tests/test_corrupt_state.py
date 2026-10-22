"""A corrupt state file is not replaced by an empty or one-entry document."""

import json

import pytest

from mempalace.config import MempalaceConfig
from mempalace.entity_registry import EntityRegistry
from mempalace.fsio import CorruptStateError
from mempalace.hallways import delete_hallway
from mempalace.palace_graph import create_tunnel, record_tunnel_traversal


def _palace_config(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    return MempalaceConfig(config_dir=str(tmp_path / "cfg"), palace_path=str(palace))


def _kept(directory, name):
    return sorted(directory.glob(name + ".corrupt-*"))


def test_corrupt_tunnels_are_not_replaced_by_one_entry(tmp_path):
    cfg = _palace_config(tmp_path)
    path = tmp_path / "tunnels.json"
    original = b'[{"id": "aaa"}, {"id": "bbb"}, {"id": "ccc"},'
    path.write_bytes(original)

    with pytest.raises(CorruptStateError):
        create_tunnel("wing_a", "room", "wing_b", "other", kind="topic", config=cfg)

    kept = _kept(tmp_path, "tunnels.json")
    assert len(kept) == 1
    assert kept[0].read_bytes() == original
    assert not path.exists()


def test_corrupt_tunnel_read_does_not_move_the_file(tmp_path):
    from mempalace.palace_graph import _load_tunnels

    cfg = _palace_config(tmp_path)
    path = tmp_path / "tunnels.json"
    original = b'[{"id": "aaa"},'
    path.write_bytes(original)

    assert _load_tunnels(cfg) == []
    assert path.read_bytes() == original
    assert _kept(tmp_path, "tunnels.json") == []


def test_recording_a_traversal_leaves_a_corrupt_file_alone(tmp_path):
    """Following a tunnel is a read; it must not move the file aside."""
    cfg = _palace_config(tmp_path)
    path = tmp_path / "tunnels.json"
    original = b'[{"id": "aaa"},'
    path.write_bytes(original)

    assert record_tunnel_traversal(["aaa"], config=cfg) == 0
    assert path.read_bytes() == original
    assert _kept(tmp_path, "tunnels.json") == []


def test_corrupt_hallways_are_not_replaced_by_one_wing(tmp_path):
    cfg = _palace_config(tmp_path)
    path = tmp_path / "hallways.json"
    original = b'{"hallways": [{"wing": "alpha", "id": "h1"}, {"wing": "beta",'
    path.write_bytes(original)

    with pytest.raises(CorruptStateError):
        delete_hallway("h1", config=cfg)

    kept = _kept(tmp_path, "hallways.json")
    assert len(kept) == 1
    assert kept[0].read_bytes() == original
    assert not path.exists()


def test_corrupt_entity_registry_save_does_not_publish_an_empty_file(tmp_path):
    path = tmp_path / "entity_registry.json"
    original = b'{"people": {"Ada": {"source": "onboarding"}}, "projects": ["MemPalace"],'
    path.write_bytes(original)
    registry = EntityRegistry.load(config_dir=tmp_path)

    with pytest.raises(CorruptStateError):
        registry.seed(
            mode="personal",
            people=[{"name": "Grace", "relationship": "friend", "context": "personal"}],
            projects=[],
        )

    kept = _kept(tmp_path, "entity_registry.json")
    assert len(kept) == 1
    assert kept[0].read_bytes() == original
    assert not path.exists()


def test_sidecar_write_keeps_other_collections_when_one_value_is_fine(tmp_path):
    from mempalace.backends._sidecar import read_embedder_sidecar, write_embedder_sidecar
    from mempalace.backends.base import EmbedderIdentity

    path = tmp_path / "mempalace_embedder.json"
    path.write_text(
        json.dumps(
            {
                "mempalace_closets": {"model_name": "minilm", "dimension": 384},
            }
        ),
        encoding="utf-8",
    )
    write_embedder_sidecar(str(path), "mempalace_drawers", EmbedderIdentity("minilm", 384))
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["mempalace_closets"]["model_name"] == "minilm"
    assert loaded["mempalace_drawers"]["model_name"] == "minilm"
    assert read_embedder_sidecar(str(path), "mempalace_closets").model_name == "minilm"
