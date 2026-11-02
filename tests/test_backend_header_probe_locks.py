"""Header probes must not drop the writer's POSIX locks.

``SQLiteExactBackend.detect()``, ``ChromaBackend.detect()`` and
``SQLiteExactBackend._database_signature()`` used to ``open()`` the palace
database directly. POSIX fcntl locks belong to the (process, inode) pair, so
closing that plain descriptor dropped every lock the process held on the
file -- including the SHARED lock the long-lived WAL writer keeps for its
whole life. From then on any external connection's clean ``close()`` could
checkpoint and unlink ``-wal`` / ``-shm`` underneath the writer, which kept
writing into the unlinked inodes with no error (silent data loss).

These tests pin the mechanism (the lock survives the probes), the symptom
(sidecars survive an external close, later writes stay visible) and the
#1893 behaviour the probe was introduced for.
"""

import builtins
import gc
import io
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from _chroma_palace_helper import make_minimal_chroma_sqlite, make_minimal_sqlite_exact_sqlite

from mempalace.backends import PalaceRef, _magic
from mempalace.backends.chroma import ChromaBackend
from mempalace.backends.sqlite_exact import _DB_FILENAME, SQLiteExactBackend
from mempalace.palace import resolve_backend_name

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX fcntl lock semantics only")

# SQLite's lock bytes (os_unix.c): PENDING_BYTE = 0x40000000, SHARED_FIRST =
# PENDING_BYTE + 2, SHARED_SIZE = 510. A WAL-mode connection holds a read
# lock on the SHARED range for its whole life.
_SHARED_FIRST = 0x40000000 + 2
_SHARED_SIZE = 510

# Try to take a conflicting *write* lock on the SHARED range from another
# process. EAGAIN/EACCES means somebody (the writer) still holds it.
_LOCK_PROBE = f"""
import fcntl, json, sys
fd = open(sys.argv[1], "r+b")
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, {_SHARED_SIZE}, {_SHARED_FIRST})
except OSError:
    print(json.dumps({{"held": True}}))
else:
    fcntl.lockf(fd, fcntl.LOCK_UN, {_SHARED_SIZE}, {_SHARED_FIRST})
    print(json.dumps({{"held": False}}))
"""

# The "attack": an ordinary read-write connection that does one read and
# closes cleanly. sqlite3WalClose() then tries for an EXCLUSIVE lock; if it
# gets it (because the writer's SHARED lock is gone) it checkpoints and
# deletes the -wal/-shm files the writer is still using.
_EXTERNAL_CLOSE = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("select count(*) from sqlite_master").fetchone()
conn.close()
"""

_COUNT_ROWS = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("select count(*) from documents").fetchone()[0])
conn.close()
"""


def _run(script: str, db_path: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", script, db_path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _shared_lock_held(db_path: str) -> bool:
    return json.loads(_run(_LOCK_PROBE, db_path))["held"]


def _lock_released_within(db_path: str, timeout: float = 5.0) -> bool:
    """Poll until no process holds the SHARED range, or ``timeout`` elapses.

    ``backend.close()`` returning does not mean the lock is already gone.
    Chroma's Rust system stops by dropping its Python reference to the native
    bindings (``del self.bindings``), so the SQLite connection that holds the
    lock is freed when that object is collected, which the garbage collector
    and native teardown decide. Asserting on the very next line raced that and
    failed intermittently on Linux CI. A lock that is never released still
    fails after ``timeout``.
    """
    deadline = time.monotonic() + timeout
    while True:
        gc.collect()
        if not _shared_lock_held(db_path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _writer(tmp_path, name="mempalace_drawers"):
    backend = SQLiteExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend, backend.get_collection(palace=palace, collection_name=name, create=True)


def _chroma_writer(tmp_path, name="mempalace_drawers"):
    db_path = os.path.join(str(tmp_path), "chroma.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()
    backend = ChromaBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    return backend, backend.get_collection(palace=palace, collection_name=name, create=True)


def _add(col, i: int) -> None:
    col.add(
        ids=[f"d{i}"],
        documents=[f"doc {i}"],
        metadatas=[{"i": i}],
        embeddings=[[1.0, float(i)]],
    )


def _sidecar_inodes(db_path: str) -> tuple:
    return tuple(os.stat(db_path + suffix).st_ino for suffix in ("-wal", "-shm"))


@pytest.fixture(autouse=True)
def _cold_probe_cache():
    _magic.forget()
    _magic.close_retained()
    yield
    _magic.forget()
    _magic.close_retained()


def test_probes_open_no_plain_descriptor_on_live_databases(tmp_path, monkeypatch):
    """No probe may open the database through anything but SQLite.

    The cache is cold, so this exercises the real probe, not a cache hit.
    """
    backend, col = _writer(tmp_path / "exact")
    _add(col, 0)
    exact_db = os.path.join(str(tmp_path / "exact"), _DB_FILENAME)
    (tmp_path / "chroma").mkdir()
    chroma_db = str(make_minimal_chroma_sqlite(tmp_path / "chroma"))

    opened = []

    def record(open_fn):
        def wrapper(file, *args, **kwargs):
            opened.append(os.fspath(file) if not isinstance(file, int) else file)
            return open_fn(file, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(builtins, "open", record(builtins.open))
    monkeypatch.setattr(io, "open", record(io.open))
    monkeypatch.setattr(os, "open", record(os.open))

    assert SQLiteExactBackend.detect(str(tmp_path / "exact")) is True
    assert SQLiteExactBackend.detect(str(tmp_path / "exact")) is True
    assert ChromaBackend.detect(str(tmp_path / "chroma")) is True
    assert resolve_backend_name(str(tmp_path / "exact")) == "sqlite_exact"
    backend._database_signature(exact_db)
    backend._database_signature(exact_db)

    touched = {p for p in opened if isinstance(p, str) and p.endswith(".sqlite3")}
    assert exact_db not in touched
    assert chroma_db not in touched
    backend.close()


@posix_only
def test_probes_keep_writer_shared_lock(tmp_path):
    """Mechanism: the writer's SHARED lock survives every probe.

    Unpatched, the first ``detect()`` after the writer opened dropped it.
    """
    backend, col = _writer(tmp_path)
    _add(col, 0)
    db_path = os.path.join(str(tmp_path), _DB_FILENAME)
    assert _shared_lock_held(db_path), "precondition: WAL writer holds SHARED"

    for _ in range(3):
        assert SQLiteExactBackend.detect(str(tmp_path)) is True
        assert resolve_backend_name(str(tmp_path)) == "sqlite_exact"
        backend._database_signature(db_path)
        assert _shared_lock_held(db_path)

    backend.close()
    del col
    assert _lock_released_within(db_path), "closing the writer releases the lock"


@posix_only
def test_probes_keep_chroma_writer_shared_lock(tmp_path):
    """Mechanism: Chroma's writer SHARED lock survives detection across cache clears.

    ChromaDB (1.5+) embeds its own SQLite inside native Rust bindings
    (chromadb_rust_bindings), which does not share the libsqlite3
    unixInodeInfo / pUnused table with Python's sqlite3 module. Closing a
    throwaway connection from Python drops Chroma's POSIX lock unless the
    probe connection is retained.
    """
    backend, col = _chroma_writer(tmp_path)
    _add(col, 0)
    db_path = os.path.join(str(tmp_path), "chroma.sqlite3")
    assert _shared_lock_held(db_path), "precondition: Chroma WAL writer holds SHARED"

    for _ in range(3):
        _magic.forget()
        assert ChromaBackend.detect(str(tmp_path)) is True
        assert resolve_backend_name(str(tmp_path)) == "chroma"
        assert _shared_lock_held(db_path), "Chroma writer SHARED lock dropped by detect()"

    backend.close()
    del col
    assert _lock_released_within(db_path), "closing the writer releases the lock"


@posix_only
def test_concurrent_cold_probes_create_one_connection_and_keep_writer_lock(tmp_path, monkeypatch):
    import chromadb

    db_path = str(tmp_path / "chroma.sqlite3")
    setup = sqlite3.connect(db_path)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.close()
    client = chromadb.PersistentClient(path=str(tmp_path))
    collection = client.get_or_create_collection("probe-race", embedding_function=None)
    collection.add(ids=["a"], documents=["alpha"], embeddings=[[1.0, 0.0]])
    st = os.stat(db_path)
    key = (st.st_dev, st.st_ino)
    first_created, second_progress, resume = (threading.Event() for _ in range(3))
    real_lock, real_connect = threading.Lock(), sqlite3.connect
    created_ids, thread_checks, results, errors = [], [], [], []

    class ObservedLock:
        def __enter__(self):
            if not real_lock.acquire(blocking=False):
                if threading.current_thread() is threads[1]:
                    second_progress.set()
                real_lock.acquire()

        def __exit__(self, *exc):
            real_lock.release()

    def paused_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if threading.current_thread() in threads:
            # Keep only identities: retaining either connection would hide an orphan's GC close.
            created_ids.append(id(conn))
            thread_checks.append(kwargs.get("check_same_thread"))
            if threading.current_thread() is threads[0]:
                first_created.set()
                if not resume.wait(30):
                    raise TimeoutError("first probe was not resumed")
            else:
                # The broken implementation reaches a second connect instead of waiting.
                second_progress.set()
        return conn

    def probe():
        try:
            results.append(_magic.has_sqlite_magic(db_path))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=probe) for _ in range(2)]
    try:
        assert key not in _magic._retained_conns and key not in _magic._positive
        assert _shared_lock_held(db_path), "precondition: native writer holds SHARED"
        # Observe the inode's probe lock; the dictionary lock stays available
        # while the first connection is paused.
        monkeypatch.setitem(_magic._probe_locks, key, ObservedLock())
        monkeypatch.setattr(_magic.sqlite3, "connect", paused_connect)
        try:
            threads[0].start()
            assert first_created.wait(30), "first probe did not open its connection"
            threads[1].start()
            assert second_progress.wait(30), "second probe did not reach the retention race"
        finally:
            resume.set()
            for thread in threads:
                if thread.ident is not None:
                    thread.join(30)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == [] and results == [True, True]
        gc.collect()
        assert (len(created_ids), len(_magic._retained_conns), _shared_lock_held(db_path)) == (
            1,
            1,
            True,
        )
        assert id(_magic._retained_conns[key]) == created_ids[0]
        assert thread_checks == [False]
    finally:
        client.close()


@pytest.mark.parametrize("probe_count", [1, 300])
@pytest.mark.parametrize("cached", [False, True])
def test_other_palaces_progress_during_cold_probes(tmp_path, monkeypatch, probe_count, cached):
    """Even a stalled first probe must not hold up another palace."""
    paths = []
    for i in range(probe_count + 1):
        path = str(tmp_path / f"probe-{i}.sqlite3")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE synthetic(value)")
        conn.close()
        paths.append(path)
    if cached:
        assert _magic.has_sqlite_magic(paths[-1])
    entered, resume, done = (threading.Event() for _ in range(3))
    real_connect = sqlite3.connect
    opened, results, errors = [], [], []

    def paused_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if threading.current_thread() is producer:
            opened.append(args[0])
            if len(opened) == 1:
                entered.set()
                if not resume.wait(5):
                    raise TimeoutError("first probe was not resumed")
        return conn

    def produce():
        try:
            for path in paths[:-1]:
                assert _magic.has_sqlite_magic(path)
        except BaseException as exc:
            errors.append(exc)

    def lookup():
        try:
            results.append(_magic.has_sqlite_magic(paths[-1]))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    producer = threading.Thread(target=produce)
    consumer = threading.Thread(target=lookup)
    monkeypatch.setattr(_magic.sqlite3, "connect", paused_connect)
    try:
        producer.start()
        assert entered.wait(5)
        consumer.start()
        assert done.wait(2), "another palace waited for the stalled probe"
        assert not resume.is_set()
    finally:
        resume.set()
        for thread in (producer, consumer):
            if thread.ident is not None:
                thread.join(5)
    assert not producer.is_alive() and not consumer.is_alive()
    assert not errors and results == [True]
    assert len(opened) == probe_count


@pytest.mark.parametrize("all_paths", [False, True])
def test_forget_during_probe_does_not_publish_old_generation(tmp_path, monkeypatch, all_paths):
    db_path = str(make_minimal_chroma_sqlite(tmp_path))
    entered, resume, forgotten = (threading.Event() for _ in range(3))
    real_connect = sqlite3.connect
    opened, results, errors = [], [], []

    def paused_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(id(conn))
        entered.set()
        if not resume.wait(5):
            raise TimeoutError("probe was not resumed")
        return conn

    def probe():
        try:
            results.append(_magic.has_sqlite_magic(db_path))
        except BaseException as exc:
            errors.append(exc)

    def forget():
        _magic.forget(None if all_paths else db_path)
        forgotten.set()

    producer = threading.Thread(target=probe)
    invalidator = threading.Thread(target=forget)
    monkeypatch.setattr(_magic.sqlite3, "connect", paused_connect)
    try:
        producer.start()
        assert entered.wait(5)
        invalidator.start()
        assert forgotten.wait(2), "invalidation held up by SQLite I/O"
    finally:
        resume.set()
        for thread in (producer, invalidator):
            if thread.ident is not None:
                thread.join(5)
    assert not producer.is_alive() and not invalidator.is_alive()
    assert not errors and results == [True]
    st = os.stat(db_path)
    key = (st.st_dev, st.st_ino)
    assert key not in _magic._positive
    assert _magic.has_sqlite_magic(db_path)
    if os.name == "posix":
        assert opened == [id(_magic._retained_conns[key])]


def test_fork_reset_does_not_close_inherited_connections(monkeypatch):
    class InheritedConnection:
        def close(self):
            pytest.fail("fork cleanup closed an inherited SQLite connection")

    connection = InheritedConnection()
    old_global, old_probe = threading.Lock(), threading.Lock()
    monkeypatch.setattr(_magic, "_lock", old_global)
    monkeypatch.setattr(_magic, "_probe_locks", {(1, 2): old_probe})
    monkeypatch.setattr(_magic, "_retained_conns", {(1, 2): connection})
    monkeypatch.setattr(_magic, "_positive", {(1, 2): True})
    monkeypatch.setattr(_magic, "_generation", 9)
    monkeypatch.setattr(_magic, "_inherited_conns", [])
    with old_global, old_probe:
        _magic._reset_after_fork_in_child()
        assert _magic._lock.acquire(blocking=False)
        _magic._lock.release()
    assert _magic._probe_locks == {}
    assert _magic._retained_conns == {} and _magic._positive == {}
    assert _magic._generation == 0
    assert _magic._inherited_conns == [connection]


@posix_only
def test_close_retained_waits_for_inflight_probe(tmp_path, monkeypatch):
    db_path = str(make_minimal_chroma_sqlite(tmp_path))
    entered, resume, closing = (threading.Event() for _ in range(3))
    real_connect = sqlite3.connect
    connections, errors = [], []

    def paused_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        entered.set()
        if not resume.wait(5):
            raise TimeoutError("probe was not resumed")
        return conn

    def probe():
        try:
            assert _magic.has_sqlite_magic(db_path)
        except BaseException as exc:
            errors.append(exc)

    def close():
        closing.set()
        try:
            _magic.close_retained()
        except BaseException as exc:
            errors.append(exc)

    producer = threading.Thread(target=probe)
    closer = threading.Thread(target=close)
    monkeypatch.setattr(_magic.sqlite3, "connect", paused_connect)
    try:
        producer.start()
        assert entered.wait(5)
        closer.start()
        assert closing.wait(5)
    finally:
        resume.set()
        for thread in (producer, closer):
            if thread.ident is not None:
                thread.join(5)
    assert not producer.is_alive() and not closer.is_alive()
    assert not errors and not _magic._retained_conns
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_fork_mid_probe_child_can_probe(tmp_path, monkeypatch):
    paths = []
    for name in ("warm", "blocked"):
        directory = tmp_path / name
        directory.mkdir()
        paths.append(str(make_minimal_chroma_sqlite(directory)))
    assert _magic.has_sqlite_magic(paths[0])
    entered, resume = threading.Event(), threading.Event()
    real_connect = sqlite3.connect
    results, errors = [], []

    def paused_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if threading.current_thread() is worker:
            entered.set()
            if not resume.wait(10):
                raise TimeoutError("parent probe was not resumed")
        return conn

    def probe():
        try:
            results.append(_magic.has_sqlite_magic(paths[1]))
        except BaseException as exc:
            errors.append(exc)

    def child_probe(pipe):
        try:
            assert not _magic._positive and not _magic._retained_conns
            assert not _magic._probe_locks and _magic._generation == 0
            assert len(_magic._inherited_conns) >= 1
            pipe.send([_magic.has_sqlite_magic(path) for path in paths])
            _magic.close_retained()
        finally:
            pipe.close()

    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=child_probe, args=(sender,))
    worker = threading.Thread(target=probe)
    monkeypatch.setattr(_magic.sqlite3, "connect", paused_connect)
    try:
        worker.start()
        assert entered.wait(5)
        # Exercise both an inherited held dictionary lock and the probe lock.
        with _magic._lock:
            child.start()
        sender.close()
        child.join(5)
        assert not child.is_alive(), "child inherited a held parent lock"
        assert child.exitcode == 0
        assert receiver.poll(1) and receiver.recv() == [True, True]
        assert not resume.is_set(), "parent probe must still be blocked"
    finally:
        resume.set()
        worker.join(5)
        if child.pid is not None and child.is_alive():
            child.terminate()
            child.join(5)
        receiver.close()
        sender.close()
    assert not worker.is_alive() and not child.is_alive()
    assert not errors and results == [True]


@posix_only
def test_external_close_cannot_orphan_wal_after_probes(tmp_path):
    """Symptom: an external clean close must not checkpoint+unlink the live WAL.

    Three tool-level operations (each goes through ``resolve_backend_name``,
    i.e. ``detect()``), then the attack, then one more write that a fresh
    process must be able to see.

    Whether the attacking close actually unlinks the sidecars depends on the
    attacker's SQLite build: observed with 3.50.4 (10/10 writes lost
    unpatched), not with 3.53.3, which leaves the sidecars alone even though
    the writer's lock is gone. ``test_probes_keep_writer_shared_lock`` is the
    build-independent guard; this test pins the user-visible symptom where the
    build reproduces it.
    """
    backend, col = _writer(tmp_path)
    db_path = os.path.join(str(tmp_path), _DB_FILENAME)
    for i in range(3):
        assert resolve_backend_name(str(tmp_path)) == "sqlite_exact"
        _add(col, i)
    before = _sidecar_inodes(db_path)

    _run(_EXTERNAL_CLOSE, db_path)

    assert os.path.exists(db_path + "-wal"), "external close unlinked the live WAL"
    assert _sidecar_inodes(db_path) == before, "external close replaced the sidecars"

    _add(col, 3)
    assert int(_run(_COUNT_ROWS, db_path)) == 4, "write after the attack is invisible"
    backend.close()


def test_detect_rejects_empty_then_accepts_written_header(tmp_path):
    """#1893: a 0-byte file is not a palace until its header lands.

    Negative probes are not cached, positive ones are keyed by inode, so a
    replaced file is re-probed.
    """
    db = tmp_path / _DB_FILENAME
    db.write_bytes(b"")
    assert SQLiteExactBackend.detect(str(tmp_path)) is False

    make_minimal_sqlite_exact_sqlite(tmp_path)
    assert SQLiteExactBackend.detect(str(tmp_path)) is True
    assert SQLiteExactBackend.detect(str(tmp_path)) is True  # cache hit

    garbage = tmp_path / "garbage"
    garbage.write_bytes(b"not a sqlite file" * 64)
    os.replace(garbage, db)  # new inode at the same path
    assert SQLiteExactBackend.detect(str(tmp_path)) is False

    marker = tmp_path / "marker"
    marker.mkdir()
    (marker / _DB_FILENAME).write_bytes(_magic.SQLITE_MAGIC)  # magic only, no page 1
    assert SQLiteExactBackend.detect(str(marker)) is True

    chroma = tmp_path / "chroma"
    chroma.mkdir()
    (chroma / "chroma.sqlite3").write_bytes(b"")
    assert ChromaBackend.detect(str(chroma)) is False
    make_minimal_chroma_sqlite(chroma)
    assert ChromaBackend.detect(str(chroma)) is True


def test_database_signature_tracks_changes(tmp_path):
    backend, col = _writer(tmp_path)
    db_path = os.path.join(str(tmp_path), _DB_FILENAME)
    first = backend._database_signature(db_path)
    assert first[-1] is not None, "header fields must be readable on a live palace"

    _add(col, 0)
    col._handle.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    second = backend._database_signature(db_path)
    assert second != first
    backend.close()

    with pytest.raises(OSError):
        backend._database_signature(os.path.join(str(tmp_path), "missing.sqlite3"))

    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"not a sqlite file" * 64)
    assert backend._database_signature(str(garbage))[-1] is None


def test_read_header_fields_reads_through_sqlite_only(tmp_path):
    db = tmp_path / "x.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 7")
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    fields = _magic.read_header_fields(str(db))
    assert fields is not None
    assert fields[3] == 7  # user_version
    assert _magic.read_header_fields(str(tmp_path)) is None  # directory
    (tmp_path / "empty").write_bytes(b"")
    assert _magic.read_header_fields(str(tmp_path / "empty")) is None


@posix_only
def test_worker_probe_is_closed_from_another_thread(tmp_path):
    db_path = str(tmp_path / "probe.sqlite3")
    database = sqlite3.connect(db_path)
    database.execute("CREATE TABLE documents (body TEXT)")
    database.commit()
    database.close()
    results = []
    worker = threading.Thread(target=lambda: results.append(_magic.has_sqlite_magic(db_path)))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert results == [True]
    stat = os.stat(db_path)
    key = (stat.st_dev, stat.st_ino)
    connection = _magic._retained_conns[key]

    def descriptors():
        if not os.path.isdir("/proc/self/fd"):
            return None
        found = set()
        for fd in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink("/proc/self/fd/" + fd)
            except OSError:
                continue
            if os.path.realpath(target) == os.path.realpath(db_path):
                found.add(fd)
        return found

    before = descriptors()
    if before is not None:
        assert before, "the worker probe owns a database descriptor"
    _magic.close_retained()
    assert key not in _magic._retained_conns
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    if before is not None:
        assert descriptors() == set(), "closing the probe releases its descriptor"


def test_retained_probe_close_reports_programming_errors(monkeypatch):
    class BrokenProbe:
        def close(self):
            raise sqlite3.ProgrammingError("probe close failed")

    with monkeypatch.context() as patch:
        patch.setattr(_magic, "_retained_conns", {(0, 0): BrokenProbe()})
        with pytest.raises(sqlite3.ProgrammingError, match="probe close failed"):
            _magic.close_retained()


def test_retained_probe_close_tolerates_os_errors(monkeypatch):
    class BrokenProbe:
        def close(self):
            raise OSError("probe descriptor already unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(_magic, "_retained_conns", {(0, 0): BrokenProbe()})
        _magic.close_retained()
        assert not _magic._retained_conns
