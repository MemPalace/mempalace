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
    from mempalace.config import MempalaceConfig


# =============================================================================
# Resolving the hallway file through MempalaceConfig and _get_hallway_file
# =============================================================================


class TestHallwayFileResolution:
    def test_default_hallway_file_is_sibling_of_default_palace(self):
        # The default palace now lives at ``<config_dir>/palace`` because the
        # config dir follows the XDG Base Directory spec (#46), so its hallway
        # sibling resolves to ``<config_dir>/hallways.json``. DEFAULT_PALACE_PATH
        # is the frozen legacy constant and is no longer the default base, so the
        # expectation is derived from the resolved ``config_dir`` instead.
        cfg = MempalaceConfig()
        from mempalace import paths

        key = paths.palace_key(cfg.palace_path)
        expected = os.path.join(cfg.config_dir, f"hallways_{key}.json")
        assert cfg.hallway_file == expected
        assert hallways_mod._get_hallway_file(cfg) == expected

    def test_hallway_file_follows_palace_path(self, tmp_path):
        """Custom palace_path → hallway sits beside the palace, not at the
        hardcoded legacy location."""
        custom_dir = tmp_path / "custom-palace"
        cfg = MempalaceConfig(config_dir=tmp_path)
        cfg._file_config["palace_path"] = str(custom_dir)
        from mempalace import paths

        key = paths.palace_key(cfg.palace_path)
        assert cfg.hallway_file == str(tmp_path / f"hallways_{key}.json")
        assert hallways_mod._get_hallway_file(cfg) == str(tmp_path / f"hallways_{key}.json")

    def test_palace_env_var_redirects_hallway_file(self, tmp_path, monkeypatch):
        """MEMPALACE_PALACE_PATH must redirect the hallway file too."""
        custom_palace = tmp_path / "envpalace" / "palace"
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(custom_palace))
        cfg = MempalaceConfig()
        from mempalace import paths

        key = paths.palace_key(cfg.palace_path)
        assert cfg.hallway_file == str(tmp_path / "envpalace" / f"hallways_{key}.json")


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

        with caplog.at_level(logging.WARNING, logger="mempalace"):
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

        with caplog.at_level(logging.WARNING, logger="mempalace"):
            assert hallways_mod._load_hallways() == []

        assert "Legacy hallways file" not in caplog.text

    def test_load_hallways_warns_on_orphaned_pre_keyed_file(self, tmp_path, monkeypatch, caplog):
        """A user upgrading from a build that wrote the unkeyed sibling
        (``<parent>/hallways.json``) must see a one-line warning naming both
        paths when the per-palace-keyed file is missing — otherwise their
        existing hallways are silently orphaned. No auto-migration."""
        palace_parent = tmp_path / "palace-parent"
        palace_parent.mkdir()
        pre_keyed = palace_parent / "hallways.json"
        pre_keyed.write_text(
            '{"schema_version": 1, "hallways": ['
            '{"id":"orphan","wing":"a","entity_a":"Alice",'
            '"entity_b":"Bob","co_occurrence_count":2,"rooms":["r"]}'
            "]}",
            encoding="utf-8",
        )
        configured = palace_parent / "hallways_a1b2c3d4e5f607182930415263748596.json"

        monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **kw: str(configured))
        # Pin the older legacy constant somewhere that does not exist so
        # only the new pre-keyed branch can fire.
        monkeypatch.setattr(
            hallways_mod,
            "_legacy_hallway_file",
            lambda: str(tmp_path / "nowhere" / "hallways.json"),
        )

        with caplog.at_level(logging.WARNING, logger="mempalace"):
            result = hallways_mod._load_hallways()

        assert result == [], "must not auto-migrate from the pre-keyed sibling"
        assert str(pre_keyed) in caplog.text
        assert str(configured) in caplog.text
        assert "Legacy hallways file" not in caplog.text

    def test_load_hallways_warns_on_orphaned_pre_keyed_with_sqlite(
        self, tmp_path, monkeypatch, caplog
    ):
        """When a pre-keyed ``.sqlite3`` store sits beside the JSON, the
        warning appends a rename hint for it (the keyed DB is derived via
        ``os.path.splitext``, so both files must move together)."""
        palace_parent = tmp_path / "palace-parent"
        palace_parent.mkdir()
        pre_keyed = palace_parent / "hallways.json"
        pre_keyed.write_text('{"hallways": []}', encoding="utf-8")
        (palace_parent / "hallways.sqlite3").write_bytes(b"SQLite3")
        configured = palace_parent / "hallways_a1b2c3d4e5f607182930415263748596.json"

        monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **kw: str(configured))
        monkeypatch.setattr(
            hallways_mod,
            "_legacy_hallway_file",
            lambda: str(tmp_path / "nowhere" / "hallways.json"),
        )

        with caplog.at_level(logging.WARNING, logger="mempalace"):
            result = hallways_mod._load_hallways()

        assert result == []
        assert str(pre_keyed) in caplog.text
        assert "hallways.sqlite3" in caplog.text

    def test_no_pre_keyed_warning_when_paths_match(self, tmp_path, monkeypatch, caplog):
        """If the resolved path is itself the unkeyed basename (default
        pre-keying install), the new check must not emit a redundant
        'ignored' warning."""
        same = tmp_path / "hallways.json"
        monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **kw: str(same))
        monkeypatch.setattr(
            hallways_mod,
            "_legacy_hallway_file",
            lambda: str(tmp_path / "nowhere" / "hallways.json"),
        )

        with caplog.at_level(logging.WARNING, logger="mempalace"):
            assert hallways_mod._load_hallways() == []

        assert "per-palace name" not in caplog.text


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
        from mempalace import paths as paths_mod

        key_a = paths_mod.palace_key(str(palace_a))
        key_b = paths_mod.palace_key(str(palace_b))
        assert file_a == str(tmp_path / "a" / f"hallways_{key_a}.json")
        assert file_b == str(tmp_path / "b" / f"hallways_{key_b}.json")

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
        from mempalace import paths as _paths_mod

        _key_a = _paths_mod.palace_key(str(palace_a))
        assert os.path.exists(str(tmp_path / "a" / f"hallways_{_key_a}.sqlite3"))

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace_b))
        assert hallways_mod._load_hallways() == []

    # ── #2710 regression: two palaces in the SAME parent directory ──────

    def test_same_parent_dir_palaces_get_distinct_files(self, tmp_path, monkeypatch):
        """Regression for #2710: two palace dirs whose *parent* is the same
        directory (e.g. ``~/work/palaceA`` and ``~/work/palaceB``) must not
        share one hallway file. Before the fix the basename was a constant
        ``hallways.json`` so both palaces resolved to the same path."""
        shared_parent = tmp_path / "shared-parent"
        shared_parent.mkdir(parents=True)
        palace_a = shared_parent / "palaceA"
        palace_b = shared_parent / "palaceB"
        palace_a.mkdir()
        palace_b.mkdir()

        cfg = MempalaceConfig(config_dir=str(tmp_path / "cfg"))
        cfg._file_config["palace_path"] = str(palace_a)
        file_a = cfg.hallway_file

        cfg._file_config["palace_path"] = str(palace_b)
        file_b = cfg.hallway_file

        assert file_a != file_b, (
            f"#2710: two palaces in same parent dir resolved to the same hallway file: {file_a!r}"
        )
        # Both files must live in the shared parent (sibling), but with
        # distinct per-palace keys.
        assert os.path.dirname(file_a) == str(shared_parent)
        assert os.path.dirname(file_b) == str(shared_parent)
        assert os.path.basename(file_a) != os.path.basename(file_b)

    def test_same_parent_dir_palaces_tunnel_files_distinct(self, tmp_path):
        """Regression for #2710: the tunnel file must use the same
        same-parent-dir isolation as the hallway file."""
        shared_parent = tmp_path / "shared-parent"
        shared_parent.mkdir(parents=True)
        palace_a = shared_parent / "palaceA"
        palace_b = shared_parent / "palaceB"
        palace_a.mkdir()
        palace_b.mkdir()

        cfg = MempalaceConfig(config_dir=str(tmp_path / "cfg"))
        cfg._file_config["palace_path"] = str(palace_a)
        tunn_a = cfg.tunnel_file

        cfg._file_config["palace_path"] = str(palace_b)
        tunn_b = cfg.tunnel_file

        assert tunn_a != tunn_b, (
            f"#2710: two palaces in same parent dir resolved to the same tunnel file: {tunn_a!r}"
        )
        assert os.path.dirname(tunn_a) == str(shared_parent)
        assert os.path.basename(tunn_a) != os.path.basename(tunn_b)
