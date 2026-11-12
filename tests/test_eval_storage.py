"""``commonplace eval``: plan storage, atomicity, WAL mode and encryption (class B)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from commonplace import plans
from commonplace.evals import source, store
from commonplace.handoff.models import ThreadMessage
from commonplace.workspace_encryption import encrypt_workspace
from commonplace.workspace_store import open_threads, thread_path, workspace_path
from mempalace import sqlite_crypto
from tests.eval_support import (
    KEY,
    archive,
    archived,
    control,
    db_rows,
    eval_db,
    listed,
    make_env,
    refused,
    run,
    store_thread,
)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
ROOT = Path(__file__).resolve().parents[1]
OTHER = "Other_eval-key-00002"


@pytest.fixture
def keys(tmp_path, monkeypatch):
    pytest.importorskip("sqlcipher3")
    server, recovery = tmp_path / "server.key", tmp_path / "recovery.key"
    for path in (server, recovery):
        path.write_bytes(os.urandom(32))
        path.chmod(0o600)
    monkeypatch.setenv("COMMONPLACE_KEY_STORE", str(tmp_path / "slots"))
    monkeypatch.setenv("COMMONPLACE_WRAPPING_KEY_FILE", str(server))
    monkeypatch.delenv("COMMONPLACE_KEY_SLOT", raising=False)
    return server, recovery, tmp_path / "slots"


def encrypt(env, keys):
    from commonplace.control_db import ControlDB

    server, recovery, slots = keys

    def authorize(ref):
        with closing(ControlDB(str(env.data / "control.sqlite3"))) as db:
            row = db.workspace(ref)
            db.require_owner(row["id"], env.owner)
            return row["id"]

    old = os.umask(0o077)
    try:
        encrypt_workspace(
            env.data,
            env.ws,
            backup_dir=env.data.parent / "before-encryption",
            key_store=slots,
            server_key_file=server,
            recovery_key_file=recovery,
            authorize=authorize,
        )
    finally:
        os.umask(old)
    env.directory = workspace_path(env.data, env.ws)


# ── B19: plan storage under the thread-store writer lock ──
def _walk(env):
    return plans.StorageMeter._walk(env.directory)


def _capture(env, limit, text):
    """A writer that measures its allowance under the thread-store lock."""
    path = thread_path(env.directory)

    def allowance():
        own = sum(
            Path(str(path) + s).stat().st_size
            for s in ("", "-wal", "-shm")
            if Path(str(path) + s).exists()
        )
        return limit - (plans.StorageMeter._walk(env.directory) - own)

    with closing(open_threads(env.directory)) as live:
        return live.store(
            env.ws,
            "t3:capture",
            [ThreadMessage(0, "user", "message", text, "{}")],
            actor="synthetic",
            max_storage_bytes=allowance,
        )


def _limits(env, monkeypatch, artifact, text):
    """Measure the eval bound and the capture growth on a scratch copy."""
    import shutil

    scratch = Path(str(env.data) + "-probe")
    shutil.copytree(env.data, scratch)
    probe = type(env)(**{**vars(env), "data": scratch})
    probe.directory = workspace_path(scratch, env.ws)
    bounds = []
    original = store.physical_bound
    monkeypatch.setattr(
        store, "physical_bound", lambda c, d: bounds.append(original(c, d)) or bounds[-1]
    )
    monkeypatch.setitem(plans.PLANS, "free", replace(plans.FREE, storage_bytes=10**12))
    archived(probe, probe.stored, artifacts=[{"name": "a", "text": artifact}])
    eval_need = bounds[-1]
    start = _walk(probe)
    _capture(probe, 10**12, text)
    growth = _walk(probe) - start
    monkeypatch.setattr(store, "physical_bound", original)
    shutil.rmtree(scratch)
    return eval_need, growth


@pytest.fixture
def planned(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    env = make_env(tmp_path)
    with closing(control(env)) as db:
        plans.set_plan(db, env.owner, "free", actor=env.owner)
    return env


def _race(env, monkeypatch):
    artifact, text = "e" * (3 * 1024 * 1024), "c" * (100 * 1024)
    env.stored = store_thread(env).id
    eval_need, growth = _limits(env, monkeypatch, artifact, text)
    limit = _walk(env) + eval_need + growth // 2
    monkeypatch.setitem(plans.PLANS, "free", replace(plans.FREE, storage_bytes=limit))
    outcomes = []

    def pause(stage):
        if stage == "before_lock":
            outcomes.append(("capture", _capture(env, limit, text).id))

    monkeypatch.setattr(store, "checkpoint", pause)
    result = archive(env, env.stored, artifacts=[{"name": "a", "text": artifact}])
    return result, outcomes, limit


def test_b19_capture_and_archive_near_the_limit_never_exceed_it(planned, monkeypatch):
    result, outcomes, limit = _race(planned, monkeypatch)
    assert [kind for kind, _ in outcomes] == ["capture"]
    refused(result, "eval_storage")
    assert _walk(planned) <= limit, "B19: storage exceeded the plan limit"
    assert db_rows(planned) == []


def test_b19_encrypted_race_takes_the_lock_through_sqlite_crypto(tmp_path, monkeypatch, keys):
    monkeypatch.setenv("COMMONPLACE_PLANS", "on")
    env = make_env(tmp_path, compact=True)
    with closing(control(env)) as db:
        plans.set_plan(db, env.owner, "free", actor=env.owner)
    encrypt(env, keys)
    result, outcomes, limit = _race(env, monkeypatch)
    refused(result, "eval_storage")
    assert _walk(env) <= limit


def test_b19_plans_disabled_takes_no_thread_store_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    env = make_env(tmp_path)
    stored = store_thread(env)
    locks, writes = [], []
    monkeypatch.setattr(source, "thread_lock", lambda d: locks.append(d))
    original = sqlite_crypto.connect
    monkeypatch.setattr(
        sqlite_crypto,
        "connect",
        lambda database, **k: writes.append(database) or original(database, **k),
    )
    archived(env, stored.id)
    assert locks == [] and not [d for d in writes if "mode=rw" in str(d)], "B19: lock taken"


# ── B20: atomicity ──
def test_b20_busy_refuses_without_partial_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    env = make_env(tmp_path)
    first, second = store_thread(env, source="t3:a"), store_thread(env, source="t3:b")
    archived(env, first.id)
    monkeypatch.setattr(store, "EVAL_LOCK_WAIT_S", 0.2)
    holder = sqlite3.connect(eval_db(env), isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        refused(archive(env, second.id, key=OTHER), "eval_busy")
    finally:
        holder.close()
    assert len(db_rows(env)) == 1
    archived(env, second.id, key=OTHER)


def test_b20_injected_full_leaves_no_run_chunk_or_key(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    env = make_env(tmp_path)
    stored = store_thread(env)
    calls = []
    original = store._insert_chunk

    def full(conn, *args):
        calls.append(args)
        if len(calls) == 3:
            raise sqlite3.OperationalError("database or disk is full")
        return original(conn, *args)

    monkeypatch.setattr(store, "_insert_chunk", full)
    artifacts = [{"name": "a", "text": "x" * (600 * 1024)}]
    refused(archive(env, stored.id, artifacts=artifacts), "eval_store_error")
    assert db_rows(env) == [] and db_rows(env, "chunks") == [], "B20: partial run left behind"
    monkeypatch.setattr(store, "_insert_chunk", original)
    archived(env, stored.id, artifacts=artifacts)


def test_b20_kill_before_commit_leaves_no_run_or_key(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    env = make_env(tmp_path)
    stored = store_thread(env)
    script = textwrap.dedent(
        """
        import os, sys
        from commonplace.evals import store
        from commonplace.cli import main

        def die(stage):
            if stage == "before_commit":
                os._exit(9)

        store.checkpoint = die
        main(sys.argv[1:])
        """
    )
    argv = [
        "--data",
        str(env.data),
        "eval",
        "archive",
        "--workspace",
        env.ws,
        "--thread",
        stored.id,
        "--key",
        KEY,
    ]
    child = subprocess.run(
        [sys.executable, "-c", script, *argv],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        timeout=120,
    )
    assert child.returncode == 9, child.stderr
    assert db_rows(env) == [] and db_rows(env, "chunks") == [], "B20: a killed archive left rows"
    archived(env, stored.id)


# ── B25: WAL mode ──
def _header_mode(path):
    return Path(path).read_bytes()[18]


def test_b25_wal_after_creation_and_after_a_delete_mode_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    env = make_env(tmp_path)
    first, second = store_thread(env, source="t3:a"), store_thread(env, source="t3:b")
    archived(env, first.id)
    assert _header_mode(eval_db(env)) == 2, "B25: a new eval DB is not in WAL mode"
    with closing(sqlite3.connect(eval_db(env))) as conn:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    assert _header_mode(eval_db(env)) == 1
    archived(env, second.id, key=OTHER)
    assert _header_mode(eval_db(env)) == 2, "B25: a restored DELETE-mode copy stayed in DELETE"


def test_b25_held_reader_during_the_switch_is_busy_without_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    env = make_env(tmp_path)
    first, second = store_thread(env, source="t3:a"), store_thread(env, source="t3:b")
    archived(env, first.id)
    with closing(sqlite3.connect(eval_db(env))) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    before = eval_db(env).read_bytes()
    monkeypatch.setattr(store, "EVAL_LOCK_WAIT_S", 0.2)
    reader = sqlite3.connect(eval_db(env), isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM runs").fetchone()
        refused(archive(env, second.id, key=OTHER), "eval_busy")
    finally:
        reader.close()
    assert eval_db(env).read_bytes() == before


def test_b25_encrypted_converted_eval_db_returns_to_wal(tmp_path, monkeypatch, keys):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    env = make_env(tmp_path, compact=True)
    first = store_thread(env, source="t3:a")
    archived(env, first.id)
    encrypt(env, keys)
    second = store_thread(env, source="t3:b")
    archived(env, second.id, key=OTHER)
    with closing(sqlite_crypto.connect(str(eval_db(env)))) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal", "B25: encrypted DB"
    assert len(listed(env)["runs"]) == 2


# ── B32: archive in an already-encrypted workspace ──
def test_b32_archive_in_an_encrypted_workspace_is_ciphertext(tmp_path, monkeypatch, keys):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    env = make_env(tmp_path, compact=True)
    stored = store_thread(env)  # listed in the encrypted generation's database paths
    encrypt(env, keys)
    run_id = archived(env, stored.id)
    assert eval_db(env).read_bytes()[:16] != b"SQLite format 3\x00", "B32: plaintext eval DB"
    code, out, err = run(env, "show", run_id, "--workspace", env.ws)
    assert code == 0, err
    assert json.loads(out)["manifest"]["stored_id"] == stored.id
    with pytest.raises(sqlite3.DatabaseError):
        with closing(sqlite3.connect(eval_db(env))) as conn:
            conn.execute("SELECT count(*) FROM runs").fetchone()
