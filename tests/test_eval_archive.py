"""``commonplace eval``: archive content, caps, keys, quota, read and flags (class B and C)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from commonplace import evals, privacy
from commonplace.evals import flags, source, store
from commonplace.handoff.models import ThreadMessage
from commonplace.import_parts import append_part
from commonplace.import_sources import parse_import
from commonplace.workspace_store import open_threads, thread_path
from tests.eval_support import (
    KEY,
    archive,
    archived,
    conversation,
    db_rows,
    eval_db,
    listed,
    make_env,
    read_all,
    refused,
    run,
    store_thread,
)

FIELDS = ("seq", "role", "kind", "name", "call_id", "at", "text", "raw")
SECRET = 'password = "Hunter2_Zq9xLm4Pw7"'


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path)


def rows_of(env, run_id):
    return json.loads(read_all(env, run_id, "rows")[0].decode("utf-8"))


def as_rows(messages):
    return [[getattr(m, f) for f in FIELDS] for m in messages]


def hide(env, stored_id):
    """Inject a continuity row that references ``stored_id`` (source becomes hidden)."""
    from commonplace import continuity_schema
    from tests.eval_support import control

    with closing(control(env)) as db:
        continuity_schema.ensure_schema(db)
        db._conn.execute(
            "INSERT INTO sync_groups VALUES (?,?,?,?,?,?)",
            ("g-" + stored_id, env.ws, env.owner, "claude-code", "n", stored_id),
        )


# ── B1 / C1: exact round trip on v1 and compact ──
@pytest.mark.parametrize("compact", [False, True], ids=["v1", "compact"])
def test_b1_c1_handoff_thread_round_trips_all_eight_fields(tmp_path, compact):
    env = make_env(tmp_path, compact=compact)
    messages = conversation()
    stored = store_thread(env, messages=messages)
    run_id = archived(env, stored.id)
    assert rows_of(env, run_id) == as_rows(messages), "B1: a field changed in the archive"
    assert len({m.at for m in messages}) == len(messages)
    assert any(m.call_id for m in messages)


def test_c2_acl_single_part_import_archives(env):
    raw = json.dumps(
        {"type": "user", "sessionId": "s", "message": {"role": "user", "content": "x"}}
    )
    messages = parse_import("claude-code", raw + "\n")
    stored = store_thread(env, source="import:claude-code:acl-single", messages=messages)
    assert stored.wing == "threads"
    assert rows_of(env, archived(env, stored.id)) == as_rows(messages)


def test_c3_acl_multipart_import_archives(env):
    whole = (
        json.dumps({"type": "user", "sessionId": "s", "message": {"role": "user", "content": "x"}})
        + "\n"
    ).encode()
    part = dict(
        version=1,
        session_bytes=len(whole),
        session_sha256=hashlib.sha256(whole).hexdigest(),
        index=0,
        offset=0,
        prefix_sha256=hashlib.sha256(b"").hexdigest(),
        sha256=hashlib.sha256(whole).hexdigest(),
        final=True,
    )
    with closing(open_threads(env.directory)) as live:
        result = append_part(
            live,
            env.ws,
            "claude-code",
            "acl-multi",
            "claude-code",
            whole.decode(),
            part,
            actor="synthetic",
            maximum=None,
        )
        stored = live.get(result["stored_thread_id"])
        messages = live.messages(stored.id)
    assert stored.source_thread_id == "import:claude-code:acl-multi" and stored.wing == "threads"
    assert rows_of(env, archived(env, stored.id)) == as_rows(messages)


# ── B2: completeness ──
@pytest.mark.parametrize("damage", ["gap", "digest"])
def test_b2_seq_gap_or_digest_mismatch_refuses(env, damage):
    stored = store_thread(env)
    with closing(sqlite3.connect(thread_path(env.directory))) as conn:
        if damage == "gap":
            conn.execute("DELETE FROM messages WHERE thread_id=? AND seq=1", (stored.id,))
        else:
            conn.execute(
                "UPDATE messages SET text='edited' WHERE thread_id=? AND seq=1", (stored.id,)
            )
        conn.commit()
    refused(archive(env, stored.id), "eval_source_gap: digest")
    assert db_rows(env) == []


# ── B3: the pre-commit fidelity proof ──
def test_b3_fidelity_proof_catches_a_corrupted_at(env, monkeypatch):
    stored = store_thread(env)
    original = source.row_bytes

    def faulty(message):
        from dataclasses import replace

        return original(replace(message, at=message.at + "!") if message.seq == 1 else message)

    monkeypatch.setattr(source, "row_bytes", faulty)
    refused(archive(env, stored.id), "eval_fidelity")
    assert db_rows(env) == [] and db_rows(env, "chunks") == []


def test_minor4_commit_rehashes_stored_rows_bytes(env, monkeypatch):
    stored = store_thread(env)
    original = store._insert_chunk

    def padded(conn, run_id, name, idx, data):
        # Same JSON values, different bytes: only a byte digest can tell.
        return original(conn, run_id, name, idx, b" " + data if name == "rows" else data)

    monkeypatch.setattr(store, "_insert_chunk", padded)
    refused(archive(env, stored.id), "eval_fidelity")
    assert db_rows(env) == [], "MINOR-4: rows bytes differing from rows_sha256 were committed"


# ── B4: one snapshot ──
def test_b4_append_after_snapshot_is_excluded(env, monkeypatch):
    messages = conversation(n=400)
    stored = store_thread(env, messages=messages)
    from commonplace.handoff.thread_store import ThreadStore

    original = ThreadStore.messages
    calls = []

    def appending(self, stored_id, start=0, end=None):
        page = original(self, stored_id, start, end)
        calls.append(start)
        if len(calls) == 2:  # after the seq-0 probe and the first page
            with closing(open_threads(env.directory)) as live:
                extra = ThreadMessage(len(messages), "user", "message", "late", "{}")
                live.append(stored_id, messages + [extra])
        return page

    monkeypatch.setattr(ThreadStore, "messages", appending)
    run_id = archived(env, stored.id)
    monkeypatch.setattr(ThreadStore, "messages", original)
    assert rows_of(env, run_id) == as_rows(messages), "B4: an appended row entered the archive"
    with closing(open_threads(env.directory)) as live:
        assert live.get(stored.id).message_count == len(messages) + 1


# ── B5: caps ──
class Counting:
    def __init__(self, data):
        self._data, self.read_bytes = data, 0

    def read(self, size=-1):
        chunk = (
            self._data[self.read_bytes :]
            if size < 0
            else self._data[self.read_bytes : self.read_bytes + size]
        )
        self.read_bytes += len(chunk)
        return chunk


def test_b5_stdin_stops_at_cap_plus_one(env, monkeypatch):
    stored = store_thread(env)
    payload = json.dumps([{"name": "big", "text": "x" * (evals.EVAL_RUN_MAX_BYTES + 4096)}])
    stream = Counting(payload.encode())
    import sys
    from types import SimpleNamespace

    from commonplace.cli import main

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stream))
    argv = ["archive", "--workspace", env.ws, "--thread", stored.id, "--key", KEY]
    code = main(["--data", str(env.data), "eval", *argv, "--artifacts-stdin"])
    assert code == 2
    assert stream.read_bytes <= evals.EVAL_RUN_MAX_BYTES + 1, "B5: stdin read past the cap"
    assert not eval_db(env).exists()


def test_b5_run_cap_counts_rows_artifacts_and_manifest(env):
    stored = store_thread(env)
    rows_bytes = len(source.row_bytes(conversation()[0])) * 3
    text = "y" * (evals.EVAL_RUN_MAX_BYTES - rows_bytes - 200)
    refused(archive(env, stored.id, artifacts=[{"name": "a", "text": text}]), "eval_too_large")
    assert db_rows(env) == []


def test_b5_chunks_and_pages_are_256_kib(env):
    stored = store_thread(env)
    text = "z" * (600 * 1024)
    run_id = archived(env, stored.id, artifacts=[{"name": "big", "text": text}])
    sizes = [
        len(row[3]) for row in db_rows(env, "chunks") if row[0] == run_id and row[1] == "artifact/0"
    ]
    assert sizes == [262144, 262144, 600 * 1024 - 524288]
    code, out, _ = run(env, "read", run_id, "--workspace", env.ws, "--object", "artifact/0")
    page = json.loads(out)
    assert len(base64.b64decode(page["data_base64"])) == 262144 and page["next"] == 262144
    data, last = read_all(env, run_id, "artifact/0")
    assert data == text.encode() and last["next"] is None


def test_b5_lone_surrogate_refuses(tmp_path):
    env = make_env(tmp_path, compact=True)
    try:
        stored = store_thread(env, messages=[ThreadMessage(0, "user", "message", "a\ud800", "{}")])
    except (UnicodeEncodeError, ValueError):
        pytest.skip("this store cannot hold a lone surrogate")
    refused(archive(env, stored.id), "eval_source_gap: unencodable")


# ── B14: flags never gate; the rescan writes nothing ──
def _db_bytes(env):
    path = eval_db(env)
    return b"".join(
        Path(str(path) + s).read_bytes() for s in ("", "-wal") if Path(str(path) + s).exists()
    )


def test_b14_bytes_kept_despite_flags_and_rescan_writes_nothing(env, monkeypatch):
    text = SECRET + "\nmy doctor changed my insulin prescription for diabetes"
    stored = store_thread(env, messages=conversation(text=text))
    run_id = archived(env, stored.id)
    assert rows_of(env, run_id)[0][6] == text
    codes = listed(env)["runs"][0]["flags"]
    assert {"secret:generic_assignment", "health"} <= set(codes)
    before = _db_bytes(env)
    current = flags.load()

    def changed():
        def scan(value):
            return current._scan(value) + (["test:rescan"] if "insulin" in value else [])

        return flags.Scanner(scan, "0" * 16)

    monkeypatch.setattr(flags, "load", changed)
    assert "test:rescan" in listed(env)["runs"][0]["flags"]
    assert _db_bytes(env) == before, "B14: the rescan changed the eval DB"


# ── B15: keys ──
def test_b15_key_reuse_conflict_and_secrecy(env, caplog):
    caplog.set_level(logging.DEBUG)
    stored = store_thread(env)
    first = archive(env, stored.id, artifacts=[{"name": "out", "text": "same"}])
    again = archive(env, stored.id, artifacts=[{"name": "out", "text": "same"}])
    a, b = json.loads(first[1]), json.loads(again[1])
    assert a["run_id"] == b["run_id"] and a["created_at"] == b["created_at"] and b["reused"]
    renamed = archive(env, stored.id, artifacts=[{"name": "renamed", "text": "same"}])
    refused(renamed, "eval_conflict")
    with closing(open_threads(env.directory)) as live:
        live.append(stored.id, conversation() + [ThreadMessage(3, "user", "message", "n", "{}")])
    refused(archive(env, stored.id, artifacts=[{"name": "out", "text": "same"}]), "eval_conflict")
    outputs = "".join(first[1:] + again[1:] + renamed[1:]) + caplog.text
    assert KEY not in outputs
    assert KEY.encode() not in _db_bytes(env), "B15: the plaintext key reached the eval DB"
    assert len(db_rows(env)) == 1


def test_b15_key_digest_is_length_prefixed():
    x = "K" * 20
    assert evals.key_digest("ab", x) != evals.key_digest("a", "b" + x), "B15: digest collision"
    assert re.fullmatch(r"[0-9a-f]{64}", evals.key_digest("ws_a", x))


# ── B16: retention ──
@pytest.fixture
def clock(monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(store, "_now", lambda: now[0])
    return now


def test_b16_retired_key_until_retention_then_reusable(env, clock, monkeypatch):
    stored = store_thread(env)
    run_id = archived(env, stored.id)
    assert run(env, "purge", run_id, "--workspace", env.ws, "--confirm", run_id)[0] == 0
    purged = clock[0]
    retention = store.EVAL_KEY_TOMBSTONE_RETENTION
    assert retention == 30 * 24 * 3600
    clock[0] = purged - 5
    refused(archive(env, stored.id), "eval_key_retired")
    clock[0] = purged + retention - 1
    refused(archive(env, stored.id), "eval_key_retired")
    monkeypatch.setattr(store, "EVAL_KEY_TOMBSTONE_RETENTION", 60)
    clock[0] = purged + 30
    refused(archive(env, stored.id), "eval_key_retired")
    clock[0] = purged + 61
    reused = archived(env, stored.id)
    assert reused != run_id
    assert [row[1] for row in db_rows(env, "tombstones")] == [None]


# ── B17 / B18: quota and limits ──
def test_b17_quota_refuses_whole_run_and_reveals_no_totals(env, monkeypatch):
    first = store_thread(env, source="t3:first")
    second = store_thread(env, source="t3:second")
    run_id = archived(env, first.id)
    used = db_rows(env)[0]
    total = dict(zip([d[1] for d in _columns(env)], used))["total_bytes"]
    monkeypatch.setattr(store, "EVAL_WORKSPACE_QUOTA", total + 10)
    hide(env, first.id)
    err = refused(archive(env, second.id, key="Other_eval-key-00002"), "eval_quota")
    assert not re.search(r"\d", err), "B17: the refusal carries totals"
    assert len(db_rows(env)) == 1 and {r[0] for r in db_rows(env, "chunks")} == {run_id}
    assert run(env, "purge", run_id, "--workspace", env.ws, "--confirm", run_id)[0] == 0
    archived(env, second.id, key="Other_eval-key-00002")


def _columns(env):
    with closing(sqlite3.connect(eval_db(env))) as conn:
        return conn.execute("PRAGMA table_info(runs)").fetchall()


def test_b18_limits_sum_only_listed_runs(env):
    shown = store_thread(env, source="t3:shown")
    hidden = store_thread(env, source="t3:hidden", messages=conversation(n=20))
    archived(env, shown.id)
    archived(env, hidden.id, key="Other_eval-key-00002")
    hide(env, hidden.id)
    result = listed(env)
    assert [r["stored_id"] for r in result["runs"]] == [shown.id]
    assert result["limits"]["used_bytes"] == result["runs"][0]["bytes"]["total"], "B18"
    assert result["limits"] == {
        "quota_bytes": 1024**3,
        "used_bytes": result["runs"][0]["bytes"]["total"],
        "run_max_bytes": 16 * 1024**2,
        "chunk_bytes": 256 * 1024,
    }


# ── B26: read ──
def test_b26_read_returns_exact_bytes_for_every_object(env):
    stored = store_thread(env)
    art = "artifact ☃ " * 40000
    run_id = archived(env, stored.id, artifacts=[{"name": "log", "text": art}])
    manifest = json.loads(read_all(env, run_id, "manifest")[0])
    for obj, expected in (
        ("rows", None),
        ("manifest", None),
        ("artifact/0", art.encode("utf-8")),
    ):
        data, last = read_all(env, run_id, obj)
        assert last["total"] == len(data) and last["sha256"] == hashlib.sha256(data).hexdigest()
        if expected is not None:
            assert data == expected
    assert manifest["artifacts"][0]["sha256"] == hashlib.sha256(art.encode()).hexdigest()
    refused(
        run(env, "read", run_id, "--workspace", env.ws, "--object", "artifact/1"), "eval_object"
    )
    refused(
        run(env, "read", run_id, "--workspace", env.ws, "--object", "rows", "--offset", "-1"),
        "eval_offset",
    )


# ── B27: artifact names ──
@pytest.mark.parametrize(
    "artifacts",
    [
        [{"name": "a", "text": "1"}, {"name": "a", "text": "2"}],
        [{"name": "bad\x07name", "text": "1"}],
        [{"name": "../escape", "text": "1"}],
        [{"name": "dir/file", "text": "1"}],
    ],
    ids=["duplicate", "control", "dotdot", "slash"],
)
def test_b27_bad_artifact_names_refuse(env, artifacts):
    stored = store_thread(env)
    refused(archive(env, stored.id, artifacts=artifacts), "eval_artifact_name")
    assert not eval_db(env).exists()


# ── B33: the per-string scanner contract ──
def test_b33_flags_come_from_per_string_scan(env):
    stored = store_thread(env, messages=conversation(text=SECRET))
    run_id = archived(env, stored.id)
    rows = read_all(env, run_id, "rows")[0].decode()
    assert privacy.scan(SECRET) == ["secret:generic_assignment"]
    assert "secret:generic_assignment" not in privacy.scan(rows), "pinned scanner premise"
    code, out, _ = run(env, "show", run_id, "--workspace", env.ws)
    shown = json.loads(out)
    assert "secret:generic_assignment" in shown["manifest"]["scanner_codes"]
    expected = hashlib.sha256(Path(privacy.__file__).read_bytes()).hexdigest()[:16]
    assert shown["manifest"]["scanner_version"] == expected


@pytest.mark.parametrize("where", ["tool_call_text", "raw"])
def test_b33_escaped_assignments_are_flagged_through_decoded_leaves(env, where):
    escaped = json.dumps({"cmd": SECRET})
    messages = conversation()
    if where == "tool_call_text":
        messages[1] = ThreadMessage(1, "assistant", "tool_call", escaped, "{}", name="bash")
    else:
        messages[0] = ThreadMessage(0, "user", "message", "plain", escaped)
    for m in messages:
        assert "secret:generic_assignment" not in privacy.scan(m.text + m.raw)
    stored = store_thread(env, messages=messages)
    run_id = archived(env, stored.id)
    assert "secret:generic_assignment" in listed(env)["runs"][0]["flags"], run_id


def test_b33_manifest_is_scanned_without_scanner_fields(env, monkeypatch):
    seen = []
    original = privacy.scan
    monkeypatch.setattr(privacy, "scan", lambda text: seen.append(text) or original(text))
    stored = store_thread(env)
    archived(env, stored.id)
    version = hashlib.sha256(Path(privacy.__file__).read_bytes()).hexdigest()[:16]
    assert stored.source_thread_id in seen and env.ws in seen
    assert not {"scanner_codes", "scanner_version", version} & set(seen), "B33: circular scan"


def test_b33_changed_scanner_file_reports_rescan_union(env, monkeypatch, tmp_path):
    stored = store_thread(env, messages=conversation(text="canary words"))
    run_id = archived(env, stored.id)
    copy = tmp_path / "privacy.py"
    copy.write_bytes(Path(privacy.__file__).read_bytes() + b"\n# changed\n")
    original = privacy.scan
    monkeypatch.setattr(privacy, "__file__", str(copy))
    monkeypatch.setattr(
        privacy, "scan", lambda text: original(text) + (["test:new"] if "canary" in text else [])
    )
    for argv in (["show", run_id], ["read", run_id, "--object", "rows"]):
        code, out, err = run(env, *argv, "--workspace", env.ws)
        assert code == 0, err
        assert "test:new" in json.loads(out)["flags"], f"B33: {argv[0]} misses the rescan union"
