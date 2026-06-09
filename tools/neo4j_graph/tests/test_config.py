from argparse import Namespace
from pathlib import Path

from mempalace_graph.config import expand_path, load_config


def test_path_expansion() -> None:
    assert str(expand_path("~/x")).startswith(str(Path.home()))


def test_config_cli_overrides(tmp_path: Path) -> None:
    args = Namespace(
        mempalace_home=str(tmp_path),
        mempalace_palace_dir=None,
        mempalace_knowledge_graph_db=None,
        mempalace_chroma_db=None,
        mempalace_write_log=None,
        mempalace_config=None,
        sync_state=str(tmp_path / "state.sqlite3"),
        delete_mode="ignore",
        neo4j_uri="bolt://example:7687",
        neo4j_username=None,
        neo4j_password=None,
        neo4j_database=None,
        store_content=False,
        store_snippet=None,
        snippet_chars=80,
        watch_debounce_seconds=None,
    )
    cfg = load_config(args, env_file=None)
    assert cfg.mempalace_home == tmp_path.resolve()
    assert cfg.sync_mode == "ignore"
    assert cfg.neo4j_uri == "bolt://example:7687"
    assert cfg.snippet_chars == 80
