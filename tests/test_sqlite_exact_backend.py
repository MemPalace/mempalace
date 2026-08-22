import math
import os
import re
import sqlite3
import subprocess
import sys
import threading

import pytest

from _chroma_palace_helper import make_minimal_chroma_sqlite, make_minimal_sqlite_exact_sqlite

import mempalace.backends.sqlite_exact as sqlite_exact_module
from mempalace.backends import (
    BackendMismatchError,
    CollectionNotInitializedError,
    DimensionMismatchError,
    PalaceRef,
    QueryResult,
    UnsupportedCapabilityError,
    available_backends,
)
from mempalace.backends.sqlite_exact import SQLiteExactBackend


def _collection(tmp_path, name="mempalace_drawers", create=True):
    backend = SQLiteExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend, backend.get_collection(palace=palace, collection_name=name, create=create)


def test_sqlite_exact_missing_collection_error_names_collection(tmp_path):
    """CollectionNotInitializedError must identify the missing collection, not
    the palace path — consistent with line 287 and the other backends."""
    backend, _ = _collection(tmp_path, name="mempalace_drawers")
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    with pytest.raises(CollectionNotInitializedError) as exc:
        backend.get_collection(palace=palace, collection_name="does_not_exist", create=False)
    assert "does_not_exist" in str(exc.value)
    assert str(tmp_path) not in str(exc.value)

    with pytest.raises(CollectionNotInitializedError) as exc2:
        backend.delete_collection(str(tmp_path), "also_missing")
    assert "also_missing" in str(exc2.value)
    assert str(tmp_path) not in str(exc2.value)


def test_registry_exposes_sqlite_exact():
    assert "sqlite_exact" in available_backends()


def test_sqlite_exact_add_query_filters_and_persistence(tmp_path):
    backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=[
            "alpha vector memory",
            "beta sqlite exact memory",
            "gamma filtered memory",
        ],
        metadatas=[
            {"wing": "alpha", "room": "notes", "chunk_index": 0, "tags": "core,vector"},
            {"wing": "alpha", "room": "notes", "chunk_index": 1, "tags": "sqlite,exact"},
            {"wing": "gamma", "room": "archive", "chunk_index": 2, "tags": "old"},
        ],
        embeddings=[[1.0, 0.0], [0.0, 1.0], [0.2, 0.8]],
    )

    ranked = col.query(query_embeddings=[[1.0, 0.0]], n_results=3)
    assert ranked.ids[0] == ["a", "c", "b"]
    assert ranked.distances[0][0] == pytest.approx(0.0)

    filtered = col.get(
        where={
            "$and": [
                {"wing": "alpha"},
                {"chunk_index": {"$gte": 1}},
                {"tags": {"$contains": "sqlite"}},
            ]
        },
        include=["documents", "metadatas", "embeddings"],
    )
    assert filtered.ids == ["b"]
    assert filtered.documents == ["beta sqlite exact memory"]
    assert filtered.embeddings == [[0.0, 1.0]]

    col.update(ids=["b"], metadatas=[{"room": "lab"}])
    assert col.get(ids=["b"]).metadatas[0]["room"] == "lab"

    backend.close_palace(str(tmp_path))
    reopened = backend.get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="mempalace_drawers",
        create=False,
    )
    assert reopened.count() == 3
    assert reopened.get(ids=["a"]).documents == ["alpha vector memory"]


def test_sqlite_exact_write_failure_rolls_back_whole_batch(tmp_path):
    _backend, col = _collection(tmp_path)

    with pytest.raises(Exception):
        col.add(
            ids=["dup", "dup"],
            documents=["first write", "duplicate write"],
            metadatas=[{}, {}],
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
        )

    assert col.count() == 0


def test_sqlite_exact_enforces_collection_dimension(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(ids=["a"], documents=["two dims"], metadatas=[{}], embeddings=[[1.0, 0.0]])

    with pytest.raises(DimensionMismatchError):
        col.add(ids=["b"], documents=["three dims"], metadatas=[{}], embeddings=[[1.0, 0.0, 0.0]])
    with pytest.raises(DimensionMismatchError):
        col.upsert(
            ids=["b"], documents=["three dims"], metadatas=[{}], embeddings=[[1.0, 0.0, 0.0]]
        )
    with pytest.raises(DimensionMismatchError):
        col.update(ids=["a"], embeddings=[[1.0, 0.0, 0.0]])
    with pytest.raises(DimensionMismatchError):
        col.query(query_embeddings=[[1.0, 0.0, 0.0]], n_results=1)

    assert col.count() == 1
    assert col.get(ids=["a"]).documents == ["two dims"]


def test_sqlite_exact_get_preserves_requested_id_order_and_duplicates(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b"],
        documents=["doc a", "doc b"],
        metadatas=[{}, {}],
        embeddings=[[1, 0], [0, 1]],
    )

    result = col.get(ids=["b", "a", "b"], include=["documents"])

    assert result.ids == ["b", "a", "b"]
    assert result.documents == ["doc b", "doc a", "doc b"]


def _doc_select_sql(col, action):
    """Run ``action`` while tracing SQL; return (result, [documents SELECTs]).

    The documents-table scan in ``_rows`` is the only statement that is both
    ``FROM documents`` and ``ORDER BY rowid`` (``count`` lacks the ORDER BY),
    so filtering on both isolates it from collection-id lookups and commits.
    """
    statements = []
    conn = col._handle.conn
    conn.set_trace_callback(statements.append)
    try:
        result = action()
    finally:
        conn.set_trace_callback(None)
    selects = [s for s in statements if "FROM documents" in s and "ORDER BY rowid" in s]
    return result, selects


def _seed(col, n):
    col.add(
        ids=[f"d{i}" for i in range(n)],
        documents=[f"doc {i}" for i in range(n)],
        metadatas=[{"wing": "w", "n": i} for i in range(n)],
        embeddings=[[float(i), 1.0] for i in range(n)],
    )


def test_sqlite_exact_get_unfiltered_page_pushes_limit_offset(tmp_path):
    _backend, col = _collection(tmp_path)
    _seed(col, 10)

    result, selects = _doc_select_sql(
        col, lambda: col.get(limit=3, offset=2, include=["documents"])
    )

    assert result.ids == ["d2", "d3", "d4"]
    assert result.documents == ["doc 2", "doc 3", "doc 4"]
    assert len(selects) == 1
    assert "LIMIT" in selects[0]
    assert "OFFSET" in selects[0]


def test_sqlite_exact_get_equality_filter_pushes_limit(tmp_path):
    _backend, col = _collection(tmp_path)
    _seed(col, 6)

    result, selects = _doc_select_sql(
        col,
        lambda: col.get(where={"wing": "w"}, limit=2, offset=1, include=["metadatas"]),
    )

    assert result.ids == ["d1", "d2"]
    assert len(selects) == 1
    assert "LIMIT" in selects[0]
    assert "OFFSET" in selects[0]
    assert "embedding" not in selects[0].split("FROM documents")[0]
    assert "document" not in selects[0].split("FROM documents")[0]


def test_sqlite_exact_get_offset_only_and_limit_only_push(tmp_path):
    _backend, col = _collection(tmp_path)
    _seed(col, 5)

    limit_only, limit_sql = _doc_select_sql(col, lambda: col.get(limit=2))
    assert limit_only.ids == ["d0", "d1"]
    assert len(limit_sql) == 1
    assert "LIMIT" in limit_sql[0]
    assert "OFFSET" not in limit_sql[0]

    offset_only, offset_sql = _doc_select_sql(col, lambda: col.get(offset=3))
    assert offset_only.ids == ["d3", "d4"]
    assert len(offset_sql) == 1
    assert "OFFSET" in offset_sql[0]
    # SQLite requires a LIMIT before OFFSET; an offset-only page uses LIMIT -1.
    assert "LIMIT" in offset_sql[0]


def test_sqlite_exact_get_negative_bounds_use_python_slice(tmp_path):
    _backend, col = _collection(tmp_path)
    _seed(col, 5)

    # Negative limit means Python "all but last", which a SQL LIMIT (negative ==
    # unbounded in SQLite) cannot express, so it must stay on the slice path.
    neg_limit, neg_limit_sql = _doc_select_sql(col, lambda: col.get(limit=-1))
    assert neg_limit.ids == ["d0", "d1", "d2", "d3"]
    assert len(neg_limit_sql) == 1
    assert "LIMIT" not in neg_limit_sql[0]

    # Negative offset means Python "last N"; it must not reach SQL either.
    neg_offset, neg_offset_sql = _doc_select_sql(col, lambda: col.get(offset=-2))
    assert neg_offset.ids == ["d3", "d4"]
    assert len(neg_offset_sql) == 1
    assert "OFFSET" not in neg_offset_sql[0]


def test_sqlite_exact_get_pages_tile_without_overlap(tmp_path):
    _backend, col = _collection(tmp_path)
    _seed(col, 10)

    seen = []
    offset = 0
    while True:
        page = col.get(limit=4, offset=offset)
        if not page.ids:
            break
        seen.extend(page.ids)
        offset += len(page.ids)

    assert seen == [f"d{i}" for i in range(10)]
    # The same set, same rowid order, as a single unfiltered scan.
    assert col.get().ids == seen


def test_sqlite_exact_get_limit_zero_pushes_empty_page(tmp_path):
    _backend, col = _collection(tmp_path)
    _seed(col, 3)

    # limit=0 is a real bound, not "no limit": it pushes LIMIT 0 and returns
    # nothing, matching the old rows[:0] slice. Guards the `is not None` check
    # against an `if limit:` regression that would treat 0 as unbounded.
    result, selects = _doc_select_sql(col, lambda: col.get(limit=0))
    assert result.ids == []
    assert len(selects) == 1
    assert "LIMIT" in selects[0]


def test_sqlite_exact_get_offset_zero_is_a_full_scan(tmp_path):
    _backend, col = _collection(tmp_path)
    _seed(col, 3)

    # offset=0 with no limit is not a page request, so it stays on the full scan.
    result, selects = _doc_select_sql(col, lambda: col.get(offset=0))
    assert result.ids == ["d0", "d1", "d2"]
    assert len(selects) == 1
    assert "LIMIT" not in selects[0]
    assert "OFFSET" not in selects[0]


def test_sqlite_exact_get_ids_looks_up_by_primary_key(tmp_path):
    _backend, col = _collection(tmp_path)
    _seed(col, 5)

    statements = []
    conn = col._handle.conn
    conn.set_trace_callback(statements.append)
    try:
        result = col.get(ids=["d4", "d3", "d2", "d1"], offset=1, limit=2)
    finally:
        conn.set_trace_callback(None)

    assert result.ids == ["d3", "d2"]
    in_selects = [s for s in statements if "FROM documents" in s and "IN (" in s]
    assert in_selects
    assert all("ORDER BY rowid" not in s for s in in_selects)


def test_sqlite_exact_upsert_delete_and_multi_collection_isolation(tmp_path):
    backend, drawers = _collection(tmp_path, "drawers")
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    closets = backend.get_collection(palace=palace, collection_name="closets", create=True)

    drawers.upsert(
        ids=["same"], documents=["drawer one"], metadatas=[{"kind": "drawer"}], embeddings=[[1, 0]]
    )
    closets.upsert(
        ids=["same"], documents=["closet one"], metadatas=[{"kind": "closet"}], embeddings=[[0, 1]]
    )
    drawers.upsert(
        ids=["same"],
        documents=["drawer replaced"],
        metadatas=[{"kind": "drawer", "version": 2}],
        embeddings=[[1, 0]],
    )

    assert drawers.count() == 1
    assert closets.count() == 1
    assert drawers.get(ids=["same"]).documents == ["drawer replaced"]
    assert closets.get(ids=["same"]).documents == ["closet one"]

    drawers.delete(where={"version": {"$in": [2, 3]}})
    assert drawers.count() == 0
    assert closets.count() == 1


def test_sqlite_exact_lexical_search_and_python_fallback(tmp_path, monkeypatch):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b", "c"],
        documents=[
            "ordinary project note",
            "rareterm rareterm sqlite exact note",
            "rareterm unrelated archive",
        ],
        metadatas=[
            {"wing": "w", "room": "a"},
            {"wing": "w", "room": "b"},
            {"wing": "old", "room": "b"},
        ],
        embeddings=[[1, 0], [0, 1], [0.5, 0.5]],
    )

    hits = col.lexical_search(query="rareterm sqlite", n_results=2, where={"wing": "w"}).hits
    assert [hit.id for hit in hits] == ["b"]

    monkeypatch.setattr(col, "_fts_available", lambda _cur: False)
    fallback_hits = col.lexical_search(query="rareterm sqlite", n_results=2).hits
    assert fallback_hits[0].id == "b"


def test_sqlite_exact_lexical_search_filters_after_full_fts_window(tmp_path):
    _backend, col = _collection(tmp_path)
    ids = [f"old-{i}" for i in range(12)] + ["target"]
    col.add(
        ids=ids,
        documents=["needle shared lexical note" for _ in ids],
        metadatas=[{"wing": "old"} for _ in range(12)] + [{"wing": "target"}],
        embeddings=[[1.0, 0.0] for _ in ids],
    )

    hits = col.lexical_search(query="needle", n_results=1, where={"wing": "target"}).hits

    assert [hit.id for hit in hits] == ["target"]


def test_sqlite_exact_logical_filters_evaluate_sibling_predicates(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a", "b"],
        documents=["alpha document", "beta document"],
        metadatas=[
            {"wing": "w", "room": "wrong", "kind": "note"},
            {"wing": "w", "room": "right", "kind": "note"},
        ],
        embeddings=[[1, 0], [0, 1]],
    )

    result = col.get(where={"$and": [{"wing": "w"}], "room": "right"})

    assert result.ids == ["b"]


def test_sqlite_exact_close_palace_marks_existing_collections_closed(tmp_path):
    backend, col = _collection(tmp_path)
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    col.add(ids=["a"], documents=["doc"], metadatas=[{}], embeddings=[[1, 0]])

    backend.close_palace(palace)

    assert not col.health().ok
    with pytest.raises(Exception):
        col.count()


def test_sqlite_exact_read_only_open_skips_schema_init_and_refuses_writes(tmp_path):
    backend, col = _collection(tmp_path)
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    col.add(ids=["a"], documents=["doc"], metadatas=[{}], embeddings=[[1, 0]])
    backend.close_palace(palace)

    db_path = tmp_path / "sqlite_exact.sqlite3"
    before = db_path.read_bytes()
    assert not (tmp_path / "sqlite_exact.sqlite3-wal").exists()
    assert not (tmp_path / "sqlite_exact.sqlite3-shm").exists()
    db_path.chmod(0o400)
    tmp_path.chmod(0o500)
    try:
        read_only = backend.get_collection(
            palace=palace,
            collection_name="mempalace_drawers",
            create=False,
            options={"read_only": True},
        )

        assert read_only.count() == 1
        assert read_only._handle.read_only is True
        assert read_only._handle.conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            read_only.add(
                ids=["b"],
                documents=["blocked"],
                metadatas=[{}],
                embeddings=[[1, 0]],
            )
        assert not (tmp_path / "sqlite_exact.sqlite3-wal").exists()
        assert not (tmp_path / "sqlite_exact.sqlite3-shm").exists()
    finally:
        tmp_path.chmod(0o700)
        db_path.chmod(0o600)
        backend.close_palace(palace)
    assert db_path.read_bytes() == before


def test_sqlite_exact_read_only_open_sees_active_writer_wal(tmp_path):
    writer_backend, writer = _collection(tmp_path)
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    writer.add(ids=["wal-row"], documents=["uncheckpointed"], metadatas=[{}], embeddings=[[1, 0]])

    db_path = tmp_path / "sqlite_exact.sqlite3"
    wal_path = tmp_path / "sqlite_exact.sqlite3-wal"
    shm_path = tmp_path / "sqlite_exact.sqlite3-shm"
    assert wal_path.is_file()
    assert shm_path.is_file()
    before = {path: path.read_bytes() for path in (db_path, wal_path, shm_path)}
    for path in before:
        path.chmod(0o400)
    tmp_path.chmod(0o500)
    try:
        reader_code = """
import sys
from mempalace.backends import PalaceRef
from mempalace.backends.sqlite_exact import SQLiteExactBackend

backend = SQLiteExactBackend()
palace = PalaceRef(id=sys.argv[1], local_path=sys.argv[1])
reader = backend.get_collection(
    palace=palace,
    collection_name="mempalace_drawers",
    create=False,
    options={"read_only": True},
)
print(reader.get(ids=["wal-row"]).documents[0])
backend.close_palace(palace)
"""
        result = subprocess.run(
            [sys.executable, "-c", reader_code, str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "uncheckpointed"
        assert {path: path.read_bytes() for path in before} == before
    finally:
        tmp_path.chmod(0o700)
        for path in before:
            path.chmod(0o600)
        writer_backend.close_palace(palace)


def test_sqlite_exact_read_only_reopens_when_wal_appears_after_immutable_open(tmp_path):
    """An immutable clean-database reader must reopen once a writer starts.

    If a read-only MCP opens a cleanly closed palace before any writer is
    alive, the connection uses immutable=1. A later daemon/HTTP writer creates
    WAL sidecars; the cached immutable handle must not keep serving the
    pre-writer snapshot forever.
    """
    writer_backend, writer = _collection(tmp_path)
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    writer.add(
        ids=["seed"],
        documents=["seed drawer"],
        metadatas=[{}],
        embeddings=[[1, 0]],
    )
    # Force a full checkpoint so the on-disk database is clean (no WAL) when
    # the first read-only open happens.
    with writer._handle.lock:
        writer._handle.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer._handle.conn.commit()
    writer_backend.close_palace(palace)

    db_path = tmp_path / "sqlite_exact.sqlite3"
    wal_path = tmp_path / "sqlite_exact.sqlite3-wal"
    shm_path = tmp_path / "sqlite_exact.sqlite3-shm"
    assert db_path.is_file()
    assert not wal_path.exists()
    assert not shm_path.exists()

    reader_backend = SQLiteExactBackend()
    first = reader_backend.get_collection(
        palace=palace,
        collection_name="mempalace_drawers",
        create=False,
        options={"read_only": True},
    )
    assert first.count() == 1
    first_handle = first._handle
    assert first_handle.immutable is True
    assert first_handle.read_only is True

    # A peer writer starts after the immutable reader cached its connection.
    later_writer_backend, later_writer = _collection(tmp_path)
    later_writer.add(
        ids=["post-writer"],
        documents=["written after immutable open"],
        metadatas=[{}],
        embeddings=[[0, 1]],
    )
    assert wal_path.is_file()
    assert shm_path.is_file()

    # Same backend instance: cache hit must detect WAL and reopen mode=ro.
    second = reader_backend.get_collection(
        palace=palace,
        collection_name="mempalace_drawers",
        create=False,
        options={"read_only": True},
    )
    assert first_handle.closed is True
    assert second._handle is not first_handle
    assert second._handle.immutable is False
    assert second.count() == 2
    got = second.get(ids=["post-writer"])
    assert got.documents[0] == "written after immutable open"

    later_writer_backend.close_palace(palace)
    reader_backend.close_palace(palace)


def test_sqlite_exact_immutable_reader_keeps_cache_on_partial_wal_sidecar(tmp_path):
    """A lone -wal or -shm file must not retire the immutable reader.

    Incomplete sidecar pairs are a transient mid-open state; forcing a
    reconnect would raise from ``_connect_read_only`` and break recall.
    """
    writer_backend, writer = _collection(tmp_path)
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    writer.add(ids=["seed"], documents=["seed"], metadatas=[{}], embeddings=[[1, 0]])
    with writer._handle.lock:
        writer._handle.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer._handle.conn.commit()
    writer_backend.close_palace(palace)

    wal_path = tmp_path / "sqlite_exact.sqlite3-wal"
    shm_path = tmp_path / "sqlite_exact.sqlite3-shm"
    assert not wal_path.exists() and not shm_path.exists()

    reader_backend = SQLiteExactBackend()
    first = reader_backend.get_collection(
        palace=palace,
        collection_name="mempalace_drawers",
        create=False,
        options={"read_only": True},
    )
    first_handle = first._handle
    assert first_handle.immutable is True

    # Simulate a torn writer open: only one sidecar present.
    wal_path.write_bytes(b"not-a-real-wal")
    second = reader_backend.get_collection(
        palace=palace,
        collection_name="mempalace_drawers",
        create=False,
        options={"read_only": True},
    )
    assert second._handle is first_handle
    assert first_handle.closed is False
    assert second.count() == 1

    reader_backend.close_palace(palace)


def test_sqlite_exact_direct_write_contends_with_palace_owner(tmp_path, monkeypatch):
    from mempalace.palace import MineAlreadyRunning

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend, col = _collection(tmp_path)
    holder_code = """
import sys
from mempalace.palace import mine_palace_lock
with mine_palace_lock(sys.argv[1]):
    print("ready", flush=True)
    sys.stdin.read()
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(MineAlreadyRunning):
            col.add(ids=["blocked"], documents=["doc"], metadatas=[{}], embeddings=[[1, 0]])
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(timeout=10)
        backend.close()


@pytest.mark.parametrize("operation", ["add", "vacuum"])
def test_sqlite_exact_waiting_thread_reacquires_palace_lease(tmp_path, monkeypatch, operation):
    """A thread queued on the handle must not inherit stale re-entrant credit.

    Thread A owns both the handle and palace locks. Thread B reaches the handle
    while A still owns the palace, then pauses immediately after the handle is
    released. An external process acquires the palace before B continues. B
    must contend again and refuse both ordinary writes and VACUUM.
    """
    from mempalace.palace import MineAlreadyRunning

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    backend, col = _collection(tmp_path)
    col.add(ids=["seed"], documents=["seed"], metadatas=[{}], embeddings=[[1, 0]])

    release_a = threading.Event()
    a_ready = threading.Event()
    b_handle_attempted = threading.Event()
    b_has_handle = threading.Event()
    allow_b = threading.Event()
    writer_ref = {"thread": None}

    class CoordinatedRLock:
        def __init__(self):
            self._lock = threading.RLock()
            self._writer_coordinated = False

        def __enter__(self):
            is_writer = threading.current_thread() is writer_ref["thread"]
            if is_writer and not self._writer_coordinated:
                self._writer_coordinated = True
                b_handle_attempted.set()
            self._lock.acquire()
            if is_writer and self._writer_coordinated and not b_has_handle.is_set():
                b_has_handle.set()
                if not allow_b.wait(10):
                    self._lock.release()
                    raise AssertionError("timed out waiting to resume writer B")
            return self

        def __exit__(self, *exc):
            self._lock.release()
            return False

    col._handle.lock = CoordinatedRLock()
    errors = {}

    def owner_a():
        try:
            with col._cursor(write=True):
                a_ready.set()
                if not release_a.wait(10):
                    raise AssertionError("timed out waiting to release writer A")
        except BaseException as exc:  # pragma: no cover - diagnostic path
            errors["a"] = exc

    if operation == "vacuum":
        monkeypatch.setattr(
            col,
            "maintenance_state",
            lambda: {"row_count": 1, "page_count": 1, "freelist_pages": 0},
        )

    def writer_b():
        try:
            if operation == "add":
                col.add(
                    ids=["writer-b"],
                    documents=["must not be written"],
                    metadatas=[{}],
                    embeddings=[[0, 1]],
                )
            else:
                col.run_maintenance("compact")
        except BaseException as exc:
            errors["b"] = exc

    thread_a = threading.Thread(target=owner_a, name="sqlite-owner-a", daemon=True)
    thread_b = threading.Thread(target=writer_b, name="sqlite-writer-b", daemon=True)
    writer_ref["thread"] = thread_b
    holder = None
    try:
        thread_a.start()
        assert a_ready.wait(10), "writer A did not acquire both locks"

        thread_b.start()
        assert b_handle_attempted.wait(10), "writer B did not reach the handle lock"

        release_a.set()
        assert b_has_handle.wait(10), "writer B did not acquire the released handle"
        thread_a.join(timeout=10)
        assert not thread_a.is_alive(), "writer A did not release the palace lease"
        assert "a" not in errors

        holder_code = """
import sys
from mempalace.palace import mine_palace_lock
with mine_palace_lock(sys.argv[1]):
    print("ready", flush=True)
    sys.stdin.read()
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(tmp_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"

        allow_b.set()
        thread_b.join(timeout=10)
        assert not thread_b.is_alive(), "writer B did not finish contention"
        assert isinstance(errors.get("b"), MineAlreadyRunning)

        if operation == "add":
            assert col.get(ids=["writer-b"]).ids == []
    finally:
        release_a.set()
        allow_b.set()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)
        if holder is not None:
            if holder.stdin is not None:
                holder.stdin.close()
            holder.wait(timeout=10)
        backend.close()


def test_palace_wrapper_embeds_for_sqlite_exact(tmp_path, monkeypatch):
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    from mempalace.palace import get_collection

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
    monkeypatch.setattr(
        embedding_wrapper,
        "_embed_texts",
        lambda texts: [[float(len(text)), 1.0] for text in texts],
    )

    col = get_collection(str(tmp_path), create=True)
    col.add(ids=["a"], documents=["abcd"], metadatas=[{"wing": "w"}])

    result = col.query(query_texts=["abcd"], n_results=1)
    assert result.ids == [["a"]]


def test_backend_mismatch_protection(tmp_path, monkeypatch):
    from mempalace.palace import get_collection

    make_minimal_chroma_sqlite(tmp_path)
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")

    with pytest.raises(BackendMismatchError):
        get_collection(str(tmp_path), create=True)


def test_mixed_backend_artifacts_are_rejected_even_when_chroma_selected(tmp_path, monkeypatch):
    from mempalace.palace import resolve_backend_name

    make_minimal_chroma_sqlite(tmp_path)
    make_minimal_sqlite_exact_sqlite(tmp_path)
    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "chroma")

    with pytest.raises(BackendMismatchError):
        resolve_backend_name(str(tmp_path))


def test_sqlite_exact_detect_matches_palace_with_sqlite_header(tmp_path):
    """A real SQLite database at ``<path>/sqlite_exact.sqlite3`` registers
    as sqlite_exact. Mirrors the chroma analog at
    ``test_chroma_detect_matches_palace_with_sqlite_header``.
    """
    make_minimal_sqlite_exact_sqlite(tmp_path)
    assert SQLiteExactBackend.detect(str(tmp_path)) is True
    assert SQLiteExactBackend.detect(str(tmp_path.parent)) is False


def test_sqlite_exact_detect_rejects_empty_sqlite_exact_sqlite(tmp_path):
    """A 0-byte ``sqlite_exact.sqlite3`` is not a sqlite_exact palace (#1893).

    Same root cause as the chroma side: bare ``sqlite3.connect()`` against
    a missing path leaves a 0-byte file behind because the SQLite header is
    written on the first statement, not on connect. Detection must reject
    that artifact so it cannot trip ``BackendMismatchError`` against a real
    non-sqlite_exact backend marker in the same directory.
    """
    (tmp_path / "sqlite_exact.sqlite3").write_bytes(b"")
    assert SQLiteExactBackend.detect(str(tmp_path)) is False


def test_sqlite_exact_detect_rejects_non_sqlite_file(tmp_path):
    """A non-SQLite file at the ``sqlite_exact.sqlite3`` path is not
    sqlite_exact. Defends against partial writes / garbage content / anything
    that lands at the canonical path but isn't actually a SQLite database.
    """
    (tmp_path / "sqlite_exact.sqlite3").write_bytes(b"not a sqlite file" * 4)
    assert SQLiteExactBackend.detect(str(tmp_path)) is False


def test_sqlite_exact_exact_ranking_uses_cosine(tmp_path):
    _backend, col = _collection(tmp_path)
    halfway = [0.5, math.sqrt(0.75)]
    col.add(
        ids=["half", "orthogonal", "same"],
        documents=["half", "orthogonal", "same"],
        metadatas=[{}, {}, {}],
        embeddings=[halfway, [0.0, 1.0], [1.0, 0.0]],
    )

    result = col.query(query_embeddings=[[1.0, 0.0]], n_results=3)
    assert result.ids[0] == ["same", "half", "orthogonal"]
    assert result.distances[0] == pytest.approx([0.0, 0.5, 1.0])


def test_search_union_uses_sqlite_exact_lexical_search(tmp_path, monkeypatch):
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    from mempalace.palace import get_collection
    from mempalace.searcher import search_memories

    def fake_embed(texts):
        vectors = []
        for text in texts:
            if text == "rareterm":
                vectors.append([1.0, 0.0])
            elif "rareterm" in text:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.5, math.sqrt(0.75)])
        return vectors

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
    monkeypatch.setattr(embedding_wrapper, "_embed_texts", fake_embed)

    col = get_collection(str(tmp_path), create=True)
    col.add(
        ids=["d1", "d2", "d3", "rare"],
        documents=[
            "ordinary support note",
            "ordinary billing note",
            "ordinary project note",
            "rareterm rareterm rareterm policy note",
        ],
        metadatas=[
            {"wing": "w", "room": "r", "source_file": "/tmp/d1.md", "chunk_index": 0},
            {"wing": "w", "room": "r", "source_file": "/tmp/d2.md", "chunk_index": 0},
            {"wing": "w", "room": "r", "source_file": "/tmp/d3.md", "chunk_index": 0},
            {"wing": "w", "room": "r", "source_file": "/tmp/rare.md", "chunk_index": 0},
        ],
    )

    result = search_memories(
        "rareterm",
        str(tmp_path),
        n_results=1,
        candidate_strategy="union",
    )

    assert result["results"][0]["source_file"] == "rare.md"
    assert result["results"][0]["matched_via"] == "bm25_backend"


def test_search_closets_use_lexical_not_vector_on_sqlite_exact(tmp_path, monkeypatch):
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    from mempalace.backends.sqlite_exact import SQLiteExactCollection
    from mempalace.palace import get_collection, get_closets_collection
    from mempalace.searcher import search_memories

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")
    monkeypatch.setattr(
        embedding_wrapper, "_embed_texts", lambda texts: [[1.0, 0.0] for _ in texts]
    )

    drawers = get_collection(str(tmp_path), create=True)
    closets = get_closets_collection(str(tmp_path), create=True)
    drawers.add(
        ids=["d1"],
        documents=["meshguard trust path"],
        metadatas=[{"source_file": "a.md", "wing": "w", "room": "r", "chunk_index": 0}],
    )
    closets.add(
        ids=["c1"],
        documents=["topic|meshguard|→d1"],
        metadatas=[{"source_file": "a.md", "wing": "w"}],
    )

    called = {"query": 0, "lex": 0}
    orig_query = SQLiteExactCollection.query
    orig_lex = SQLiteExactCollection.lexical_search

    def wrapped_query(self, *args, **kwargs):
        if self._collection_name == "mempalace_closets":
            called["query"] += 1
        return orig_query(self, *args, **kwargs)

    def wrapped_lex(self, *args, **kwargs):
        if self._collection_name == "mempalace_closets":
            called["lex"] += 1
        return orig_lex(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteExactCollection, "query", wrapped_query)
    monkeypatch.setattr(SQLiteExactCollection, "lexical_search", wrapped_lex)

    result = search_memories("meshguard", str(tmp_path), n_results=1)
    assert "error" not in result
    assert called["lex"] == 1
    assert called["query"] == 0


def test_search_union_reports_unsupported_lexical_capability(monkeypatch, tmp_path):
    import mempalace.searcher as searcher

    class NoLexicalCollection:
        def query(self, **_kwargs):
            return QueryResult(
                ids=[["a"]],
                documents=[["ordinary note"]],
                metadatas=[[{"source_file": "/tmp/a.md", "chunk_index": 0}]],
                distances=[[0.5]],
            )

        def lexical_search(self, **_kwargs):
            raise UnsupportedCapabilityError("no lexical support")

    monkeypatch.setattr(searcher, "get_collection", lambda *_args, **_kwargs: NoLexicalCollection())
    monkeypatch.setattr(
        searcher,
        "get_closets_collection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("no closets")),
    )

    result = searcher.search_memories(
        "anything",
        str(tmp_path),
        n_results=1,
        candidate_strategy="union",
    )

    assert result["unsupported_capability"] == "supports_lexical_search"


def test_search_vector_disabled_fallback_is_chroma_only(tmp_path, monkeypatch):
    from mempalace.searcher import search_memories

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", "sqlite_exact")

    result = search_memories("anything", str(tmp_path), vector_disabled=True)

    assert result["unsupported_capability"] == "chroma_hnsw_fallback"
    assert result["backend"] == "sqlite_exact"


def test_concurrent_first_open_single_connection_no_leak(tmp_path, monkeypatch):
    """Two threads first-opening the same palace concurrently must share one
    handle and one sqlite connection.

    The barrier inside the patched ``sqlite3.connect`` releases immediately
    only when both threads pass the cache-miss check together: the broken
    interleaving, which also ran ``_init_schema`` concurrently on a fresh
    file and surfaced "database is locked". With creation serialized under
    ``_clients_lock`` the second thread waits on the lock instead, the
    winner's barrier times out, and exactly one connection is ever created.
    """
    created = []
    barrier = threading.Barrier(2)
    real_connect = sqlite3.connect

    def racing_connect(*args, **kwargs):
        try:
            barrier.wait(timeout=1.0)
        except threading.BrokenBarrierError:
            pass
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    monkeypatch.setattr(sqlite_exact_module.sqlite3, "connect", racing_connect)

    backend = SQLiteExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    results = [None, None]
    errors = []

    def open_collection(i):
        try:
            results[i] = backend.get_collection(
                palace=palace, collection_name="drawers", create=True
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=open_collection, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert len(created) == 1
    assert results[0]._handle is results[1]._handle

    backend.close()
    with pytest.raises(sqlite3.ProgrammingError):
        created[0].execute("SELECT 1")


def test_sqlite_exact_backend_advertises_supports_metadata_facets():
    assert "supports_metadata_facets" in SQLiteExactBackend.capabilities


def test_sqlite_exact_collection_exposes_backend(tmp_path):
    backend, col = _collection(tmp_path)
    assert col._backend is backend
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    wrapped = EmbeddingCollection(col)
    assert wrapped._backend is backend
    assert "supports_metadata_facets" in wrapped._backend.capabilities


def test_sqlite_exact_facet_counts(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["1", "2", "3", "4"],
        documents=["a", "b", "c", "d"],
        metadatas=[
            {"wing": "alpha"},
            {"wing": "alpha"},
            {"wing": "beta"},
            {"wing": "gamma"},
        ],
        embeddings=[[1, 0], [1, 0], [1, 0], [1, 0]],
    )
    assert col.facet_counts("wing") == {
        "alpha": 2,
        "beta": 1,
        "gamma": 1,
    }


def test_sqlite_exact_facet_counts_where(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["1", "2", "3"],
        documents=["a", "b", "c"],
        metadatas=[
            {"wing": "engineering", "room": "backend"},
            {"wing": "engineering", "room": "frontend"},
            {"wing": "design", "room": "ux"},
        ],
        embeddings=[[1, 0], [1, 0], [1, 0]],
    )
    assert col.facet_counts("room", where={"wing": "engineering"}) == {
        "backend": 1,
        "frontend": 1,
    }


def test_sqlite_exact_facet_counts_rejects_local_filters(tmp_path):
    _backend, col = _collection(tmp_path)
    with pytest.raises(UnsupportedCapabilityError):
        col.facet_counts(
            "room",
            where={"$or": [{"wing": "a"}, {"wing": "b"}]},
        )


def test_sqlite_exact_facet_counts_ignores_missing_metadata(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["1", "2", "3"],
        documents=["a", "b", "c"],
        metadatas=[
            {"wing": "alpha"},
            {"wing": "beta"},
            {},
        ],
        embeddings=[[1, 0], [1, 0], [1, 0]],
    )
    assert col.facet_counts("wing") == {"alpha": 1, "beta": 1}


def test_sqlite_exact_facet_counts_empty_collection(tmp_path):
    _backend, col = _collection(tmp_path)
    assert col.facet_counts("wing") == {}


def test_sqlite_exact_query_unfiltered_does_not_load_documents_for_ranking(tmp_path):
    """Unfiltered query ranks from id+embedding only, then hydrates top-k."""
    _backend, col = _collection(tmp_path)
    _seed(col, 6)

    statements = []
    conn = col._handle.conn
    conn.set_trace_callback(statements.append)
    try:
        result = col.query(
            query_embeddings=[[5.0, 1.0]],
            n_results=2,
            include=["documents", "metadatas", "distances"],
        )
    finally:
        conn.set_trace_callback(None)

    assert result.ids[0][0] == "d5"
    ranking = [
        s
        for s in statements
        if "FROM documents" in s and "embedding" in s and "ORDER BY rowid" in s
    ]
    assert ranking, statements
    for sql in ranking:
        select_list = sql.split("FROM documents", 1)[0]
        assert "embedding" in select_list
        # Ranking may json_extract metadata keys but must not load the
        # verbatim document column for every row.
        assert re.search(r"\bdocument\b", select_list) is None


def test_sqlite_exact_query_respects_equality_where(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["keep", "drop"],
        documents=["keep me", "drop me"],
        metadatas=[{"wing": "keep"}, {"wing": "drop"}],
        embeddings=[[1.0, 0.0], [1.0, 0.0]],
    )
    ranked = col.query(
        query_embeddings=[[1.0, 0.0]],
        n_results=5,
        where={"wing": "keep"},
        include=["documents", "metadatas", "distances"],
    )
    assert ranked.ids[0] == ["keep"]
    assert ranked.documents[0] == ["keep me"]


def test_sqlite_exact_query_where_uses_cached_matrix(tmp_path):
    """After the first scan, equality filters slice the cached matrix."""
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["keep", "drop"],
        documents=["keep me", "drop me"],
        metadatas=[{"wing": "keep"}, {"wing": "drop"}],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
    )
    col.query(query_embeddings=[[1.0, 0.0]], n_results=2)
    ranked = col.query(
        query_embeddings=[[1.0, 0.0]],
        n_results=5,
        where={"wing": "keep"},
        include=["documents"],
    )
    assert ranked.ids[0] == ["keep"]
    assert ranked.documents[0] == ["keep me"]


def test_sqlite_exact_query_cache_invalidates_on_add(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["old"],
        documents=["old"],
        metadatas=[{}],
        embeddings=[[0.0, 1.0]],
    )
    first = col.query(query_embeddings=[[1.0, 0.0]], n_results=1)
    assert first.ids[0] == ["old"]

    col.add(
        ids=["new"],
        documents=["new"],
        metadatas=[{}],
        embeddings=[[1.0, 0.0]],
    )
    second = col.query(query_embeddings=[[1.0, 0.0]], n_results=1)
    assert second.ids[0] == ["new"]


def test_sqlite_exact_wing_room_counts(tmp_path):
    from mempalace.backends.sqlite_exact import sqlite_wing_room_counts

    _backend, col = _collection(tmp_path)
    col.add(
        ids=["1", "2", "3"],
        documents=["a", "b", "c"],
        metadatas=[
            {"wing": "alpha", "room": "notes"},
            {"wing": "alpha", "room": "code"},
            {"wing": "beta", "room": "notes"},
        ],
        embeddings=[[1, 0], [1, 0], [1, 0]],
    )

    total, wing_rooms = sqlite_wing_room_counts(str(tmp_path), "mempalace_drawers")
    assert total == 3
    assert wing_rooms["alpha"]["notes"] == 1
    assert wing_rooms["alpha"]["code"] == 1
    assert wing_rooms["beta"]["notes"] == 1


def test_sqlite_exact_get_metadatas_skips_document_and_embedding(tmp_path):
    _backend, col = _collection(tmp_path)
    _seed(col, 3)

    result, selects = _doc_select_sql(col, lambda: col.get(limit=2, include=["metadatas"]))
    assert result.ids == ["d0", "d1"]
    assert result.documents == []
    assert result.embeddings is None
    assert len(selects) == 1
    select_list = selects[0].split("FROM documents")[0]
    assert "metadata_json" in select_list
    assert re.search(r"\bdocument\b", select_list) is None
    assert "embedding" not in select_list


def test_sqlite_exact_room_wing_hall_counts(tmp_path):
    from mempalace.backends.sqlite_exact import sqlite_room_wing_hall_counts

    _backend, col = _collection(tmp_path)
    col.add(
        ids=["1", "2", "3"],
        documents=["a", "b", "c"],
        metadatas=[
            {"room": "chromadb", "wing": "wing_code", "hall": "db", "date": "2026-01-02"},
            {"room": "chromadb", "wing": "wing_project", "hall": "db"},
            {"room": "auth", "wing": "wing_code", "hall": "security"},
        ],
        embeddings=[[1, 0], [1, 0], [1, 0]],
    )
    rows = sqlite_room_wing_hall_counts(str(tmp_path), "mempalace_drawers")
    grouped = {(room, wing, hall): (n, last) for room, wing, hall, n, last in rows}
    assert grouped[("chromadb", "wing_code", "db")] == (1, "2026-01-02")
    assert grouped[("chromadb", "wing_project", "db")] == (1, "")
    assert grouped[("auth", "wing_code", "security")] == (1, "")


def test_sqlite_exact_locus_columns_and_index(tmp_path):
    from mempalace.backends.sqlite_exact import _LOCUS_FIELDS, _LOCUS_INDEX

    _backend, col = _collection(tmp_path)
    col.add(
        ids=["a"],
        documents=["alpha note"],
        metadatas=[{"wing": "alpha", "room": "notes", "hall": "db"}],
        embeddings=[[1.0, 0.0]],
    )
    conn = col._handle.conn
    from mempalace.backends.sqlite_exact import _document_column_names

    cols = _document_column_names(conn)
    assert set(_LOCUS_FIELDS) <= cols
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(documents)").fetchall()}
    assert _LOCUS_INDEX in indexes
    row = conn.execute("SELECT wing, room, hall FROM documents WHERE id = 'a'").fetchone()
    assert tuple(row) == ("alpha", "notes", "db")


def test_sqlite_exact_equality_where_uses_locus_column(tmp_path):
    _backend, col = _collection(tmp_path)
    col.add(
        ids=["keep", "drop"],
        documents=["keep me", "drop me"],
        metadatas=[{"wing": "keep", "hall": "db"}, {"wing": "drop", "hall": "other"}],
        embeddings=[[1.0, 0.0], [1.0, 0.0]],
    )
    result, selects = _doc_select_sql(
        col, lambda: col.get(where={"wing": "keep"}, include=["metadatas"])
    )
    assert result.ids == ["keep"]
    assert selects
    assert "json_extract" not in selects[0]
    assert "wing =" in selects[0] or "wing=?" in selects[0].replace(" ", "")

    hall = col.get(where={"hall": "db"}, include=["metadatas"])
    assert hall.ids == ["keep"]


def test_sqlite_exact_migrates_locus_columns_on_existing_palace(tmp_path):
    import numpy as np
    from mempalace.backends.sqlite_exact import _LOCUS_FIELDS, _LOCUS_INDEX

    db = tmp_path / "sqlite_exact.sqlite3"
    conn = sqlite3.connect(str(db))
    blob = np.asarray([1.0, 0.0], dtype=np.float32).tobytes()
    conn.executescript(
        """
        CREATE TABLE collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE documents (
            collection_id INTEGER NOT NULL,
            id TEXT NOT NULL,
            document TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            embedding BLOB NOT NULL,
            dim INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (collection_id, id)
        );
        """
    )
    conn.execute(
        "INSERT INTO collections(id, name, created_at) VALUES (1, 'mempalace_drawers', 't')"
    )
    conn.execute(
        """
        INSERT INTO documents
            (collection_id, id, document, metadata_json, embedding, dim, created_at, updated_at)
        VALUES (1, 'a', 'alpha note', ?, ?, 2, 't', 't')
        """,
        ('{"hall":"db","room":"notes","wing":"alpha"}', blob),
    )
    conn.commit()
    conn.close()

    _backend, col = _collection(tmp_path, create=False)
    handle = col._handle.conn
    from mempalace.backends.sqlite_exact import _document_column_names

    cols = _document_column_names(handle)
    assert set(_LOCUS_FIELDS) <= cols
    indexes = {row[1] for row in handle.execute("PRAGMA index_list(documents)").fetchall()}
    assert _LOCUS_INDEX in indexes
    assert tuple(
        handle.execute("SELECT wing, room, hall FROM documents WHERE id='a'").fetchone()
    ) == (
        "alpha",
        "notes",
        "db",
    )
    from mempalace.backends.sqlite_exact import sqlite_wing_room_counts

    total, wings = sqlite_wing_room_counts(str(tmp_path), "mempalace_drawers")
    assert total == 1
    assert wings["alpha"]["notes"] == 1
