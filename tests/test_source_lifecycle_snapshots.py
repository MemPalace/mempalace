"""Real SQL snapshots and OS reader leases, including subprocess ownership."""

import subprocess
import sys

import pytest

from mempalace.sources.lifecycle import (
    LifecycleConflictError,
    SourceLifecycleSnapshot,
    SourceLifecycleStore,
    StaleLifecycleSnapshot,
    reclamation_lease,
)
from mempalace.sources.read_visibility import open_logical_reader


def exact_db(tmp_path):
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    backend = SQLiteExactBackend()
    col = backend.get_collection(str(tmp_path), "mempalace_drawers", create=True)
    return backend, col


def test_two_readers_exclude_gc_and_process_death_releases(tmp_path):
    SourceLifecycleStore(str(tmp_path))
    code = """
import sys
from mempalace.sources.lifecycle import reclamation_lease
with reclamation_lease(sys.argv[1]):
    print('ready', flush=True)
    sys.stdin.read()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        # This second reader must coexist even on Windows (CRT read locks don't).
        with reclamation_lease(str(tmp_path)):
            with pytest.raises(LifecycleConflictError):
                with reclamation_lease(str(tmp_path), exclusive=True):
                    pass
        child.kill()
        child.wait(timeout=5)
        with reclamation_lease(str(tmp_path), exclusive=True):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()


def test_registry_first_open_race_retries_before_streaming(tmp_path, monkeypatch):
    import mempalace.sources.read_visibility as reads

    backend, col = exact_db(tmp_path)
    store = SourceLifecycleStore(str(tmp_path))
    old = store.begin("a", "s", "v1")
    col.add(ids=["old"], documents=["old"], metadatas=[old.metadata], embeddings=[[1, 0]])
    store.activate(old)
    new = store.begin("a", "s", "v2")
    col.add(ids=["new"], documents=["new"], metadatas=[new.metadata], embeddings=[[1, 0]])
    real = reads.visibility_epoch
    calls = []

    def activate_between_snapshots(path):
        calls.append(path)
        if len(calls) == 1:
            store.activate(new)
        return real(path)

    monkeypatch.setattr(reads, "visibility_epoch", activate_between_snapshots)
    try:
        with open_logical_reader(tmp_path / "sqlite_exact.sqlite3", backend="exact") as conn:
            assert conn.execute("SELECT id FROM documents").fetchall() == [("new",)]
        assert len(calls) == 2
    finally:
        backend.close()


def test_begin_between_registry_and_backend_snapshot_retries(tmp_path, monkeypatch):
    import mempalace.sources.read_visibility as reads

    backend, col = exact_db(tmp_path)
    store = SourceLifecycleStore(str(tmp_path))
    original = reads.LogicalSqliteReader
    attempts = []

    def inject(conn, path, kind, snapshot=None):
        attempts.append(1)
        if len(attempts) == 1:
            pending = store.begin("a", "s", "new")
            col.add(
                ids=["staged"],
                documents=["hidden"],
                metadatas=[pending.metadata],
                embeddings=[[1, 0]],
            )
        return original(conn, path, kind, snapshot=snapshot)

    monkeypatch.setattr(reads, "LogicalSqliteReader", inject)
    try:
        with open_logical_reader(tmp_path / "sqlite_exact.sqlite3", backend="exact") as conn:
            assert conn.execute("SELECT id FROM documents").fetchall() == []
        assert len(attempts) == 2
    finally:
        backend.close()


@pytest.mark.parametrize("fetch", ["fetchone", "fetchmany", "fetchall", "iterate", "cursor"])
def test_udf_failure_never_becomes_empty_fallback(tmp_path, fetch):
    backend, col = exact_db(tmp_path)
    SourceLifecycleStore(str(tmp_path))
    col.add(
        ids=["bad"],
        documents=["never expose"],
        metadatas=[
            {
                "source_generation": "unknown",
                "source_version": "v",
                "adapter_name": "a",
                "source_file": "s",
            }
        ],
        embeddings=[[1, 0]],
    )
    try:
        with open_logical_reader(tmp_path / "sqlite_exact.sqlite3", backend="exact") as conn:
            with pytest.raises(StaleLifecycleSnapshot):
                cursor = conn.cursor() if fetch == "cursor" else conn
                result = cursor.execute("SELECT id FROM documents")
                if fetch == "iterate":
                    list(result)
                else:
                    getattr(result, "fetchall" if fetch == "cursor" else fetch)()
            conn.close()
            conn.close()
        with reclamation_lease(str(tmp_path), exclusive=True):
            pass
    finally:
        backend.close()


def test_snapshot_owns_old_backend_rows_across_activation(tmp_path):
    backend, col = exact_db(tmp_path)
    store = SourceLifecycleStore(str(tmp_path))
    old = store.begin("a", "s", "v1")
    col.add(ids=["old"], documents=["old"], metadatas=[old.metadata], embeddings=[[1, 0]])
    store.activate(old)
    new = store.begin("a", "s", "v2")
    col.add(ids=["new"], documents=["new"], metadatas=[new.metadata], embeddings=[[1, 0]])
    try:
        with open_logical_reader(tmp_path / "sqlite_exact.sqlite3", backend="exact") as conn:
            store.activate(new)
            assert conn.execute("SELECT id FROM documents").fetchall() == [("old",)]
            with pytest.raises(LifecycleConflictError):
                store.reclaim(col, "a", "s")
        assert col.get().ids == ["new"]
        store.reclaim(col, "a", "s")
        assert col.physical_count() == 1
    finally:
        backend.close()


def test_shared_reader_closes_on_export_failure(tmp_path, monkeypatch):
    from mempalace import exporter
    from mempalace.palace import get_collection

    col = get_collection(str(tmp_path / "palace"), backend="sqlite_exact")
    store = SourceLifecycleStore(str(tmp_path / "palace"))
    generation = store.begin("a", "s", "v")
    col.add(ids=["a"], documents=["content"], metadatas=[generation.metadata])
    store.activate(generation)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(exporter, "_safe_open_for_write", fail)
    with pytest.raises(OSError, match="disk full"):
        exporter.export_palace(str(tmp_path / "palace"), str(tmp_path / "out"))
    with reclamation_lease(str(tmp_path / "palace"), exclusive=True):
        pass


def test_legacy_source_fields_without_managed_stamp_remain_compatible(tmp_path):
    with SourceLifecycleSnapshot(str(tmp_path)) as reader:
        assert reader.visible(
            {
                "adapter_name": "old",
                "source_file": "s",
                "version": "1",
                "adapter_version": "2",
                "normalize_version": 3,
            }
        )
    assert list(tmp_path.iterdir()) == []


def test_logical_exact_query_does_not_cache_snapshot_owner(tmp_path):
    backend, col = exact_db(tmp_path)
    store = SourceLifecycleStore(str(tmp_path))
    record = store.begin("a", "s", "v")
    col.add(ids=["a"], documents=["a"], metadatas=[record.metadata], embeddings=[[1, 0]])
    store.activate(record)
    try:
        assert col.query(query_embeddings=[[1, 0]]).ids == [["a"]]
        assert col._handle._vector_cache == {}
        assert col._handle._vector_cache_data_version is None
        with reclamation_lease(str(tmp_path), exclusive=True):
            pass
    finally:
        backend.close()


def test_open_retry_budget_closes_all_snapshots(tmp_path, monkeypatch):
    import mempalace.sources.read_visibility as reads

    backend, _col = exact_db(tmp_path)
    store = SourceLifecycleStore(str(tmp_path))
    epoch = reads.visibility_epoch
    calls = []

    def moving(path):
        calls.append(1)
        store.begin("a", str(len(calls)), "v")
        return epoch(path)

    monkeypatch.setattr(reads, "visibility_epoch", moving)
    try:
        with pytest.raises(StaleLifecycleSnapshot):
            open_logical_reader(tmp_path / "sqlite_exact.sqlite3", backend="exact")
        assert len(calls) == 3
        with reclamation_lease(str(tmp_path), exclusive=True):
            pass
    finally:
        backend.close()


def test_missing_registry_legacy_fast_path_is_pinned(tmp_path, monkeypatch):
    backend, col = exact_db(tmp_path)
    col.add(ids=["legacy"], documents=["legacy"], embeddings=[[1, 0]])
    original = col._rows

    def insert_between_check_and_read(cur, **kwargs):
        # Different SQLite connection, real committed tagged restore.
        import sqlite3

        with sqlite3.connect(str(tmp_path / "sqlite_exact.sqlite3")) as writer:
            writer.execute(
                "UPDATE documents SET metadata_json=? WHERE id='legacy'",
                ('{"source_generation":"unknown"}',),
            )
        return original(cur, **kwargs)

    monkeypatch.setattr(col, "_rows", insert_between_check_and_read)
    try:
        assert col.get().ids == ["legacy"]  # old untagged snapshot
        monkeypatch.setattr(col, "_rows", original)
        from mempalace.sources.lifecycle import LifecycleStateError

        with pytest.raises(LifecycleStateError):
            col.get()
    finally:
        backend.close()


def test_nonexistent_logical_database_does_not_create_palace(tmp_path):
    import sqlite3

    palace = tmp_path / "absent"
    with pytest.raises(sqlite3.Error):
        open_logical_reader(palace / "sqlite_exact.sqlite3", backend="exact")
    assert not palace.exists()


def test_borrowed_snapshot_owner_released_only_by_caller(tmp_path):
    backend, col = exact_db(tmp_path)
    store = SourceLifecycleStore(str(tmp_path))
    gen = store.begin("a", "s", "v")
    col.add(ids=["a"], documents=["a"], metadatas=[gen.metadata], embeddings=[[1, 0]])
    store.activate(gen)
    try:
        with store.snapshot() as snapshot:
            with open_logical_reader(
                tmp_path / "sqlite_exact.sqlite3", backend="exact", snapshot=snapshot
            ) as reader:
                assert reader.execute("SELECT id FROM documents").fetchall() == [("a",)]
            assert snapshot.visible(gen.metadata)
            with pytest.raises(LifecycleConflictError):
                with reclamation_lease(str(tmp_path), exclusive=True):
                    pass
        with reclamation_lease(str(tmp_path), exclusive=True):
            pass
    finally:
        backend.close()
