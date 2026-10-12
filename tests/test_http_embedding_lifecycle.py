"""Regression coverage for HTTP embedding/reconnect handle lifetime."""

import json
import threading
import time
from types import SimpleNamespace

import pytest
from mempalace.backends.embedding_wrapper import _embed_texts as production_embed_texts


@pytest.mark.parametrize("allow_release", [True, False])
def test_reconnect_cannot_close_diary_handle_before_embedding(monkeypatch, tmp_path, allow_release):
    from mempalace import mcp_server as mcp, palace
    from mempalace.backends import PalaceRef
    from mempalace.backends.sqlite_exact import SQLiteExactBackend
    from mempalace.backends.embedding_wrapper import EmbeddingCollection
    import mempalace.backends.embedding_wrapper as wrapper
    import mempalace.embedding as embedding

    backend = SQLiteExactBackend()
    ref = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    config = SimpleNamespace(
        palace_path=str(tmp_path),
        collection_name="mempalace_drawers",
        chunk_size=1800,
        backend="sqlite_exact",
    )
    monkeypatch.setattr(mcp, "_config", config)
    monkeypatch.setattr(mcp, "_HTTP_REQUEST_LOCK", mcp._RWLock())
    monkeypatch.setattr(mcp, "_HTTP_EMBEDDING_LIFECYCLE_LOCK", threading.Lock())
    monkeypatch.setattr(mcp, "_is_chroma_backend", lambda: False)
    monkeypatch.setattr(mcp, "_refresh_sqlite_integrity_status", lambda: None)
    monkeypatch.setattr(mcp, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp, "_attach_stale_library_warning", lambda result: result)
    monkeypatch.setattr(palace, "get_backend_for_palace", lambda path: backend)
    monkeypatch.setattr(mcp, "_client_cache", None)
    monkeypatch.setattr(mcp, "_collection_cache_backend", None)
    monkeypatch.setattr(
        mcp,
        "_get_collection",
        lambda create=False: EmbeddingCollection(
            backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
        ),
    )
    monkeypatch.setattr(
        embedding, "get_embedding_function", lambda: lambda input: [[1.0, 0.0] for _ in input]
    )
    # Restore the actual transport hook: conftest replaces this for normal tests.
    monkeypatch.setattr(wrapper, "_embed_texts", production_embed_texts)
    if not allow_release:
        monkeypatch.setattr(mcp, "_HTTP_EMBEDDING_RELEASE_TOOLS", frozenset())

    got_handle = threading.Event()
    reconnect_queued = threading.Event()
    result = {}
    errors = []

    # Pause at the actual diary's WAL announcement, after acquiring its
    # collection and before col.add. Reconnect then takes lifecycle and waits
    # for the diary's write lease, a valid unmodified production schedule.
    def wal_barrier(*args, **kwargs):
        got_handle.set()
        deadline = time.monotonic() + 3
        while mcp._HTTP_REQUEST_LOCK._waiting_writers == 0:
            assert time.monotonic() < deadline, "reconnect never queued"
            time.sleep(0.001)
        reconnect_queued.set()

    monkeypatch.setattr(mcp, "_wal_log", wal_barrier)

    def dispatch_to_actual_tool(request):
        name = request["params"]["name"]
        if name == "mempalace_diary_write":
            return mcp.tool_diary_write(**request["params"]["arguments"])
        return mcp.tool_reconnect()

    monkeypatch.setattr(mcp, "handle_request", dispatch_to_actual_tool)

    def run(name):
        try:
            arguments = (
                {"agent_name": "probe", "entry": "Preserve this exact diary entry."}
                if name == "mempalace_diary_write"
                else {}
            )
            result[name] = mcp._http_dispatch(
                {
                    "jsonrpc": "2.0",
                    "id": name,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
        except Exception as e:
            errors.append(repr(e))

    a = threading.Thread(target=run, args=("mempalace_diary_write",), daemon=True)
    b = threading.Thread(target=run, args=("mempalace_reconnect",), daemon=True)
    try:
        a.start()
        assert got_handle.wait(3)
        b.start()
        a.join(5)
        b.join(5)
        assert not a.is_alive() and not b.is_alive(), "request deadlocked"
        assert not errors, errors
        assert reconnect_queued.is_set()
        saved = backend.get_collection(palace=ref, collection_name="mempalace_drawers").get(
            include=["documents"]
        )
        print(
            json.dumps(
                {
                    "release_enabled": allow_release,
                    "responses": result,
                    "persisted_documents": saved.documents,
                }
            )
        )
        assert result["mempalace_diary_write"]["success"], result
        assert saved.documents == ["Preserve this exact diary entry."]
    finally:
        backend.close()


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("raise_during_embedding", [False, True])
def test_busy_lifecycle_keeps_request_lease_without_waiting(
    monkeypatch, mode, raise_during_embedding
):
    """A lifecycle owner may itself be waiting for this request lease."""
    from mempalace import mcp_server as mcp

    request_lock = mcp._RWLock()
    lifecycle = threading.Lock()
    monkeypatch.setattr(mcp, "_HTTP_REQUEST_LOCK", request_lock)
    monkeypatch.setattr(mcp, "_HTTP_EMBEDDING_LIFECYCLE_LOCK", lifecycle)
    completed = threading.Event()
    errors = []

    def infer():
        try:
            lease = request_lock.read_lock() if mode == "read" else request_lock
            with lease:
                try:
                    with mcp._http_release_request_lock_for_embedding(mode):
                        # A nested embedding hook must not drop the lease either.
                        with mcp._http_release_request_lock_for_embedding(mode):
                            assert request_lock._readers == (mode == "read")
                            assert request_lock._writer == (mode == "write")
                            if raise_during_embedding:
                                raise ValueError("synthetic inference failure")
                except ValueError:
                    assert raise_during_embedding
                assert request_lock._readers == (mode == "read")
                assert request_lock._writer == (mode == "write")
            assert getattr(mcp._http_embedding_release_tls, "depth", 0) == 0
        except Exception as exc:
            errors.append(exc)
        finally:
            completed.set()

    lifecycle.acquire()
    worker = threading.Thread(target=infer, daemon=True)
    worker.start()
    try:
        finished_while_lifecycle_busy = completed.wait(2)
    finally:
        lifecycle.release()
        worker.join(5)
    assert finished_while_lifecycle_busy, (
        "embedding waited on a lifecycle owner while dropping its lease"
    )
    assert not worker.is_alive()
    assert not errors, errors


@pytest.mark.parametrize("mode", ["read", "write"])
def test_uncontended_embedding_releases_and_restores_lease_on_error(monkeypatch, mode):
    """The responsive path still releases, nests safely and recovers on error."""
    from mempalace import mcp_server as mcp

    request_lock = mcp._RWLock()
    lifecycle = threading.Lock()
    monkeypatch.setattr(mcp, "_HTTP_REQUEST_LOCK", request_lock)
    monkeypatch.setattr(mcp, "_HTTP_EMBEDDING_LIFECYCLE_LOCK", lifecycle)
    lease = request_lock.read_lock() if mode == "read" else request_lock
    with lease:
        with pytest.raises(ValueError, match="synthetic"):
            with mcp._http_release_request_lock_for_embedding(mode):
                with mcp._http_release_request_lock_for_embedding(mode):
                    assert request_lock._readers == 0
                    assert not request_lock._writer
                    assert lifecycle.locked()
                    raise ValueError("synthetic inference failure")
        assert request_lock._readers == (mode == "read")
        assert request_lock._writer == (mode == "write")
        assert not lifecycle.locked()
    assert getattr(mcp._http_embedding_release_tls, "depth", 0) == 0


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("peer_error", [False, True])
def test_peer_writer_finishes_while_embedding_owner_reclaims_lease(monkeypatch, mode, peer_error):
    from mempalace import mcp_server as mcp

    request = mcp._RWLock()
    lifecycle = threading.Lock()
    monkeypatch.setattr(mcp, "_HTTP_REQUEST_LOCK", request)
    monkeypatch.setattr(mcp, "_HTTP_EMBEDDING_LIFECYCLE_LOCK", lifecycle)
    embedding_started = threading.Event()
    peer_started = threading.Event()
    peer_release = threading.Event()
    completed = []
    errors = []

    def original():
        try:
            lease = request.read_lock() if mode == "read" else request
            with lease:
                with mcp._http_release_request_lock_for_embedding(mode):
                    assert lifecycle.locked()
                    embedding_started.set()
                    assert peer_started.wait(3)
                assert getattr(mcp._http_embedding_release_tls, "depth", 0) == 0
            completed.append("original")
        except Exception as exc:
            errors.append(exc)

    def peer():
        try:
            assert embedding_started.wait(3)
            with request:
                try:
                    with mcp._http_release_request_lock_for_embedding("write"):
                        with mcp._http_release_request_lock_for_embedding("write"):
                            assert request._writer
                            assert lifecycle.locked()
                            peer_started.set()
                            assert peer_release.wait(3)
                            if peer_error:
                                raise ValueError("peer inference failure")
                except ValueError:
                    assert peer_error
                assert request._writer
                assert getattr(mcp._http_embedding_release_tls, "depth", 0) == 0
            completed.append("peer")
        except Exception as exc:
            errors.append(exc)

    a = threading.Thread(target=original, daemon=True)
    b = threading.Thread(target=peer, daemon=True)
    a.start()
    b.start()
    try:
        assert peer_started.wait(3)
        assert "original" not in completed
    finally:
        peer_release.set()
        a.join(5)
        b.join(5)
    assert not a.is_alive() and not b.is_alive(), "peer writer and lifecycle owner deadlocked"
    assert not errors, errors
    assert set(completed) == {"peer", "original"}
    assert not lifecycle.locked()
    assert not request._writer and request._readers == 0
