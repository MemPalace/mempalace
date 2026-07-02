"""Tests for the ``mempalace hermes install`` CLI command helpers.

The end-to-end install flow shells out to ``pip`` and writes into the user's
Hermes home — out of scope for unit tests. These tests cover the pure helpers
that resolve paths and edit ``config.yaml``, since those carried the riskiest
review findings on PR #1684 (palace_path divergence, YAML edit brittleness,
Windows path).
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
import yaml

from mempalace.cli import (
    _atomic_write_text,
    _resolve_hermes_home,
    _resolve_install_palace_path,
    _resolve_install_python,
    _update_hermes_config_yaml,
)


# ---------------------------------------------------------------------------
# _resolve_hermes_home
# ---------------------------------------------------------------------------


def _args(hermes_home=None):
    return types.SimpleNamespace(hermes_home=hermes_home)


def test_resolve_hermes_home_uses_explicit_arg(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    explicit = tmp_path / "custom-home"
    assert _resolve_hermes_home(_args(str(explicit))) == explicit.resolve()


def test_resolve_hermes_home_uses_env_var(tmp_path, monkeypatch):
    target = tmp_path / "env-home"
    monkeypatch.setenv("HERMES_HOME", str(target))
    assert _resolve_hermes_home(_args(None)) == target


def test_resolve_hermes_home_defaults_to_dot_hermes(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert _resolve_hermes_home(_args(None)) == Path("~/.hermes").expanduser()


def test_resolve_hermes_home_refuses_cwd(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    # ``.`` resolves to CWD — would silently install plugin files there.
    with pytest.raises(SystemExit) as excinfo:
        _resolve_hermes_home(_args("."))
    assert excinfo.value.code == 2


def test_resolve_hermes_home_refuses_root(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        _resolve_hermes_home(_args("/"))
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# _resolve_install_python
# ---------------------------------------------------------------------------


def test_resolve_install_python_prefers_posix_venv(tmp_path):
    venv_python = tmp_path / "hermes-agent" / "venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/bin/sh\n")
    assert _resolve_install_python(tmp_path) == str(venv_python)


def test_resolve_install_python_falls_back_to_python_no_3(tmp_path):
    # Some venvs only ship `python` not `python3` (Windows-style scaffolds).
    p = tmp_path / "hermes-agent" / "venv" / "bin" / "python"
    p.parent.mkdir(parents=True)
    p.write_text("#!/bin/sh\n")
    assert _resolve_install_python(tmp_path) == str(p)


def test_resolve_install_python_finds_windows_layout(tmp_path):
    p = tmp_path / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    p.parent.mkdir(parents=True)
    p.write_text("#!/bin/sh\n")
    assert _resolve_install_python(tmp_path) == str(p)


def test_resolve_install_python_falls_back_to_sys_executable(tmp_path):
    import sys

    # No venv anywhere under hermes_home → fall back gracefully.
    assert _resolve_install_python(tmp_path) == sys.executable


# ---------------------------------------------------------------------------
# _resolve_install_palace_path
# ---------------------------------------------------------------------------


def test_palace_path_honors_env_var(tmp_path, monkeypatch):
    target = tmp_path / "user-chosen-palace"
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(target))
    assert _resolve_install_palace_path() == target


def test_palace_path_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("MEMPALACE_PALACE_PATH", raising=False)
    assert _resolve_install_palace_path() == Path("~/.mempalace/palace").expanduser()


def test_palace_path_ignores_empty_env_var(monkeypatch):
    # ``export MEMPALACE_PALACE_PATH=`` is intent to unset, not to use "".
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", "")
    assert _resolve_install_palace_path() == Path("~/.mempalace/palace").expanduser()


# ---------------------------------------------------------------------------
# _atomic_write_text
# ---------------------------------------------------------------------------


def test_atomic_write_text_creates_parent_dirs(tmp_path):
    target = tmp_path / "deep" / "nested" / "file.yaml"
    _atomic_write_text(target, "hello\n")
    assert target.read_text() == "hello\n"


def test_atomic_write_text_overwrites_existing(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("original\n")
    _atomic_write_text(target, "replaced\n")
    assert target.read_text() == "replaced\n"
    # No stray .tmp left behind.
    assert not (tmp_path / "f.txt.tmp").exists()


# ---------------------------------------------------------------------------
# _update_hermes_config_yaml
# ---------------------------------------------------------------------------


def test_yaml_update_adds_memory_section_when_missing(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("model: claude-opus\n")
    status, msg = _update_hermes_config_yaml(config, "mempalace")
    assert status == "updated"
    data = yaml.safe_load(config.read_text())
    assert data["memory"]["provider"] == "mempalace"
    assert data["model"] == "claude-opus"
    assert "Updated" in msg


def test_yaml_update_replaces_existing_provider(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("memory:\n  provider: honcho\n")
    status, _ = _update_hermes_config_yaml(config, "mempalace")
    assert status == "updated"
    assert yaml.safe_load(config.read_text())["memory"]["provider"] == "mempalace"


def test_yaml_update_handles_scalar_memory_value(tmp_path):
    # Original line-based heuristic produced duplicate `memory:` keys for this.
    config = tmp_path / "config.yaml"
    config.write_text("memory: ~\nmodel: x\n")
    status, _ = _update_hermes_config_yaml(config, "mempalace")
    assert status == "updated"
    data = yaml.safe_load(config.read_text())
    assert data["memory"]["provider"] == "mempalace"
    assert data["model"] == "x"
    # Exactly one top-level ``memory:`` key. The earlier heuristic produced
    # two when the original value was a scalar — yaml round-tripping rules
    # that out structurally, but assert it via the actual count to catch any
    # future regression at the serialiser level.
    text = config.read_text()
    top_level_memory = sum(
        1 for line in text.splitlines() if line.startswith("memory:") or line.rstrip() == "memory:"
    )
    assert top_level_memory == 1


def test_yaml_update_creates_file_when_missing(tmp_path):
    config = tmp_path / "config.yaml"
    assert not config.exists()
    status, _ = _update_hermes_config_yaml(config, "mempalace")
    assert status == "updated"
    assert config.exists()
    assert yaml.safe_load(config.read_text())["memory"]["provider"] == "mempalace"


def test_yaml_update_noop_when_already_set(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("memory:\n  provider: mempalace\n")
    status, msg = _update_hermes_config_yaml(config, "mempalace")
    # Distinct from "error" — caller should NOT fail the install for this.
    assert status == "noop"
    assert "already has" in msg


def test_yaml_update_rejects_non_mapping(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("- not\n- a\n- mapping\n")
    status, msg = _update_hermes_config_yaml(config, "mempalace")
    # Distinct from "noop" — install command should exit non-zero.
    assert status == "error"
    assert "not a YAML mapping" in msg


def test_yaml_update_handles_malformed_yaml(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("{ this is: not: parseable\n")
    status, msg = _update_hermes_config_yaml(config, "mempalace")
    assert status == "error"
    assert "Could not parse" in msg


def test_yaml_update_is_atomic(tmp_path):
    # The write happens via _atomic_write_text — no .tmp leaks on success.
    config = tmp_path / "config.yaml"
    config.write_text("model: x\n")
    _update_hermes_config_yaml(config, "mempalace")
    assert not (tmp_path / "config.yaml.tmp").exists()


# ---------------------------------------------------------------------------
# Backfill / live-provider routing parity.
# ---------------------------------------------------------------------------


def _load_backfill_module():
    """Load backfill.py by path — the same way the install command does.

    Tests must use this loading mode (not a package import) so they catch
    anything that only breaks under ``spec_from_file_location``.
    """
    import importlib.util

    backfill_path = (
        Path(__file__).resolve().parent.parent
        / "mempalace"
        / "integrations"
        / "hermes"
        / "backfill.py"
    )
    spec = importlib.util.spec_from_file_location("hermes_backfill", backfill_path)
    assert spec is not None and spec.loader is not None
    backfill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)
    return backfill


def test_backfill_classify_wing_delegates_to_live_provider():
    """Backfill must route wings exactly like live writes.

    ``classify_wing`` delegates to the provider's
    ``_match_wing_by_keywords`` — checks the delegation survives the
    by-path loading mode, so nobody reintroduces a drifting copy.
    """
    backfill = _load_backfill_module()

    wing_config = {"wing_ai": {"keywords": ["ai"]}}
    # Word-boundary matching: substring matching would route "said" /
    # "available" into the ai wing.
    assert backfill.classify_wing("She said rain is available", wing_config) == "wing_general"
    assert backfill.classify_wing("write some ai bindings", wing_config) == "wing_ai"
