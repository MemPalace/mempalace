"""Acceptance regressions for the three candidate-v1 review findings."""

import contextlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import chromadb
import numpy as np
import pytest
from _mcp_server_helpers import _patch_mcp_server
from chromadb.config import Settings, System
from chromadb.db.impl.sqlite import SqliteDB
from chromadb.segment.impl.metadata.sqlite import SqliteMetadataSegment
from chromadb.types import Operation, ScalarEncoding
from test_source_incremental_runner import contents, current, setup_runner as setup_runner

from mempalace.backends._inproc_sqlite import release
from mempalace.backends.chroma import ChromaCollection, sqlite_list_id_metadata
from mempalace.sources import DrawerRecord, SourceItemMetadata
from mempalace.sources.incremental import IncrementalProtocolError
from mempalace.sources.lifecycle import LifecycleStateError, SourceLifecycleStore


@pytest.mark.parametrize("backend_name", ["sqlite_exact", "rust_exact"])
def test_public_exact_read_after_exclusive_gc_completes(tmp_path, backend_name):
    # A child bounds the old deadlock as well as a failing test: no stuck
    # reader/GC threads or OS leases can survive a regression.
    code = """
import contextlib, os, sys, threading
from pathlib import Path
from mempalace import palace
from mempalace.backends.sqlite_exact import SQLiteExactBackend
from mempalace.backends.rust_exact import RustExactBackend
from mempalace.sources import lifecycle
root = Path(sys.argv[1])
palace.paths.lock_dir = lambda: str(root / 'locks')
backend = (SQLiteExactBackend if sys.argv[2] == 'sqlite_exact' else RustExactBackend)()
path = str(root / 'palace')
col = backend.get_collection(path, 'mempalace_drawers', create=True)
reader_col = backend.get_collection(path, 'mempalace_drawers', create=True)
assert col._handle is reader_col._handle
store = lifecycle.SourceLifecycleStore(path)
old = store.begin('a', 's', 'v1')
col.add(ids=['old'], documents=['old'], metadatas=[old.metadata], embeddings=[[1, 0]])
store.activate(old)
new = store.begin('a', 's', 'v2')
col.add(ids=['new'], documents=['new'], metadatas=[new.metadata], embeddings=[[1, 0]])
store.activate(new)
exclusive, reader_arriving = threading.Event(), threading.Event()
original = lifecycle.reclamation_lease
@contextlib.contextmanager
def scheduled(path, *, exclusive=False):
    if threading.current_thread().name == 'reader':
        reader_arriving.set()
    with original(path, exclusive=exclusive):
        if exclusive:
            acquired.set()
            assert reader_arriving.wait(3), 'reader did not reach shared lease'
        yield
acquired = exclusive
lifecycle.reclamation_lease = scheduled
errors, rows = [], []
def collect():
    try:
        store.reclaim(col, 'a', 's')
    except BaseException as exc:
        errors.append(repr(exc))
def read():
    try:
        assert acquired.wait(3), 'GC did not acquire exclusive lease'
        rows.extend(reader_col.get().ids)
    except BaseException as exc:
        errors.append(repr(exc))
gc = threading.Thread(target=collect, name='gc', daemon=True)
reader = threading.Thread(target=read, name='reader', daemon=True)
gc.start(); reader.start()
gc.join(4); reader.join(4)
if gc.is_alive() or reader.is_alive():
    print('read/GC failed to complete', flush=True)
    os._exit(2)
assert not errors, errors
assert rows == ['new'], rows
assert col.physical_count() == 1
backend.close()
print('both public read and GC completed', flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), backend_name],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "both public read and GC completed" in result.stdout


@pytest.mark.parametrize("after_write", [False, True])
@pytest.mark.parametrize("ending", ["exhaust", "next-item"])
def test_direct_skip_assignment_poison_preserves_current(setup_runner, after_write, ending):
    adapter, path, run = setup_runner
    assert run() == 3
    old, old_contents = current(path, adapter), contents(path)
    adapter.version = "v2"

    def ingest(*, source, palace):
        yield SourceItemMetadata(adapter.source_file, "v2")
        if after_write:
            palace.upsert_drawer(DrawerRecord("partial extraction", adapter.source_file, 0))
        # Catching the assignment error must not permit publication at the
        # next yield or successful StopIteration.
        try:
            palace._skip_requested = True
        except IncrementalProtocolError:
            pass
        if ending == "next-item":
            yield SourceItemMetadata(adapter.source_file + "/next", "next-version")

    adapter.ingest = ingest
    with pytest.raises(IncrementalProtocolError, match="verified current"):
        run()
    assert current(path, adapter) == old
    assert contents(path) == old_contents
    with SourceLifecycleStore(path).snapshot() as snapshot:
        assert snapshot.current(adapter.name, adapter.source_file + "/next") is None


@contextlib.contextmanager
def queued_palace(path, operation, *, managed=False):
    """Real persisted producer operations; leave the metadata consumer stopped."""
    path = str(path)
    store = SourceLifecycleStore(path) if managed else None
    active = store.begin("a", "s", "v1") if store else None
    if store:
        store.activate(active)
        staged = store.begin("a", "s", "v2")
    else:
        staged = None
    meta = {"wing": "alpha", "room": "notes", **(active.metadata if active else {})}
    client = chromadb.PersistentClient(path=path, settings=Settings(anonymized_telemetry=False))
    system = segment = None
    db = Path(path) / "chroma.sqlite3"
    try:
        col = client.create_collection("mempalace_drawers", embedding_function=None)
        col.add(
            ids=["original"], documents=["old document"], embeddings=[[1.0, 0.0]], metadatas=[meta]
        )
        if staged:
            col.add(
                ids=["hidden"],
                documents=["hidden staged text"],
                embeddings=[[1.0, 0.0]],
                metadatas=[{"wing": "secret-fixture", **staged.metadata}],
            )
        cid = col.id
        client.close()
        with sqlite3.connect(db) as conn:
            sid = conn.execute(
                "SELECT id FROM segments WHERE collection=? AND scope='METADATA'", (str(cid),)
            ).fetchone()[0]
        system = System(
            Settings(is_persistent=True, persist_directory=path, anonymized_telemetry=False)
        )
        producer = system.instance(SqliteDB)
        system.start()
        segment = SqliteMetadataSegment(system, {"id": UUID(sid), "collection": cid})
        if operation == "add":
            record = {
                "id": "queued",
                "embedding": np.array([0.0, 1.0], dtype=np.float32),
                "encoding": ScalarEncoding.FLOAT32,
                "metadata": {**meta, "chroma:document": "queued document"},
                "operation": Operation.ADD,
            }
            expected = {"old document", "queued document"}
        elif operation == "update":
            record = {
                "id": "original",
                "embedding": None,
                "encoding": None,
                "metadata": {"chroma:document": "queued replacement"},
                "operation": Operation.UPDATE,
            }
            expected = {"queued replacement"}
        else:
            record = {
                "id": "original",
                "embedding": None,
                "encoding": None,
                "metadata": None,
                "operation": Operation.DELETE,
            }
            expected = set()
        producer.submit_embeddings(cid, [record])
        client = chromadb.PersistentClient(path=path, settings=Settings(anonymized_telemetry=False))
        wrapper = ChromaCollection(
            client.get_collection("mempalace_drawers", embedding_function=None), palace_path=path
        )
        # Reopening the SDK collection alone must not have consumed the queue.
        assert sqlite_list_id_metadata(path, "mempalace_drawers") is None
        yield wrapper, expected, store, staged
    finally:
        if segment is not None:
            segment.stop()
        if system is not None:
            system.stop()
        client.close()
        release(str(db))


@pytest.mark.parametrize("operation", ["add", "update", "delete"])
@pytest.mark.parametrize("surface", ["get", "list", "export", "stream"])
@pytest.mark.parametrize("managed", [False, True])
def test_public_chroma_recovers_queue_and_revalidates(
    tmp_path, monkeypatch, config, kg, operation, surface, managed
):
    from mempalace import exporter, mcp_server as mcp
    from mempalace.sources.read_visibility import iter_drawer_batches

    path = tmp_path / "queued-palace"
    with queued_palace(path, operation, managed=managed) as (wrapper, expected, _store, _staged):
        calls = []
        sdk_get = wrapper._collection.get

        def recover(**kwargs):
            calls.append(kwargs)
            return sdk_get(**kwargs)

        monkeypatch.setattr(wrapper._collection, "get", recover)
        if surface == "get":
            result = set(wrapper.get().documents)
        elif surface == "list":
            config._palace_path_override = str(path)
            _patch_mcp_server(monkeypatch, config, kg)
            monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: True)
            # The fallback receives the actual production wrapper, never SDK.
            monkeypatch.setattr(mcp, "_get_collection", lambda *a, **k: wrapper)
            page = mcp.tool_list_drawers()
            assert "drawers" in page, page
            result = {row["content_preview"] for row in page["drawers"]}
        elif surface == "export":
            monkeypatch.setattr(exporter, "get_collection", lambda *_a: wrapper)
            output = tmp_path / "exported"
            stats = exporter.export_palace(str(path), str(output))
            assert stats["drawers"] == len(expected)
            text = "\n".join(p.read_text() for p in output.rglob("*.md"))
            assert "hidden staged text" not in text
            assert all(value in text for value in expected)
            assert ("old document" in text) == ("old document" in expected)
            result = expected
        else:
            result = {
                doc
                for batch in iter_drawer_batches(wrapper, str(path), batch_size=1)
                for doc in batch.documents
            }
        assert result == expected
        assert calls == [{"limit": 1, "include": []}]
        assert sqlite_list_id_metadata(str(path), "mempalace_drawers") is not None


def test_queue_recovery_reopens_registry_after_activation(tmp_path, monkeypatch):
    with queued_palace(tmp_path / "managed", "add", managed=True) as (
        wrapper,
        _expected,
        store,
        staged,
    ):
        sdk_get = wrapper._collection.get

        def recover_then_activate(**kwargs):
            result = sdk_get(**kwargs)
            store.activate(staged)
            return result

        monkeypatch.setattr(wrapper._collection, "get", recover_then_activate)
        assert wrapper.get().documents == ["hidden staged text"]


def test_queue_recovery_cannot_return_unregistered_managed_content(tmp_path, monkeypatch):
    with queued_palace(tmp_path / "missing-state", "add") as (wrapper, _expected, _store, _staged):
        sdk_get = wrapper._collection.get

        def recover_then_tag(**kwargs):
            result = sdk_get(**kwargs)
            wrapper._collection.update(
                ids=["queued"],
                metadatas=[
                    {
                        "adapter_name": "a",
                        "source_file": "s",
                        "source_generation": "unknown",
                        "source_version": "v1",
                    }
                ],
            )
            return result

        monkeypatch.setattr(wrapper._collection, "get", recover_then_tag)
        with pytest.raises(LifecycleStateError):
            wrapper.get()
