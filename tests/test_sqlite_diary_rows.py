"""SQLite diary reads preserve collection semantics without hydrating a palace."""

from contextlib import contextmanager
import sqlite3

import pytest

from mempalace.backends import chroma


_NAME = "mempalace_drawers"
_MISSING = object()


@contextmanager
def _database(tmp_path, columns=("string_value", "int_value", "float_value", "bool_value")):
    db = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            "CREATE TABLE collections(id TEXT PRIMARY KEY, name TEXT);"
            "CREATE TABLE segments(id TEXT PRIMARY KEY, collection TEXT, scope TEXT);"
            "CREATE TABLE embeddings(id INTEGER PRIMARY KEY, embedding_id TEXT, segment_id TEXT);"
            "CREATE TABLE max_seq_id(segment_id TEXT PRIMARY KEY, seq_id BLOB NOT NULL);"
            "CREATE TABLE embeddings_queue(seq_id INTEGER PRIMARY KEY, topic TEXT NOT NULL);"
            "CREATE UNIQUE INDEX embedding_identity ON embeddings(segment_id, embedding_id);"
            "CREATE TABLE embedding_metadata(id INTEGER, key TEXT, "
            + ", ".join(
                f"{col} {'TEXT' if col == 'string_value' else 'NUMERIC'}" for col in columns
            )
            + ", PRIMARY KEY(id, key));"
            "CREATE INDEX metadata_strings ON embedding_metadata(key, string_value);"
        )
        conn.execute("INSERT INTO collections VALUES ('drawers', ?)", (_NAME,))
        conn.executemany(
            "INSERT INTO segments VALUES (?, 'drawers', ?)",
            [("metadata", "METADATA"), ("vector", "VECTOR")],
        )
        conn.executemany("INSERT INTO max_seq_id VALUES (?, 0)", [("metadata",), ("vector",)])
        conn.commit()
        yield conn
    finally:
        conn.close()


def _row(conn, index, *, document=_MISSING, metadata=None, segment="metadata", drawer_id=None):
    conn.execute(
        "INSERT INTO embeddings VALUES (?, ?, ?)",
        (index, drawer_id or f"diary-{index}", segment),
    )
    meta = {"room": "diary", "agent": "observer", **(metadata or {})}
    if document is not _MISSING:
        meta["chroma:document"] = document
    for key, value in meta.items():
        column = (
            "bool_value"
            if isinstance(value, bool)
            else "int_value"
            if isinstance(value, int)
            else "float_value"
            if isinstance(value, float)
            else "string_value"
        )
        conn.execute(
            f"INSERT INTO embedding_metadata(id, key, {column}) VALUES (?, ?, ?)",
            (index, key, value),
        )


def _read(tmp_path, **kwargs):
    return chroma.sqlite_diary_rows(str(tmp_path), _NAME, agent_name="observer", **kwargs)


def test_timestamp_order_ties_and_missing_documents_match_chroma(collection, palace_path):
    ids = ["z-last-id", "a-first-id", "m-middle-id", "missing-timestamp"]
    metas = [
        {"room": "diary", "agent": "observer", "filed_at": "2026-01-02", "topic": str(i)}
        for i in range(3)
    ] + [{"room": "diary", "agent": "observer"}]
    collection.add(ids=ids, metadatas=metas, embeddings=[[1.0] + [0.0] * 383] * len(ids))
    physical = collection.get(include=["documents", "metadatas"])
    assert physical["documents"] == [None] * len(ids)
    expected = sorted(
        zip(physical["ids"], physical["documents"], physical["metadatas"]),
        key=lambda row: row[2].get("filed_at", ""),
        reverse=True,
    )

    result = chroma.sqlite_diary_rows(palace_path, _NAME, agent_name="observer", limit=3)
    assert result == (4, expected[:3])


def test_scope_is_collection_metadata_agent_room_and_optional_wing(tmp_path):
    with _database(tmp_path) as conn:
        conn.execute("INSERT INTO collections VALUES ('closets', 'mempalace_closets')")
        conn.execute("INSERT INTO segments VALUES ('closet-meta', 'closets', 'METADATA')")
        _row(
            conn, 1, document="older café 東京", metadata={"filed_at": "2026-01-01", "wing": "home"}
        )
        _row(conn, 2, document="newer", metadata={"filed_at": "2026-01-02", "wing": "work"})
        _row(conn, 3, document="other agent", metadata={"agent": "someone"})
        _row(conn, 4, document="other room", metadata={"room": "planning"})
        _row(conn, 5, document="vector ghost", segment="vector")
        _row(conn, 6, document="closet ghost", segment="closet-meta")
        conn.commit()

        total, rows = _read(tmp_path)
        assert total == 2
        assert [row[1] for row in rows] == ["newer", "older café 東京"]
        total, rows = _read(tmp_path, wing="home")
        assert total == 1
        assert rows[0][1] == "older café 東京"
        assert _read(tmp_path, wing="absent") == (0, [])


@pytest.mark.parametrize(
    "columns",
    [
        ("string_value", "int_value"),
        ("string_value", "int_value", "float_value"),
        ("string_value", "int_value", "float_value", "bool_value"),
    ],
    ids=["oldest", "without-bool", "current"],
)
def test_metadata_decoding_supports_older_schemas(tmp_path, columns):
    with _database(tmp_path, columns) as conn:
        meta = {"filed_at": "2026-01-02", "chunk_index": 7, "topic": "literal"}
        if "float_value" in columns:
            meta["weight"] = 0.25
        if "bool_value" in columns:
            meta["flag"] = False
        _row(conn, 1, document="exact [entry] `café` 東京\n", metadata=meta)
        conn.commit()
        total, rows = _read(tmp_path)
        assert total == 1
        assert rows == [
            (
                "diary-1",
                "exact [entry] `café` 東京\n",
                {"room": "diary", "agent": "observer", **meta},
            )
        ]


@pytest.mark.parametrize("timestamp", [42, 1.5, True, None])
def test_nonstring_timestamp_anywhere_in_matching_rows_requires_fallback(tmp_path, timestamp):
    with _database(tmp_path) as conn:
        _row(conn, 1, document="newest", metadata={"filed_at": "2026-01-02"})
        _row(conn, 2, document="unsupported old timestamp", metadata={"filed_at": timestamp})
        conn.commit()
        assert _read(tmp_path, limit=1) is None


def test_missing_and_empty_timestamps_keep_storage_order_and_count_chunks(tmp_path):
    with _database(tmp_path) as conn:
        _row(
            conn,
            7,
            document="second",
            metadata={"filed_at": "", "parent_entry_id": "logical", "chunk_index": 1},
        )
        _row(conn, 3, document="first", metadata={"parent_entry_id": "logical", "chunk_index": 0})
        _row(conn, 9, document="newest", metadata={"filed_at": "2026-01-02"})
        conn.commit()
        total, rows = _read(tmp_path)
        assert total == 3
        assert [row[0] for row in rows] == ["diary-9", "diary-3", "diary-7"]


class _ObservedReader:
    def __init__(self, conn, *, after_count=None):
        self.conn = conn
        self.after_count = after_count
        self.hydrated = []
        self.plans = []
        self.closed = False
        self.transaction_on_close = None

    def execute(self, sql, parameters=()):
        cursor = self.conn.execute(sql, parameters)
        if sql.startswith("SELECT id, key,"):
            self.hydrated.extend(parameters)
        if sql.startswith("SELECT e.id, e.embedding_id"):
            self.plans.extend(self.conn.execute("EXPLAIN QUERY PLAN " + sql, parameters).fetchall())
        if sql.startswith("SELECT COUNT(*), COALESCE") and self.after_count is not None:
            callback, self.after_count = self.after_count, None
            callback()
        return cursor

    def close(self):
        self.transaction_on_close = self.conn.in_transaction
        self.conn.close()
        self.closed = True


def test_sparse_diary_hydrates_only_selected_rows_through_index(monkeypatch, tmp_path):
    with _database(tmp_path) as conn:
        conn.executemany(
            "INSERT INTO embeddings VALUES (?, ?, 'metadata')",
            [(i, f"unrelated-{i}") for i in range(1000, 11000)],
        )
        conn.executemany(
            "INSERT INTO embedding_metadata(id,key,string_value) VALUES (?, 'room', 'notes')",
            [(i,) for i in range(1000, 11000)],
        )
        for i in range(1, 6):
            _row(conn, i, document="verbatim " * 500, metadata={"filed_at": f"2026-01-0{i}"})
        conn.commit()
        open_reader = chroma.open_palace_reader
        observed = _ObservedReader(open_reader(tmp_path / "chroma.sqlite3"))
        monkeypatch.setattr(chroma, "open_palace_reader", lambda _: observed)

        total, rows = _read(tmp_path, limit=2)
        assert total == 5
        assert [row[0] for row in rows] == ["diary-5", "diary-4"]
        assert observed.hydrated == [5, 4]
        assert any(
            "SEARCH f USING" in plan[3] and "metadata_strings" in plan[3] for plan in observed.plans
        )
        assert observed.closed and observed.transaction_on_close


def test_count_selection_and_documents_share_snapshot(monkeypatch, tmp_path):
    with _database(tmp_path) as conn:
        _row(conn, 1, document="before commit", metadata={"filed_at": "2026-01-01"})
        conn.commit()

        def commit_between_reads():
            conn.execute(
                "UPDATE embedding_metadata SET string_value='after commit' WHERE id=1 AND key='chroma:document'"
            )
            _row(conn, 2, document="new entry", metadata={"filed_at": "2026-01-02"})
            conn.commit()

        open_reader = chroma.open_palace_reader
        observed = _ObservedReader(
            open_reader(tmp_path / "chroma.sqlite3"), after_count=commit_between_reads
        )
        with monkeypatch.context() as patch:
            patch.setattr(chroma, "open_palace_reader", lambda _: observed)
            total, rows = _read(tmp_path)
        assert total == 1
        assert [row[1] for row in rows] == ["before commit"]
        assert observed.closed and observed.transaction_on_close
        total, rows = _read(tmp_path)
        assert total == 2
        assert [row[1] for row in rows] == ["new entry", "after commit"]


def test_missing_database_does_not_create_files(tmp_path):
    assert _read(tmp_path) is None
    assert not (tmp_path / "chroma.sqlite3").exists()


@pytest.mark.parametrize(
    "broken",
    [
        "collection-missing",
        "collection-ambiguous",
        "segment-missing",
        "segment-ambiguous",
        "schema-missing",
        "queue-missing",
        "watermark-schema-missing",
    ],
)
def test_unavailable_or_ambiguous_schema_falls_back_and_closes(monkeypatch, tmp_path, broken):
    with _database(tmp_path) as conn:
        if broken == "collection-missing":
            conn.execute("DELETE FROM collections")
        elif broken == "collection-ambiguous":
            conn.execute("INSERT INTO collections VALUES ('duplicate', ?)", (_NAME,))
        elif broken == "segment-missing":
            conn.execute("DELETE FROM segments")
        elif broken == "segment-ambiguous":
            conn.execute("INSERT INTO segments VALUES ('extra', 'drawers', 'METADATA')")
        elif broken == "queue-missing":
            conn.execute("DROP TABLE embeddings_queue")
        elif broken == "watermark-schema-missing":
            conn.execute("DROP TABLE max_seq_id")
        else:
            conn.execute("DROP TABLE embedding_metadata")
        conn.commit()
        open_reader = chroma.open_palace_reader
        observed = _ObservedReader(open_reader(tmp_path / "chroma.sqlite3"))
        monkeypatch.setattr(chroma, "open_palace_reader", lambda _: observed)
        assert _read(tmp_path) is None
        assert observed.closed


def test_sqlite_open_error_falls_back(monkeypatch, tmp_path):
    with _database(tmp_path):

        def unavailable(_):
            raise sqlite3.OperationalError("locked")

        monkeypatch.setattr(chroma, "open_palace_reader", unavailable)
        assert _read(tmp_path) is None


@pytest.mark.parametrize("consumed", [5, (5).to_bytes(8, "big")], ids=["integer", "legacy-blob"])
@pytest.mark.parametrize("queued", [4, 5, 6], ids=["earlier", "retained-last", "pending"])
def test_only_unconsumed_collection_queue_requires_recovery(tmp_path, consumed, queued):
    with _database(tmp_path) as conn:
        _row(conn, 1, document="stored diary", metadata={"filed_at": "2026-01-01"})
        conn.execute("UPDATE max_seq_id SET seq_id=? WHERE segment_id='metadata'", (consumed,))
        conn.execute(
            "INSERT INTO embeddings_queue VALUES (?, 'persistent://custom/namespace/drawers')",
            (queued,),
        )
        conn.commit()
        result = _read(tmp_path)
        if queued > 5:
            assert result is None
        else:
            assert result[0] == 1
            assert result[1][0][1] == "stored diary"


def test_other_collection_and_vector_backlogs_do_not_reject_metadata(tmp_path):
    with _database(tmp_path) as conn:
        _row(conn, 1, document="metadata is current")
        conn.execute("UPDATE max_seq_id SET seq_id=10 WHERE segment_id='metadata'")
        conn.executemany(
            "INSERT INTO embeddings_queue VALUES (?, ?)",
            [
                (10, "persistent://default/default/drawers"),
                (999, "persistent://default/default/other"),
            ],
        )
        conn.commit()
        result = _read(tmp_path)
        assert result[0] == 1
        assert result[1][0][1] == "metadata is current"


@pytest.mark.parametrize("marker", ["unknown", -1, b"short", b"\x11\x11000005", b"\xff" * 8])
def test_unsupported_metadata_watermark_falls_back(tmp_path, marker):
    with _database(tmp_path) as conn:
        _row(conn, 1, document="stored diary")
        conn.execute("UPDATE max_seq_id SET seq_id=? WHERE segment_id='metadata'", (marker,))
        conn.commit()
        assert _read(tmp_path) is None


@pytest.mark.parametrize("state", ["empty", "stored", "queued"])
def test_missing_watermark_permits_only_truly_empty_collection(tmp_path, state):
    with _database(tmp_path) as conn:
        conn.execute("DELETE FROM max_seq_id WHERE segment_id='metadata'")
        if state == "stored":
            _row(conn, 1, document="unverified diary")
        elif state == "queued":
            conn.execute(
                "INSERT INTO embeddings_queue VALUES (1, 'persistent://default/default/drawers')"
            )
        conn.commit()
        assert _read(tmp_path) == ((0, []) if state == "empty" else None)


@pytest.mark.parametrize("requested, showing", [(0, 1), (-1, 1), (101, 100), (10_000, 100)])
def test_limit_bounds_document_hydration(monkeypatch, tmp_path, requested, showing):
    with _database(tmp_path) as conn:
        for i in range(1, 106):
            _row(conn, i, document=f"exact entry {i}")
        conn.commit()
        observed = _ObservedReader(chroma.open_palace_reader(tmp_path / "chroma.sqlite3"))
        monkeypatch.setattr(chroma, "open_palace_reader", lambda _: observed)
        total, records = _read(tmp_path, limit=requested)
        assert total == 105
        assert len(records) == showing
        assert len(observed.hydrated) == showing
        assert observed.closed


def _array_schema(conn):
    conn.executescript(
        "CREATE TABLE embedding_metadata_array(id INTEGER NOT NULL,key TEXT NOT NULL,"
        "string_value TEXT,int_value INTEGER,float_value REAL,bool_value INTEGER);"
        "CREATE INDEX embedding_metadata_array_id_key ON embedding_metadata_array(id,key);"
    )


@pytest.mark.parametrize("key", ["filed_at", "date", "topic"])
def test_real_chroma_diary_array_fields_require_legacy_reader(collection, palace_path, key):
    values = ["first", "second"]
    meta = {"room": "diary", "agent": "observer", key: values}
    collection.add(ids=["array-diary"], documents=["exact array diary"], metadatas=[meta])
    stored = collection.get(include=["documents", "metadatas"])
    assert stored["metadatas"][0][key] == values
    assert chroma.sqlite_diary_rows(palace_path, _NAME, agent_name="observer") is None


@pytest.mark.parametrize("key", ["filed_at", "date", "topic"])
def test_array_fields_on_unselected_matching_rows_require_fallback(tmp_path, key):
    with _database(tmp_path) as conn:
        _array_schema(conn)
        _row(conn, 1, document="newest", metadata={"filed_at": "2026-01-02"})
        _row(conn, 2, document="older array diary")
        conn.execute(
            "INSERT INTO embedding_metadata_array(id,key,string_value) VALUES (2,?,'exact array item')",
            (key,),
        )
        conn.commit()
        assert _read(tmp_path, limit=1) is None


def test_unrelated_array_metadata_does_not_reject_scalar_diary(tmp_path):
    with _database(tmp_path) as conn:
        _array_schema(conn)
        _row(conn, 1, document="scalar diary", metadata={"wing": "home"})
        _row(conn, 2, document="other agent", metadata={"agent": "someone"})
        _row(conn, 3, document="other wing", metadata={"wing": "work"})
        conn.executemany(
            "INSERT INTO embedding_metadata_array(id,key,string_value) VALUES (?,?,'item')",
            [(1, "tags"), (2, "filed_at"), (3, "topic")],
        )
        conn.commit()
        total, rows = _read(tmp_path, wing="home")
        assert total == 1
        assert rows[0][1] == "scalar diary"


def test_malformed_optional_array_schema_requires_fallback(tmp_path):
    with _database(tmp_path) as conn:
        _row(conn, 1, document="stored diary")
        conn.execute("CREATE TABLE embedding_metadata_array(unrelated TEXT)")
        conn.commit()
        assert _read(tmp_path) is None
