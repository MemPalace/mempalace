"""Pure client durability, privacy and native-source selection checks."""

import hashlib
import json
import os
from dataclasses import FrozenInstanceError, replace

import pytest

from commonplace.onboarding import POINTER
from commonplace.sync_protocol import CURSOR_MESSAGE
from commonplace.sync_sources import (
    SliceConflict,
    SourceError,
    body,
    discover,
    probe,
    scan,
    snapshot_targets,
)
from commonplace.sync_state import StateError, feed_key, open_state

NOW = 2_000_000_000


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def record(text="synthetic verbatim café 🦉", **extra):
    return (
        json.dumps(
            {"type": "user", "sessionId": "session", "message": {"content": text}, **extra},
            ensure_ascii=False,
        )
        + "\r\n"
    ).encode()


@pytest.fixture
def client(tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "native"
    root.mkdir()
    config = {"home": str(home), "sources": [{"source": "claude-code", "root": str(root)}]}
    with open_state(home / ".commonplace/sync/state.db") as state:
        yield config, state, root


def initialize(config, state, *, offset=0, prefix=None):
    for source in discover(config):
        state.store_cursor(source.source, source.path, source.native_id, offset, prefix or sha(b""))


def ack(state, op, raw):
    state.outbox_ack(op.op_id, {"offset": op.offset + op.length, "prefix_sha256": sha(raw)})


def test_unknown_cursor_fetch_before_grown_session(client):
    config, state, root = client
    first, suffix = record(), record("new")
    path = root / "session.jsonl"
    path.write_bytes(first + suffix)
    source = discover(config)[0]
    assert state.cursor(source.source, source.path, source.native_id) is None
    assert scan(config, state, now=NOW) == []
    # Model GET /api/capture on a previously imported/grown server session.
    state.store_cursor(source.source, source.path, source.native_id, len(first), sha(first))
    (op,) = scan(config, state, now=NOW)
    assert body(op)["transcript"].encode() == suffix
    assert body(op)["prefix_sha256"] == sha(first)
    ack(state, op, first + suffix)
    assert state.cursor(source.source, source.path, source.native_id)["offset"] == len(
        first + suffix
    )
    assert scan(config, state, now=NOW) == []


def test_outbox_immutable_durable_and_content_free(client, monkeypatch):
    config, state, root = client
    raw = record("synthetic_sensitive_value")
    path = root / "session.jsonl"
    path.write_bytes(raw)
    initialize(config, state)
    monkeypatch.setenv("COMMONPLACE_CAPTURE_TOKEN_CLAUDE_CODE", "synthetic_secret_token")
    (op,) = scan(config, state, now=NOW)
    assert state.outbox_pending() == [op]
    with pytest.raises(FrozenInstanceError):
        op.length = 1
    with pytest.raises(StateError, match="immutable"):
        state.outbox_add(replace(op, length=op.length + 1))
    with pytest.raises(StateError, match="immutable"):
        state.store_cursor(op.source, op.path, op.native_id, 0, sha(b""))
    with pytest.raises(Exception):
        state.outbox_add(replace(op, op_id="a" * 32))
    state.outbox_attempt(op.op_id)
    assert state.outbox_pending()[0].attempts == 1
    # Another process sees the durable operation before any network send.
    with open_state(config["home"] + "/.commonplace/sync/state.db") as other:
        assert other.outbox_pending()[0].op_id == op.op_id
        assert body(other.outbox_pending()[0])["transcript"].encode() == raw
    for path in state_path(config).parent.glob("state.db*"):
        data = path.read_bytes()
        assert b"synthetic_sensitive_value" not in data
        assert b"synthetic_secret_token" not in data
        assert path.stat().st_mode & 0o777 == 0o600


def state_path(config):
    from pathlib import Path

    return Path(config["home"]) / ".commonplace/sync/state.db"


def test_ack_and_cursor_are_one_transaction(client, monkeypatch):
    config, state, root = client
    raw = record()
    (root / "session.jsonl").write_bytes(raw)
    initialize(config, state)
    (op,) = scan(config, state, now=NOW)
    update = state._cursor

    def crash(*args):
        update(*args)
        raise RuntimeError("injected crash")

    monkeypatch.setattr(state, "_cursor", crash)
    with pytest.raises(RuntimeError):
        ack(state, op, raw)
    assert state.outbox_pending() == [op]
    assert state.cursor(op.source, op.path, op.native_id)["offset"] == 0
    monkeypatch.setattr(state, "_cursor", update)
    with pytest.raises(StateError, match="mismatch"):
        state.outbox_ack(op.op_id, {"offset": 0, "prefix_sha256": sha(raw)})
    ack(state, op, raw)
    assert state.outbox_pending() == []
    assert not state.outbox_ack(op.op_id, {})


@pytest.mark.parametrize("mutation", ["slice", "prefix", "shrink", "remove", "symlink"])
def test_pending_changed_or_removed_slice_keeps_cursor(client, mutation):
    config, state, root = client
    first, second = record("first"), record("second")
    path = root / "session.jsonl"
    path.write_bytes(first + second)
    initialize(config, state, offset=len(first), prefix=sha(first))
    (op,) = scan(config, state, now=NOW)
    if mutation == "slice":
        path.write_bytes(first + second.replace(b"second", b"SECOND"))
    elif mutation == "prefix":
        path.write_bytes(first.replace(b"first", b"FIRST") + second)
    elif mutation == "shrink":
        path.write_bytes(first)
    else:
        path.unlink()
        if mutation == "symlink":
            target = root / "target"
            target.write_bytes(first + second)
            path.symlink_to(target)
    with pytest.raises(OSError if mutation == "remove" else SliceConflict):
        body(op)
    assert state.cursor(op.source, op.path, op.native_id)["offset"] == len(first)
    assert state.outbox_pending() == [op]


def test_target_ends_and_unflushed_tail(client):
    config, state, root = client
    first = record()
    path = root / "session.jsonl"
    path.write_bytes(first)
    initialize(config, state)
    targets = snapshot_targets(config)
    path.write_bytes(first + record("later") + b'{"unfinished":"\xf0')
    (op,) = scan(config, state, now=NOW, targets=targets)
    request = body(op)
    assert request["transcript"].encode() == first
    assert scan(config, state, now=NOW) == [op]
    ack(state, op, first)
    (next_op,) = scan(config, state, now=NOW)
    assert body(next_op)["transcript"].encode() == record("later")
    ack(state, next_op, first + record("later"))
    assert scan(config, state, now=NOW) == []


def test_native_rename_preserves_cursor(client):
    config, state, root = client
    first, second = record("first"), record("second")
    path = root / "session.jsonl"
    path.write_bytes(first)
    initialize(config, state)
    (op,) = scan(config, state, now=NOW)
    ack(state, op, first)
    renamed = root / "renamed.jsonl"
    path.rename(renamed)
    renamed.write_bytes(first + second)
    (op,) = scan(config, state, now=NOW)
    assert body(op)["transcript"].encode() == second
    ack(state, op, first + second)
    renamed.rename(path)
    assert scan(config, state, now=NOW) == []


def test_changed_saved_prefix_marks_conflict(client):
    config, state, root = client
    path = root / "session.jsonl"
    path.write_bytes(record())
    initialize(config, state, offset=1, prefix=sha(b"different"))
    assert scan(config, state, now=NOW) == []
    assert state.statuses()[0]["status"] == "conflict"
    assert state.outbox_pending() == []


def test_capture_first_and_memory_target_snapshot(client):
    config, state, root = client
    (root / "session.jsonl").write_bytes(record())
    initialize(config, state)
    notes = root.parent / "notes"
    notes.mkdir()
    note = notes / "note.md"
    note.write_bytes(b"synthetic old version")
    os.utime(note, (NOW - 120, NOW - 120))
    config["memories"] = [{"label": "notes", "root": str(notes)}]
    targets = snapshot_targets(config)
    operations = scan(config, state, now=NOW, targets=targets)
    assert [op.kind for op in operations] == ["capture", "import"]
    state.outbox_ack(operations[1].op_id, {"accepted": 1, "errors": 0})
    note.write_bytes(b"synthetic new longer version")
    os.utime(note, (NOW - 120, NOW - 120))
    assert scan(config, state, now=NOW, targets=targets) == [operations[0]]
    assert len(scan(config, state, now=NOW)) == 2


def test_memory_ack_crash_keeps_unseen_version(client):
    config, state, root = client
    note = root / "note.md"
    note.write_bytes(b"synthetic memory")
    os.utime(note, (NOW - 120, NOW - 120))
    config["memories"] = [{"label": "notes", "root": str(root)}]
    (op,) = scan(config, state, now=NOW)
    with pytest.raises(StateError):
        state.outbox_ack(op.op_id, {"accepted": 0, "errors": 1})
    original = state.conn

    class CrashBeforeDelete:
        def execute(self, sql, parameters=()):
            if sql.startswith("DELETE FROM outbox"):
                raise RuntimeError("injected crash")
            return original.execute(sql, parameters)

    state.conn = CrashBeforeDelete()
    with pytest.raises(RuntimeError):
        state.outbox_ack(op.op_id, {"accepted": 1, "errors": 0})
    state.conn = original
    assert not state.memory_seen("notes", "note.md", sha(note.read_bytes()))
    assert state.outbox_pending() == [op]


def event(seq, kind="sync.request"):
    return {
        "seq": seq,
        "at": "2030-01-01T00:00:00Z",
        "kind": kind,
        "data": {"enabled": False} if kind == "sync.capture" else {},
    }


def envelope(seq, events=(), *, reset=False):
    return {
        "feed": "sync",
        "seq": seq,
        "events": list(events),
        "more": False,
        "reset": reset,
        "waited": False,
        "state": {"capture_enabled": True, "interval_seconds": 900, "next_window_at": None},
    }


def test_feed_dedup_reset_prune_and_key_isolation(client):
    _, state, _ = client
    key = feed_key("https://example.invalid", "COMMONPLACE_IMPORT_TOKEN")
    assert state.feed_cursor(key) is None
    assert state.store_feed(key, envelope(1, [event(1)]), fetched_at=1) == [event(1)]
    assert state.store_feed(key, envelope(1, [event(1)]), fetched_at=2) == []
    assert state.store_feed("other", envelope(1, [event(1)]), fetched_at=1) == [event(1)]
    for start in range(2, 1003, 200):
        end = min(start + 199, 1002)
        state.store_feed(
            key, envelope(end, [event(i) for i in range(start, end + 1)]), fetched_at=end
        )
    assert (
        state.conn.execute("SELECT COUNT(*) FROM events WHERE feed_key=?", (key,)).fetchone()[0]
        == 1000
    )
    state.store_feed(key, envelope(0, reset=True), fetched_at=1003)
    assert state.feed_cursor(key)["seq"] == 0
    assert (
        state.conn.execute("SELECT COUNT(*) FROM events WHERE feed_key=?", (key,)).fetchone()[0]
        == 0
    )
    assert state.feed_cursor("other")["seq"] == 1
    assert state.store_feed(key, envelope(1, [event(1)]), fetched_at=1004) == [event(1)]


def test_feed_cache_and_cursor_crash_boundary(client):
    config, state, _ = client
    original = state.conn

    class CrashBeforeCursor:
        def execute(self, sql, parameters=()):
            if sql.startswith("INSERT OR REPLACE INTO feed_cursor"):
                raise RuntimeError("injected crash")
            return original.execute(sql, parameters)

    state.conn = CrashBeforeCursor()
    with pytest.raises(RuntimeError):
        state.store_feed("key", envelope(1, [event(1)]), fetched_at=1)
    state.conn = original
    assert state.feed_cursor("key") is None
    assert state.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert state.store_feed("key", envelope(1, [event(1)]), fetched_at=1) == [event(1)]
    with open_state(state_path(config)) as restarted:
        assert restarted.feed_cursor("key")["seq"] == 1
        assert restarted.store_feed("key", envelope(1, [event(1)]), fetched_at=2) == []


@pytest.mark.parametrize("field", ["event-data", "state", "at", "ordinary-feed"])
def test_feed_rejects_non_protocol_content(client, field):
    _, state, _ = client
    page = envelope(1, [event(1)])
    if field == "event-data":
        page["events"][0]["data"] = {"text": "synthetic_secret"}
    elif field == "state":
        page["state"]["transcript"] = "synthetic_secret"
    elif field == "at":
        page["events"][0]["at"] = "synthetic_secret"
    else:
        page["feed"] = "ordinary"
    with pytest.raises(StateError):
        state.store_feed("key", page, fetched_at=0)
    assert state.feed_cursor("key") is None


@pytest.mark.parametrize("target", ["directory", "db", "wal", "shm", "symlink"])
def test_state_refuses_non_private_files(tmp_path, target):
    directory = tmp_path / "sync"
    directory.mkdir(mode=0o700)
    path = directory / "state.db"
    if target == "directory":
        directory.chmod(0o755)
    elif target == "symlink":
        other = directory / "other"
        other.touch(mode=0o600)
        path.symlink_to(other)
    else:
        selected = path if target == "db" else directory / ("state.db-" + target)
        selected.touch(mode=0o644)
    with pytest.raises(StateError):
        open_state(path)


def test_source_status_and_cadence(client):
    _, state, _ = client
    assert state.next_due("capture") == 0
    state.defer("capture", NOW + 900)
    assert state.next_due("capture") == NOW + 900
    for status in ("paused", "revoked", "conflict"):
        state.set_status("codex", "synthetic-path", "session", status)
        assert state.statuses()[0]["status"] == status
    with pytest.raises(StateError):
        state.set_status("codex", "synthetic-path", "session", "private remote error")


def test_memory_versions_skips_and_verbatim(client):
    config, state, root = client
    memory = root.parent / "notes"
    memory.mkdir()
    config["memories"] = [{"label": "notes", "root": str(memory)}]
    content = "  synthetic café 🦉\r\nEvery\tword."
    note = memory / "note.md"
    note.write_bytes(content.encode())
    (memory / "pointer.md").write_bytes(POINTER.encode())
    original = memory / "original.md"
    original.write_bytes(b"synthetic onboarded original")
    manifest = state_path(config).parent.parent / "onboarding.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {str(original): {"original_sha256": sha(original.read_bytes())}},
            }
        )
    )
    (memory / "large.md").write_bytes(b"x" * 400_001)
    (memory / "linked.md").symlink_to(note)
    (memory / "recent.md").write_bytes(b"synthetic recent")
    for path in memory.iterdir():
        if not path.is_symlink():
            os.utime(path, (NOW - 120, NOW - 120))
    os.utime(memory / "recent.md", (NOW - 59, NOW - 59))
    (op,) = scan(config, state, now=NOW)
    request = body(op)
    assert request["source"] == "native-memory"
    assert request["threads"][0]["transcript"].encode() == note.read_bytes()
    assert not state.memory_seen("notes", "note.md", sha(note.read_bytes()))
    state.outbox_ack(op.op_id, {"accepted": 1, "errors": 0})
    assert state.memory_seen("notes", "note.md", sha(note.read_bytes()))
    assert scan(config, state, now=NOW) == []
    note.write_bytes((content + "\nnew").encode())
    os.utime(note, (NOW - 120, NOW - 120))
    (next_op,) = scan(config, state, now=NOW)
    assert next_op.native_id != op.native_id
    assert body(next_op)["threads"][0]["transcript"] == content + "\nnew"
    # A broad memory root cannot read the client's own files.
    config["memories"] = [{"label": "private", "root": config["home"], "glob": "**/*"}]
    assert scan(config, state, now=NOW) == [next_op]


@pytest.mark.parametrize("underlying", ["claude-code", "codex"])
def test_t3_uses_native_format_and_no_symlinks(client, underlying):
    config, _, root = client
    config["sources"][0]["source"] = "t3"
    raw = (
        record()
        if underlying == "claude-code"
        else b'{"type":"session_meta","payload":{"id":"session"}}\n'
    )
    (root / "session.jsonl").write_bytes(raw)
    (root / "linked.jsonl").symlink_to(root / "session.jsonl")
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "hidden.jsonl").write_bytes(record("hidden", sessionId="hidden"))
    (root / "linked-dir").symlink_to(outside, target_is_directory=True)
    (source,) = discover(config)
    assert source.source == underlying
    assert source.native_id == "session"


def test_cursor_probe_has_no_values_or_unknown_key_names(client):
    config, _, root = client
    config["sources"] = [{"source": "cursor"}]
    with pytest.raises(SourceError, match=CURSOR_MESSAGE):
        discover(config)
    path = root / "probe.jsonl"
    secret_key, value = "synthetic_unknown_key", "synthetic_private_value"
    path.write_text(json.dumps({"type": value, secret_key: {"content": [value, 123]}}))
    report = json.dumps(probe(path))
    assert secret_key not in report and value not in report and "123" not in report
    assert sha(secret_key.encode())[:12] in report
    assert "content" in report
