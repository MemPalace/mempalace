import io
import json
from unittest.mock import MagicMock

import pytest

import mempalace.hooks_cli as hooks_cli
from mempalace.config import MempalaceConfig


def _config(tmp_path, monkeypatch, hooks):
    monkeypatch.delenv("MEMPALACE_HOOKS_AUTO_SAVE", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return MempalaceConfig(config_dir=str(tmp_path))


def _install_config(monkeypatch, cfg):
    monkeypatch.setattr(hooks_cli, "MempalaceConfig", lambda: cfg)


def test_per_hook_controls_default_to_enabled(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, {})
    _install_config(monkeypatch, cfg)
    assert hooks_cli._hook_auto_save_enabled("stop") is True
    assert hooks_cli._hook_auto_save_enabled("precompact") is True
    assert hooks_cli._hook_auto_save_enabled("session-end") is True


def test_per_hook_controls_are_independent(tmp_path, monkeypatch):
    cfg = _config(
        tmp_path,
        monkeypatch,
        {"stop": False, "pre_compact": True, "session_end": False},
    )
    _install_config(monkeypatch, cfg)
    assert hooks_cli._hook_auto_save_enabled("stop") is False
    assert hooks_cli._hook_auto_save_enabled("precompact") is True
    assert hooks_cli._hook_auto_save_enabled("session-end") is False


def test_master_auto_save_false_disables_every_hook(tmp_path, monkeypatch):
    cfg = _config(
        tmp_path,
        monkeypatch,
        {"auto_save": False, "stop": True, "pre_compact": True, "session_end": True},
    )
    _install_config(monkeypatch, cfg)
    assert hooks_cli._hook_auto_save_enabled("stop") is False
    assert hooks_cli._hook_auto_save_enabled("precompact") is False
    assert hooks_cli._hook_auto_save_enabled("session-end") is False


def test_env_master_true_still_allows_per_hook_opt_out(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(
        json.dumps({"hooks": {"auto_save": False, "stop": False}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMPALACE_HOOKS_AUTO_SAVE", "true")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    _install_config(monkeypatch, cfg)
    assert cfg.hooks_auto_save is True
    assert hooks_cli._hook_auto_save_enabled("stop") is False
    assert hooks_cli._hook_auto_save_enabled("precompact") is True


def test_non_boolean_per_hook_value_preserves_enabled_behavior(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, {"stop": "false"})
    _install_config(monkeypatch, cfg)
    assert hooks_cli._hook_auto_save_enabled("stop") is True


@pytest.mark.parametrize(
    ("hook_name", "config_key"),
    [("stop", "stop"), ("precompact", "pre_compact")],
)
def test_disabled_hook_short_circuits_before_handler(
    tmp_path, monkeypatch, hook_name, config_key
):
    cfg = _config(tmp_path, monkeypatch, {config_key: False})
    _install_config(monkeypatch, cfg)
    output = []
    handler = MagicMock()
    monkeypatch.setattr(hooks_cli, "_output", output.append)
    monkeypatch.setattr(
        hooks_cli,
        "hook_stop" if hook_name == "stop" else "hook_precompact",
        handler,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s"})))

    hooks_cli.run_hook(hook_name, "claude-code")

    assert output == [{}]
    handler.assert_not_called()


def test_disabled_session_end_skips_handler_but_keeps_cleanup(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, {"session_end": False})
    _install_config(monkeypatch, cfg)
    output = []
    handler = MagicMock()
    cleanup = MagicMock()
    monkeypatch.setattr(hooks_cli, "_output", output.append)
    monkeypatch.setattr(hooks_cli, "hook_session_end", handler)
    monkeypatch.setattr(hooks_cli, "_clear_session_last_save", cleanup)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "session-7"})))

    hooks_cli.run_hook("session-end", "claude-code")

    assert output == [{}]
    handler.assert_not_called()
    cleanup.assert_called_once_with("session-7")


def test_enabled_hook_dispatches_normally(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch, {"stop": True})
    _install_config(monkeypatch, cfg)
    handler = MagicMock()
    payload = {"session_id": "s", "transcript_path": ""}
    monkeypatch.setattr(hooks_cli, "hook_stop", handler)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    hooks_cli.run_hook("stop", "claude-code")

    handler.assert_called_once_with(payload, "claude-code")
