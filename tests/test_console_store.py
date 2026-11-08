"""U2 contracts, using synthetic identities and fake upstream interfaces."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from commonplace.console import settings, store, types
from commonplace.workspace_store import POINTER, workspace_path


class ControlDB:
    """The control DB interface used by settings, without tokens or workers."""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript("""
            CREATE TABLE connections(id TEXT PRIMARY KEY, user_id TEXT, workspace_id TEXT);
            INSERT INTO connections VALUES ('a1','u1','ws_one'), ('a2','u2','ws_one'),
                                           ('a3','u1','ws_two');
        """)
        self.callbacks = []
        self.audits = []

    @contextmanager
    def _tx(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def add_pre_close_callback(self, callback):
        self.callbacks.append(callback)

    def audit(self, **fields):
        self.audits.append(fields)

    def close(self):
        for callback in self.callbacks:
            callback()
        self.callbacks.clear()
        self._conn.close()


@pytest.fixture
def db(tmp_path):
    db = ControlDB(tmp_path / "control.sqlite3")
    settings.ensure_schema(db)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(settings, "monotonic", lambda: clock.now)
    monkeypatch.setattr(store, "monotonic", lambda: clock.now)
    return clock


def person():
    return SimpleNamespace(person=True, user_id="u1", workspace_id="ws_one")


class DenyLock:
    def __init__(self):
        self.timeouts = []

    def acquire(self, *, timeout):
        self.timeouts.append(timeout)
        return False

    def release(self):
        pytest.fail("a failed acquire must not release the lock")


def test_settings_schema_and_reader_lifecycle(db, monkeypatch):
    settings.ensure_schema(db)
    assert len(db.callbacks) == 1
    assert db._console_settings.reader is None
    actual_connect = sqlite3.connect
    calls = []

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return actual_connect(*args, **kwargs)

    monkeypatch.setattr(settings.sqlite3, "connect", connect)
    assert settings.recording(db, "missing") is False
    assert settings.recording(db, "another") is False
    assert calls == [
        ((db.path,), {"timeout": 0.05, "check_same_thread": False, "isolation_level": None})
    ]
    reader = db._console_settings.reader
    assert reader is not db._conn
    assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 50
    assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("DELETE FROM console_settings")
    columns = {row[1] for row in db._conn.execute("PRAGMA table_info(console_settings)")}
    assert columns == {"connection_id", "workspace_id", "record", "updated_by", "updated_at"}
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute("INSERT INTO console_settings VALUES ('bad','ws_one',2,'u1','now')")
    db.callbacks[0]()
    assert db._console_settings.reader is None
    assert db._console_settings.cache == {}
    with pytest.raises(sqlite3.ProgrammingError):
        reader.execute("SELECT 1")


def test_settings_cache_ttl_and_write_through(db, clock):
    assert settings.recording(db, "a1") is False
    settings.set_recording(db, person(), "a1", True)
    assert settings.recording(db, "a1") is True
    reader = db._console_settings.reader
    queries = []
    reader.set_trace_callback(queries.append)
    db._conn.execute("UPDATE console_settings SET record=0 WHERE connection_id='a1'")
    clock.now += 4.999
    assert settings.recording(db, "a1") is True
    assert not queries
    clock.now = 105.0
    assert settings.recording(db, "a1") is False
    assert len(queries) == 1
    assert queries[0].startswith("SELECT record FROM console_settings WHERE connection_id=")
    settings.set_recording(db, person(), "a1", True)
    assert settings.recording(db, "a1") is True
    settings.set_recording(db, person(), "a1", False)
    assert settings.recording(db, "a1") is False
    assert len(db._conn.execute("SELECT * FROM console_settings").fetchall()) == 1


@pytest.mark.parametrize(
    ("principal", "cid"),
    [
        (SimpleNamespace(person=False, user_id="u1", workspace_id="ws_one"), "a1"),
        (person(), "a2"),
        (person(), "a3"),
        (person(), "missing"),
    ],
)
def test_settings_deny_agents_other_people_and_other_workspaces(db, principal, cid):
    with pytest.raises(types.ConsoleDenied):
        settings.set_recording(db, principal, cid, True)
    assert db._conn.execute("SELECT COUNT(*) FROM console_settings").fetchone()[0] == 0
    assert db._console_settings.cache == {}
    assert db.audits == []


def test_settings_audit_and_recording_ids_are_workspace_scoped(db):
    settings.set_recording(db, person(), "a1", True)
    settings.set_recording(
        db, SimpleNamespace(person=True, user_id="u1", workspace_id="ws_two"), "a3", True
    )
    assert settings.recording_ids(db, "ws_one") == {"a1"}
    assert settings.recording_ids(db, "ws_two") == {"a3"}
    settings.set_recording(db, person(), "a1", False)
    assert settings.recording_ids(db, "ws_one") == set()
    assert [a["reason"] for a in db.audits] == ["on", "on", "off"]
    assert db.audits[-1] == {
        "action": "console.setting",
        "decision": "ok",
        "workspace_id": "ws_one",
        "user_id": "u1",
        "connection_id": "a1",
        "reason": "off",
    }
    row = db._conn.execute("SELECT * FROM console_settings WHERE connection_id='a1'").fetchone()
    assert row["updated_by"] == "u1"
    assert row["record"] == 0
    datetime.strptime(row["updated_at"], "%Y-%m-%dT%H:%M:%SZ")


def test_forget_removes_only_one_cached_value_and_forces_a_read(db):
    settings.set_recording(db, person(), "a1", True)
    assert settings.recording(db, "absent") is False
    reader = db._console_settings.reader
    rows = list(db._conn.execute("SELECT * FROM console_settings"))
    audits = list(db.audits)
    other = db._console_settings.cache["absent"]
    settings.forget(db, "a1")
    settings.forget(db, "a1")
    settings.forget(db, "missing")
    assert db._console_settings.cache == {"absent": other}
    assert db._console_settings.reader is reader
    assert list(db._conn.execute("SELECT * FROM console_settings")) == rows
    assert db.audits == audits
    queries = []
    reader.set_trace_callback(queries.append)
    assert settings.recording(db, "a1") is True
    assert len(queries) == 1


def test_settings_cache_is_per_control_db(db, tmp_path):
    other = ControlDB(tmp_path / "other.sqlite3")
    try:
        settings.ensure_schema(other)
        settings.set_recording(db, person(), "a1", True)
        assert settings.recording(other, "a1") is False
        settings.forget(other, "a1")
        assert settings.recording(db, "a1") is True
    finally:
        other.close()


def test_fresh_settings_cache_does_not_acquire_control_lock(db, clock, monkeypatch):
    settings.set_recording(db, person(), "a1", True)
    lock = DenyLock()
    with monkeypatch.context() as patch:
        patch.setattr(db, "_lock", lock)
        assert settings.recording(db, "a1") is True
    assert lock.timeouts == []


def test_failed_setting_write_keeps_previous_cache_and_row(db, monkeypatch):
    settings.set_recording(db, person(), "a1", True)
    before = dict(db._console_settings.cache)

    def fail(**kwargs):
        raise sqlite3.OperationalError("audit unavailable")

    monkeypatch.setattr(db, "audit", fail)
    with pytest.raises(sqlite3.OperationalError):
        settings.set_recording(db, person(), "a1", False)
    assert db._console_settings.cache == before
    assert settings.recording_ids(db, "ws_one") == {"a1"}


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("failure", ["lock", "select", "connect"])
def test_settings_miss_falls_back_without_cache_write(db, clock, monkeypatch, stale, failure):
    if stale:
        settings.set_recording(db, person(), "a1", True)
        clock.now += 5
    before = dict(db._console_settings.cache)
    skipped = settings.skipped

    class BusyReader:
        def execute(self, sql, params):
            assert sql == "SELECT record FROM console_settings WHERE connection_id=?"
            assert params == ("a1",)
            raise sqlite3.OperationalError("busy")

        def close(self):
            pass

    def busy_connect(*args, **kwargs):
        raise sqlite3.OperationalError("busy")

    with monkeypatch.context() as patch:
        if failure == "lock":
            lock = DenyLock()
            patch.setattr(db, "_lock", lock)
        elif failure == "select":
            patch.setattr(db._console_settings, "reader", BusyReader())
        else:
            patch.setattr(settings.sqlite3, "connect", busy_connect)
        assert settings.recording(db, "a1") is stale
        assert db._console_settings.cache == before
        assert settings.skipped == skipped + 1
        if failure == "lock":
            assert lock.timeouts == [0.05]
        else:
            # Failed SQL must release the RLock for another caller, too.
            acquired = []

            def acquire():
                ok = db._lock.acquire(timeout=0.05)
                acquired.append(ok)
                if ok:
                    db._lock.release()

            worker = threading.Thread(target=acquire, daemon=True)
            worker.start()
            worker.join(5)
            assert acquired == [True]
    assert settings.recording(db, "a1") is stale


@pytest.mark.parametrize("stale", [False, True])
def test_settings_miss_finishes_while_another_thread_holds_lock(db, clock, stale):
    if stale:
        settings.set_recording(db, person(), "a1", True)
        clock.now += 5
    before = dict(db._console_settings.cache)
    skipped = settings.skipped
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []

    def hold():
        with db._lock:
            held.set()
            release.wait(10)

    def read():
        try:
            results.append(settings.recording(db, "a1"))
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    holder = threading.Thread(target=hold, daemon=True)
    worker = threading.Thread(target=read, daemon=True)
    holder.start()
    try:
        assert held.wait(5)
        worker.start()
        assert done.wait(5)
        assert errors == []
        assert results == [stale]
        assert db._console_settings.cache == before
        assert settings.skipped == skipped + 1
    finally:
        release.set()
        holder.join(5)
        if worker.ident is not None:
            worker.join(5)
    assert not holder.is_alive() and not worker.is_alive()
    assert settings.recording(db, "a1") is stale


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    def forbidden_mkdir(ws):
        pytest.fail("console must resolve through the supervisor")

    gateway = SimpleNamespace(
        supervisor=SimpleNamespace(workspace_dir=lambda ws: workspace_path(tmp_path, ws)),
        workspace_dir=forbidden_mkdir,
    )
    # U1 owns redaction. This test exercises U2's use of that frozen interface.
    monkeypatch.setattr(
        store.redact,
        "redact",
        lambda text: (
            text.replace("sensitive", "[redacted:test]") if isinstance(text, str) else text,
            [],
        ),
    )
    try:
        yield gateway
    finally:
        for activity in gateway.__dict__.get("_console_activity", {}).values():
            if activity._conn is not None:
                activity._conn.close()


def test_activity_reads_and_open_do_not_create_files(gateway, tmp_path):
    assert store.exists(gateway, "ws_one") is False
    assert store.peek(gateway, "ws_one") is None
    activity = store.open_activity(gateway, "ws_one")
    assert activity is store.open_activity(gateway, "ws_one")
    assert activity.after("a1", 0, 100) == []
    assert activity.tail("a1", 100) == []
    assert activity.latest_at(["a1"]) == {}
    assert activity.latest_at([]) == {}
    assert not list(tmp_path.rglob("*"))
    assert activity.append("a1", "task_list", "ok", 1) == 1
    assert store.exists(gateway, "ws_one") is True
    assert store.peek(gateway, "ws_one") is activity


def test_activity_schema_crypto_connection_redaction_and_queries(gateway, monkeypatch):
    monkeypatch.setattr(store, "datetime", FixedDatetime)
    calls = []
    real_connect = store.connect

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store, "connect", connect)
    activity = store.open_activity(gateway, "ws_one")
    assert activity.append("a1", "sensitive", "ok", 0) == 1
    conn = activity._conn
    assert conn.row_factory is store.row_factory
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 50
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert activity.append("a2", "task_add", "error", 10_000_000) == 2
    assert activity.append("a1", "x" * 100, "denied", 12) == 3
    assert len(calls) == 1
    assert calls[0][1] == {
        "uri": False,
        "timeout": 0.05,
        "check_same_thread": False,
        "isolation_level": None,
    }
    conn.execute("UPDATE console_activity SET at='2026-11-01T00:00:00Z' WHERE seq=1")
    conn.execute("UPDATE console_activity SET at='2026-11-02T00:00:00Z' WHERE seq=3")
    rows = activity.after("a1", 0, 100)
    assert [r["seq"] for r in rows] == [1, 3]
    assert rows[0] == {
        "seq": 1,
        "at": "2026-11-01T00:00:00Z",
        "connection_id": "a1",
        "name": "unknown",
        "outcome": "ok",
        "ms": 0,
    }
    assert rows[1]["name"] == "unknown"
    assert activity.after("a1", 1, 1) == [rows[1]]
    assert activity.tail("a1", 1) == [rows[1]]
    assert activity.tail("a1", 100) == rows[::-1]
    assert activity.after("a1", 0, 0) == []
    assert activity.latest_at(["a1", "missing"]) == {"a1": "2026-11-02T00:00:00Z"}
    assert store.open_activity(gateway, "ws_two").after("a1", 0, 100) == []
    columns = {r[1] for r in conn.execute("PRAGMA table_info(console_activity)")}
    assert columns == {"seq", "at", "connection_id", "name", "outcome", "ms"}
    assert [r[2] for r in conn.execute("PRAGMA index_info(console_activity_conn)")] == [
        "connection_id",
        "seq",
    ]
    for outcome, ms in [("other", 0), ("ok", -1), ("ok", 10_000_001)]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO console_activity(at,connection_id,name,outcome,ms) VALUES ('now','a1','n',?,?)",
                (outcome, ms),
            )


def test_pointer_flip_closes_old_connection_and_reopens_for_reads_and_writes(gateway):
    activity = store.open_activity(gateway, "ws_one")
    assert activity.append("a1", "old", "ok", 1) == 1
    root = gateway.supervisor.workspace_dir("ws_one")
    old_conn = activity._conn
    generation = root.with_name(root.name + ".generation-" + "a" * 24)
    generation.mkdir()
    pointer = root / POINTER
    pointer.write_text(json.dumps({"active": generation.name}))
    assert store.peek(gateway, "ws_one") is None
    assert not (generation / "console.sqlite3").exists()
    with pytest.raises(sqlite3.ProgrammingError):
        old_conn.execute("SELECT 1")
    assert activity.append("a1", "new", "ok", 2) == 1
    new_conn = activity._conn
    assert activity.after("a1", 0, 10)[0]["name"] == "new"
    pointer.write_text(json.dumps({"active": root.name}))
    assert store.peek(gateway, "ws_one") is activity
    assert activity.tail("a1", 10)[0]["name"] == "old"
    with pytest.raises(sqlite3.ProgrammingError):
        new_conn.execute("SELECT 1")
    pointer.write_text(json.dumps({"active": generation.name}))
    assert activity.latest_at(["a1"]) == {"a1": activity.tail("a1", 1)[0]["at"]}
    assert activity.append("a1", "newer", "ok", 3) == 2
    assert [r["name"] for r in activity.after("a1", 0, 10)] == ["new", "newer"]
    with sqlite3.connect(root / "console.sqlite3") as conn:
        assert conn.execute("SELECT name FROM console_activity").fetchall() == [("old",)]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a" * 64, "a" * 64),
        ("a" * 65, "unknown"),
        ("task_list\n", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
        (123, "unknown"),
        ("abc:def-._123", "abc:def-._123"),
        ("white space", "unknown"),
        ("Ω", "unknown"),
    ],
)
def test_activity_stores_only_the_bounded_name_shape(gateway, name, expected):
    activity = store.open_activity(gateway, "ws_one")
    assert activity.append("a1", name, "ok", 1) == 1
    assert activity.tail("a1", 1)[0]["name"] == expected


def test_activity_existing_file_opens_without_create(gateway, monkeypatch):
    first = store.open_activity(gateway, "ws_one")
    first.append("a1", "task_list", "ok", 1)
    first._conn.close()
    gateway._console_activity.clear()
    calls = []
    real_connect = store.connect

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(store, "connect", connect)
    reopened = store.peek(gateway, "ws_one")
    assert reopened.tail("a1", 1)[0]["name"] == "task_list"
    assert calls[0][0][0].endswith("console.sqlite3?mode=rw")
    assert calls[0][1]["uri"] is True
    assert reopened.append("a1", "task_add", "ok", 1) == 2


def test_activity_read_rechecks_file_existence(gateway):
    activity = store.open_activity(gateway, "ws_one")
    activity.append("a1", "task_list", "ok", 1)
    conn = activity._conn
    # Close before unlink so the contract also runs on Windows.
    conn.close()
    activity._path.unlink()
    assert store.peek(gateway, "ws_one") is None
    assert activity._conn is None
    assert activity.after("a1", 0, 100) == []
    assert not activity._path.exists()


def test_activity_lock_drop_count_and_failure_log_are_per_workspace(gateway, clock, monkeypatch):
    failures = []
    monkeypatch.setattr(store, "log_failure", lambda *args: failures.append(args))
    activity = store.open_activity(gateway, "ws_one")
    denied = DenyLock()
    with monkeypatch.context() as patch:
        patch.setattr(activity, "_lock", denied)
        assert activity.append("a1", "task_list", "ok", 1) is None
        assert activity.append("a1", "task_list", "ok", 1) is None
        assert activity.dropped == 2
        assert len(failures) == 1
        clock.now += 59.999
        assert activity.append("a1", "task_list", "ok", 1) is None
        assert len(failures) == 1
        clock.now = 160
        assert activity.append("a1", "task_list", "ok", 1) is None
        assert len(failures) == 2
    assert denied.timeouts == [0.05] * 4
    assert not store.exists(gateway, "ws_one")
    other = store.open_activity(gateway, "ws_two")
    with monkeypatch.context() as patch:
        patch.setattr(other, "_lock", DenyLock())
        assert other.append("a1", "task_list", "ok", 1) is None
    assert other.dropped == 1 and activity.dropped == 4
    assert len(failures) == 3
    assert activity.append("a1", "task_list", "ok", 1) == 1


def test_activity_sqlite_busy_drops_then_recovers(gateway):
    activity = store.open_activity(gateway, "ws_one")
    assert activity.append("a1", "task_list", "ok", 1) == 1
    with sqlite3.connect(activity._path, isolation_level=None) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            assert activity.append("a1", "task_list", "ok", 1) is None
            assert activity.dropped == 1
            assert len(activity.after("a1", 0, 10)) == 1
        finally:
            writer.rollback()
    assert activity.append("a1", "task_list", "ok", 1) == 2
    assert activity.dropped == 1


def test_activity_open_failure_is_dropped_and_logs_no_exception_text(gateway, monkeypatch, caplog):
    activity = store.open_activity(gateway, "ws_one")

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic-private-payload")

    with monkeypatch.context() as patch:
        patch.setattr(store, "connect", fail)
        assert activity.append("a1", "task_list", "ok", 1) is None
    assert activity.dropped == 1
    assert "console activity dropped" in caplog.text
    assert "synthetic-private-payload" not in caplog.text
    assert activity.append("a1", "task_list", "ok", 1) == 1


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("reader", ["after", "tail", "latest_at"])
def test_idle_activity_reads_exclude_rows_older_than_seven_days(gateway, monkeypatch, reader):
    monkeypatch.setattr(store, "datetime", FixedDatetime)
    activity = store.open_activity(gateway, "ws_one")
    activity.append("a1", "task_list", "ok", 1)
    conn = activity._conn
    conn.execute("DELETE FROM console_activity")
    conn.executemany(
        "INSERT INTO console_activity(seq,at,connection_id,name,outcome,ms) "
        "VALUES (?,?,?,'task_list','ok',1)",
        [
            (1, "2026-10-28T11:59:59Z", "a1"),
            (2, "2026-10-28T12:00:00Z", "a1"),
            (3, "2026-11-04T12:00:00Z", "a1"),
            (4, "2026-10-28T11:59:59Z", "a2"),
        ],
    )
    # No append/prune happens after time advances in an idle workspace.
    if reader == "latest_at":
        assert activity.latest_at(["a1", "a2"]) == {"a1": "2026-11-04T12:00:00Z"}
    else:
        rows = activity.after("a1", 0, 100) if reader == "after" else activity.tail("a1", 100)
        assert [row["seq"] for row in rows] == ([2, 3] if reader == "after" else [3, 2])
    assert conn.execute("SELECT COUNT(*) FROM console_activity").fetchone()[0] == 4


def seed(conn, start, end, at):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT INTO console_activity(seq,at,connection_id,name,outcome,ms) "
            "VALUES (?,?,'a1','task_list','ok',1)",
            ((seq, at(seq) if callable(at) else at) for seq in range(start, end)),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


@pytest.mark.parametrize("retention", ["age", "count"])
def test_prune_only_on_multiple_of_500_deletes_at_most_500(gateway, monkeypatch, retention):
    monkeypatch.setattr(store, "datetime", FixedDatetime)
    activity = store.open_activity(gateway, "ws_one")
    activity.append("a1", "task_list", "ok", 1)
    conn = activity._conn
    conn.execute("DELETE FROM console_activity")
    if retention == "age":
        end = 1000
        seed(
            conn,
            1,
            end - 1,
            lambda seq: "2026-10-27T12:00:00Z" if seq <= 800 else "2026-10-28T12:00:00Z",
        )
    else:
        end = 21000
        seed(conn, 1, end - 1, "2026-11-04T12:00:00Z")
    queries = []
    conn.set_trace_callback(queries.append)
    assert activity.append("a1", "task_list", "ok", 1) == end - 1
    assert not any(q.lstrip().startswith("DELETE") for q in queries)
    assert activity.append("a1", "task_list", "ok", 1) == end
    deletes = [q for q in queries if q.lstrip().startswith("DELETE")]
    assert len(deletes) == 1
    assert "ORDER BY seq LIMIT 500" in deletes[0]
    assert conn.execute("SELECT COUNT(*) FROM console_activity").fetchone()[0] == end - 500
    assert conn.execute("SELECT MIN(seq) FROM console_activity").fetchone()[0] == 501
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 50
    prune_at = queries.index(deletes[0])
    assert queries[prune_at - 1] == "PRAGMA busy_timeout=0"
    assert queries[prune_at + 1] == "PRAGMA busy_timeout=50"
    if retention == "age":
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM console_activity WHERE at < '2026-10-28T12:00:00Z'"
            ).fetchone()[0]
            == 300
        )
    assert activity.dropped == 0


def test_busy_prune_keeps_appended_row_and_retries_at_next_multiple(gateway, monkeypatch):
    monkeypatch.setattr(store, "datetime", FixedDatetime)
    activity = store.open_activity(gateway, "ws_one")
    activity.append("a1", "task_list", "ok", 1)
    conn = activity._conn
    conn.execute("DELETE FROM console_activity")
    seed(conn, 1, 500, "2026-10-01T00:00:00Z")
    timeouts = []
    writer = sqlite3.connect(activity._path, isolation_level=None)

    class BusyPrune:
        def execute(self, sql, *args):
            if sql == store._PRUNE:
                timeouts.append(conn.execute("PRAGMA busy_timeout").fetchone()[0])
                writer.execute("BEGIN IMMEDIATE")
                try:
                    return conn.execute(sql, *args)
                finally:
                    writer.rollback()
            return conn.execute(sql, *args)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(activity, "_conn", BusyPrune())
            assert activity.append("a1", "task_list", "ok", 1) == 500
        assert timeouts == [0]
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 50
        assert len(activity.after("a1", 0, 1000)) == 1
        assert activity.dropped == 0
        seed(conn, 501, 1000, "2026-10-01T00:00:00Z")
        assert activity.append("a1", "task_list", "ok", 1) == 1000
        assert conn.execute("SELECT COUNT(*) FROM console_activity").fetchone()[0] == 500
        assert [r["seq"] for r in activity.after("a1", 0, 2)] == [500, 1000]
        assert activity.dropped == 0
    finally:
        writer.close()


@pytest.mark.parametrize("reader", ["after", "tail", "latest_at"])
@pytest.mark.parametrize("backend", ["sqlite", "sqlcipher"])
def test_busy_activity_queries_return_empty_and_recover(gateway, monkeypatch, reader, backend):
    from mempalace.sqlite_crypto import driver

    if backend == "sqlcipher":
        pytest.importorskip("sqlcipher3")  # same convention as test_workspace_encryption
    module = driver() if backend == "sqlcipher" else sqlite3
    activity = store.open_activity(gateway, "ws_one")
    activity.append("a1", "task_list", "ok", 1)

    class BusyConnection(module.Connection):
        __module__ = "sqlcipher3" if backend == "sqlcipher" else "sqlite3"

        def execute(self, sql, *args):
            raise module.OperationalError("database is locked")

    busy = module.connect(":memory:", factory=BusyConnection)
    args = {"after": ("a1", 0, 10), "tail": ("a1", 10), "latest_at": (["a1"],)}[reader]
    try:
        with monkeypatch.context() as patch:
            patch.setattr(activity, "_conn", busy)
            try:
                result = getattr(activity, reader)(*args)
            except module.OperationalError as exc:
                result = exc
            assert result == ({} if reader == "latest_at" else [])
        assert getattr(activity, reader)(*args)
    finally:
        busy.close()
