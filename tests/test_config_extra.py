"""Extra tests for mempalace.config to cover remaining gaps."""

import json
import os

from mempalace.config import MempalaceConfig


def test_config_bad_json(tmp_path):
    """Bad JSON in config file falls back to empty."""
    (tmp_path / "config.json").write_text("not json", encoding="utf-8")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.palace_path  # still returns default


def test_people_map_from_file(tmp_path):
    (tmp_path / "people_map.json").write_text(json.dumps({"bob": "Robert"}), encoding="utf-8")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.people_map == {"bob": "Robert"}


def test_people_map_bad_json(tmp_path):
    (tmp_path / "people_map.json").write_text("bad", encoding="utf-8")
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.people_map == {}


def test_people_map_missing(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.people_map == {}


def test_topic_wings_default(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert isinstance(cfg.topic_wings, list)
    assert "emotions" in cfg.topic_wings


def test_hall_keywords_default(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert isinstance(cfg.hall_keywords, dict)
    assert "technical" in cfg.hall_keywords


def test_init_idempotent(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg.init()
    cfg.init()  # second call should not overwrite
    with open(tmp_path / "config.json") as f:
        data = json.load(f)
    assert "palace_path" in data


def test_save_people_map(tmp_path):
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    result = cfg.save_people_map({"alice": "Alice Smith"})
    assert result.exists()
    with open(result) as f:
        data = json.load(f)
    assert data["alice"] == "Alice Smith"


def test_env_mempal_palace_path(tmp_path):
    """MEMPAL_PALACE_PATH (legacy) should also work."""
    os.environ.pop("MEMPALACE_PALACE_PATH", None)
    raw = "/legacy/path"
    os.environ["MEMPAL_PALACE_PATH"] = raw
    try:
        cfg = MempalaceConfig(config_dir=str(tmp_path))
        # palace_path is normalized via abspath + expanduser — compare
        # against the normalized form so the test is portable between
        # POSIX (no-op) and Windows (prepends current drive letter).
        assert cfg.palace_path == os.path.abspath(os.path.expanduser(raw))
    finally:
        del os.environ["MEMPAL_PALACE_PATH"]


def test_collection_name_from_config(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"collection_name": "custom_col"}), encoding="utf-8"
    )
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.collection_name == "custom_col"


# ── #2451 status_protocol ────────────────────────────────────────────────────
# Operator-supplied protocol text: `mempalace_status`'s "protocol" field is
# otherwise hardcoded (PALACE_PROTOCOL in tools_read.py). Mirrors the
# topic_wings / hall_keywords pattern: env takes precedence over config file,
# and a None / empty value means "use the built-in default."


def _write_config(tmp_path, data: dict):
    (tmp_path / "config.json").write_text(json.dumps(data), encoding="utf-8")


def test_status_protocol_defaults_to_none(tmp_path, monkeypatch):
    """`status_protocol` defaults to None when unset. That's what tells a
    consumer (mcp_server/tools_read.py) to fall back to the built-in
    PALACE_PROTOCOL — so the built-in text isn't copied into config.json."""
    monkeypatch.delenv("MEMPALACE_STATUS_PROTOCOL", raising=False)
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.status_protocol is None


def test_status_protocol_from_file(tmp_path, monkeypatch):
    """An operator-supplied override in config.json wins over the built-in."""
    monkeypatch.delenv("MEMPALACE_STATUS_PROTOCOL", raising=False)
    _write_config(tmp_path, {"status_protocol": "CUSTOM PROTOCOL TEXT"})
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.status_protocol == "CUSTOM PROTOCOL TEXT"


def test_status_protocol_env_overrides_file(tmp_path, monkeypatch):
    """MEMPALACE_STATUS_PROTOCOL beats config.json (mirror the palace_path
    env > file precedence in the same class)."""
    monkeypatch.setenv("MEMPALACE_STATUS_PROTOCOL", "ENV WINS")
    _write_config(tmp_path, {"status_protocol": "FILE LOSES"})
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.status_protocol == "ENV WINS"


def test_status_protocol_blank_env_falls_through_to_file(tmp_path, monkeypatch):
    """A whitespace-only MEMPALACE_STATUS_PROTOCOL must not mask a real
    config.json value. Previously the property returned ``None`` the instant
    the env var was present but blank, so the file value was never read.
    A blank env is now treated as unset, so the config file wins."""
    monkeypatch.setenv("MEMPALACE_STATUS_PROTOCOL", "   ")
    _write_config(tmp_path, {"status_protocol": "FILE WINS"})
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.status_protocol == "FILE WINS"


def test_status_protocol_blank_string_is_treated_as_unset(tmp_path, monkeypatch):
    """Whitespace / empty strings from file or env mean "use the built-in
    default" — consistent with other optional-string settings in this class
    (e.g. palace_path env branch)."""
    monkeypatch.setenv("MEMPALACE_STATUS_PROTOCOL", "   ")
    _write_config(tmp_path, {"status_protocol": ""})
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.status_protocol is None
