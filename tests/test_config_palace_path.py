"""Tests for palace_path tilde expansion in MempalaceConfig."""

import os
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
