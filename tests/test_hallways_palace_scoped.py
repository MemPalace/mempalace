"""Tests for the palace-scoped hallway-file migration.

The pre-3.4 hallway store was hardcoded at ``~/.mempalace/hallways.json``
regardless of the configured ``palace_path``, so two palaces on one host
silently shared one file. This file covers the migration: ``hallways.py``
now resolves the path through ``MempalaceConfig.hallway_file`` (sibling of
``palace_path``), mirroring the 3.3.6 tunnel-file migration in
``palace_graph._get_tunnel_file``.

Style and structure mirror ``tests/test_palace_graph_tunnels.py``'s
analogous coverage for tunnels (orphaned-legacy warning, same-path
no-warning, palace_path-follows behavior).
"""

import logging
import os
from unittest.mock import MagicMock, patch

with patch.dict("sys.modules", {"chromadb": MagicMock()}):
    from mempalace import hallways as hallways_mod
    from mempalace.config import DEFAULT_PALACE_PATH, MempalaceConfig


# =============================================================================
# Resolver: MempalaceConfig.hallway_file + _get_hallway_file
# =============================================================================


class TestHallwayFileResolution:
    def test_default_hallway_file_is_sibling_of_default_palace(self):
        cfg = MempalaceConfig()
        expected = os.path.join(os.path.dirname(DEFAULT_PALACE_PATH), "hallways.json")
        assert cfg.hallway_file == expected
        assert hallways_mod._get_hallway_file(cfg) == expected

    def test_hallway_file_follows_palace_path(self, tmp_path):
        """Custom palace_path → hallway sits beside the palace, not at the
        hardcoded legacy location."""
        custom_dir = tmp_path / "custom-palace"
        cfg = MempalaceConfig(config_dir=tmp_path)
        cfg._file_config["palace_path"] = str(custom_dir)
        assert cfg.hallway_file == str(tmp_path / "hallways.json")
        assert hallways_mod._get_hallway_file(cfg) == str(tmp_path / "hallways.json")

    def test_palace_env_var_redirects_hallway_file(self, tmp_path, monkeypatch):
        """MEMPALACE_PALACE_PATH must redirect the hallway file too."""
        custom_palace = tmp_path / "envpalace" / "palace"
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(custom_palace))
        cfg = MempalaceConfig()
        assert cfg.hallway_file == str(tmp_path / "envpalace" / "hallways.json")


# =============================================================================
# Orphan detection: legacy file present, configured file missing
# =============================================================================


class TestLegacyHallwayFileDetection:
    def test_load_hallways_warns_on_orphaned_legacy_file(self, tmp_path, monkeypatch, caplog):
        """When the configured hallway file is missing but a legacy file
        exists at a different path, _load_hallways logs a one-line warning
        naming both paths and returns []. Critically, it does NOT
        auto-migrate — silent merging risks clobbering newer data."""
        configured = tmp_path / "configured" / "hallways.json"
        legacy = tmp_path / "legacy" / "hallways.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            '{"schema_version": 1, "hallways": ['
            '{"id":"orphan","wing":"a","entity_a":"Alice",'
            '"entity_b":"Bob","co_occurrence_count":2,"rooms":["r"]}'
            "]}",
            encoding="utf-8",
        )

        # Point the module constant at the patched-legacy path so the
        # back-compat shim treats it as "legacy, defer to resolver".
        monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **kw: str(configured))
        monkeypatch.setattr(hallways_mod, "_legacy_hallway_file", lambda: str(legacy))

        with caplog.at_level(logging.WARNING, logger="mempalace_hallways"):
            result = hallways_mod._load_hallways()

        assert result == [], "must not auto-migrate from legacy file"
        assert str(legacy) in caplog.text
        assert str(configured) in caplog.text

    def test_no_legacy_warning_when_paths_match(self, tmp_path, monkeypatch, caplog):
        """If configured and legacy resolve to the same path (default install),
        we must not emit a misleading 'legacy file ignored' warning when the
        file simply doesn't exist yet."""
        same = tmp_path / "hallways.json"
        monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **kw: str(same))
        monkeypatch.setattr(hallways_mod, "_legacy_hallway_file", lambda: str(same))

        with caplog.at_level(logging.WARNING, logger="mempalace_hallways"):
            assert hallways_mod._load_hallways() == []

        assert "Legacy hallways file" not in caplog.text


# =============================================================================
# Multi-palace isolation: two palaces no longer share the file
# =============================================================================


class TestMultiPalaceIsolation:
    def test_two_palaces_get_distinct_hallway_files(self, tmp_path, monkeypatch):
        """The original bug: switching MEMPALACE_PALACE_PATH between two
        palace dirs must produce two distinct hallway files, not one shared.
        """
        palace_a = tmp_path / "a" / "palace"
        palace_b = tmp_path / "b" / "palace"
        palace_a.mkdir(parents=True)
        palace_b.mkdir(parents=True)

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace_a))
        file_a = MempalaceConfig().hallway_file

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace_b))
        file_b = MempalaceConfig().hallway_file

        assert file_a != file_b
        assert file_a == str(tmp_path / "a" / "hallways.json")
        assert file_b == str(tmp_path / "b" / "hallways.json")

    def test_save_then_load_under_different_palace_returns_empty(self, tmp_path, monkeypatch):
        """End-to-end: writing hallways under palace-A and then loading under
        palace-B must NOT return palace-A's records. This is the regression
        guard for the original bug."""
        palace_a = tmp_path / "a" / "palace"
        palace_b = tmp_path / "b" / "palace"
        palace_a.mkdir(parents=True)
        palace_b.mkdir(parents=True)

        # Pin the legacy-file lookup to a temp path so the legacy-warning
        # branch never checks the host's real ~/.mempalace/hallways.json.
        monkeypatch.setattr(
            hallways_mod,
            "_legacy_hallway_file",
            lambda: str(tmp_path / "legacy-hallways.json"),
        )

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace_a))
        hallways_mod._save_hallways(
            [
                {
                    "id": "h_from_a",
                    "wing": "wing_a",
                    "entity_a": "Alice",
                    "entity_b": "Bob",
                    "co_occurrence_count": 2,
                    "rooms": ["room_a"],
                }
            ]
        )
        assert os.path.exists(str(tmp_path / "a" / "hallways.json"))

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace_b))
        assert hallways_mod._load_hallways() == []
