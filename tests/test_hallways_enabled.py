"""Regression tests for the supported hallway-construction off-switch (#2329)."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mempalace import convo_miner, hallways, miner, palace_graph
from mempalace.config import MempalaceConfig
from mempalace.palace import get_collection


ENV_SETTING = "MEMPALACE_KG_HALLWAYS_ENABLED"


@pytest.fixture(autouse=True)
def _clear_hallways_override(monkeypatch):
    monkeypatch.delenv(ENV_SETTING, raising=False)


def _config(tmp_path, **settings):
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(settings), encoding="utf-8")
    return MempalaceConfig(config_dir=config_dir, palace_path=tmp_path / "data" / "palace")


def test_config_accepts_canonical_false(tmp_path):
    assert _config(tmp_path, hallways_enabled=False).hallways_enabled is False


def test_disabled_compute_skips_drawer_fetch(tmp_path):
    config = _config(tmp_path, hallways_enabled=False)
    collection = MagicMock()
    collection.get.return_value = {"metadatas": []}

    assert hallways.compute_hallways_for_wing("alpha", col=collection, config=config) == []

    collection.get.assert_not_called()


def test_disabled_entity_tunnels_skip_hallway_scan(tmp_path, monkeypatch):
    config = _config(tmp_path, hallways_enabled=False)
    scan = MagicMock(return_value=[])
    monkeypatch.setattr(hallways, "list_hallways", scan)

    assert miner._compute_entity_tunnels_for_wing("alpha", config=config) == 0

    scan.assert_not_called()


@pytest.mark.parametrize(
    "env_value, expected",
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        (" TRUE ", True),
        (" YeS ", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        (" FALSE ", False),
        (" OfF ", False),
    ],
)
def test_environment_overrides_opposite_file_value(tmp_path, monkeypatch, env_value, expected):
    config = _config(tmp_path, hallways_enabled=not expected)
    monkeypatch.setenv(ENV_SETTING, env_value)
    assert config.hallways_enabled is expected


@pytest.mark.parametrize("env_value", ["", " ", "maybe", "2", "null"])
@pytest.mark.parametrize("file_value", [True, False])
def test_invalid_environment_falls_through_to_file(tmp_path, monkeypatch, env_value, file_value):
    config = _config(tmp_path, hallways_enabled=file_value)
    monkeypatch.setenv(ENV_SETTING, env_value)
    assert config.hallways_enabled is file_value


@pytest.mark.parametrize(
    "file_value, expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        (" OFF ", False),
    ],
)
def test_valid_file_settings_return_boolean(tmp_path, file_value, expected):
    assert _config(tmp_path, hallways_enabled=file_value).hallways_enabled is expected


@pytest.mark.parametrize("file_value", [0, 1, 0.0, 1.0, None, [], {}, "maybe", ""])
def test_invalid_file_settings_preserve_enabled_default(tmp_path, file_value):
    assert _config(tmp_path, hallways_enabled=file_value).hallways_enabled is True


def test_missing_setting_is_enabled(tmp_path):
    assert _config(tmp_path).hallways_enabled is True


def test_absent_config_is_not_written_by_setting_resolution(tmp_path, monkeypatch):
    config_dir = tmp_path / "absent-config"
    config = MempalaceConfig(config_dir=config_dir)
    assert config.hallways_enabled is True
    monkeypatch.setenv(ENV_SETTING, "false")
    assert config.hallways_enabled is False
    assert not config_dir.exists()


@pytest.mark.parametrize("collection_supplied", [True, False])
def test_disabled_compute_skips_all_graph_work_and_logs_once(
    tmp_path, monkeypatch, caplog, collection_supplied
):
    config = _config(tmp_path, hallways_enabled=False)
    collection = MagicMock() if collection_supplied else None
    lock = MagicMock()
    load = MagicMock()
    save = MagicMock()
    monkeypatch.setattr(hallways, "_hallway_file_lock", lock)
    monkeypatch.setattr(hallways, "_load_hallways", load)
    monkeypatch.setattr(hallways, "_save_hallways", save)

    with caplog.at_level(logging.INFO, logger=hallways.logger.name):
        # An opt-out precedes even validation of work-specific arguments.
        assert (
            hallways.compute_hallways_for_wing(
                "alpha", col=collection, min_count="invalid", config=config
            )
            == []
        )

    lock.assert_not_called()
    load.assert_not_called()
    save.assert_not_called()
    if collection is not None:
        collection.get.assert_not_called()
    messages = [r.getMessage() for r in caplog.records if r.name == hallways.logger.name]
    assert len(messages) == 1
    assert "disabled" in messages[0].lower()


def test_environment_disables_calls_without_explicit_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path / "absent-config"))
    monkeypatch.setenv(ENV_SETTING, "off")
    collection = MagicMock()
    scan = MagicMock()
    monkeypatch.setattr(hallways, "list_hallways", scan)

    assert hallways.compute_hallways_for_wing("alpha", col=collection) == []
    assert miner._compute_entity_tunnels_for_wing("alpha") == 0

    collection.get.assert_not_called()
    scan.assert_not_called()
    assert not (tmp_path / "absent-config").exists()


def _hallway_record(wing, entity_a, entity_b):
    return {
        "id": f"existing-{wing}",
        "wing": wing,
        "entity_a": entity_a,
        "entity_b": entity_b,
        "co_occurrence_count": 7,
        "rooms": ["general"],
        "created_by": "auto",
    }


def _seed_sidecars(config):
    records = [_hallway_record("alpha", "Aya", "Old"), _hallway_record("beta", "Aya", "Lumi")]
    hallway_path = Path(config.hallway_file)
    hallway_path.parent.mkdir(parents=True, exist_ok=True)
    hallway_path.write_text(
        json.dumps({"schema_version": 1, "hallways": records}, indent=4) + "\n",
        encoding="utf-8",
    )
    tunnel = {
        "id": "existing-manual-tunnel",
        "source": {"wing": "alpha", "room": "general"},
        "target": {"wing": "beta", "room": "general"},
        "label": "Keep this explicit link",
        "kind": "explicit",
    }
    tunnel_path = Path(config.tunnel_file)
    tunnel_path.write_text(
        json.dumps([tunnel], indent=4) + "\n",
        encoding="utf-8",
    )
    return records, tunnel


def test_disabled_computation_preserves_sidecar_bytes_and_explicit_reads(tmp_path, caplog):
    config = _config(tmp_path, hallways_enabled=False)
    records, tunnel = _seed_sidecars(config)
    hallway_path, tunnel_path = Path(config.hallway_file), Path(config.tunnel_file)
    before = hallway_path.read_bytes(), tunnel_path.read_bytes()

    with caplog.at_level(logging.INFO):
        assert hallways.compute_hallways_for_wing("alpha", col=MagicMock(), config=config) == []
        assert miner._compute_entity_tunnels_for_wing("alpha", config=config) == 0

    assert (hallway_path.read_bytes(), tunnel_path.read_bytes()) == before
    assert hallways.list_hallways(config=config) == records
    assert palace_graph._load_tunnels(config=config) == [tunnel]
    assert (
        palace_graph.follow_tunnels("alpha", "general", config=config, record=False)[0]["tunnel_id"]
        == tunnel["id"]
    )
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 1


def test_path_only_config_preserves_enabled_compatibility(tmp_path):
    config = SimpleNamespace(hallway_file=str(tmp_path / "hallways.json"))
    collection = MagicMock()
    collection.get.return_value = {
        "metadatas": [
            {"wing": "alpha", "room": "general", "entities": "Aya;Lumi"},
            {"wing": "alpha", "room": "general", "entities": "Aya;Lumi"},
        ]
    }

    created = hallways.compute_hallways_for_wing("alpha", col=collection, config=config)

    assert len(created) == 1
    assert created[0]["co_occurrence_count"] == 2
    assert hallways.list_hallways(config=config) == created


def test_entity_tunnels_accept_path_only_config(tmp_path):
    config = SimpleNamespace(
        hallway_file=str(tmp_path / "hallways.json"),
        tunnel_file=str(tmp_path / "tunnels.json"),
    )
    _, explicit_tunnel = _seed_sidecars(config)

    assert miner._compute_entity_tunnels_for_wing("alpha", config=config) == 1

    tunnels = palace_graph._load_tunnels(config=config)
    assert explicit_tunnel in tunnels
    assert any(t.get("kind") == "entity" for t in tunnels)


@pytest.mark.parametrize("drawers_filed", [0, -1])
def test_conversation_safe_wrapper_does_no_work_when_nothing_filed(
    tmp_path, monkeypatch, drawers_filed, caplog
):
    config = _config(tmp_path, hallways_enabled=False)
    compute = MagicMock()
    monkeypatch.setattr(hallways, "compute_hallways_for_wing", compute)

    with caplog.at_level(logging.INFO):
        convo_miner._compute_hallways_for_wing_safe(
            "alpha", MagicMock(), drawers_filed, config=config
        )

    compute.assert_not_called()
    assert not caplog.records


def test_conversation_safe_wrapper_remains_best_effort(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    compute = MagicMock(side_effect=RuntimeError("derived graph failure"))
    monkeypatch.setattr(hallways, "compute_hallways_for_wing", compute)

    convo_miner._compute_hallways_for_wing_safe("alpha", MagicMock(), 2, config=config)

    assert "hallways skipped: derived graph failure" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["project", "conversation"])
@pytest.mark.parametrize("setting", ["default", "file-off", "environment-off"])
def test_miners_honor_switch_and_keep_verbatim_drawers(
    tmp_path, monkeypatch, caplog, mode, setting
):
    settings = {"hallways_enabled": False} if setting == "file-off" else {}
    config = _config(tmp_path, **settings)
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config._config_dir))
    if setting == "environment-off":
        monkeypatch.setenv(ENV_SETTING, "false")
    _, explicit_tunnel = _seed_sidecars(config)
    original_hallway_bytes = Path(config.hallway_file).read_bytes()
    collection = get_collection(config.palace_path)
    collection.add(
        ids=["seed-1", "seed-2", "seed-3"],
        documents=[
            "Original Aya and Lumi text one",
            "Original Aya and Lumi text two",
            "Original Aya and Lumi text three",
        ],
        metadatas=[
            {"wing": "alpha", "room": "general", "entities": "Aya;Lumi"},
            {"wing": "alpha", "room": "general", "entities": "Aya;Lumi"},
            {"wing": "alpha", "room": "general", "entities": "Aya;Lumi"},
        ],
    )
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    if mode == "project":
        source = source_dir / "app.py"
        content = "def main():\n    print('Preserve my exact words, including café.')\n" * 3
        source.write_text(content, encoding="utf-8")
        # Topic tunnels continue to be built when entity-derived work is disabled.
        monkeypatch.setattr(
            miner, "get_topics_by_wing", lambda: {"alpha": ["Python"], "beta": ["Python"]}
        )
    else:
        source = source_dir / "session.txt"
        content = (
            "> What should we preserve?\n"
            "Keep the exact original words, including café.\n\n"
            "> What follows?\n"
            "Continue mining while hallway construction is disabled.\n"
        )
        source.write_text(content, encoding="utf-8")

    load = MagicMock(wraps=hallways._load_hallways)
    scan = MagicMock(wraps=hallways.list_hallways)
    monkeypatch.setattr(hallways, "_load_hallways", load)
    monkeypatch.setattr(hallways, "list_hallways", scan)
    with caplog.at_level(logging.INFO, logger=hallways.logger.name):
        if mode == "project":
            miner.mine(str(source_dir), config.palace_path, wing_override="alpha")
        else:
            convo_miner.mine_convos(str(source_dir), config.palace_path, wing="alpha")

    # Post-mine integrity validation closes the native backend's live handle.
    collection = get_collection(config.palace_path)
    mined = collection.get(where={"source_file": str(source)}, include=["documents"])
    assert mined["ids"]
    expected_documents = (
        {content.strip()} if mode == "project" else set(content.strip().split("\n\n"))
    )
    assert set(mined["documents"]) == expected_documents
    original = collection.get(ids=["seed-1", "seed-2", "seed-3"], include=["documents"])
    assert dict(zip(original["ids"], original["documents"])) == {
        "seed-1": "Original Aya and Lumi text one",
        "seed-2": "Original Aya and Lumi text two",
        "seed-3": "Original Aya and Lumi text three",
    }
    logs = [
        r for r in caplog.records if r.name == hallways.logger.name and r.levelno == logging.INFO
    ]
    if setting == "default":
        assert load.call_count >= 1
        assert not logs
        records = hallways.list_hallways(wing="alpha", config=config)
        assert any({h["entity_a"], h["entity_b"]} == {"Aya", "Lumi"} for h in records)
    else:
        load.assert_not_called()
        scan.assert_not_called()
        assert Path(config.hallway_file).read_bytes() == original_hallway_bytes
        assert len(logs) == 1

    tunnels = palace_graph._load_tunnels(config=config)
    assert explicit_tunnel in tunnels
    if mode == "project":
        assert any(t.get("kind") == "topic" for t in tunnels)
        assert any(t.get("kind") == "entity" for t in tunnels) is (setting == "default")


def test_explicit_cli_rebuild_honors_disabled_construction(tmp_path, monkeypatch, caplog):
    from mempalace import cli, palace

    config = _config(tmp_path, hallways_enabled=False)
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config._config_dir))
    records, _ = _seed_sidecars(config)
    before = Path(config.hallway_file).read_bytes()
    collection = MagicMock()
    monkeypatch.setattr(palace, "get_collection", lambda *args, **kwargs: collection)
    args = SimpleNamespace(palace=config.palace_path, wing="alpha", rebuild=True)

    with caplog.at_level(logging.INFO, logger=hallways.logger.name):
        cli.cmd_hallways(args)

    collection.get.assert_not_called()
    assert Path(config.hallway_file).read_bytes() == before
    assert hallways.list_hallways(config=config) == records
    assert len([r for r in caplog.records if r.name == hallways.logger.name]) == 1
