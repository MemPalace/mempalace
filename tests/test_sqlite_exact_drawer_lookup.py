"""Logical drawer lookups must not hydrate unrelated memories."""

import pytest

from mempalace import mcp_server
from mempalace.backends import PalaceRef, sqlite_exact as storage
from mempalace.backends._filters import matches_where


@pytest.fixture
def drawer_collection(tmp_path, monkeypatch):
    backend = storage.SQLiteExactBackend()
    col = backend.get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="mempalace_drawers",
        create=True,
    )
    col.add(
        ids=[f"unrelated-{i}" for i in range(400)],
        documents=["Unrelated verbatim memory " * 40] * 400,
        metadatas=[{"wing": "elsewhere", "room": "archive"}] * 400,
        embeddings=[[1.0, 0.0]] * 400,
    )
    monkeypatch.setattr(mcp_server, "_is_chroma_backend", lambda: False)
    monkeypatch.setattr(mcp_server, "_get_collection", lambda: col)
    yield col
    backend.close_palace(str(tmp_path))


def test_missing_drawer_does_not_decode_unrelated_records(drawer_collection, monkeypatch):
    decoded = []
    original = storage.decode_record

    def counted(document, metadata):
        decoded.append(1)
        return original(document, metadata)

    monkeypatch.setattr(storage, "decode_record", counted)
    result = mcp_server.tool_get_drawer("missing-drawer")
    assert result == {"error": "Drawer not found: missing-drawer"}
    assert len(decoded) == 0, f"Missing drawer decoded {len(decoded)} unrelated memories"


@pytest.mark.parametrize("parent_key", ["parent_drawer_id", "parent_entry_id", "both"])
def test_logical_drawer_reads_only_its_chunks(drawer_collection, monkeypatch, parent_key):
    col = drawer_collection
    parent = "logical\x00\\memory"
    keys = ["parent_drawer_id", "parent_entry_id"] if parent_key == "both" else [parent_key]
    col.add(
        ids=["chunk-b", "chunk-a"],
        documents=["second\x00part", "first\udcffpart"],
        metadatas=[{**dict.fromkeys(keys, parent), "chunk_index": i} for i in [1, 0]],
        embeddings=[[1.0, 0.0]] * 2,
    )
    decoded = []
    original = storage.decode_record

    def counted(document, metadata):
        decoded.append(1)
        return original(document, metadata)

    monkeypatch.setattr(storage, "decode_record", counted)
    result = mcp_server.tool_get_drawer(parent)
    assert result["content"] == "first\udcffpartsecond\x00part"
    assert result["chunk_ids"] == ["chunk-a", "chunk-b"]
    assert len(decoded) == 2


@pytest.mark.parametrize("operation", ["missing", "bulk", "page", "document_filter"])
def test_parent_lookup_work_is_bounded(drawer_collection, operation):
    col = drawer_collection
    col.add(
        ids=["a", "b", "c"],
        documents=["first", "second", "third"],
        metadatas=[
            {"parent_drawer_id": "group", "parent_entry_id": "group", "chunk_index": 0},
            {"parent_entry_id": "group", "chunk_index": 1},
            {"parent_drawer_id": "other", "chunk_index": 0},
        ],
        embeddings=[[1.0, 0.0]] * 3,
    )
    statements = []
    progress = []
    conn = col._handle.conn
    conn.set_trace_callback(statements.append)
    conn.set_progress_handler(lambda: progress.append(1) or 0, 100)
    try:
        if operation == "missing":
            assert mcp_server.tool_get_drawer("absent") == {"error": "Drawer not found: absent"}
        elif operation == "bulk":
            result = mcp_server._bulk_drawer_records(col, ["group", "other", "absent", "a"])
            assert set(result) == {"group", "other", "a"}
            assert result["group"]["content"] == "firstsecond"
            assert result["a"]["content"] == "first"
        else:
            kwargs = (
                {"limit": 1, "offset": 1}
                if operation == "page"
                else {"where_document": {"$contains": "second"}}
            )
            result = col.get(where=mcp_server._logical_parent_where("group"), **kwargs)
            assert result.ids == ["b"]
        # A scan over the 400 unrelated rows costs thousands of VM steps.
        # This bound catches it without relying on wall-clock timing in CI.
        assert len(progress) < 10, f"Lookup used at least {len(progress) * 100} SQLite steps"
    finally:
        conn.set_trace_callback(None)
        conn.set_progress_handler(None, 0)
    queries = [s for s in statements if "FROM documents" in s and "parent_" in s]
    assert queries
    for sql in queries:
        plan = " ".join(row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
        for key in ["parent_drawer_id", "parent_entry_id"]:
            assert f"idx_documents_coll_{key}" in plan, plan
        assert "<expr>=" in plan, plan


@pytest.mark.parametrize(
    "where",
    [
        {"$or": [{"parent_drawer_id": "group"}, {"parent_entry_id": "group"}]},
        {"$or": [{"parent_drawer_id": {"$in": ["group", "other"]}}, {"flag": True}]},
        {"$or": [{"parent_drawer_id": None}, {"parent_entry_id": "group"}]},
        {"$or": [{"parent_drawer_id": {"$eq": None}}, {"flag": 0}]},
        {"$or": [{"parent_drawer_id": {"$in": [None, "group"]}}, {"flag": False}]},
        {"$or": [{"parent_drawer_id": {"$contains": "rou"}}, {"flag": True}]},
        {"$or": [{"$and": [{"flag": 1}, {"parent_entry_id": "group"}]}, {"n": 2.5}]},
        {"$or": [{"$or": [{"flag": 1}, {"flag": 0}]}, {"n": 2.5}]},
        {"$or": []},
        {"$or": [{}, {"parent_entry_id": "group"}]},
        {"$or": [{"flag": 1}] * 101},
        {"$or": [{"wing": 1}, {"parent_entry_id": "absent"}]},
        {"$or": [{"wing": {"$eq": 1}}, {"parent_entry_id": "absent"}]},
        {"$or": [{"wing": {"$in": [1, "other"]}}, {"parent_entry_id": "absent"}]},
    ],
)
def test_disjunctions_preserve_filter_and_page_semantics(drawer_collection, where):
    col = drawer_collection
    metadatas = [
        {"parent_drawer_id": "group", "parent_entry_id": "group", "flag": True},
        {"parent_entry_id": "group", "flag": False, "wing": "1"},
        {"parent_drawer_id": "other", "n": 2.5, "wing": 1},
    ]
    col.add(
        ids=["a", "b", "c"],
        documents=["one", "two", "three"],
        metadatas=metadatas,
        embeddings=[[1.0, 0.0]] * 3,
    )
    all_rows = col.get(include=["metadatas"])
    expected = [
        doc_id
        for doc_id, meta in zip(all_rows.ids, all_rows.metadatas)
        if matches_where(meta, where)
    ]
    assert col.get(where=where).ids == expected
    assert col.get(where=where, offset=1, limit=2).ids == expected[1:3]
    assert col.get(where=where, offset=-2).ids == expected[-2:]
    assert col.get(where=where, limit=-1).ids == expected[:-1]


def test_parent_indexes_track_writes_and_collection_isolation(drawer_collection, tmp_path):
    col = drawer_collection
    col.add(
        ids=["child"],
        documents=["verbatim"],
        embeddings=[[1.0, 0.0]],
        metadatas=[{"parent_drawer_id": "old", "parent_entry_id": "old"}],
    )
    other = col._backend.get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="other",
        create=True,
    )
    other.add(
        ids=["foreign"],
        documents=["foreign"],
        embeddings=[[1.0, 0.0]],
        metadatas=[{"parent_entry_id": "old"}],
    )
    where = mcp_server._logical_parent_where
    assert col.get(where=where("old")).ids == ["child"]
    col.update(ids=["child"], metadatas=[{"parent_drawer_id": "new", "parent_entry_id": "new"}])
    assert col.get(where=where("old")).ids == []
    assert col.get(where=where("new")).ids == ["child"]
    col.upsert(
        ids=["child"],
        documents=["updated"],
        embeddings=[[1.0, 0.0]],
        metadatas=[{"parent_entry_id": "upserted"}],
    )
    assert col.get(where=where("new")).ids == []
    assert col.get(where=where("upserted")).documents == ["updated"]
    col.delete(ids=["child"])
    assert col.get(where=where("upserted")).ids == []
    assert other.get(where=where("old")).ids == ["foreign"]


def test_parent_index_migration_preserves_rows_and_read_only_schema(drawer_collection, tmp_path):
    col = drawer_collection
    col.add(
        ids=["child"],
        documents=["verbatim\x00text"],
        embeddings=[[1.0, 0.0]],
        metadatas=[{"parent_entry_id": "legacy"}],
    )
    conn = col._handle.conn
    indexes = {f"idx_documents_coll_{key}" for key in ["parent_drawer_id", "parent_entry_id"]}
    for index in indexes:
        conn.execute(f"DROP INDEX {index}")
    conn.commit()
    before = [tuple(row) for row in conn.execute("SELECT * FROM documents ORDER BY rowid")]
    col._backend.close()
    backend = storage.SQLiteExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    try:
        reader = backend.get_collection(
            palace=palace, collection_name="mempalace_drawers", options={"read_only": True}
        )
        assert reader.get(where=mcp_server._logical_parent_where("legacy")).ids == ["child"]
        assert not indexes & {
            row[1] for row in reader._handle.conn.execute("PRAGMA index_list(documents)")
        }
        backend.close_palace(palace)
        writer = backend.get_collection(palace=palace, collection_name="mempalace_drawers")
        assert indexes <= {
            row[1] for row in writer._handle.conn.execute("PRAGMA index_list(documents)")
        }
        after = [
            tuple(row)
            for row in writer._handle.conn.execute("SELECT * FROM documents ORDER BY rowid")
        ]
        assert before == after
        assert writer.get(where=mcp_server._logical_parent_where("legacy")).documents == [
            "verbatim\x00text"
        ]
        backend.close_palace(palace)
        reader = backend.get_collection(
            palace=palace, collection_name="mempalace_drawers", options={"read_only": True}
        )
        assert reader.get(where=mcp_server._logical_parent_where("legacy")).ids == ["child"]
    finally:
        backend.close()
