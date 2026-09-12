import sqlite3
from pathlib import Path

from mempalace_graph.mempalace_schema_inspector import inspect_schema


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memories (id TEXT PRIMARY KEY, title TEXT, content TEXT, metadata TEXT, created_at TEXT);
        CREATE TABLE edges (id TEXT PRIMARY KEY, source_id TEXT, target_id TEXT, relationship_type TEXT, score REAL);
        INSERT INTO memories VALUES ('m1', 'Title', 'Body', '{}', '2026-01-01');
        INSERT INTO edges VALUES ('e1', 'm1', 'm2', 'similar', 0.8);
        """
    )
    conn.commit()
    conn.close()


def test_schema_inspection_candidates(tmp_path: Path) -> None:
    db = tmp_path / "kg.sqlite3"
    make_db(db)
    inspection = inspect_schema(db)
    assert "memories" in inspection.likely_memory_tables
    assert "edges" in inspection.likely_relationship_tables
