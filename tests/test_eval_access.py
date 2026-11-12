"""``commonplace eval``: eligibility, authorization, read-only access, purge and refusals."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from commonplace import capture, continuity as core, continuity_schema as schema, plans, privacy
from commonplace.backup import FENCE_NAME
from commonplace.cli import main
from commonplace.control_db import ControlDB
from commonplace.evals import source, store
from commonplace.handoff.models import ThreadMessage
from commonplace.import_grant import grant_id
from commonplace.import_sources import parse_import
from commonplace.workspace_store import POINTER, open_threads, thread_path, workspace_root
from tests.eval_support import (
    archive,
    archived,
    control,
    conversation,
    db_rows,
    eval_db,
    listed,
    make_env,
    refused,
    run,
    store_thread,
)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
UNSUPPORTED = "eval_source_gap: unsupported_in_slice1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    return make_env(tmp_path)


def native(sid="S", word="first"):
    record = dict(type="user", sessionId=sid, message=dict(role="user", content=word))
    return (json.dumps(record) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def cont(env):
    db = control(env)
    schema.ensure_schema(db)
    live = open_threads(env.directory)
    yield SimpleNamespace(db=db, store=live, user=env.owner, ws=env.ws)
    live.close()
    db.close()


def push(c, raw, *, name, before=b""):
    with c.db._lock, c.db._tx() as conn:
        return core.stream_push(
            conn,
            c.store,
            ws=c.ws,
            user_id=c.user,
            connection_id="",
            kind="capture",
            stream_id=f"capture:{c.user}:claude-code:{name}",
            source="claude-code",
            native_source="claude-code",
            data=raw,
            offset=len(before),
            prefix=sha(before),
            actor="synthetic",
            maximum=None,
            gate=lambda: None,
        )


def referenced(db):
    conn = db._conn
    ids = {r[0] for r in conn.execute("SELECT own_stored_id FROM sync_streams")}
    ids |= {r[0] for r in conn.execute("SELECT fork_stored_id FROM sync_streams") if r[0]}
    ids |= {r[0] for r in conn.execute("SELECT holder_stored_id FROM sync_groups")}
    return ids


def inject(env, stored_id):
    with closing(control(env)) as db:
        schema.ensure_schema(db)
        db._conn.execute(
            "INSERT INTO sync_groups VALUES (?,?,?,?,?,?)",
            ("g-" + stored_id, env.ws, env.owner, "claude-code", "x", stored_id),
        )


# ── B6: every refusal fixture of §3 ──
def test_b6_every_refusal_fixture_refuses_without_filer_lookup(env, cont, monkeypatch):
    c = cont
    records = [native(word=str(i)) for i in range(3)]
    push(c, b"".join(records), name="main")
    push(c, b"".join(records) + native(word="more"), name="longer")
    push(c, native(word="edited"), name="fork0")
    push(c, records[0] + native(word="edited"), name="fork1")
    rows = c.db._conn.execute(
        "SELECT fork_offset FROM sync_streams WHERE fork_stored_id IS NOT NULL"
    )
    assert {r[0] for r in rows} >= {0, len(records[0])}, "fixture: forks at 0 and above 0"
    grant = c.store.store(
        c.ws,
        grant_id(c.user, "claude-code", "imported"),
        parse_import("claude-code", b"".join(records).decode()),
        actor="synthetic",
        wing=capture.PRIVATE_WING,
    )
    legacy = capture.append_capture(
        c.store,
        c.ws,
        f"capture:{c.user}:claude-code:legacy",
        "claude-code",
        records[0].decode(),
        0,
        sha(b""),
        actor="synthetic",
        maximum=None,
        interval=0,
    )
    core.backfill(c.db, c.store, c.ws, c.user)
    assert c.db._conn.execute("SELECT 1 FROM sync_streams WHERE legacy=1").fetchone()
    holder = next(iter(referenced(c.db)))
    orphan = core._marker(c.store, c.ws, f"capture:{c.user}:claude-code:orphan", holder, "s")
    other = [
        c.store.store(c.ws, sid, conversation(), actor="s", wing=wing).id
        for sid, wing in (
            (grant_id(c.user, "claude-code", "snap") + "#sha256:" + "0" * 64, "private_chats"),
            ("capture:usr_someoneelse:claude-code:x", "private_chats"),
            ("t3:unknown-wing", "notes"),
        )
    ]
    fallback = capture.append_capture(
        c.store,
        c.ws,
        f"capture:{c.user}:claude-code:fallback",
        "claude-code",
        native(sid="T").decode(),
        0,
        sha(b""),
        actor="synthetic",
        maximum=None,
        interval=0,
    )
    targets = referenced(c.db) | {grant.id, legacy["stored_thread_id"], orphan.id}
    targets |= set(other) | {fallback["stored_thread_id"]}
    calls = []
    import commonplace.continuity_read as continuity_read

    monkeypatch.setattr(continuity_read, "filing_user", lambda *a: calls.append(a))
    for stored_id in sorted(targets):
        refused(archive(env, stored_id), UNSUPPORTED)
    assert calls == [], "B6: filing_user ran before the rule refused"
    assert db_rows(env) == []


# ── B7: probe-to-commit race ──
@pytest.mark.parametrize("plan", ["off", "on"])
def test_b7_race_between_probe_and_commit(env, cont, monkeypatch, plan):
    monkeypatch.setenv("COMMONPLACE_PLANS", plan)
    if plan == "on":
        plans.set_plan(cont.db, env.owner, "free", actor=env.owner)
    messages = conversation()
    eligible = store_thread(env, messages=messages)
    seen = []

    def pause(stage):
        if stage != "before_lock":
            return
        seen.append(stage)
        push(cont, native(), name="race")
        capture.append_capture(
            cont.store,
            env.ws,
            f"capture:{env.owner}:claude-code:race2",
            "claude-code",
            (native() + native(word="second")).decode(),
            0,
            sha(b""),
            actor="s",
            maximum=None,
            interval=0,
        )
        core.backfill(cont.db, cont.store, env.ws, env.owner)
        extra = ThreadMessage(len(messages), "user", "message", "appended", "{}")
        cont.store.append(eligible.id, messages + [extra])

    monkeypatch.setattr(store, "checkpoint", pause)
    run_id = archived(env, eligible.id)
    assert seen == ["before_lock"]
    assert eligible.id not in referenced(cont.db)
    assert len(referenced(cont.db)) >= 2
    rows = json.loads(_object(env, run_id, "rows"))
    assert len(rows) == len(messages), "B7: the appended row entered the archive"


def _object(env, run_id, obj):
    import base64

    code, out, err = run(env, "read", run_id, "--workspace", env.ws, "--object", obj)
    assert code == 0, err
    return base64.b64decode(json.loads(out)["data_base64"])


# ── B8 / B9 / B10: per-response checks ──
def test_b8_relationship_after_commit_hides_the_run(env):
    stored = store_thread(env)
    run_id = archived(env, stored.id)
    inject(env, stored.id)
    assert listed(env)["runs"] == []
    refused(run(env, "show", run_id, "--workspace", env.ws), "eval_not_found")
    refused(run(env, "read", run_id, "--workspace", env.ws, "--object", "rows"), "eval_not_found")


def test_b9_probe_lookup_error_refuses_and_absence_needs_proof(env):
    stored = store_thread(env)
    with closing(sqlite3.connect(env.data / "control.sqlite3")) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert not {"sync_streams", "sync_groups"} & names, "fixture: tables absent"
    run_id = archived(env, stored.id)
    with closing(sqlite3.connect(env.data / "control.sqlite3")) as conn:
        conn.execute("CREATE TABLE sync_streams (unrelated TEXT)")
        conn.execute("CREATE TABLE sync_groups (unrelated TEXT)")
        conn.commit()
    other = store_thread(env, source="t3:other")
    refused(archive(env, other.id, key="Other_eval-key-00002"), "eval_lookup_failed")
    refused(run(env, "list", "--workspace", env.ws), "eval_lookup_failed")
    refused(run(env, "show", run_id, "--workspace", env.ws), "eval_lookup_failed")
    refused(
        run(env, "read", run_id, "--workspace", env.ws, "--object", "rows"), "eval_lookup_failed"
    )


def test_minor1_transient_lookup_lock_refuses_list_instead_of_omitting(env, monkeypatch):
    run_id = archived(env, store_thread(env).id)

    def locked(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(source, "related", locked)
    code, out, err = run(env, "list", "--workspace", env.ws)
    assert (code, out) == (2, ""), "MINOR-1: a lookup error omitted the run and exited 0"
    assert err.startswith("commonplace: eval_busy")
    refused(run(env, "show", run_id, "--workspace", env.ws), "eval_busy")


# ── MINOR-2: every control table that records stored IDs is probed ──
@pytest.mark.parametrize("table", ["sync_backfill", "capture_ops"])
def test_minor2_backfill_cursor_and_capture_response_hide_the_run(env, table):
    stored = store_thread(env)
    run_id = archived(env, stored.id)
    with closing(control(env)) as db:
        schema.ensure_schema(db)
        if table == "sync_backfill":
            db._conn.execute(
                "INSERT INTO sync_backfill VALUES (?,?,?,?,?,?)",
                (env.ws, env.owner, 0, "capture:x", stored.id, 0),
            )
        else:
            response = json.dumps({"status": "appended", "stored_thread_id": stored.id})
            db._conn.execute(
                "INSERT INTO capture_ops VALUES (?,?,?,?,?,?)",
                (env.ws, env.owner, "op-1", "0" * 64, response, "2026-11-10T00:00:00+00:00"),
            )
    assert listed(env)["runs"] == [], f"MINOR-2: a {table} reference left the run listed"
    refused(run(env, "show", run_id, "--workspace", env.ws), "eval_not_found")
    other = store_thread(env, source="t3:other")
    archived(env, other.id, key="Other_eval-key-00002")


def _demote(env):
    with closing(sqlite3.connect(env.data / "control.sqlite3")) as conn:
        conn.execute("UPDATE memberships SET role='member' WHERE user_id=?", (env.owner,))
        conn.commit()


def test_b10_demoted_owner_and_missing_source_deny_every_response(env):
    stored = store_thread(env)
    run_id = archived(env, stored.id)
    for argv in (["list"], ["show", run_id], ["read", run_id, "--object", "rows"]):
        assert run(env, *argv, "--workspace", env.ws)[0] == 0
    path = thread_path(env.directory)
    path.rename(path.with_name("moved.sqlite3"))
    assert listed(env)["runs"] == []
    refused(run(env, "show", run_id, "--workspace", env.ws), "eval_not_found")
    path.with_name("moved.sqlite3").rename(path)
    _demote(env)
    for argv in (["list"], ["show", run_id], ["read", run_id, "--object", "rows"]):
        refused(run(env, *argv, "--workspace", env.ws), "eval_denied")
    refused(run(env, "list", "--workspace", env.ws, by=env.member), "eval_denied")


# ── B11 / B12 / B13: principal and read-only openers ──
def test_b11_principal_fields_and_exact_scopes(env, monkeypatch):
    from commonplace.handoff import recall

    seen = []
    original = recall.readable
    monkeypatch.setattr(recall, "readable", lambda p, s: seen.append(p) or original(p, s))
    archived(env, store_thread(env).id)
    who = seen[0]
    assert (who.auth, who.connection, who.client, who.connection_id) == (
        "local",
        "cli",
        "commonplace",
        "",
    )
    assert (who.user_id, who.workspace_id, who.role, who.person) == (
        env.owner,
        env.ws,
        "owner",
        False,
    )
    assert set(who.policy.scopes) == {"threads:read", "memory:read"}, "B11: wrong scope set"


def test_b12_control_db_is_never_constructed_and_writes_are_denied(env, monkeypatch):
    stored = store_thread(env)
    constructed = []
    original = ControlDB.__init__
    monkeypatch.setattr(
        ControlDB,
        "__init__",
        lambda self, *a, **k: constructed.append(a) or original(self, *a, **k),
    )
    run_id = archived(env, stored.id)
    for argv in (["list"], ["show", run_id], ["read", run_id, "--object", "rows"], ["preflight"]):
        assert run(env, *argv, "--workspace", env.ws)[0] == 0
    assert run(env, "purge", run_id, "--workspace", env.ws, "--confirm", run_id)[0] == 0
    assert constructed == [], "B12: an eval command constructed ControlDB"
    with source.control_snapshot(env.data) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute("INSERT INTO users(id, email, created_at) VALUES ('u', 'e', 'now')")


def _tree(root):
    return sorted(
        (str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
        for p in Path(root).rglob("*")
    )


def test_b13_preflight_opens_no_sqlite_file_and_creates_nothing(env, monkeypatch):
    from mempalace import sqlite_crypto

    assert run(env, "list", "--workspace", env.ws)[0] == 0
    before = _tree(env.data)
    opened = []
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: opened.append(a))
    monkeypatch.setattr(sqlite_crypto, "connect", lambda *a, **k: opened.append(a))
    code, out, err = run(env, "preflight", "--workspace", env.ws)
    assert code == 0, err
    assert opened == [], "B13: preflight opened a SQLite file"
    assert _tree(env.data) == before
    report = json.loads(out)
    assert {"classification", "permissions", "paths"} <= set(report)
    assert report["classification"] == "plain" and report["permissions"] == "ok"
    assert not (env.directory / "evals").exists()


# ── B21 / B22: purge ──
def test_b21_purge_authority_acknowledgment_and_triggers(env):
    stored = store_thread(env)
    run_id = archived(env, stored.id)
    refused(
        run(env, "purge", run_id, "--workspace", env.ws, "--confirm", run_id, by=env.member),
        "eval_denied",
    )
    refused(
        run(env, "purge", run_id, "--workspace", env.ws, "--confirm", "run_" + "0" * 24),
        "eval_confirm_mismatch",
    )
    with closing(sqlite3.connect(eval_db(env))) as conn:
        for statement in (
            "DELETE FROM runs",
            "UPDATE runs SET created_at='x'",
            "DELETE FROM chunks",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(statement)
    inject(env, stored.id)
    refused(run(env, "show", run_id, "--workspace", env.ws), "eval_not_found")
    outputs = []
    for target in (run_id, run_id, "run_" + "f" * 24):
        code, out, err = run(env, "purge", target, "--workspace", env.ws, "--confirm", target)
        assert code == 0, err
        outputs.append(out)
    assert len(set(outputs)) == 1, "B21: the acknowledgment reveals whether the run existed"
    assert db_rows(env) == [] and db_rows(env, "chunks") == []
    assert [row[0] for row in db_rows(env, "tombstones")] == [run_id]


def test_b21_lost_owner_cannot_purge(env):
    run_id = archived(env, store_thread(env).id)
    _demote(env)
    refused(run(env, "purge", run_id, "--workspace", env.ws, "--confirm", run_id), "eval_denied")
    assert len(db_rows(env)) == 1


def test_b21_capability_resets_on_rollback(env, monkeypatch):
    run_id = archived(env, store_thread(env).id)
    conn, capability = store.open_write(env.data, env.directory, create=False)
    try:
        original = conn.execute

        class Failing:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *args):
                if sql.startswith("INSERT INTO tombstones"):
                    raise sqlite3.OperationalError("database or disk is full")
                return original(sql, *args)

        with pytest.raises(sqlite3.OperationalError):
            store.purge(Failing(), capability, run_id, env.ws)
        assert capability.armed is False
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM runs")
    finally:
        conn.close()
    assert len(db_rows(env)) == 1


def test_b22_generation_disclosure_precedes_confirmation(env):
    run_id = archived(env, store_thread(env).id)
    root = workspace_root(env.data, env.ws)
    retired = root.name + ".generation-" + "a" * 24
    (root / POINTER).write_text(
        json.dumps({"active": root.name, "retained": [{"name": retired, "retired_at": 1.7e9}]})
    )
    code, out, err = run(env, "purge", run_id, "--workspace", env.ws)
    assert code == 2 and "eval_confirm_required" in err
    assert retired in out and "prune-eligible after 14 days" in out, "B22: no disclosure"
    assert len(db_rows(env)) == 1
    code, out, _ = run(env, "purge", run_id, "--workspace", env.ws, "--confirm", run_id)
    assert code == 0 and out.index(retired) < out.index("acknowledged")


# ── B23: modes, owners, symlinks ──
@pytest.mark.parametrize("damage", ["evals_mode", "db_mode", "ancestor", "symlink", "uid"])
def test_b23_unsafe_paths_refuse_before_payload_without_chmod(env, monkeypatch, damage):
    run_id = archived(env, store_thread(env).id)
    evals = env.directory / "evals"
    db = evals / "runs.sqlite3"
    if damage == "evals_mode":
        evals.chmod(0o750)
        expected = f"chmod 0700 {evals}"
    elif damage == "db_mode":
        db.chmod(0o640)
        expected = f"chmod 0600 {db}"
    elif damage == "ancestor":
        env.directory.chmod(0o770)
        expected = f"chmod go-w {env.directory}"
    elif damage == "symlink":
        moved = env.directory / "elsewhere"
        evals.rename(moved)
        evals.symlink_to(moved)
        expected = "the admin must fix it"
    else:
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
        expected = "the admin must fix it"
    modes = {p: p.lstat().st_mode for p in (env.directory, evals, db) if os.path.lexists(p)}
    chmods = []
    monkeypatch.setattr(os, "chmod", lambda *a, **k: chmods.append(a))
    for argv in (["list"], ["show", run_id], ["purge", run_id, "--confirm", run_id]):
        code, out, err = run(env, *argv, "--workspace", env.ws)
        assert code == 2 and "eval_permissions" in err and expected in err, err
        assert run_id not in out
    assert chmods == [], "B23: eval changed a mode"
    assert modes == {p: p.lstat().st_mode for p in modes}


def test_minor3_permission_gate_runs_before_any_payload(env, monkeypatch):
    stored = store_thread(env)
    archived(env, stored.id)
    (env.directory / "evals").chmod(0o750)
    read = []

    class Stdin:
        def read(self, size=-1):
            read.append(size)
            return b"[]"

    snapshots = []
    original = source.snapshot_rows
    monkeypatch.setattr(source, "snapshot_rows", lambda *a: snapshots.append(a) or original(*a))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=Stdin()))
    argv = [
        "archive",
        "--workspace",
        env.ws,
        "--thread",
        stored.id,
        "--key",
        "Other_eval-key-00002",
    ]
    code = main(["--data", str(env.data), "eval", *argv, "--artifacts-stdin"])
    assert code == 2
    assert read == [] and snapshots == [], "MINOR-3: payload read before the permission gate"


# ── MINOR-6: preflight on a fresh data dir ──
@pytest.mark.parametrize("fresh", ["empty", "missing"])
def test_minor6_preflight_creates_nothing_on_a_fresh_data_dir(tmp_path, fresh):
    data = tmp_path / "fresh"
    if fresh == "empty":
        data.mkdir(mode=0o700)
    code = main(["--data", str(data), "eval", "preflight", "--workspace", "ws_abc123"])
    assert code == 0
    created = sorted(p.name for p in data.rglob("*")) if data.exists() else None
    assert created == ([] if fresh == "empty" else None), f"MINOR-6: preflight created {created}"


# ── B24: backup fence ──
def test_b24_exclusive_backup_fence_makes_eval_wait_then_refuse(env, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_FENCE_WAIT_S", "0.2")
    fd = os.open(env.data / FENCE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, out, err = run(env, "list", "--workspace", env.ws)
    finally:
        os.close(fd)
    assert code == 2 and "a backup is running" in err, "B24: eval ignored the backup fence"
    assert out == ""
    assert listed(env)["limits"].get("quota_bytes") == 1024**3, "eval runs once the fence is free"


# ── B28 / B29: no network, process or model; Linux only ──
def test_b28_no_socket_subprocess_or_model_call(env, monkeypatch):
    import http.client
    import socket
    import subprocess
    import urllib.request

    def trap(*_a, **_k):
        raise AssertionError("B28: eval reached a network, process or model call")

    stored = store_thread(env)
    for target, name in (
        (socket, "create_connection"),
        (socket.socket, "connect"),
        (subprocess, "Popen"),
        (os, "system"),
        (urllib.request, "urlopen"),
        (http.client.HTTPConnection, "request"),
    ):
        monkeypatch.setattr(target, name, trap)
    run_id = archived(env, stored.id, artifacts=[{"name": "a", "text": "b"}])
    for argv in (
        ["list"],
        ["show", run_id],
        ["read", run_id, "--object", "manifest"],
        ["preflight"],
        ["purge", run_id, "--confirm", run_id],
    ):
        code, _, err = run(env, *argv, "--workspace", env.ws)
        assert code == 0, err
    assert db_rows(env) == [] and len(db_rows(env, "tombstones")) == 1


def test_b29_non_linux_refuses(env, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    refused(run(env, "list", "--workspace", env.ws), "eval_platform")
    assert not (env.directory / "evals").exists()


# ── B30: refusals are codes, never tracebacks or keys ──
def test_b30_every_refusal_prints_a_code_without_traceback(env):
    stored = store_thread(env)
    run_id = archived(env, stored.id)
    bad_key = "short!secret"
    cases = [
        (archive(env, stored.id, key=bad_key), "eval_key_invalid"),
        (archive(env, "not-a-thread"), "eval_not_found"),
        (archive(env, "thr_" + "0" * 16), "eval_not_found"),
        (run(env, "show", "run_" + "0" * 24, "--workspace", env.ws), "eval_not_found"),
        (run(env, "show", "bogus", "--workspace", env.ws), "eval_not_found"),
        (run(env, "read", run_id, "--workspace", env.ws, "--object", "x"), "eval_object_invalid"),
        (
            run(env, "purge", "bogus", "--workspace", env.ws, "--confirm", "bogus"),
            "eval_run_invalid",
        ),
        (run(env, "list", "--workspace", "not_a_workspace"), "eval_workspace"),
        (archive(env, stored.id, artifacts={"not": "a list"}), "eval_artifacts_invalid"),
    ]
    for result, code in cases:
        refused(result, code)
    assert bad_key not in "".join(cases[0][0][1:])
    code, out, err = run(env, "list", "--workspace", env.ws, "--unknown-flag")
    assert code == 2 and "unrecognized arguments" in err


# ── B31: scanner failures fail closed ──
def _break(monkeypatch, how, tmp_path):
    if how == "import":
        monkeypatch.setitem(sys.modules, "commonplace.privacy", None)
    elif how == "scan":
        monkeypatch.setattr(privacy, "scan", lambda text: (_ for _ in ()).throw(RuntimeError("x")))
    elif how == "file_none":
        monkeypatch.setattr(privacy, "__file__", None)
    else:
        monkeypatch.setattr(privacy, "__file__", str(tmp_path / "secret-path" / "privacy.py"))


@pytest.mark.parametrize("how", ["import", "scan", "file_none", "file_oserror"])
def test_b31_scanner_failure_fails_closed(tmp_path, monkeypatch, how):
    monkeypatch.setenv("COMMONPLACE_PLANS", "off")
    first = make_env(tmp_path / "first")
    stored = store_thread(first)
    with monkeypatch.context() as broken:
        _break(broken, how, tmp_path)
        err = refused(archive(first, stored.id), "eval_scan_failed")
        assert "secret-path" not in err
        assert not eval_db(first).exists(), "B31: a failed first archive left an eval DB"
    env = make_env(tmp_path / "later")
    stored = store_thread(env)
    run_id = archived(env, stored.id)
    before = eval_db(env).read_bytes()
    with monkeypatch.context() as broken:
        _break(broken, how, tmp_path)
        refused(archive(env, stored.id, key="Other_eval-key-00002"), "eval_scan_failed")
        assert eval_db(env).read_bytes() == before
        for argv in (["list"], ["show", run_id], ["read", run_id, "--object", "rows"]):
            refused(run(env, *argv, "--workspace", env.ws), "eval_scan_failed")
        assert run(env, "preflight", "--workspace", env.ws)[0] == 0
        assert run(env, "purge", run_id, "--workspace", env.ws, "--confirm", run_id)[0] == 0
        assert main(["--data", str(env.data), "audit", "--workspace", env.ws]) == 0
    assert db_rows(env) == []
