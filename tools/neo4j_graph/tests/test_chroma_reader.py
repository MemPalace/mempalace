import sqlite3
from pathlib import Path

from mempalace_graph.content_resolver import resolve_content
from mempalace_graph.mempalace_chroma_reader import read_chroma_records


def make_chroma(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE embeddings (
            id INTEGER PRIMARY KEY,
            segment_id TEXT NOT NULL,
            embedding_id TEXT NOT NULL,
            seq_id BLOB NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE embedding_metadata (
            id INTEGER,
            key TEXT NOT NULL,
            string_value TEXT,
            int_value INTEGER,
            float_value REAL,
            bool_value INTEGER,
            PRIMARY KEY (id, key)
        );
        INSERT INTO embeddings (id, segment_id, embedding_id, seq_id, created_at)
        VALUES (1, 's', 'drawer_1', x'01', '2026-01-01');
        INSERT INTO embedding_metadata VALUES (1, 'chroma:document', '# Title\nFull content', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'wing', 'wing_project_demo', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'room', 'demo-room', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'hall', 'hall_events', NULL, NULL, NULL);
        INSERT INTO embedding_metadata VALUES (1, 'content_type', 'note', NULL, NULL, NULL);
        """
    )
    conn.commit()
    conn.close()


def test_chroma_reader_indexes_metadata_without_persisting_content(tmp_path: Path) -> None:
    db = tmp_path / "chroma.sqlite3"
    make_chroma(db)
    records, errors = read_chroma_records(db)
    assert errors == []
    assert records[0].id == "drawer_1"
    assert records[0].title == "Title"
    assert records[0].wing == "wing_project_demo"
    assert records[0].source_record_locator == "chroma:embedding:drawer_1"


def test_chroma_content_resolver_reads_original_source(tmp_path: Path) -> None:
    db = tmp_path / "chroma.sqlite3"
    make_chroma(db)
    resolved = resolve_content(str(db), "chroma:embedding:drawer_1")
    assert resolved.title == "Title"
    assert resolved.content == "# Title\nFull content"
    assert "chroma:document" not in resolved.metadata
