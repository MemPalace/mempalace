"""Tests for mempalace.schema — schema snapshot, comparison, patching, BLOB fix."""

import os
import sqlite3

import pytest

from mempalace.schema import (
    compare_schemas,
    create_reference_schema,
    fix_blob_seq_ids,
    snapshot_schema,
    validate_and_patch,
)


@pytest.fixture
def tmp_db(tmp_dir):
    """Create a minimal SQLite database and return its path."""
    db_path = os.path.join(tmp_dir, "test.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE alpha (id INTEGER PRIMARY KEY, name TEXT, score REAL)")
    conn.execute("CREATE TABLE beta (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE INDEX idx_alpha_name ON alpha (name)")
    conn.commit()
    conn.close()
    return db_path


# ── snapshot_schema ──────────────────────────────────────────────────────


def test_snapshot_schema(tmp_db):
    """Snapshot captures tables, columns, and indexes."""
    schema = snapshot_schema(tmp_db)

    assert "alpha" in schema["tables"]
    assert "beta" in schema["tables"]

    alpha_cols = {c["name"] for c in schema["tables"]["alpha"]["columns"]}
    assert alpha_cols == {"id", "name", "score"}

    beta_cols = {c["name"] for c in schema["tables"]["beta"]["columns"]}
    assert beta_cols == {"id", "value"}

    # Check notnull flag
    value_col = next(c for c in schema["tables"]["beta"]["columns"] if c["name"] == "value")
    assert value_col["notnull"] is True

    assert "idx_alpha_name" in schema["indexes"]
    assert schema["indexes"]["idx_alpha_name"]["table"] == "alpha"


def test_snapshot_schema_skips_internal_tables(tmp_dir):
    """Internal SQLite tables and FTS auxiliaries are excluded."""
    db_path = os.path.join(tmp_dir, "fts.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE real_table (id INTEGER PRIMARY KEY)")
    # FTS5 creates auxiliary tables like *_content, *_docsize, etc.
    conn.execute("CREATE VIRTUAL TABLE fts_search USING fts5(body)")
    conn.commit()
    conn.close()

    schema = snapshot_schema(db_path)
    assert "real_table" in schema["tables"]
    # FTS auxiliary tables should be excluded
    for tname in schema["tables"]:
        assert not tname.endswith("_content")
        assert not tname.endswith("_docsize")
        assert not tname.endswith("_data")
        assert not tname.endswith("_idx")
        assert not tname.endswith("_config")


# ── create_reference_schema ──────────────────────────────────────────────


def test_create_reference_schema():
    """Reference schema is populated and temp dir is cleaned up."""
    ref = create_reference_schema()

    # Should have tables from ChromaDB
    assert len(ref["tables"]) > 0
    # ChromaDB always creates these core tables
    assert "collections" in ref["tables"]
    assert "embeddings" in ref["tables"]

    # Temp dir should be cleaned up (no lingering mempalace_ref_schema_* dirs)
    # We can't check the exact dir, but verify the schema is complete
    assert len(ref["indexes"]) > 0


# ── compare_schemas ──────────────────────────────────────────────────────


def test_compare_schemas_missing_table():
    """Detects a table present in reference but missing from actual."""
    reference = {
        "tables": {
            "alpha": {
                "columns": [{"name": "id", "type": "INTEGER", "notnull": False, "pk": True}],
                "create_sql": "CREATE TABLE alpha (id INTEGER PRIMARY KEY)",
            },
            "beta": {
                "columns": [{"name": "id", "type": "INTEGER", "notnull": False, "pk": True}],
                "create_sql": "CREATE TABLE beta (id INTEGER PRIMARY KEY)",
            },
        },
        "indexes": {},
    }
    actual = {
        "tables": {
            "alpha": {
                "columns": [{"name": "id", "type": "INTEGER", "notnull": False, "pk": True}],
                "create_sql": "CREATE TABLE alpha (id INTEGER PRIMARY KEY)",
            },
        },
        "indexes": {},
    }

    gaps = compare_schemas(reference, actual)
    assert len(gaps) == 1
    assert gaps[0]["kind"] == "missing_table"
    assert gaps[0]["table"] == "beta"
    assert "CREATE TABLE beta" in gaps[0]["fix_sql"]


def test_compare_schemas_missing_column():
    """Detects a column present in reference but missing from actual."""
    reference = {
        "tables": {
            "alpha": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "notnull": False, "pk": True},
                    {
                        "name": "schema_str",
                        "type": "TEXT",
                        "notnull": False,
                        "pk": False,
                    },
                ],
                "create_sql": "CREATE TABLE alpha (id INTEGER PRIMARY KEY, schema_str TEXT)",
            },
        },
        "indexes": {},
    }
    actual = {
        "tables": {
            "alpha": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "notnull": False, "pk": True},
                ],
                "create_sql": "CREATE TABLE alpha (id INTEGER PRIMARY KEY)",
            },
        },
        "indexes": {},
    }

    gaps = compare_schemas(reference, actual)
    assert len(gaps) == 1
    assert gaps[0]["kind"] == "missing_column"
    assert "schema_str" in gaps[0]["detail"]
    assert "ALTER TABLE alpha ADD COLUMN schema_str TEXT" == gaps[0]["fix_sql"]


def test_compare_schemas_missing_index():
    """Detects an index present in reference but missing from actual."""
    reference = {
        "tables": {
            "alpha": {
                "columns": [{"name": "id", "type": "INTEGER", "notnull": False, "pk": True}],
                "create_sql": "CREATE TABLE alpha (id INTEGER PRIMARY KEY)",
            },
        },
        "indexes": {
            "idx_alpha_id": {
                "table": "alpha",
                "create_sql": "CREATE INDEX idx_alpha_id ON alpha (id)",
            },
        },
    }
    actual = {
        "tables": {
            "alpha": {
                "columns": [{"name": "id", "type": "INTEGER", "notnull": False, "pk": True}],
                "create_sql": "CREATE TABLE alpha (id INTEGER PRIMARY KEY)",
            },
        },
        "indexes": {},
    }

    gaps = compare_schemas(reference, actual)
    assert len(gaps) == 1
    assert gaps[0]["kind"] == "missing_index"
    assert gaps[0]["table"] == "alpha"
    assert "CREATE INDEX idx_alpha_id" in gaps[0]["fix_sql"]


def test_compare_schemas_identical():
    """No gaps when schemas match."""
    schema = {
        "tables": {
            "alpha": {
                "columns": [{"name": "id", "type": "INTEGER", "notnull": False, "pk": True}],
                "create_sql": "CREATE TABLE alpha (id INTEGER PRIMARY KEY)",
            },
        },
        "indexes": {
            "idx_alpha_id": {
                "table": "alpha",
                "create_sql": "CREATE INDEX idx_alpha_id ON alpha (id)",
            },
        },
    }

    gaps = compare_schemas(schema, schema)
    assert gaps == []


# ── fix_blob_seq_ids ─────────────────────────────────────────────────────


def test_fix_blob_seq_ids_converts(tmp_dir):
    """BLOB seq_id values are converted to INTEGER."""
    db_path = os.path.join(tmp_dir, "blob_test.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
    conn.execute("CREATE TABLE max_seq_id (rowid INTEGER PRIMARY KEY, seq_id)")

    # Insert BLOB-encoded seq_ids (big-endian)
    blob_42 = (42).to_bytes(8, byteorder="big")
    blob_99 = (99).to_bytes(8, byteorder="big")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (blob_42,))
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (blob_99,))
    conn.execute("INSERT INTO max_seq_id (seq_id) VALUES (?)", (blob_99,))
    conn.commit()
    conn.close()

    fixed = fix_blob_seq_ids(db_path)
    assert fixed == 3

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings ORDER BY seq_id").fetchall()
    assert rows == [(42, "integer"), (99, "integer")]

    max_rows = conn.execute("SELECT seq_id, typeof(seq_id) FROM max_seq_id").fetchall()
    assert max_rows == [(99, "integer")]
    conn.close()


def test_fix_blob_seq_ids_noop(tmp_dir):
    """No-op when seq_ids are already INTEGER."""
    db_path = os.path.join(tmp_dir, "int_test.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id INTEGER)")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (42)")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (99)")
    conn.commit()
    conn.close()

    fixed = fix_blob_seq_ids(db_path)
    assert fixed == 0


def test_fix_blob_seq_ids_missing_table(tmp_dir):
    """Gracefully handles missing tables."""
    db_path = os.path.join(tmp_dir, "empty.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    fixed = fix_blob_seq_ids(db_path)
    assert fixed == 0


# ── validate_and_patch ───────────────────────────────────────────────────


def test_validate_and_patch_applies_fixes(tmp_dir):
    """End-to-end: synthetic gaps are detected and patched."""
    db_path = os.path.join(tmp_dir, "migrate.sqlite3")
    conn = sqlite3.connect(db_path)
    # Create a table missing a column that the reference has
    conn.execute("CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE embeddings (id INTEGER PRIMARY KEY, seq_id)")
    # Insert a BLOB seq_id
    blob_10 = (10).to_bytes(8, byteorder="big")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (blob_10,))
    conn.commit()
    conn.close()

    reference = {
        "tables": {
            "collections": {
                "columns": [
                    {"name": "id", "type": "TEXT", "notnull": False, "pk": True},
                    {"name": "name", "type": "TEXT", "notnull": False, "pk": False},
                    {
                        "name": "schema_str",
                        "type": "TEXT",
                        "notnull": False,
                        "pk": False,
                    },
                ],
                "create_sql": "CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT, schema_str TEXT)",
            },
            "embeddings": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "notnull": False, "pk": True},
                    {"name": "seq_id", "type": "", "notnull": False, "pk": False},
                ],
                "create_sql": "CREATE TABLE embeddings (id INTEGER PRIMARY KEY, seq_id)",
            },
            "acquire_write": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "notnull": False, "pk": True},
                    {
                        "name": "lock_status",
                        "type": "INTEGER",
                        "notnull": False,
                        "pk": False,
                    },
                ],
                "create_sql": "CREATE TABLE acquire_write (id INTEGER PRIMARY KEY, lock_status INTEGER)",
            },
        },
        "indexes": {},
    }

    valid, actions = validate_and_patch(db_path, reference=reference)
    assert valid is True

    # Should have fixed: missing column (schema_str), missing table (acquire_write), BLOB seq_id
    action_text = " ".join(actions)
    assert "schema_str" in action_text
    assert "acquire_write" in action_text
    assert "BLOB" in action_text

    # Verify the column was added
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(collections)").fetchall()]
    assert "schema_str" in cols

    # Verify the table was created
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    ]
    assert "acquire_write" in tables

    # Verify BLOB was fixed
    row = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings").fetchone()
    assert row == (10, "integer")
    conn.close()


def test_validate_and_patch_creates_reference_if_none(tmp_dir):
    """Auto-creates reference schema when none is provided."""
    # Create a ChromaDB palace with one document to get a valid schema
    import chromadb

    palace_path = os.path.join(tmp_dir, "palace")
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers")
    col.add(ids=["test1"], documents=["test doc"], metadatas=[{"wing": "w", "room": "r"}])
    del col
    del client

    db_path = os.path.join(palace_path, "chroma.sqlite3")
    valid, actions = validate_and_patch(db_path, reference=None)
    assert valid is True
    # A well-formed palace should have no gaps
    assert any("Schema OK" in a or "no structural gaps" in a for a in actions)
