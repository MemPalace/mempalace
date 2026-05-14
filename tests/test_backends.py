import sqlite3

import chromadb
import pytest

from mempalace import palace
from mempalace.backends import (
    GetResult,
    PalaceRef,
    QueryResult,
    UnsupportedFilterError,
    available_backends,
    get_backend_class,
    get_backend,
)
from mempalace.backends.chroma import ChromaBackend, ChromaCollection, _fix_blob_seq_ids
from mempalace.backends.postgres import (
    VECTOR_INDEX_CHECK_INTERVAL_ROWS,
    VECTOR_INDEX_MIN_ROWS,
    PostgresBackend,
    PostgresCollection,
    _metadata_value,
    _parse_vector_literal,
    _vec_literal,
)


class _FakeCollection:
    """Stand-in for a chromadb.Collection returning raw chroma-shaped dicts."""

    def __init__(self, query_response=None, get_response=None, count_value=7):
        self.calls = []
        self._query_response = query_response or {
            "ids": [["a", "b"]],
            "documents": [["da", "db"]],
            "metadatas": [[{"wing": "w1"}, {"wing": "w2"}]],
            "distances": [[0.1, 0.2]],
        }
        self._get_response = get_response or {
            "ids": ["a"],
            "documents": ["da"],
            "metadatas": [{"wing": "w1"}],
        }
        self._count_value = count_value

    def add(self, **kwargs):
        self.calls.append(("add", kwargs))

    def upsert(self, **kwargs):
        self.calls.append(("upsert", kwargs))

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return self._query_response

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return self._get_response

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))

    def count(self):
        self.calls.append(("count", {}))
        return self._count_value


class _FakeComposable(str):
    def format(self, *args):
        return _FakeComposable(str.format(self, *(str(arg) for arg in args)))

    def join(self, items):
        return _FakeComposable(str(self).join(str(item) for item in items))


class _FakeSql:
    @staticmethod
    def SQL(value):
        return _FakeComposable(value)

    @staticmethod
    def Identifier(value):
        return _FakeComposable(f'"{value}"')

    @staticmethod
    def Placeholder():
        return _FakeComposable("%s")


def test_chroma_collection_returns_typed_query_result():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)

    result = collection.query(query_texts=["q"])

    assert isinstance(result, QueryResult)
    assert result.ids == [["a", "b"]]
    assert result.documents == [["da", "db"]]
    assert result.metadatas == [[{"wing": "w1"}, {"wing": "w2"}]]
    assert result.distances == [[0.1, 0.2]]
    assert result.embeddings is None


def test_chroma_collection_returns_typed_get_result():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)

    result = collection.get(where={"wing": "w1"})

    assert isinstance(result, GetResult)
    assert result.ids == ["a"]
    assert result.documents == ["da"]
    assert result.metadatas == [{"wing": "w1"}]


def test_query_result_empty_preserves_outer_dimension():
    empty = QueryResult.empty(num_queries=2)
    assert empty.ids == [[], []]
    assert empty.documents == [[], []]
    assert empty.distances == [[], []]
    assert empty.embeddings is None


def test_typed_results_support_dict_compat_access():
    """Transitional compat shim per base.py — retained until callers migrate to attrs."""
    result = GetResult(ids=["a"], documents=["da"], metadatas=[{"w": 1}])
    assert result["ids"] == ["a"]
    assert result.get("documents") == ["da"]
    assert result.get("missing", "default") == "default"
    assert "ids" in result
    assert "missing" not in result


def test_chroma_collection_query_empty_result_preserves_outer_shape():
    fake = _FakeCollection(
        query_response={"ids": [], "documents": [], "metadatas": [], "distances": []}
    )
    collection = ChromaCollection(fake)

    result = collection.query(query_texts=["q1", "q2"])
    assert result.ids == [[], []]
    assert result.documents == [[], []]
    assert result.distances == [[], []]


def test_chroma_collection_rejects_unknown_where_operator():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)

    with pytest.raises(UnsupportedFilterError):
        collection.query(query_texts=["q"], where={"$regex": "foo"})


def test_chroma_collection_delegates_writes():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)

    collection.add(documents=["d"], ids=["1"], metadatas=[{"wing": "w"}])
    collection.upsert(documents=["u"], ids=["2"], metadatas=[{"room": "r"}])
    collection.delete(ids=["1"])
    assert collection.count() == 7

    kinds = [call[0] for call in fake.calls]
    assert kinds == ["add", "upsert", "delete", "count"]


def test_registry_exposes_chroma_by_default():
    names = available_backends()
    assert "chroma" in names
    assert isinstance(get_backend("chroma"), ChromaBackend)


def test_registry_exposes_postgres_by_default():
    names = available_backends()
    assert "postgres" in names
    assert get_backend_class("postgres") is PostgresBackend
    assert isinstance(get_backend("postgres"), PostgresBackend)


def test_registry_unknown_backend_raises():
    with pytest.raises(KeyError):
        get_backend("no-such-backend-exists")


def test_resolve_backend_priority_order(tmp_path):
    from mempalace.backends import resolve_backend_for_palace

    # explicit kwarg wins over everything
    assert resolve_backend_for_palace(explicit="pg", config_value="lance") == "pg"
    # config value wins over env / default
    assert resolve_backend_for_palace(config_value="lance", env_value="qdrant") == "lance"
    # env wins over default
    assert resolve_backend_for_palace(env_value="qdrant", default="chroma") == "qdrant"
    # falls back to default
    assert resolve_backend_for_palace() == "chroma"


def test_vec_literal_formats_postgres_vector_literal():
    assert _vec_literal([1.0, 0.5, -0.25]) == "[1.00000000,0.50000000,-0.25000000]"


def test_parse_vector_literal_accepts_pgvector_text():
    assert _parse_vector_literal("[1,0.5,-0.25]") == [1.0, 0.5, -0.25]
    assert _parse_vector_literal([]) == []
    assert _parse_vector_literal("") == []


def test_metadata_value_normalizes_bools_to_chroma_style_strings():
    assert _metadata_value(True) == "true"
    assert _metadata_value(False) == "false"
    assert _metadata_value(7) == "7"


def test_postgres_backend_requires_dsn(monkeypatch):
    monkeypatch.delenv("MEMPALACE_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("MEMPALACE_PG_DSN", raising=False)
    backend = PostgresBackend()

    with pytest.raises(RuntimeError, match="no DSN"):
        backend.get_collection(
            palace=PalaceRef(id="/tmp/palace", local_path="/tmp/palace"),
            collection_name="mempalace_drawers",
            create=False,
        )


def test_postgres_backend_create_false_does_not_setup_missing_table(monkeypatch):
    calls = []

    class FakeCollection:
        def __init__(self, dsn, table_name):
            calls.append(("init", dsn, table_name))

        def _open(self, *, create):
            calls.append(("open", create))
            raise FileNotFoundError("missing")

    import mempalace.backends.postgres as postgres_mod

    monkeypatch.setattr(postgres_mod, "PostgresCollection", FakeCollection)
    backend = PostgresBackend("postgresql://example")

    with pytest.raises(FileNotFoundError):
        backend.get_collection(
            palace=PalaceRef(id="/ignored", local_path="/ignored"),
            collection_name="mempalace_drawers",
            create=False,
        )

    assert calls == [("init", "postgresql://example", "mempalace_drawers"), ("open", False)]


def test_postgres_backend_create_true_sets_up_collection(monkeypatch):
    calls = []

    class FakeCollection:
        def __init__(self, dsn, table_name):
            self.dsn = dsn
            self.table_name = table_name
            calls.append(("init", dsn, table_name))

        def _open(self, *, create):
            calls.append(("open", create))

        def _ensure_setup(self, *, create):
            calls.append(("ensure", create))

        def close(self):
            calls.append(("close", self.table_name))

    import mempalace.backends.postgres as postgres_mod

    monkeypatch.setattr(postgres_mod, "PostgresCollection", FakeCollection)
    backend = PostgresBackend("postgresql://example")
    collection = backend.get_collection(
        palace=PalaceRef(id="/ignored", local_path="/ignored"),
        collection_name="mempalace_drawers",
        create=True,
    )

    assert collection.table_name == "mempalace_drawers"
    assert calls == [("init", "postgresql://example", "mempalace_drawers"), ("open", True)]

    backend.get_collection(
        palace=PalaceRef(id="/ignored", local_path="/ignored"),
        collection_name="mempalace_drawers",
        create=True,
    )
    assert calls[-1] == ("ensure", True)


def test_postgres_collection_validates_add_lengths(monkeypatch):
    collection = PostgresCollection("postgresql://example")
    monkeypatch.setattr(PostgresCollection, "_ensure_setup", lambda self, **kwargs: None)

    with pytest.raises(ValueError, match="documents and ids"):
        collection.add(documents=["doc"], ids=[])
    with pytest.raises(ValueError, match="metadatas and documents"):
        collection.add(documents=["doc"], ids=["id"], metadatas=[])
    with pytest.raises(ValueError, match="embeddings and documents"):
        collection.add(documents=["doc"], ids=["id"], embeddings=[])


def test_postgres_query_validates_input_before_loading_driver():
    collection = PostgresCollection("postgresql://example")

    with pytest.raises(ValueError, match="exactly one"):
        collection.query()
    with pytest.raises(ValueError, match="exactly one"):
        collection.query(query_texts=["q"], query_embeddings=[[0.1]])
    with pytest.raises(ValueError, match="non-empty"):
        collection.query(query_embeddings=[])
    with pytest.raises(ValueError, match="positive"):
        collection.query(query_embeddings=[[0.1]], n_results=0)
    with pytest.raises(UnsupportedFilterError, match="where_document"):
        collection.query(query_embeddings=[[0.1]], where_document={"$contains": "x"})


def test_postgres_get_and_delete_reject_empty_ids_before_loading_driver():
    collection = PostgresCollection("postgresql://example")

    with pytest.raises(ValueError, match="non-empty list in get"):
        collection.get(ids=[])
    with pytest.raises(UnsupportedFilterError, match="where_document"):
        collection.get(where_document={"$contains": "x"})
    with pytest.raises(ValueError, match="non-empty list in delete"):
        collection.delete(ids=[])


def test_postgres_where_supports_required_operators_without_psycopg2(monkeypatch):
    collection = PostgresCollection("postgresql://example")
    monkeypatch.setattr(PostgresCollection, "_sql", property(lambda self: _FakeSql))

    clause, params = collection._where_to_sql({"$or": [{"wing": "a"}, {"room": {"$ne": "b"}}]})
    assert str(clause) == '("wing" = %s) OR ("room" <> %s)'
    assert params == ["a", "b"]

    clause, params = collection._where_to_sql({"source_file": {"$in": ["a.py", "b.py"]}})
    assert str(clause) == "metadata->>%s IN (%s, %s)"
    assert params == ["source_file", "a.py", "b.py"]


def test_postgres_where_rejects_unsupported_filters_without_psycopg2(monkeypatch):
    collection = PostgresCollection("postgresql://example")
    monkeypatch.setattr(PostgresCollection, "_sql", property(lambda self: _FakeSql))

    with pytest.raises(UnsupportedFilterError, match="where operator"):
        collection._where_to_sql({"$gt": 3})
    with pytest.raises(UnsupportedFilterError, match="field operator"):
        collection._where_to_sql({"source_mtime": {"$gt": 1}})


def test_postgres_upsert_uses_batch_on_conflict_without_delete(monkeypatch):
    events = []

    class FakeCursor:
        def execute(self, sql, params=None):
            events.append(("execute", str(sql), params))

    class FakeConnection:
        def cursor(self):
            events.append("cursor")
            return FakeCursor()

    collection = PostgresCollection("postgresql://example")
    collection._vec_type = "vector"
    collection._table_am = "heap"
    collection._index_am = "hnsw"
    collection._setup_done = True

    monkeypatch.setattr(PostgresCollection, "_sql", property(lambda self: _FakeSql))
    monkeypatch.setattr(PostgresCollection, "_get_conn", lambda self: FakeConnection())
    monkeypatch.setattr(
        PostgresCollection,
        "_maybe_create_vector_index",
        lambda self, **kwargs: events.append(("index", kwargs)),
    )
    monkeypatch.setattr(
        PostgresCollection,
        "delete",
        lambda self, **kwargs: (_ for _ in ()).throw(AssertionError("delete must not run")),
    )

    collection.upsert(
        documents=["doc1", "doc2"],
        ids=["same", "same"],
        metadatas=[{"wing": "w", "room": "r"}, {"wing": "w2", "room": "r2"}],
        embeddings=[[0.1], [0.2]],
    )

    assert events[0] == "cursor"
    assert "FROM unnest(" in events[1][1]
    assert "ON CONFLICT (id) DO UPDATE" in events[1][1]
    assert events[1][2][0] == ["same"]
    assert events[2] == ("index", {"inserted_rows": 1})


def test_postgres_query_returns_typed_result_with_outer_shape(monkeypatch):
    events = []

    class FakeCursor:
        def execute(self, sql, params=None):
            events.append(("execute", str(sql), params))

        def fetchall(self):
            return [("id1", "doc1", "wing1", "room1", {"source_file": "a.py"}, 0.25, "[0.1,0.2]")]

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    collection = PostgresCollection("postgresql://example")
    collection._vec_type = "vector"
    collection._table_am = "heap"
    collection._setup_done = True
    monkeypatch.setattr(PostgresCollection, "_sql", property(lambda self: _FakeSql))
    monkeypatch.setattr(PostgresCollection, "_get_conn", lambda self: FakeConnection())

    result = collection.query(query_embeddings=[[0.1], [0.2]], include=["embeddings"])

    assert isinstance(result, QueryResult)
    assert result.ids == [["id1"], ["id1"]]
    assert result.documents == [[], []]
    assert result.metadatas == [[], []]
    assert result.distances == [[], []]
    assert result.embeddings == [[[0.1, 0.2]], [[0.1, 0.2]]]
    assert len(events) == 2
    assert "embedding::text" in events[0][1]


def test_postgres_get_returns_typed_result_with_embeddings(monkeypatch):
    class FakeCursor:
        def execute(self, sql, params=None):
            self.sql = str(sql)
            self.params = params

        def fetchall(self):
            return [("id1", "doc1", "wing1", "room1", {"source_file": "a.py"}, "[0.1,0.2]")]

    cursor = FakeCursor()

    class FakeConnection:
        def cursor(self):
            return cursor

    collection = PostgresCollection("postgresql://example")
    collection._setup_done = True
    monkeypatch.setattr(PostgresCollection, "_sql", property(lambda self: _FakeSql))
    monkeypatch.setattr(PostgresCollection, "_get_conn", lambda self: FakeConnection())

    result = collection.get(ids=["id1"], include=["documents", "metadatas", "embeddings"])

    assert isinstance(result, GetResult)
    assert result.ids == ["id1"]
    assert result.documents == ["doc1"]
    assert result.metadatas == [{"source_file": "a.py", "wing": "wing1", "room": "room1"}]
    assert result.embeddings == [[0.1, 0.2]]
    assert "embedding::text" in cursor.sql
    assert cursor.params == ["id1"]


def test_postgres_estimated_count_uses_catalog_stats_and_local_floor(monkeypatch):
    events = []

    class FakeCursor:
        def execute(self, sql, params=None):
            events.append(("execute", params))

        def fetchone(self):
            return (42,)

    class FakeConnection:
        def cursor(self):
            events.append("cursor")
            return FakeCursor()

    collection = PostgresCollection("postgresql://example")
    collection._local_row_estimate = 100
    monkeypatch.setattr(PostgresCollection, "_get_conn", lambda self: FakeConnection())

    assert collection._estimated_count() == 100
    assert events == ["cursor", ("execute", (collection.table_name,))]


def test_postgres_lazy_vector_index_create_is_serialized(monkeypatch):
    events = []

    class FakeCursor:
        def __init__(self):
            self.fetchone_results = [None, None]

        def execute(self, sql, params=None):
            events.append(("execute", str(sql), params))

        def fetchone(self):
            return self.fetchone_results.pop(0)

    cursor = FakeCursor()

    class FakeConnection:
        def cursor(self):
            events.append("cursor")
            return cursor

    collection = PostgresCollection("postgresql://example")
    collection._vec_type = "vector"
    collection._table_am = "heap"
    collection._index_am = "hnsw"
    collection._rows_since_index_check = VECTOR_INDEX_CHECK_INTERVAL_ROWS
    monkeypatch.setattr(PostgresCollection, "_sql", property(lambda self: _FakeSql))
    monkeypatch.setattr(PostgresCollection, "_get_conn", lambda self: FakeConnection())
    monkeypatch.setattr(
        PostgresCollection,
        "_estimated_count",
        lambda self: events.append(("estimated_count", None, None)) or VECTOR_INDEX_MIN_ROWS,
    )

    collection._maybe_create_vector_index(inserted_rows=1)

    executed_sql = [event[1] for event in events if event[0] == "execute"]
    assert "pg_advisory_lock" in executed_sql[1]
    assert "CREATE INDEX IF NOT EXISTS" in executed_sql[3]
    assert "pg_advisory_unlock" in executed_sql[4]
    assert collection._vector_index_ready is True


def test_postgres_lazy_vector_index_accepts_compatible_existing_index(monkeypatch):
    events = []

    class FakeCursor:
        def execute(self, sql, params=None):
            events.append(("execute", str(sql), params))

        def fetchone(self):
            return (1,)

    class FakeConnection:
        def cursor(self):
            events.append("cursor")
            return FakeCursor()

    collection = PostgresCollection("postgresql://example")
    collection._vec_type = "vector"
    collection._table_am = "heap"
    collection._index_am = "hnsw"
    collection._rows_since_index_check = VECTOR_INDEX_CHECK_INTERVAL_ROWS
    monkeypatch.setattr(PostgresCollection, "_get_conn", lambda self: FakeConnection())
    monkeypatch.setattr(
        PostgresCollection,
        "_estimated_count",
        lambda self: (_ for _ in ()).throw(AssertionError("count must not run")),
    )

    collection._maybe_create_vector_index(inserted_rows=1)

    assert collection._vector_index_ready is True
    assert len([event for event in events if event[0] == "execute"]) == 1
    assert "pg_advisory_lock" not in events[1][1]


def test_postgres_lazy_vector_index_unlocks_when_create_fails(monkeypatch):
    events = []

    class FakeCursor:
        def __init__(self):
            self.fetchone_results = [None, None]

        def execute(self, sql, params=None):
            sql_text = str(sql)
            events.append(("execute", sql_text, params))
            if "CREATE INDEX" in sql_text:
                raise RuntimeError("ddl failed")

        def fetchone(self):
            return self.fetchone_results.pop(0)

    cursor = FakeCursor()

    class FakeConnection:
        def cursor(self):
            return cursor

    collection = PostgresCollection("postgresql://example")
    collection._vec_type = "vector"
    collection._table_am = "heap"
    collection._index_am = "hnsw"
    collection._rows_since_index_check = VECTOR_INDEX_CHECK_INTERVAL_ROWS
    monkeypatch.setattr(PostgresCollection, "_sql", property(lambda self: _FakeSql))
    monkeypatch.setattr(PostgresCollection, "_get_conn", lambda self: FakeConnection())
    monkeypatch.setattr(PostgresCollection, "_estimated_count", lambda self: VECTOR_INDEX_MIN_ROWS)

    with pytest.raises(RuntimeError, match="ddl failed"):
        collection._maybe_create_vector_index(inserted_rows=1)

    assert "pg_advisory_unlock" in events[-1][1]
    assert collection._vector_index_ready is False


def test_palace_get_collection_selects_postgres_backend_from_env(monkeypatch):
    calls = []

    class FakeBackend:
        def get_collection(self, *, palace, collection_name, create, options=None):
            calls.append((palace, collection_name, create, options))
            return "postgres-collection"

    monkeypatch.setenv("MEMPALACE_BACKEND", "postgres")
    monkeypatch.setenv("MEMPALACE_POSTGRES_DSN", "postgresql://example")
    monkeypatch.setattr(palace, "get_backend", lambda name: FakeBackend())

    result = palace.get_collection("/ignored", create=True)

    assert result == "postgres-collection"
    assert calls[0][0] == PalaceRef(id="/ignored", local_path="/ignored")
    assert calls[0][1] == "mempalace_drawers"
    assert calls[0][2] is True
    assert calls[0][3] == {"dsn": "postgresql://example"}


def test_chroma_detect_matches_palace_with_chroma_sqlite(tmp_path):
    (tmp_path / "chroma.sqlite3").write_bytes(b"")
    assert ChromaBackend.detect(str(tmp_path)) is True
    assert ChromaBackend.detect(str(tmp_path.parent)) is False


def test_query_rejects_missing_input():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)
    with pytest.raises(ValueError):
        collection.query()


def test_query_rejects_both_texts_and_embeddings():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)
    with pytest.raises(ValueError):
        collection.query(query_texts=["q"], query_embeddings=[[0.1, 0.2]])


def test_query_rejects_empty_input_list():
    fake = _FakeCollection()
    collection = ChromaCollection(fake)
    with pytest.raises(ValueError):
        collection.query(query_texts=[])


def test_query_empty_preserves_embeddings_outer_shape_when_requested():
    fake = _FakeCollection(
        query_response={"ids": [], "documents": [], "metadatas": [], "distances": []}
    )
    collection = ChromaCollection(fake)

    requested = collection.query(query_texts=["q1", "q2"], include=["documents", "embeddings"])
    assert requested.embeddings == [[], []]

    not_requested = collection.query(query_texts=["q1", "q2"], include=["documents"])
    assert not_requested.embeddings is None


def test_chroma_cache_invalidates_when_db_file_missing(tmp_path):
    """A palace rebuild that removes chroma.sqlite3 must drop the stale cache.

    Primes backend._clients/_freshness directly with a sentinel rather than
    opening a real ``PersistentClient``: on Windows the sqlite file handle
    would still be live and ``Path.unlink`` would raise ``PermissionError``,
    making the test unable to exercise the branch we care about. The decision
    logic under test is pure (no chromadb calls before the branch), so a
    sentinel is sufficient.
    """
    backend = ChromaBackend()
    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    db_file = palace_path / "chroma.sqlite3"
    db_file.write_bytes(b"")  # any file is enough for _db_stat to see it
    st = db_file.stat()

    sentinel = object()
    backend._clients[str(palace_path)] = sentinel
    backend._freshness[str(palace_path)] = (st.st_ino, st.st_mtime)

    # Simulate a rebuild mid-flight: chroma.sqlite3 goes away. Safe to unlink
    # because nothing in this test is holding an OS handle on the file.
    db_file.unlink()

    prior_freshness = (st.st_ino, st.st_mtime)
    new_client = backend._client(str(palace_path))
    # Cache was replaced (not the sentinel) and freshness reflects the post-
    # rebuild stat (chromadb re-creates chroma.sqlite3 during PersistentClient
    # construction; _client re-stats after the constructor so freshness is
    # not frozen at the pre-rebuild value). The stale cached sentinel would
    # have served wrong data if returned.
    assert new_client is not sentinel
    assert backend._freshness[str(palace_path)] != prior_freshness


def test_chroma_cache_picks_up_db_created_after_first_open(tmp_path):
    """The 0 → nonzero stat transition invalidates a cache built before the DB existed."""
    backend = ChromaBackend()
    palace_path = tmp_path / "palace"
    palace_path.mkdir()

    # Seed an entry in the caches as if a prior _client() call had opened the
    # palace when chroma.sqlite3 did not exist yet. Freshness (0, 0.0) is the
    # signal that the DB was absent at cache time.
    sentinel = object()
    backend._clients[str(palace_path)] = sentinel
    backend._freshness[str(palace_path)] = (0, 0.0)

    # The DB file now appears (real chromadb would have created it by now).
    # Use a real chromadb call so _fix_blob_seq_ids and PersistentClient succeed.
    import chromadb as _chromadb

    _chromadb.PersistentClient(path=str(palace_path)).get_or_create_collection("seed")
    assert (palace_path / "chroma.sqlite3").is_file()

    # Next _client() call must detect the 0 → nonzero transition and rebuild.
    refreshed = backend._client(str(palace_path))
    assert refreshed is not sentinel
    assert backend._freshness[str(palace_path)] != (0, 0.0)


def test_base_collection_update_default_rejects_mismatched_lengths():
    """The ABC default update() raises ValueError rather than silently misaligning."""
    from mempalace.backends.base import BaseCollection

    collection = ChromaCollection(_FakeCollection())

    with pytest.raises(ValueError, match="documents length"):
        BaseCollection.update(collection, ids=["1", "2"], documents=["only-one"])

    with pytest.raises(ValueError, match="metadatas length"):
        BaseCollection.update(collection, ids=["1", "2"], metadatas=[{"k": 9}])


def test_chroma_backend_accepts_palace_ref_kwarg(tmp_path):
    palace_path = tmp_path / "palace"
    backend = ChromaBackend()
    collection = backend.get_collection(
        palace=PalaceRef(id=str(palace_path), local_path=str(palace_path)),
        collection_name="mempalace_drawers",
        create=True,
    )
    assert palace_path.is_dir()
    assert isinstance(collection, ChromaCollection)


def test_chroma_backend_create_false_raises_without_creating_directory(tmp_path):
    palace_path = tmp_path / "missing-palace"

    with pytest.raises(FileNotFoundError):
        ChromaBackend().get_collection(
            str(palace_path),
            collection_name="mempalace_drawers",
            create=False,
        )

    assert not palace_path.exists()


def test_chroma_backend_create_true_creates_directory_and_collection(tmp_path):
    palace_path = tmp_path / "palace"

    collection = ChromaBackend().get_collection(
        str(palace_path),
        collection_name="mempalace_drawers",
        create=True,
    )

    assert palace_path.is_dir()
    assert isinstance(collection, ChromaCollection)

    client = chromadb.PersistentClient(path=str(palace_path))
    client.get_collection("mempalace_drawers")


def test_chroma_backend_creates_collection_with_cosine_distance(tmp_path):
    palace_path = tmp_path / "palace"

    ChromaBackend().get_collection(
        str(palace_path),
        collection_name="mempalace_drawers",
        create=True,
    )

    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.get_collection("mempalace_drawers")
    assert col.metadata.get("hnsw:space") == "cosine"


def test_fix_blob_seq_ids_converts_blobs_to_integers(tmp_path):
    """Simulate a ChromaDB 0.6.x database with BLOB seq_ids and verify repair."""
    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id)")
    conn.execute("CREATE TABLE max_seq_id (rowid INTEGER PRIMARY KEY, seq_id)")
    # Insert BLOB seq_ids like ChromaDB 0.6.x would
    blob_42 = (42).to_bytes(8, byteorder="big")
    blob_99 = (99).to_bytes(8, byteorder="big")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (?)", (blob_42,))
    conn.execute("INSERT INTO max_seq_id (seq_id) VALUES (?)", (blob_99,))
    conn.commit()
    conn.close()

    _fix_blob_seq_ids(str(tmp_path))

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings").fetchone()
    assert row == (42, "integer")
    row = conn.execute("SELECT seq_id, typeof(seq_id) FROM max_seq_id").fetchone()
    assert row == (99, "integer")
    conn.close()


def test_fix_blob_seq_ids_noop_without_blobs(tmp_path):
    """No error when seq_ids are already integers."""
    db_path = tmp_path / "chroma.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE embeddings (rowid INTEGER PRIMARY KEY, seq_id INTEGER)")
    conn.execute("INSERT INTO embeddings (seq_id) VALUES (42)")
    conn.commit()
    conn.close()

    _fix_blob_seq_ids(str(tmp_path))

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT seq_id, typeof(seq_id) FROM embeddings").fetchone()
    assert row == (42, "integer")
    conn.close()


def test_fix_blob_seq_ids_noop_without_database(tmp_path):
    """No error when palace has no chroma.sqlite3."""
    _fix_blob_seq_ids(str(tmp_path))  # should not raise
