"""Regression tests for disabling hallway construction while preserving stored data."""

import io
import json
import logging
from contextlib import nullcontext
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


@pytest.mark.parametrize("wing", ["alpha", None])
def test_explicit_cli_rebuild_honors_disabled_construction(tmp_path, monkeypatch, capsys, wing):
    from mempalace import cli, palace

    config = _config(tmp_path, hallways_enabled=False)
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config._config_dir))
    records, _ = _seed_sidecars(config)
    before = Path(config.hallway_file).read_bytes()
    backend = MagicMock()
    reader = MagicMock()
    lock = MagicMock()
    compute = MagicMock()
    monkeypatch.setattr(palace, "get_collection", backend)
    monkeypatch.setattr(palace_graph, "sqlite_grouped_counts_reader", reader)
    monkeypatch.setattr(cli, "_repair_lock", lock)
    monkeypatch.setattr(hallways, "compute_hallways_for_wing", compute)
    args = SimpleNamespace(palace=config.palace_path, wing=wing, rebuild=True)

    cli.cmd_hallways(args)

    backend.assert_not_called()
    reader.assert_not_called()
    lock.assert_not_called()
    compute.assert_not_called()
    assert Path(config.hallway_file).read_bytes() == before
    assert hallways.list_hallways(config=config) == records
    output = capsys.readouterr()
    assert output.out == ""
    assert len(output.err.strip().splitlines()) == 1
    assert "disabled" in output.err.lower()
    assert "stored hallways" in output.err.lower()
    assert "unchanged" in output.err.lower()
    assert "Rebuilt" not in output.err


@pytest.mark.parametrize("wing", ["alpha", None])
def test_explicit_cli_rebuild_reports_enabled_results(tmp_path, monkeypatch, capsys, wing):
    from mempalace import cli, palace

    config = _config(tmp_path)
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config._config_dir))
    collection = MagicMock()
    backend = MagicMock(return_value=collection)
    grouped_counts = MagicMock(return_value=[(1, "beta"), (2, "alpha")])
    reader = MagicMock(return_value=grouped_counts)
    lock = MagicMock(return_value=nullcontext())
    results = {"alpha": [{"id": "alpha-1"}, {"id": "alpha-2"}], "beta": [{"id": "beta-1"}]}
    compute = MagicMock(side_effect=lambda wing, **kwargs: results[wing])
    monkeypatch.setattr(palace, "get_collection", backend)
    monkeypatch.setattr(palace_graph, "sqlite_grouped_counts_reader", reader)
    monkeypatch.setattr(cli, "_repair_lock", lock)
    monkeypatch.setattr(hallways, "compute_hallways_for_wing", compute)

    cli.cmd_hallways(SimpleNamespace(palace=config.palace_path, wing=wing, rebuild=True))

    backend.assert_called_once_with(config.palace_path, create=False, read_only=True)
    lock.assert_called_once_with(config.palace_path)
    wings = ["alpha"] if wing else ["alpha", "beta"]
    assert [call.args[0] for call in compute.call_args_list] == wings
    for call in compute.call_args_list:
        assert call.kwargs["col"] is collection
        assert call.kwargs["config"].hallways_enabled is True
    if wing:
        reader.assert_not_called()
        grouped_counts.assert_not_called()
    else:
        reader.assert_called_once()
        grouped_counts.assert_called_once_with(config.palace_path, config.collection_name)
    output = capsys.readouterr()
    for name in wings:
        assert f"  {name:<36} {len(results[name]):>7} hallways" in output.out
    total = sum(len(results[name]) for name in wings)
    assert f"Rebuilt {total} hallways across {len(wings)} wing(s)." in output.out
    assert output.err == ""


def _notice_logging(monkeypatch, request, level):
    """Control ordinary CLI and configured INFO logging without pytest's handlers."""
    log_output = io.StringIO()
    handler = logging.StreamHandler(log_output)
    root_logger = logging.getLogger()
    previous_root_level = root_logger.level
    previous_hallway_level = hallways.logger.level
    root_logger.setLevel(level)
    hallways.logger.setLevel(logging.NOTSET)

    def restore_levels():
        root_logger.setLevel(previous_root_level)
        hallways.logger.setLevel(previous_hallway_level)

    request.addfinalizer(restore_levels)
    monkeypatch.setattr(root_logger, "handlers", [handler])
    monkeypatch.setattr(hallways.logger, "handlers", [])
    monkeypatch.setattr(hallways.logger, "propagate", True)
    monkeypatch.setattr(hallways.logger, "disabled", False)
    return log_output


def _mining_source(tmp_path, mode):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    if mode == "project":
        (source_dir / "app.py").write_text(
            "def main():\n    print('Keep the exact original words, including café.')\n" * 3,
            encoding="utf-8",
        )
    else:
        (source_dir / "session.txt").write_text(
            "> What should we preserve?\n"
            "Keep the exact original words, including café.\n\n"
            "> What follows?\n"
            "Continue mining while hallway construction is disabled.\n",
            encoding="utf-8",
        )
    return source_dir


@pytest.mark.parametrize("mode", ["project", "conversation"])
@pytest.mark.parametrize("logging_level", [logging.WARNING, logging.INFO])
def test_disabled_post_mine_notice_is_visible_once(
    tmp_path, monkeypatch, capsys, request, mode, logging_level
):
    config = _config(tmp_path, hallways_enabled=False)
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config._config_dir))
    source_dir = _mining_source(tmp_path, mode)
    _seed_sidecars(config)
    before = Path(config.hallway_file).read_bytes()
    log_output = _notice_logging(monkeypatch, request, logging_level)
    if mode == "project":
        miner.mine(str(source_dir), config.palace_path, wing_override="alpha")
    else:
        convo_miner.mine_convos(str(source_dir), config.palace_path, wing="alpha")

    output = capsys.readouterr()
    assert "Drawers filed: 0" not in output.out
    assert "hallway construction disabled" not in output.out.lower()
    logged = log_output.getvalue()
    notices = output.err.lower().count("hallway construction disabled")
    notices += logged.lower().count("hallway construction disabled")
    assert notices == 1
    if logging_level == logging.WARNING:
        assert "hallway construction disabled" in output.err.lower()
        assert "hallway construction disabled" not in logged.lower()
    else:
        assert "hallway construction disabled" in logged.lower()
        assert "hallway construction disabled" not in output.err.lower()
    assert Path(config.hallway_file).read_bytes() == before


@pytest.mark.parametrize(
    "logging_setup, fallback_expected",
    [
        ("warning-handler", True),
        ("null-handler", True),
        ("disabled-logger", True),
        ("globally-disabled", True),
        ("no-propagation", True),
        ("child-info-root-warning", False),
    ],
)
def test_conversation_notice_survives_unavailable_info_logging(
    tmp_path, monkeypatch, capsys, request, logging_setup, fallback_expected
):
    config = _config(tmp_path, hallways_enabled=False)
    log_output = _notice_logging(monkeypatch, request, logging.INFO)
    root_logger = logging.getLogger()
    if logging_setup == "warning-handler":
        root_logger.handlers[0].setLevel(logging.WARNING)
    elif logging_setup == "null-handler":
        monkeypatch.setattr(root_logger, "handlers", [logging.NullHandler()])
    elif logging_setup == "disabled-logger":
        monkeypatch.setattr(hallways.logger, "disabled", True)
    elif logging_setup == "no-propagation":
        monkeypatch.setattr(hallways.logger, "propagate", False)
    elif logging_setup == "child-info-root-warning":
        root_logger.setLevel(logging.WARNING)
        hallways.logger.setLevel(logging.INFO)

    previous_disable = root_logger.manager.disable
    try:
        if logging_setup == "globally-disabled":
            logging.disable(logging.INFO)
        convo_miner._compute_hallways_for_wing_safe("alpha", MagicMock(), 1, config=config)
    finally:
        if logging_setup == "globally-disabled":
            logging.disable(previous_disable)

    output = capsys.readouterr()
    assert output.out == ""
    logged = log_output.getvalue()
    assert (
        output.err.lower().count("hallway construction disabled")
        + logged.lower().count("hallway construction disabled")
        == 1
    )
    assert ("hallway construction disabled" in output.err.lower()) is fallback_expected
    assert ("hallway construction disabled" in logged.lower()) is not fallback_expected


@pytest.mark.parametrize("mode", ["project", "conversation"])
def test_disabled_dry_run_has_no_hallway_notice(tmp_path, monkeypatch, capsys, request, mode):
    config = _config(tmp_path, hallways_enabled=False)
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config._config_dir))
    source_dir = _mining_source(tmp_path, mode)
    log_output = _notice_logging(monkeypatch, request, logging.INFO)
    if mode == "project":
        miner.mine(str(source_dir), config.palace_path, wing_override="alpha", dry_run=True)
    else:
        convo_miner.mine_convos(str(source_dir), config.palace_path, wing="alpha", dry_run=True)

    output = capsys.readouterr()
    assert "DRY RUN" in output.out
    assert "hallway" not in output.err.lower()
    assert "hallway construction disabled" not in log_output.getvalue().lower()


@pytest.mark.parametrize("logging_level", [logging.WARNING, logging.INFO])
def test_conversation_without_new_drawers_has_no_hallway_notice(
    tmp_path, monkeypatch, capsys, request, logging_level
):
    config = _config(tmp_path, hallways_enabled=False)
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(config._config_dir))
    source_dir = _mining_source(tmp_path, "conversation")
    convo_miner.mine_convos(str(source_dir), config.palace_path, wing="alpha")
    capsys.readouterr()
    log_output = _notice_logging(monkeypatch, request, logging_level)

    convo_miner.mine_convos(str(source_dir), config.palace_path, wing="alpha")

    output = capsys.readouterr()
    assert "Drawers filed: 0" in output.out
    assert "hallway" not in output.err.lower()
    assert "hallway construction disabled" not in log_output.getvalue().lower()
