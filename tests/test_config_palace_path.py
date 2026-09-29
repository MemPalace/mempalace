"""Tests for palace_path tilde expansion in MempalaceConfig."""

import json
import os
import tempfile
from mempalace.config import MempalaceConfig


def test_palace_path_expands_tilde_from_config_file():
    """palace_path must expand ~ even when read from config.json, not env."""
    cfg = MempalaceConfig()
    cfg._file_config["palace_path"] = "~/.mempalace/palace"
    result = cfg.palace_path
    assert not result.startswith("~"), (
        f"palace_path returned unexpanded tilde: {result!r}. "
        "This causes mempalace mine to create a literal '~' directory "
        "relative to CWD instead of writing to the home directory."
    )
    assert result == os.path.expanduser("~/.mempalace/palace")


def test_palace_path_expands_tilde_nested():
    """Nested tilde paths (e.g. ~/custom/palace) are also expanded."""
    cfg = MempalaceConfig()
    cfg._file_config["palace_path"] = "~/custom/mempalace"
    result = cfg.palace_path
    assert not result.startswith("~")
    assert result == os.path.expanduser("~/custom/mempalace")


def test_palace_path_absolute_unchanged():
    """Absolute paths pass through without modification."""
    cfg = MempalaceConfig()
    cfg._file_config["palace_path"] = "/tmp/test_palace"
    assert cfg.palace_path == "/tmp/test_palace"


def test_init_persists_constructor_override_not_default():
    """init() must persist the resolved palace_path, not the hardcoded default.

    `mempalace --palace <custom> init` passes palace_path via the constructor
    (mirrored from cli.py's env-var write for cmd_init). The persisted
    config.json must record that custom path so a later invocation with no
    --palace flag (e.g. `mempalace status`) still finds it.
    """
    config_dir = tempfile.mkdtemp()
    custom_palace = os.path.join(tempfile.mkdtemp(), "custom-palace")
    cfg = MempalaceConfig(config_dir=config_dir, palace_path=custom_palace)
    assert cfg.palace_path == custom_palace

    cfg.init()

    with open(os.path.join(config_dir, "config.json")) as f:
        saved = json.load(f)
    assert saved["palace_path"] == custom_palace

    # A later invocation with no override must read the persisted path back.
    later_cfg = MempalaceConfig(config_dir=config_dir)
    assert later_cfg.palace_path == custom_palace


def test_init_persists_env_var_palace_path():
    """init() must persist a MEMPALACE_PALACE_PATH override, not the default.

    cmd_init sets this env var before constructing MempalaceConfig() when
    --palace is passed (cli.py:308); init() must write what it resolved to,
    not the module-level default.
    """
    config_dir = tempfile.mkdtemp()
    custom_palace = os.path.join(tempfile.mkdtemp(), "env-palace")
    os.environ["MEMPALACE_PALACE_PATH"] = custom_palace
    try:
        cfg = MempalaceConfig(config_dir=config_dir)
        cfg.init()
    finally:
        del os.environ["MEMPALACE_PALACE_PATH"]

    with open(os.path.join(config_dir, "config.json")) as f:
        saved = json.load(f)
    assert saved["palace_path"] == custom_palace

    # A later invocation with no --palace and no env var must still resolve
    # to the persisted custom path, not silently fall back to the default.
    later_cfg = MempalaceConfig(config_dir=config_dir)
    assert later_cfg.palace_path == custom_palace
