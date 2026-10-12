"""Direct SQLite drawer lookup retains complete groups and scalar metadata."""

import sqlite3

import pytest

from mempalace.backends import chroma
from test_sqlite_diary_rows import _NAME, _array_schema, _database, _row


def _read(tmp_path, drawer_id, collection_name=_NAME):
    return chroma.sqlite_drawer_rows(str(tmp_path), collection_name, drawer_id=drawer_id)


class _ObservedReader:
    def __init__(self, conn, *, after_selection=None):
        self.conn = conn
        self.after_selection = after_selection
        self.hydrated = []
        self.plans = []
        self.closed = False
        self.transaction_on_close = None

    def execute(self, sql, parameters=()):
        cursor = self.conn.execute(sql, parameters)
        if sql.startswith("SELECT id, key,"):
            self.hydrated.append(list(parameters))
        if sql.startswith(("SELECT id, embedding_id", "SELECT DISTINCT e.id")):
            self.plans.extend(self.conn.execute("EXPLAIN QUERY PLAN " + sql, parameters).fetchall())
        if sql.startswith("SELECT DISTINCT e.id") and self.after_selection is not None:
            callback, self.after_selection = self.after_selection, None
            callback()
        return cursor

    def close(self):
        self.transaction_on_close = self.conn.in_transaction
        self.conn.close()
        self.closed = True


@pytest.mark.parametrize(
    "keys", [("parent_drawer_id",), ("parent_entry_id",), ("parent_drawer_id", "parent_entry_id")]
)
def test_real_chroma_group_lookup_matches_physical_get(collection, palace_path, keys):
    docs = ["exact café 東京 [first]\n", "second `literal` chunk"]
    metas = [
        {**dict.fromkeys(keys, "logical"), "chunk_index": i, "score": 0.5, "flag": False}
        for i in range(2)
    ]
    collection.add(ids=["part-z", "part-a"], documents=docs, metadatas=metas)
    actual = collection.get(include=["documents", "metadatas"])
    expected = list(zip(actual["ids"], actual["documents"], actual["metadatas"]))
    assert chroma.sqlite_drawer_rows(palace_path, _NAME, drawer_id="logical") == (False, expected)


def test_real_chroma_missing_document_and_typed_metadata_are_preserved(collection, palace_path):
    meta = {
        "wing": "literal",
        "counter": 7,
        "weight": 0.25,
        "flag": True,
        "label": "café 東京",
    }
    collection.add(ids=["embedding-only"], metadatas=[meta], embeddings=[[1.0] + [0.0] * 383])
    assert collection.get(ids=["embedding-only"], include=["documents"])["documents"] == [None]
    assert chroma.sqlite_drawer_rows(palace_path, _NAME, drawer_id="embedding-only") == (
        True,
        [("embedding-only", None, meta)],
    )


def test_direct_row_takes_precedence_over_linked_group_and_its_arrays(tmp_path):
    with _database(tmp_path) as conn:
        _array_schema(conn)
        _row(conn, 1, drawer_id="logical", document="direct row")
        _row(conn, 2, document="group chunk", metadata={"parent_drawer_id": "logical"})
        conn.execute(
            "INSERT INTO embedding_metadata_array(id,key,string_value) VALUES (2,'tags','array')"
        )
        conn.commit()
        direct, rows = _read(tmp_path, "logical")
        assert direct is True
        assert [row[:2] for row in rows] == [("logical", "direct row")]


def test_legacy_empty_metadata_key_is_preserved(tmp_path):
    with _database(tmp_path) as conn:
        _row(conn, 1, document="exact content", metadata={"": "literal empty key"})
        conn.commit()
        assert _read(tmp_path, "diary-1")[1][0][2][""] == "literal empty key"


def test_either_parent_key_matches_and_dual_stamped_rows_are_deduplicated(tmp_path):
    with _database(tmp_path) as conn:
        _row(
            conn,
            1,
            document="one",
            metadata={"parent_drawer_id": "group", "parent_entry_id": "group"},
        )
        _row(conn, 2, document="two", metadata={"parent_entry_id": "group"})
        _row(
            conn,
            3,
            document="three",
            metadata={"parent_drawer_id": "other", "parent_entry_id": "group"},
        )
        conn.commit()
        direct, rows = _read(tmp_path, "group")
        assert direct is False
        assert [row[1] for row in rows] == ["one", "two", "three"]
        assert len({row[0] for row in rows}) == 3
        assert _read(tmp_path, "diary-1")[0] is True


def test_complete_group_larger_than_hydration_batch_is_not_truncated(monkeypatch, tmp_path):
    with _database(tmp_path) as conn:
        for i in range(1, 651):
            _row(
                conn,
                i,
                document=f"exact chunk {i}\n",
                metadata={
                    "parent_drawer_id": "large",
                    "parent_entry_id": "large",
                    "chunk_index": i - 1,
                },
            )
        conn.commit()
        observed = _ObservedReader(chroma.open_palace_reader(tmp_path / "chroma.sqlite3"))
        monkeypatch.setattr(chroma, "open_palace_reader", lambda _: observed)
        direct, rows = _read(tmp_path, "large")
        assert direct is False
        assert len(rows) == 650
        assert [row[1] for row in rows] == [f"exact chunk {i}\n" for i in range(1, 651)]
        assert [len(batch) for batch in observed.hydrated] == [500, 150]
        assert observed.closed and observed.transaction_on_close


@pytest.mark.parametrize(
    "drawer_id", ["quote'--", 'double"quote', "literal%_slash\\id", "café 東京\n[handle]"]
)
def test_arbitrary_ids_are_bound_literals_for_direct_and_parent_reads(tmp_path, drawer_id):
    with _database(tmp_path) as conn:
        _row(conn, 1, drawer_id=drawer_id, document="direct")
        _row(conn, 2, document="linked", metadata={"parent_entry_id": drawer_id})
        conn.commit()
        assert _read(tmp_path, drawer_id)[1][0][:2] == (drawer_id, "direct")
        conn.execute("DELETE FROM embedding_metadata WHERE id=1")
        conn.execute("DELETE FROM embeddings WHERE id=1")
        conn.commit()
        assert _read(tmp_path, drawer_id)[1][0][:2] == ("diary-2", "linked")


def test_custom_collection_and_metadata_scope_exclude_vector_and_other_collection(tmp_path):
    with _database(tmp_path) as conn:
        conn.execute("UPDATE collections SET name='custom' WHERE id='drawers'")
        conn.execute("INSERT INTO collections VALUES ('other','other')")
        conn.execute("INSERT INTO segments VALUES ('other-meta','other','METADATA')")
        _row(conn, 1, drawer_id="target", document="other collection", segment="other-meta")
        _row(conn, 2, drawer_id="target", document="vector ghost", segment="vector")
        _row(conn, 3, document="right group", metadata={"parent_entry_id": "target"})
        conn.commit()
        assert _read(tmp_path, "target") is None
        assert _read(tmp_path, "target", "custom")[1][0][1] == "right group"


@pytest.mark.parametrize(
    "columns", [("string_value", "int_value"), ("string_value", "int_value", "float_value")]
)
def test_older_value_columns_preserve_all_available_scalar_metadata(tmp_path, columns):
    with _database(tmp_path, columns) as conn:
        meta = {"counter": 3, "source_file": "exact/path.py", "filed_at": 9}
        if "float_value" in columns:
            meta["weight"] = 0.5
        _row(conn, 1, document="unchanged", metadata=meta)
        conn.commit()
        assert _read(tmp_path, "diary-1") == (
            True,
            [("diary-1", "unchanged", {"room": "diary", "agent": "observer", **meta})],
        )


@pytest.mark.parametrize("grouped", [False, True], ids=["direct", "group"])
def test_any_array_on_selected_rows_requires_full_metadata_fallback(tmp_path, grouped):
    with _database(tmp_path) as conn:
        _array_schema(conn)
        _row(conn, 1, document="array record", metadata={"parent_drawer_id": "group"})
        conn.execute(
            "INSERT INTO embedding_metadata_array(id,key,string_value) VALUES (1,'arbitrary-property','exact')"
        )
        conn.commit()
        assert _read(tmp_path, "group" if grouped else "diary-1") is None


def test_real_chroma_array_metadata_keeps_legacy_reader(collection, palace_path):
    meta = {"array": ["exact", "café"], "counter": 7}
    collection.add(ids=["array"], documents=["original"], metadatas=[meta])
    assert collection.get(include=["metadatas"])["metadatas"][0] == meta
    assert chroma.sqlite_drawer_rows(palace_path, _NAME, drawer_id="array") is None


def test_array_metadata_after_first_group_batch_still_requires_fallback(tmp_path):
    with _database(tmp_path) as conn:
        _array_schema(conn)
        for i in range(1, 502):
            _row(conn, i, document=f"chunk {i}", metadata={"parent_entry_id": "large"})
        conn.execute(
            "INSERT INTO embedding_metadata_array(id,key,string_value) VALUES (501,'tags','last row')"
        )
        conn.commit()
        assert _read(tmp_path, "large") is None


def test_array_parent_alone_does_not_become_a_scalar_group(tmp_path):
    with _database(tmp_path) as conn:
        _array_schema(conn)
        _row(conn, 1, document="array-linked")
        conn.execute(
            "INSERT INTO embedding_metadata_array(id,key,string_value) VALUES (1,'parent_drawer_id','group')"
        )
        conn.commit()
        assert _read(tmp_path, "group") is None


@pytest.mark.parametrize("queue_seq, expected", [(5, True), (6, False)])
def test_recovery_guard_rejects_only_unconsumed_collection_operations(
    tmp_path, queue_seq, expected
):
    with _database(tmp_path) as conn:
        _row(conn, 1, document="current row")
        conn.execute("UPDATE max_seq_id SET seq_id=5 WHERE segment_id='metadata'")
        conn.execute(
            "INSERT INTO embeddings_queue VALUES (?, 'persistent://custom/namespace/drawers')",
            (queue_seq,),
        )
        conn.execute(
            "INSERT INTO embeddings_queue VALUES (100, 'persistent://default/default/other')"
        )
        conn.commit()
        result = _read(tmp_path, "diary-1")
        assert (result is not None) is expected


def test_selection_and_metadata_hydration_share_one_snapshot(monkeypatch, tmp_path):
    with _database(tmp_path) as conn:
        _row(conn, 1, document="before", metadata={"parent_entry_id": "group", "counter": 1})
        conn.commit()

        def concurrent_commit():
            conn.execute(
                "UPDATE embedding_metadata SET string_value='after' WHERE id=1 AND key='chroma:document'"
            )
            conn.execute("UPDATE embedding_metadata SET int_value=2 WHERE id=1 AND key='counter'")
            _row(conn, 2, document="new member", metadata={"parent_entry_id": "group"})
            conn.commit()

        observed = _ObservedReader(
            chroma.open_palace_reader(tmp_path / "chroma.sqlite3"),
            after_selection=concurrent_commit,
        )
        with monkeypatch.context() as patch:
            patch.setattr(chroma, "open_palace_reader", lambda _: observed)
            _, rows = _read(tmp_path, "group")
        assert len(rows) == 1
        assert rows[0][1] == "before" and rows[0][2]["counter"] == 1
        assert observed.closed and observed.transaction_on_close
        _, current = _read(tmp_path, "group")
        assert [row[1] for row in current] == ["after", "new member"]


@pytest.mark.parametrize("grouped", [False, True], ids=["direct", "parent"])
def test_sparse_lookup_uses_indexes_and_hydrates_selected_rows_only(monkeypatch, tmp_path, grouped):
    with _database(tmp_path) as conn:
        conn.executemany(
            "INSERT INTO embeddings VALUES (?,?,'metadata')",
            [(i, f"unrelated-{i}") for i in range(1000, 11000)],
        )
        conn.executemany(
            "INSERT INTO embedding_metadata(id,key,string_value) VALUES (?,'parent_entry_id','other')",
            [(i,) for i in range(1000, 11000)],
        )
        _row(conn, 1, document="exact target", metadata={"parent_entry_id": "group"})
        conn.commit()
        observed = _ObservedReader(chroma.open_palace_reader(tmp_path / "chroma.sqlite3"))
        monkeypatch.setattr(chroma, "open_palace_reader", lambda _: observed)
        assert _read(tmp_path, "group" if grouped else "diary-1")[1][0][1] == "exact target"
        assert observed.hydrated == [[1]]
        index = "metadata_strings" if grouped else "embedding_identity"
        assert any("SEARCH" in plan[3] and index in plan[3] for plan in observed.plans)
        assert observed.closed


@pytest.mark.parametrize(
    "broken",
    [
        "missing-id",
        "missing-collection",
        "duplicate-collection",
        "watermark",
        "queue-table",
        "metadata-table",
        "array-schema",
    ],
)
def test_unavailable_lookup_returns_none_and_releases_reader(monkeypatch, tmp_path, broken):
    with _database(tmp_path) as conn:
        _row(conn, 1, document="original")
        if broken == "missing-collection":
            conn.execute("DELETE FROM collections")
        elif broken == "duplicate-collection":
            conn.execute("INSERT INTO collections VALUES ('duplicate',?)", (_NAME,))
        elif broken == "watermark":
            conn.execute("UPDATE max_seq_id SET seq_id='unknown'")
        elif broken == "queue-table":
            conn.execute("DROP TABLE embeddings_queue")
        elif broken == "metadata-table":
            conn.execute("DROP TABLE embedding_metadata")
        elif broken == "array-schema":
            conn.execute("CREATE TABLE embedding_metadata_array(unrelated TEXT)")
        conn.commit()
        observed = _ObservedReader(chroma.open_palace_reader(tmp_path / "chroma.sqlite3"))
        monkeypatch.setattr(chroma, "open_palace_reader", lambda _: observed)
        assert _read(tmp_path, "missing" if broken == "missing-id" else "diary-1") is None
        assert observed.closed


def test_missing_database_is_not_created(tmp_path):
    assert _read(tmp_path, "absent") is None
    assert not (tmp_path / "chroma.sqlite3").exists()


def test_sqlite_open_error_preserves_fallback(monkeypatch, tmp_path):
    with _database(tmp_path):

        def unavailable(_):
            raise sqlite3.OperationalError("locked")

        monkeypatch.setattr(chroma, "open_palace_reader", unavailable)
        assert _read(tmp_path, "absent") is None
