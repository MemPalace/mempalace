"""TDD coverage for issue #185 enhancement: configurable init config path."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

from mempalace.miner import load_config


def _base_args(project_dir: Path, **overrides):
    args = argparse.Namespace(
        dir=str(project_dir),
        yes=True,
        auto_mine=False,
        empty=False,
        no_llm=True,
        project_config_dir=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_resolve_init_project_config_dir_defaults_to_project_root_when_yes(tmp_path):
    from mempalace.cli import _resolve_init_project_config_dir

    project = tmp_path / "project"
    project.mkdir()
    args = _base_args(project, yes=True, project_config_dir=None)

    resolved = _resolve_init_project_config_dir(args=args, project_dir=str(project))
    assert resolved == project.resolve()


def test_resolve_init_project_config_dir_prompts_for_custom_path(tmp_path):
    from mempalace.cli import _resolve_init_project_config_dir

    project = tmp_path / "project"
    project.mkdir()
    args = _base_args(project, yes=False, project_config_dir=None)

    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=["2", ".mempalace/config"]),
    ):
        resolved = _resolve_init_project_config_dir(args=args, project_dir=str(project))

    assert resolved == (project / ".mempalace" / "config").resolve()


def test_resolve_init_project_config_dir_eof_falls_back_to_project_root(tmp_path):
    from mempalace.cli import _resolve_init_project_config_dir

    project = tmp_path / "project"
    project.mkdir()
    args = _base_args(project, yes=False, project_config_dir=None)

    with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=EOFError):
        resolved = _resolve_init_project_config_dir(args=args, project_dir=str(project))

    assert resolved == project.resolve()


def test_cmd_init_writes_entities_to_selected_config_dir_and_passes_room_target(tmp_path):
    from mempalace.cli import cmd_init

    project = tmp_path / "my-project"
    project.mkdir()
    (project / "README.md").write_text("hello", encoding="utf-8")
    args = _base_args(project, project_config_dir=".mempalace/config")
    expected_dir = (project / ".mempalace" / "config").resolve()

    detected = {"people": ["Alice"], "projects": [], "topics": [], "uncertain": []}
    confirmed = {"people": ["Alice"], "projects": [], "topics": []}

    with (
        patch("mempalace.cli.MempalaceConfig") as mock_cfg_cls,
        patch("mempalace.cli._run_pass_zero", return_value=None),
        patch("mempalace.project_scanner.discover_entities", return_value=detected),
        patch("mempalace.entity_detector.confirm_entities", return_value=confirmed),
        patch("mempalace.miner.add_to_known_entities", return_value=str(tmp_path / "registry.json")),
        patch("mempalace.room_detector_local.detect_rooms_local") as mock_rooms,
        patch("mempalace.cli._maybe_run_mine_after_init"),
    ):
        cfg = MagicMock()
        cfg.palace_path = str(tmp_path / "palace")
        mock_cfg_cls.return_value = cfg
        cmd_init(args)

    assert (expected_dir / "entities.json").exists()
    assert not (project / "entities.json").exists()
    pointer = project / ".mempalace" / "config_dir.txt"
    assert pointer.exists()
    mock_rooms.assert_called_once_with(
        project_dir=str(project),
        yes=True,
        config_dir=str(expected_dir),
    )


def test_load_config_uses_project_config_pointer_file(tmp_path):
    project = tmp_path / "proj"
    config_dir = project / ".mempalace" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "mempalace.yaml").write_text("wing: pointer_wing\nrooms: []\n", encoding="utf-8")

    marker_dir = project / ".mempalace"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "config_dir.txt").write_text(".mempalace/config\n", encoding="utf-8")

    loaded = load_config(str(project))
    assert loaded["wing"] == "pointer_wing"


def test_cmd_init_empty_skips_detection_and_mine(tmp_path):
    from mempalace.cli import cmd_init

    project = tmp_path / "empty-project"
    project.mkdir()
    args = _base_args(project, empty=True, project_config_dir=".mempalace/config")
    expected_dir = (project / ".mempalace" / "config").resolve()

    with (
        patch("mempalace.cli.MempalaceConfig") as mock_cfg_cls,
        patch("mempalace.cli._run_pass_zero") as mock_pass_zero,
        patch("mempalace.project_scanner.discover_entities") as mock_discover,
        patch("mempalace.room_detector_local.detect_rooms_local") as mock_detect_rooms,
        patch("mempalace.room_detector_local.save_config") as mock_save_config,
        patch("mempalace.cli._maybe_run_mine_after_init") as mock_after_init_mine,
    ):
        cfg = MagicMock()
        cfg.palace_path = str(tmp_path / "palace")
        mock_cfg_cls.return_value = cfg
        cmd_init(args)

    mock_pass_zero.assert_not_called()
    mock_discover.assert_not_called()
    mock_detect_rooms.assert_not_called()
    mock_after_init_mine.assert_not_called()
    mock_save_config.assert_called_once_with(
        project_dir=str(project),
        project_name="empty_project",
        rooms=[],
        config_dir=str(expected_dir),
    )
    assert (project / ".mempalace" / "config_dir.txt").exists()
