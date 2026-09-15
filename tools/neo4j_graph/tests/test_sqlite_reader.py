import sqlite3
from pathlib import Path

from mempalace_graph.mempalace_schema_inspector import inspect_schema
from mempalace_graph.mempalace_sqlite_reader import parse_metadata, read_sqlite_records


def test_parse_metadata_decodes_bytes_json() -> None:
    metadata = parse_metadata(b'{"wing":"wing_project","importance":3}')
    assert metadata == {"wing": "wing_project", "importance": 3}


def test_read_sqlite_records_handles_without_rowid_table(tmp_path: Path) -> None:
    db = tmp_path / "kg.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE memories (
            key TEXT PRIMARY KEY,
            title TEXT,
            content TEXT,
            metadata BLOB
        ) WITHOUT ROWID;
        INSERT INTO memories VALUES (
            'm1',
            'Title',
            'Body',
            CAST('{"wing":"wing_project","room":"room"}' AS BLOB)
        );
        """
    )
    conn.commit()
    conn.close()

    inspection = inspect_schema(db)
    records, relationships, warnings = read_sqlite_records(db, inspection)

    assert relationships == []
    assert warnings == []
    assert len(records) == 1
    assert records[0].id == "m1"
    assert records[0].source_record_locator == "sqlite:memories:m1"
    assert records[0].wing == "wing_project"
    assert records[0].room == "room"
