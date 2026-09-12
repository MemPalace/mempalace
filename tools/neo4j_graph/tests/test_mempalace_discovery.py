from argparse import Namespace
from pathlib import Path

import pytest

from mempalace_graph.config import ConfigError, load_config
from mempalace_graph.mempalace_discovery import discover_mempalace


def ns(home: Path) -> Namespace:
    return Namespace(
        mempalace_home=str(home),
        mempalace_palace_dir=None,
        mempalace_knowledge_graph_db=None,
        mempalace_chroma_db=None,
        mempalace_write_log=None,
        mempalace_config=None,
        sync_state=None,
        delete_mode=None,
        neo4j_uri=None,
        neo4j_username=None,
        neo4j_password=None,
        neo4j_database=None,
        store_content=False,
        store_snippet=None,
        snippet_chars=None,
        watch_debounce_seconds=None,
    )


def test_discovery_finds_files(tmp_path: Path) -> None:
    palace = tmp_path / "palace"
    palace.mkdir()
    (palace / "knowledge_graph.sqlite3").write_bytes(b"db")
    (palace / "chroma.sqlite3").write_bytes(b"chroma")
    (palace / "config.json").write_text("{}")
    (tmp_path / "wal").mkdir()
    (tmp_path / "wal" / "write_log.jsonl").write_text("{}\n")
    cfg = load_config(ns(tmp_path), env_file=None)
    report = discover_mempalace(cfg)
    assert report.knowledge_graph_db.exists
    assert report.write_log.path == tmp_path / "wal" / "write_log.jsonl"


def test_missing_mempalace_handling(tmp_path: Path) -> None:
    cfg = load_config(ns(tmp_path / "missing"), env_file=None)
    with pytest.raises(ConfigError):
        cfg.validate_mempalace_paths()
