"""MEMPALACE_CONFIG_DIR — the config-directory lever, mirroring MEMPALACE_PALACE_PATH.

A host that embeds mempalace as a sidecar spawns it as a process, so it can pass env and argv and
nothing else. Without an env lever the config directory hardwires to ~/.mempalace and every spawned
process reads a user-level config it does not own — carrying backend, collection_name,
embedding_model, write_routing and ~20 more keys across a boundary the embedding was meant to hold.

These cover the resolution order the docstring promises: explicit arg > env > default.
"""

import json
import os

from mempalace.config import MempalaceConfig


def test_env_var_sets_the_config_dir(tmp_path, monkeypatch):
    """MEMPALACE_CONFIG_DIR redirects both config-dir-derived files."""
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path))
    cfg = MempalaceConfig()
    assert cfg._config_dir == tmp_path
    assert cfg._config_file == tmp_path / "config.json"
    assert cfg._people_map_file == tmp_path / "people_map.json"


def test_env_var_config_file_actually_loads(tmp_path, monkeypatch):
    """A config.json under the env-named dir feeds the resolved values — the point of the lever."""
    (tmp_path / "config.json").write_text(json.dumps({"collection_name": "sidecar_only"}))
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(tmp_path))
    cfg = MempalaceConfig()
    assert cfg._file_config.get("collection_name") == "sidecar_only"


def test_explicit_arg_outranks_the_env_var(tmp_path, monkeypatch):
    """Explicit arg > env, matching how palace_path resolves an explicit --palace over its env."""
    env_dir = tmp_path / "from_env"
    arg_dir = tmp_path / "from_arg"
    env_dir.mkdir()
    arg_dir.mkdir()
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", str(env_dir))
    cfg = MempalaceConfig(config_dir=str(arg_dir))
    assert cfg._config_dir == arg_dir


def test_unset_env_keeps_the_home_default(monkeypatch):
    """Absent the env var the directory stands where it always stood — behaviour-preserving."""
    monkeypatch.delenv("MEMPALACE_CONFIG_DIR", raising=False)
    cfg = MempalaceConfig()
    assert cfg._config_dir == type(cfg._config_dir)(os.path.expanduser("~/.mempalace"))


def test_blank_env_falls_through_to_the_default(monkeypatch):
    """An exported-but-empty value reads as unset rather than as the current directory."""
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", "   ")
    cfg = MempalaceConfig()
    assert cfg._config_dir == type(cfg._config_dir)(os.path.expanduser("~/.mempalace"))


def test_env_var_expands_a_tilde(monkeypatch):
    """`~` expands, so an operator may export a home-relative path."""
    monkeypatch.setenv("MEMPALACE_CONFIG_DIR", "~/some-sidecar-root")
    cfg = MempalaceConfig()
    assert str(cfg._config_dir) == os.path.expanduser("~/some-sidecar-root")
