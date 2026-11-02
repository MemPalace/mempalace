"""Synthetic snapshot, failure, driver and composed-ingest regressions."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from _thread_search_oracle import assert_alignment, old_search

from commonplace.handoff import thread_store as module
from commonplace.handoff.models import ThreadMessage
from commonplace.handoff.thread_store import ThreadStorageLimit, ThreadStore
from mempalace import sqlite_crypto


class ObservedConnection(sqlite3.Connection):
    after_execute = None

    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if self.after_execute:
            self.after_execute(sql)
        return result


@pytest.fixture
def observed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        module,
        "connect",
        lambda *args, **kwargs: sqlite3.connect(*args, factory=ObservedConnection, **kwargs),
    )
    path = tmp_path / "threads.sqlite3"
    store = ThreadStore(str(path))
    try:
        yield store, path
    finally:
        store.close()


def put(store, source, text="needle"):
    return store.store(
        "test", source, [ThreadMessage(0, "user", "message", text, " exact raw\r\n")], actor="test"
    )


def drop_index(path, thread_id):
    with sqlite3.connect(path) as writer:
        writer.execute("DELETE FROM chunks_fts WHERE thread_id=?", (thread_id,))


@pytest.mark.parametrize("stage", ["snapshot", "token", "rank"])
def test_external_commit_cannot_split_search_snapshot_across_batches(observed, stage):
    store, path = observed
    ids = [put(store, str(i)).id for i in range(5)]
    expected = old_search(store, ids, "needle", 100)
    store._conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
    ready, committed = threading.Event(), threading.Event()
    fired = False

    def after(sql):
        nonlocal fired
        matches = {
            "snapshot": sql.startswith("SELECT rowid FROM chunks LIMIT 1"),
            "token": sql == "PRAGMA data_version",
            "rank": sql.startswith("WITH "),
        }
        if not fired and matches[stage]:
            fired = True
            ready.set()
            assert committed.wait(3), "external commit did not finish"

    def writer():
        assert ready.wait(3), "search did not reach snapshot barrier"
        try:
            drop_index(path, ids[-1])
        finally:
            committed.set()

    store._conn.after_execute = after
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(writer)
        try:
            assert store.search(ids, "needle", 100) == expected
        finally:
            store._conn.after_execute = None
        future.result(timeout=3)
    assert fired
    assert store._conn.execute("PRAGMA data_version").fetchone()[0] != store._fts_verified_version
    with patch.object(store, "_repair_fts", wraps=store._repair_fts) as repair:
        assert store.search(ids, "needle", 100) == expected
        assert repair.call_count == 1
    assert_alignment(store)


def test_verified_token_is_captured_before_commit_and_published_after_it(observed):
    store, path = observed
    item = put(store, "one")
    drop_index(path, item.id)
    original = store._transaction
    captured = []
    from contextlib import contextmanager

    @contextmanager
    def interleaving_transaction():
        with original():
            yield
            captured.append(store._conn.execute("PRAGMA data_version").fetchone()[0])
            assert store._fts_verified_version is None
        # A later external commit must not be published as already verified.
        drop_index(path, item.id)
        assert store._fts_verified_version is None

    with patch.object(store, "_transaction", interleaving_transaction):
        assert store.search([item.id], "needle")
    assert store._fts_verified_version == captured[0]
    assert store._conn.execute("PRAGMA data_version").fetchone()[0] != captured[0]
    with patch.object(store, "_repair_fts", wraps=store._repair_fts) as repair:
        assert store.search([item.id], "needle")
        assert repair.call_count == 1
    assert_alignment(store)


def test_failed_search_commit_does_not_publish_verified_token(observed):
    store, path = observed
    item = put(store, "one")
    drop_index(path, item.id)

    def deny_commit(action, operation, *args):
        if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    store._conn.set_authorizer(deny_commit)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            store.search([item.id], "needle")
    finally:
        store._conn.set_authorizer(None)
    assert store._fts_verified_version is None
    assert old_search(store, [item.id], "needle") == []
    assert store.search([item.id], "needle")


@pytest.mark.parametrize("commit_outer", [False, True])
def test_outer_repair_never_publishes_trust_it_cannot_commit(observed, commit_outer):
    store, path = observed
    item = put(store, "one")
    drop_index(path, item.id)
    version = store._conn.execute("PRAGMA data_version").fetchone()[0]
    store._conn.execute("BEGIN IMMEDIATE")
    try:
        store.put_doc("caller", "kept only on commit")
        assert store.search([item.id], "needle")
        assert store._conn.in_transaction
        assert store._fts_verified_version is None
        if commit_outer:
            store._conn.commit()
        else:
            store._conn.rollback()
    finally:
        if store._conn.in_transaction:
            store._conn.rollback()
    assert store._conn.execute("PRAGMA data_version").fetchone()[0] == version
    with patch.object(store, "_repair_fts", wraps=store._repair_fts) as repair:
        assert store.search([item.id], "needle")
        assert repair.call_count == 1
    assert bool(store.doc("caller")) == commit_outer
    assert_alignment(store)


def test_invalidated_search_with_locked_writer_is_explicit_and_untrusted(observed):
    store, path = observed
    item = put(store, "one")
    drop_index(path, item.id)
    store._conn.execute("PRAGMA busy_timeout=0")
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                store.search([item.id], "needle")
            assert store._fts_verified_version is None
            assert not store._conn.in_transaction
        finally:
            writer.rollback()
    assert store.search([item.id], "needle")
    assert_alignment(store)


def test_caller_snapshot_upgrade_failure_preserves_transaction_and_invalidates_cache(observed):
    store, path = observed
    item = put(store, "one")
    drop_index(path, item.id)
    store._conn.execute("PRAGMA busy_timeout=0")
    store._conn.execute("BEGIN")
    store._conn.execute("SELECT * FROM chunks").fetchall()
    # Invalidate the caller's already pinned snapshot, preventing upgrade.
    with sqlite3.connect(path) as writer:
        writer.execute("INSERT INTO handoff_docs VALUES ('external', 'committed')")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.search([item.id], "needle")
        assert store._conn.in_transaction
        assert store._fts_verified_version is None
        assert store.doc("external") == ""
        assert old_search(store, [item.id], "needle") == []
    finally:
        store._conn.rollback()
    assert store.search([item.id], "needle")
    assert store.doc("external") == "committed"


@pytest.mark.parametrize("mutation", ["delete", "rename"])
def test_external_changes_below_unchanged_maximum_require_repair(observed, mutation):
    store, path = observed
    first = put(store, "first")
    put(store, "last", "last")
    maximum = store._conn.execute("SELECT max(rowid) FROM chunks_fts").fetchone()[0]
    with sqlite3.connect(path) as writer:
        if mutation == "delete":
            writer.execute("DELETE FROM chunks_fts WHERE rowid=1")
        else:
            writer.execute("UPDATE chunks_fts SET thread_id='foreign' WHERE rowid=1")
    assert store._conn.execute("SELECT max(rowid) FROM chunks_fts").fetchone()[0] == maximum
    assert store.search([first.id], "needle")
    assert_alignment(store)


def test_integrity_error_dispatch_uses_only_the_connection_driver(monkeypatch):
    class CipherIntegrityError(Exception):
        pass

    fake = SimpleNamespace(IntegrityError=CipherIntegrityError)
    calls = []
    monkeypatch.setattr(sqlite_crypto, "driver", lambda: calls.append(True) or fake)
    with closing(sqlite3.connect(":memory:")) as connection:
        assert sqlite_crypto.integrity_error_type(connection) is sqlite3.IntegrityError
        assert not calls
    cipher = type("Connection", (), {"__module__": "sqlcipher3.dbapi2"})()
    assert sqlite_crypto.integrity_error_type(cipher) is CipherIntegrityError
    assert calls == [True]


def test_fake_optional_driver_collision_uses_its_integrity_class(observed, monkeypatch):
    store, _ = observed
    item = put(store, "first")

    class CipherIntegrityError(Exception):
        pass

    class CipherConnection:
        __module__ = "sqlcipher3.dbapi2"

        def executemany(self, sql, parameters):
            identity = next(iter(parameters))
            rowid = store._conn.execute(
                "SELECT rowid FROM chunks WHERE thread_id=? AND idx=?", identity
            ).fetchone()[0]
            store._conn.execute("UPDATE chunks_fts SET thread_id='foreign' WHERE rowid=?", (rowid,))
            raise CipherIntegrityError("constraint failed")

        def execute(self, *args):
            return store._conn.execute(*args)

    reader = ThreadStore.__new__(ThreadStore)
    reader._conn = CipherConnection()
    monkeypatch.setattr(
        sqlite_crypto, "driver", lambda: SimpleNamespace(IntegrityError=CipherIntegrityError)
    )
    with pytest.raises(module._FTSRowidCollision) as caught:
        reader._insert_fts([(item.id, 0)])
    assert isinstance(caught.value.__cause__, CipherIntegrityError)


@pytest.mark.parametrize("compact", [False, True])
def test_capture_quota_nested_rollback_and_duplicate_compatibility(tmp_path, compact):
    from commonplace.capture import EMPTY_SHA256, append_capture
    from commonplace.handoff.thread_store_v2 import ThreadStoreV2

    cls = ThreadStoreV2 if compact else ThreadStore
    filename = "threads.v2.sqlite3" if compact else "threads.sqlite3"
    with closing(cls(str(tmp_path / filename))) as store:

        def capture(text, offset=0, prefix=EMPTY_SHA256, maximum=None):
            return append_capture(
                store,
                "test",
                "capture",
                "claude-code",
                text,
                offset,
                prefix,
                actor="test",
                maximum=maximum,
                interval=0,
            )

        raw = json.dumps({"type": "user", "message": {"role": "user", "content": "café"}}) + "\r\n"
        with pytest.raises(ThreadStorageLimit):
            capture(raw, maximum=1)
        assert store.by_source("test", "capture") is None
        first = capture(raw)
        stored_id = first["stored_thread_id"]
        before = store.messages(stored_id)
        chunks = store.chunks(stored_id)
        check = store._check_storage_budget

        def after_write(maximum, messages=()):
            check(maximum, messages)
            if not messages:
                raise ThreadStorageLimit("quota")

        with patch.object(store, "_check_storage_budget", after_write):
            with pytest.raises(ThreadStorageLimit):
                capture(raw, first["offset"], first["prefix_sha256"])
        assert store.messages(stored_id) == before
        assert store.chunks(stored_id) == chunks
        second = capture(raw, first["offset"], first["prefix_sha256"])
        assert second["offset"] == len((raw * 2).encode())
        assert store.messages(stored_id)[: len(before)] == before
        assert store.search([stored_id], "café")
        assert capture(raw, maximum=1)["status"] == "duplicate"


def test_migrated_snapshot_preserves_search_append_and_v1_sql_isolation(tmp_path, monkeypatch):
    from commonplace.handoff.thread_store_migrate import migrate_v1, read_store
    from commonplace.handoff.thread_store_v2 import ThreadStoreV2

    source = tmp_path / "threads.sqlite3"
    target = tmp_path / "threads.v2.sqlite3"
    items = [ThreadMessage(0, "user", "message", "needle\x00 café\r\n" * 500, " raw\t")]
    with closing(ThreadStore(str(source))) as store:
        item = store.store("test", "one", items, actor="test")
        store.append(item.id, items + [ThreadMessage(1, "assistant", "message", "needle", "tail")])
        expected_messages = store.messages(item.id)
        expected_chunks = store.chunks(item.id)
        expected_search = {
            query: store.search([item.id], query)
            for query in ("needle", '"needle"', "café", "needle\x00", "absent")
        }
    migrate_v1(str(source), str(target))
    # V2 overrides v1 search/write paths, and must never issue v1 repair SQL.
    monkeypatch.setattr(ThreadStore, "_repair_fts", lambda self: pytest.fail("v1 repair on v2"))
    with closing(ThreadStoreV2(str(target))) as store:
        statements = []
        store._conn.set_trace_callback(statements.append)
        assert store.messages(item.id) == expected_messages
        assert store.chunks(item.id) == expected_chunks
        for query, expected in expected_search.items():
            assert store.search([item.id], query) == expected
        extended = expected_messages + [ThreadMessage(2, "user", "message", "needle", "last")]
        store.append(item.id, extended)
        assert store.messages(item.id) == extended
        assert store.chunks(item.id)[: len(expected_chunks)] == expected_chunks
        assert not any("chunks_fts" in sql or "FROM chunks " in sql for sql in statements)
    with read_store(target, compact=True) as reader:
        assert reader.messages(item.id) == extended
        assert reader.search([item.id], "needle")


def test_database_aborted_outer_repair_preserves_original_failure(observed, monkeypatch):
    store, path = observed
    item = put(store, "one")
    drop_index(path, item.id)
    error = sqlite3.OperationalError("disk I/O error")
    repair = store._repair_fts

    def aborted_repair():
        repair()
        # Model SQLite's own transaction abort, including all savepoints.
        store._conn.rollback()
        raise error

    with monkeypatch.context() as context:
        context.setattr(store, "_repair_fts", aborted_repair)
        store._conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            store.search([item.id], "needle")
        assert caught.value is error
    assert not store._conn.in_transaction
    assert store._fts_verified_version is None
    assert old_search(store, [item.id], "needle") == []
    assert store.search([item.id], "needle")
