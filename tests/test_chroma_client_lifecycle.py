"""Client lifecycle operations never nest locks for different palace files."""

import subprocess
import sys
import textwrap

import pytest

_COMMON = """
import os
import random
import threading
import time
from pathlib import Path
from chromadb.api.client import Client
from chromadb.api.shared_system_client import SharedSystemClient
from mempalace.backends import chroma, _inproc_sqlite
from mempalace.backends.chroma import ChromaBackend
from mempalace.backends.base import PalaceRef

root = Path(sys.argv[1])
paths = [str(root / name) for name in ('p', 'q', 'r')]
options = {'caller_vectors': True}
seed = ChromaBackend()
for path in paths:
    col = seed.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', create=True, options=options)
    col.add(ids=['a'], documents=['quartz note'], embeddings=[[1., 0.]], metadatas=[{'wing': 'public'}])
seed.close()
closes = []
original_close = Client.close
def checked_close(client):
    if not client._closed:
        key = _inproc_sqlite._key(os.path.join(client._identifier, 'chroma.sqlite3'))
        assert _inproc_sqlite.held_palace_db_paths() == {key}
        closes.append(key)
    return original_close(client)
Client.close = checked_close
errors = []
def check_finished(threads):
    for thread in threads:
        thread.join(15)
    assert not any(thread.is_alive() for thread in threads), 'lifecycle deadlock'
    assert not errors, errors

def check_released():
    import gc
    gc.collect()
    chroma._drain_pending_guard_releases()
    assert closes
    for path in paths:
        key = _inproc_sqlite._key(os.path.join(path, 'chroma.sqlite3'))
        with _inproc_sqlite.palace_db_lock(key):
            assert key not in _inproc_sqlite._anchors
            assert key not in _inproc_sqlite._holders
            assert path not in SharedSystemClient._identifier_to_system
            assert not SharedSystemClient._identifier_to_refcount.get(path, 0)
"""


def _run_probe(tmp_path, body):
    # A deadlock fails the test without leaving blocked threads in pytest.
    result = subprocess.run(
        [sys.executable, "-c", "import sys\n" + textwrap.dedent(_COMMON + body), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_concurrent_reopens_do_not_nest_palace_locks(tmp_path):
    _run_probe(
        tmp_path,
        """
backends = [ChromaBackend(), ChromaBackend()]
for backend in backends:
    for path in paths[:2]:
        backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
barrier = threading.Barrier(2, timeout=5)
original_drain = ChromaBackend._drain_clients
local = threading.local()
def synchronized_drain(self, *args, **kwargs):
    if not getattr(local, 'arrived', False):
        local.arrived = True
        barrier.wait()
    return original_drain(self, *args, **kwargs)
ChromaBackend._drain_clients = synchronized_drain
for path in paths[:2]:
    stamp = time.time() + 5
    os.utime(os.path.join(path, 'chroma.sqlite3'), (stamp, stamp))
def reopen(backend, path):
    try:
        backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
    except BaseException as error:
        errors.append(repr(error))
threads = [threading.Thread(target=reopen, args=pair, daemon=True) for pair in zip(backends, paths)]
for thread in threads:
    thread.start()
check_finished(threads)
ChromaBackend._drain_clients = original_drain
for backend in backends:
    backend.close()
check_released()
""",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="bounded concurrent POSIX lifecycle probe")
def test_three_palace_random_lifecycle_stress(tmp_path):
    _run_probe(
        tmp_path,
        """
barrier = threading.Barrier(3)
operations = [0, 0, 0]
def exercise(index):
    backend = ChromaBackend()
    rng = random.Random(index)
    try:
        barrier.wait(timeout=5)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            path = rng.choice(paths)
            with _inproc_sqlite.palace_db_lock(os.path.join(path, 'chroma.sqlite3')):
                col = backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
                client = backend._client(path)
                assert client._identifier == path
                assert client.get_settings().persist_directory == path
                assert col.query(query_embeddings=[[1., 0.]], n_results=1).ids == [['a']]
                if rng.randrange(10) == 0:
                    lost = ChromaBackend.make_client(path)
                    lost = None
                    chroma._clear_chroma_system_cache(palace_path=path)
                if rng.randrange(2):
                    backend.close_palace(path)
            operations[index] += 1
    except BaseException as error:
        errors.append(repr(error))
    finally:
        backend.close()
threads = [threading.Thread(target=exercise, args=(i,), daemon=True) for i in range(3)]
for thread in threads:
    thread.start()
check_finished(threads)
assert min(operations) > 3, operations
check_released()
""",
    )


def test_reopen_preserves_other_palace_live_system(tmp_path):
    import os
    import time

    from mempalace.backends.base import PalaceRef
    from mempalace.backends.chroma import ChromaBackend

    backend = ChromaBackend()
    paths = [str(tmp_path / name) for name in ("p", "q")]
    try:
        for path in paths:
            col = backend.get_collection(
                palace=PalaceRef(id=path, local_path=path),
                collection_name="mempalace_drawers",
                create=True,
                options={"caller_vectors": True},
            )
            col.add(ids=["a"], documents=["quartz"], embeddings=[[1.0, 0.0]])
        other = backend.get_collection(
            palace=PalaceRef(id=paths[1], local_path=paths[1]),
            collection_name="mempalace_drawers",
            options={"caller_vectors": True},
        )
        client = backend._client(paths[1])
        system = client._system
        assert backend._client(paths[0]).get_settings().persist_directory == paths[0]
        assert client.get_settings().persist_directory == paths[1]
        stamp = time.time() + 5
        os.utime(os.path.join(paths[0], "chroma.sqlite3"), (stamp, stamp))
        backend.get_collection(
            palace=PalaceRef(id=paths[0], local_path=paths[0]),
            collection_name="mempalace_drawers",
            options={"caller_vectors": True},
        )
        assert backend._client(paths[1]) is client
        assert client._system is system
        assert other.get(ids=["a"]).documents == ["quartz"]
    finally:
        backend.close()


def test_dropped_client_does_not_hide_peer_vector_writes(tmp_path):
    _run_probe(
        tmp_path,
        """
import subprocess
backend = ChromaBackend()
path = paths[0]
col = backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
col.query(query_embeddings=[[0., 1.]], n_results=1)
lost = ChromaBackend.make_client(path)
lost = None
writer = '''
import sys
from mempalace.backends.chroma import ChromaBackend
backend = ChromaBackend()
col = backend.get_collection(sys.argv[1], 'mempalace_drawers')
col.add(ids=['peer'], documents=['new peer drawer'], embeddings=[[0., 1.]], metadatas=[{'wing': 'public'}])
backend.close()
'''
subprocess.run([sys.executable, '-c', writer, path], check=True, capture_output=True, timeout=20)
stamp = time.time() + 5
os.utime(os.path.join(path, 'chroma.sqlite3'), (stamp, stamp))
fresh = backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
assert fresh.count() == 2
assert fresh.query(query_embeddings=[[0., 1.]], n_results=1).ids == [['peer']]
col = None
backend.close()
check_released()
""",
    )


def test_dropped_client_does_not_break_in_place_repair(tmp_path):
    _run_probe(
        tmp_path,
        """
from mempalace import repair
path = paths[0]
lost = ChromaBackend.make_client(path)
lost = None
repair.rebuild_from_sqlite(path, path, archive_existing_dest=True)
backend = ChromaBackend()
col = backend.get_collection(path, 'mempalace_drawers')
assert Path(path, 'chroma.sqlite3').is_file()
assert col.get().ids == ['a']
backend.close()
""",
    )


def test_session_closes_dropped_client_across_external_rebuild(tmp_path):
    _run_probe(
        tmp_path,
        """
import json
import subprocess
from mempalace import mcp_server
from mempalace.backends import embedding_wrapper
from mempalace.config import MempalaceConfig
embedding_wrapper._embed_texts = lambda texts, **kw: [[1., 0.] for _ in texts]
path = paths[0]
config = root / 'config'
config.mkdir()
(config / 'config.json').write_text(json.dumps({'palace_path': path}))
mcp_server._config = MempalaceConfig(config_dir=str(config))
assert mcp_server._get_collection().count() == 1
old_client = mcp_server._client_cache
lost = ChromaBackend.make_client(path)
lost = None
os.rename(path, path + '.old')
mcp_server._get_client()
assert old_client._closed
writer = '''
import sys
from mempalace.backends.chroma import ChromaBackend
backend = ChromaBackend()
col = backend.get_collection(sys.argv[1], 'mempalace_drawers', create=True)
col.add(ids=[f'new{i}' for i in range(5)], documents=['new drawer'] * 5, embeddings=[[1., 0.]] * 5, metadatas=[{'wing': 'public'}] * 5)
backend.close()
'''
subprocess.run([sys.executable, '-c', writer, path], check=True, capture_output=True, timeout=20)
assert mcp_server._get_collection().count() == 5
assert set(mcp_server._get_collection().get().ids) == {f'new{i}' for i in range(5)}
mcp_server._drop_session_client_for_system_reset()
""",
    )


def test_close_under_foreign_lock_releases_all_owned_clients(tmp_path):
    _run_probe(
        tmp_path,
        """
backend = ChromaBackend()
for path in paths[:2]:
    backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
with _inproc_sqlite.palace_db_lock(os.path.join(paths[0], 'chroma.sqlite3')):
    backend.close()
with chroma._CLIENT_REGISTRY_LOCK:
    worker = chroma._pending_close_worker
if worker is not None:
    worker.join(10)
    assert not worker.is_alive(), 'deferred close deadlock'
assert not chroma._PENDING_CLIENT_CLOSES
check_released()
""",
    )


def test_reset_drains_deferred_owner_before_replacing_system(tmp_path):
    _run_probe(
        tmp_path,
        """
backend = ChromaBackend()
for path in paths[:2]:
    backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
other = paths[1]
def close_under_foreign_lock():
    try:
        with _inproc_sqlite.palace_db_lock(os.path.join(paths[0], 'chroma.sqlite3')):
            backend.close()
    except BaseException as error:
        errors.append(repr(error))
with _inproc_sqlite.palace_db_lock(os.path.join(other, 'chroma.sqlite3')):
    thread = threading.Thread(target=close_under_foreign_lock, daemon=True)
    thread.start()
    check_finished([thread])
    assert chroma._PENDING_CLIENT_CLOSES
    chroma._clear_chroma_system_cache(palace_path=other)
    fresh_backend = ChromaBackend()
    fresh = fresh_backend.get_collection(palace=PalaceRef(id=other, local_path=other), collection_name='mempalace_drawers', options=options)
with chroma._CLIENT_REGISTRY_LOCK:
    worker = chroma._pending_close_worker
if worker is not None:
    worker.join(10)
    assert not worker.is_alive()
assert fresh.query(query_embeddings=[[1., 0.]], n_results=1).ids == [['a']]
fresh_backend.close()
check_released()
""",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX index-check guard lifecycle")
@pytest.mark.parametrize("helper_guard", [False, True])
def test_orphaned_system_reset_releases_transient_guards(tmp_path, helper_guard):
    _run_probe(
        tmp_path,
        f"""
if {helper_guard!r}:
    _inproc_sqlite._ANCHORED = False
    _inproc_sqlite._HOLD_LOCKS = True
backend = ChromaBackend()
path = paths[0]
backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
lost = ChromaBackend.make_client(path)
lost = None
key = _inproc_sqlite._key(os.path.join(path, 'chroma.sqlite3'))
assert key in _inproc_sqlite._anchors or key in _inproc_sqlite._holders
chroma._clear_chroma_system_cache(palace_path=path)
backend.close()
check_released()
""",
    )


@pytest.mark.parametrize("legacy_client", [False, True])
def test_cold_open_drains_its_pending_client_before_peer_vector_read(tmp_path, legacy_client):
    _run_probe(
        tmp_path,
        f"legacy_client = {legacy_client!r}\n"
        + """
import subprocess
backend = ChromaBackend()
for path in paths:
    col = backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
    col.query(query_embeddings=[[0., 1.]], n_results=1)
held, release = threading.Event(), threading.Event()
def writer_lock():
    with _inproc_sqlite.palace_db_lock(os.path.join(paths[0], 'chroma.sqlite3')):
        held.set()
        release.wait(20)
writer_thread = threading.Thread(target=writer_lock, daemon=True)
writer_thread.start()
assert held.wait(5)
with _inproc_sqlite.palace_db_lock(os.path.join(paths[2], 'chroma.sqlite3')):
    backend.close()
writer = '''
import sys
from mempalace.backends.chroma import ChromaBackend
backend = ChromaBackend()
col = backend.get_collection(sys.argv[1], 'mempalace_drawers')
col.add(ids=[f'peer{i}' for i in range(20)], documents=['peer'] * 20, embeddings=[[0., 1.]] * 20, metadatas=[{'wing': 'public'}] * 20)
backend.close()
'''
subprocess.run([sys.executable, '-c', writer, paths[1]], check=True, capture_output=True, timeout=20)
fresh_backend = ChromaBackend()
if legacy_client:
    client = ChromaBackend.make_client(paths[1])
    fresh = client.get_collection('mempalace_drawers')
    ids = fresh.query(query_embeddings=[[0., 1.]], n_results=10)['ids'][0]
else:
    fresh = fresh_backend.get_collection(palace=PalaceRef(id=paths[1], local_path=paths[1]), collection_name='mempalace_drawers', options=options)
    ids = fresh.query(query_embeddings=[[0., 1.]], n_results=10).ids[0]
assert len(ids) == 10 and all(id.startswith('peer') for id in ids), ids
release.set()
check_finished([writer_thread])
if legacy_client:
    chroma._close_client(client, paths[1])
fresh_backend.close()
chroma._drain_pending_clients()
""",
    )


def test_busy_palace_does_not_block_another_pending_close(tmp_path):
    _run_probe(
        tmp_path,
        """
backend = ChromaBackend()
for path in paths:
    backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
closed = threading.Event()
checked_close = Client.close
def observe_close(client):
    result = checked_close(client)
    if client._identifier == paths[1]:
        closed.set()
    return result
Client.close = observe_close
def close_backend():
    with _inproc_sqlite.palace_db_lock(os.path.join(paths[2], 'chroma.sqlite3')):
        backend.close()
with _inproc_sqlite.palace_db_lock(os.path.join(paths[0], 'chroma.sqlite3')):
    thread = threading.Thread(target=close_backend, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert closed.wait(2), 'busy P blocked the close of Q'
    assert not SharedSystemClient._identifier_to_refcount.get(paths[1], 0)
with chroma._CLIENT_REGISTRY_LOCK:
    worker = chroma._pending_close_worker
if worker is not None:
    worker.join(5)
    assert not worker.is_alive()
check_released()
""",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fork lifecycle")
def test_fork_resets_pending_queue_worker_and_registry_lock(tmp_path):
    _run_probe(
        tmp_path,
        """
from multiprocessing import get_context
inherited = object()
forgotten = type('ForgottenSystem', (), {})()
with chroma._CLIENT_REGISTRY_LOCK:
    chroma._FORGOTTEN_SYSTEMS[paths[0]] = [chroma.weakref.ref(forgotten)]
    chroma._PENDING_GUARD_RELEASES.add(paths[0])
    chroma._PENDING_CLIENT_CLOSES[id(inherited)] = (paths[0], inherited)
    chroma._pending_close_worker = threading.Thread()
held, release = threading.Event(), threading.Event()
def hold_registry():
    with chroma._CLIENT_REGISTRY_LOCK:
        held.set()
        release.wait(10)
thread = threading.Thread(target=hold_registry, daemon=True)
thread.start()
assert held.wait(5)
def child():
    assert not chroma._PENDING_CLIENT_CLOSES, 'child inherited pending clients'
    assert not chroma._FORGOTTEN_SYSTEMS
    assert not chroma._PENDING_GUARD_RELEASES
    assert chroma._pending_close_worker is None
    closed = threading.Event()
    chroma._close_client = lambda client, path: closed.set()
    owner = object()
    with chroma._CLIENT_REGISTRY_LOCK:
        chroma._PENDING_CLIENT_CLOSES[id(owner)] = (paths[0], owner)
    chroma._schedule_pending_closes()
    assert closed.wait(2), 'child worker did not start'
process = get_context('fork').Process(target=child)
process.start()
process.join(5)
if process.is_alive():
    process.kill()
    process.join()
    raise AssertionError('child inherited a locked registry')
release.set()
thread.join(5)
assert process.exitcode == 0
with chroma._CLIENT_REGISTRY_LOCK:
    assert chroma._PENDING_CLIENT_CLOSES[id(inherited)][1] is inherited
    assert chroma._FORGOTTEN_SYSTEMS[paths[0]][0]() is forgotten
    chroma._FORGOTTEN_SYSTEMS.clear()
    chroma._PENDING_GUARD_RELEASES.clear()
    chroma._PENDING_CLIENT_CLOSES.clear()
    chroma._pending_close_worker = None
""",
    )


def test_pending_worker_failure_is_logged_and_later_closes_restart_it(tmp_path):
    _run_probe(
        tmp_path,
        """
import io
import logging
output = io.StringIO()
handler = logging.StreamHandler(output)
chroma.logger.addHandler(handler)
original_drain = chroma._drain_pending_clients
failed = threading.Event()
def fail_once(*args, **kwargs):
    if not failed.is_set():
        failed.set()
        raise RuntimeError('injected cleanup failure')
    return original_drain(*args, **kwargs)
chroma._drain_pending_clients = fail_once
closed = []
chroma._close_client = lambda client, path: closed.append(client)
first, second = object(), object()
with chroma._CLIENT_REGISTRY_LOCK:
    chroma._PENDING_CLIENT_CLOSES[id(first)] = (paths[0], first)
chroma._schedule_pending_closes()
with chroma._CLIENT_REGISTRY_LOCK:
    worker = chroma._pending_close_worker
if worker is not None:
    worker.join(5)
assert failed.is_set()
assert chroma._pending_close_worker is None
assert 'Pending Chroma client cleanup failed' in output.getvalue()
with chroma._CLIENT_REGISTRY_LOCK:
    chroma._PENDING_CLIENT_CLOSES[id(second)] = (paths[1], second)
chroma._schedule_pending_closes()
with chroma._CLIENT_REGISTRY_LOCK:
    worker = chroma._pending_close_worker
if worker is not None:
    worker.join(5)
    assert not worker.is_alive()
assert set(closed) == {first, second}
assert not chroma._PENDING_CLIENT_CLOSES
""",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX orphan guard lifecycle")
@pytest.mark.parametrize("helper_guard", [False, True])
def test_live_forgotten_system_retains_guards_during_peer_writes(tmp_path, helper_guard):
    _run_probe(
        tmp_path,
        f"helper_guard = {helper_guard!r}\n"
        + """
import gc
import subprocess
if helper_guard:
    _inproc_sqlite._ANCHORED = False
    _inproc_sqlite._HOLD_LOCKS = True
backend = ChromaBackend()
path = paths[0]
backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
orphan = ChromaBackend.make_client(path)
orphan_collection = orphan.get_collection('mempalace_drawers')
key = _inproc_sqlite._key(os.path.join(path, 'chroma.sqlite3'))
chroma._clear_chroma_system_cache(palace_path=path)
assert key in _inproc_sqlite._anchors or key in _inproc_sqlite._holders, 'live orphan lost its read guard'
writer = '''
import sys
from mempalace.backends.chroma import ChromaBackend
backend = ChromaBackend()
col = backend.get_collection(sys.argv[1], 'mempalace_drawers')
col.add(ids=['peer'], documents=['peer note'], embeddings=[[0., 1.]], metadatas=[{'wing': 'public'}])
backend.close()
'''
subprocess.run([sys.executable, '-c', writer, path], check=True, capture_output=True, timeout=20)
assert orphan_collection.get(ids=['a'])['documents'] == ['quartz note']
assert key in _inproc_sqlite._anchors or key in _inproc_sqlite._holders
fresh_backend = ChromaBackend()
fresh = fresh_backend.get_collection(palace=PalaceRef(id=path, local_path=path), collection_name='mempalace_drawers', options=options)
assert fresh.query(query_embeddings=[[0., 1.]], n_results=1).ids == [['peer']]
fresh_backend.close()
assert key in _inproc_sqlite._anchors or key in _inproc_sqlite._holders
orphan_collection = orphan = None
gc.collect()
# The worker removes pending entries before releasing guards under this lock.
# Wait for an in-flight release even when the pending set is already empty.
with _inproc_sqlite.palace_db_lock(key):
    chroma._drain_pending_guard_releases(palace_path=path)
    assert key not in _inproc_sqlite._anchors
    assert key not in _inproc_sqlite._holders
backend.close()
""",
    )
