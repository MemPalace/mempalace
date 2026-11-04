"""Pure protocol contracts and temporary control-schema compatibility."""

import json
import re

import pytest

from commonplace import continuity_schema as schema, sync_protocol as protocol
from commonplace.control_db import ControlDB
from commonplace.handoff.models import ThreadMessage


def native(sid="session", agent=None):
    record = {"type": "user", "sessionId": sid, "message": {"content": "synthetic words"}}
    if agent:
        record["agentId"] = agent
    return json.dumps(record) + "\n"


@pytest.mark.parametrize(
    ("source", "fmt", "text", "expected"),
    [
        ("t3", "auto", native(), "claude-code"),
        ("t3", "codex", "{}", "codex"),
        ("t3", "auto", '[{"role":"user","content":"synthetic"}]', None),
        ("t3", "auto", "garbage", None),
        ("claude-code", "auto", "garbage", "claude-code"),
        ("chatgpt", "auto", native(), None),
    ],
    ids=["NS1", "NS2", "NS3", "NS4", "NS5", "NS6"],
)
def test_native_source(source, fmt, text, expected):
    assert protocol.native_source(source, fmt, text) == expected


def test_native_identity_is_bounded_and_never_invents_an_id():
    assert (
        protocol.native_identity(native(agent="helper").encode(), "claude-code")
        == "session:agent:helper"
    )
    assert (
        protocol.native_identity(
            b'{"type":"session_meta","payload":{"id":"native-codex"}}\n', "codex"
        )
        == "native-codex"
    )
    assert protocol.native_identity(b'{"type":"response_item","payload":{}}\n', "codex") is None
    for data in (b"garbage", b"\xff\n", b"[]\n", b"", None):
        assert protocol.native_identity(data, "claude-code") is None
    assert protocol.native_identity(native().encode(), "t3") is None
    assert (
        protocol.native_identity(b" " * (1024 * 1024 + 1) + native().encode(), "claude-code")
        is None
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{}\n{"unfinished":', True),
        ('{}\r{"unfinished":', True),
        ('{}\n{"complete":1}', False),
        ("{}\n", False),
        ("{}\r", False),
        ("{}\n   ", False),
        ("", False),
        (None, False),
        ('"complete"', False),
    ],
)
def test_live_tail(text, expected):
    assert protocol.live_tail(text) is expected


def test_memory_identity_validation():
    digest = "a" * 64
    assert (
        protocol.memory_thread_id("notes", "folder/é.md", digest) == f"notes:folder/é.md:{digest}"
    )
    for label, path, value in (
        ("Notes", "note.md", digest),
        ("", "note.md", digest),
        ("a" * 33, "note.md", digest),
        ("notes", "/note.md", digest),
        ("notes", "a/../note.md", digest),
        ("notes", "\x00note.md", digest),
        ("notes", "", digest),
        ("notes", "x" * 500, digest),
        ("notes", "note.md", "A" * 64),
        ("notes", "note.md", "a" * 63),
    ):
        with pytest.raises(ValueError, match="Invalid memory identity"):
            protocol.memory_thread_id(label, path, value)


def test_wire_constants_and_validation_patterns():
    assert protocol.PROTOCOL_VERSION == 1
    assert (
        protocol.PUSH_WINDOW_SECONDS,
        protocol.MAX_WAIT_SECONDS,
        protocol.LIVE_TICK_SECONDS,
    ) == (60, 25, 2)
    assert protocol.SYNC_FEED == "sync"
    assert protocol.SYNC_KINDS == {"sync.capture", "sync.request"}
    assert protocol.SYNC_ENVELOPE_KEYS == {
        "feed",
        "events",
        "seq",
        "more",
        "reset",
        "waited",
        "state",
    }
    assert protocol.SYNC_EVENT_KEYS == {"seq", "at", "kind", "data"}
    assert protocol.SYNC_STATE_KEYS == {"capture_enabled", "interval_seconds", "next_window_at"}
    assert (protocol.SYNC_RETENTION, protocol.SYNC_PAGE, protocol.WRITE_BUDGET) == (
        1000,
        200,
        (120, 3600),
    )
    assert protocol.TRANSCRIPT_SOURCES == ("claude-code", "codex", "t3")
    assert (protocol.MEMORY_IMPORT_SOURCE, protocol.MEMORY_FORMAT) == ("native-memory", "file")
    assert protocol.TOKEN_ENV == {
        "claude-code": "COMMONPLACE_CAPTURE_TOKEN_CLAUDE_CODE",
        "codex": "COMMONPLACE_CAPTURE_TOKEN_CODEX",
        "native-memory": "COMMONPLACE_IMPORT_TOKEN",
    }
    assert {"type", "message", "payload", "content"} <= protocol.PROBE_KEY_ALLOWLIST
    assert "synthetic_unlisted_key" not in protocol.PROBE_KEY_ALLOWLIST
    for value in ("a" * 16, "Z0_-" * 16):
        assert re.fullmatch(protocol.OP_ID_PATTERN, value)
    for value in ("a" * 15, "a" * 65, "a" * 16 + "\n", "!" * 16):
        assert re.fullmatch(protocol.OP_ID_PATTERN, value) is None
    for message in (
        "Capture not due; retry in 840 seconds",
        "Import not due; retry in 900 seconds",
    ):
        assert re.search(protocol.RETRY_RE, message)


def test_schema_idempotence_and_caller_transaction(tmp_path):
    db = ControlDB(str(tmp_path / "control.sqlite3"))
    try:
        schema.ensure_schema(db)
        schema.ensure_schema(db)
        assert not db._conn.in_transaction
        with db._lock, db._tx():
            schema.ensure_schema(db)
            assert db._conn.in_transaction
        assert not db._conn.in_transaction
        streams = [row[1] for row in db._conn.execute("PRAGMA table_info(sync_streams)")]
        assert streams == list(schema.StreamRow.__dataclass_fields__)
        groups = [row[1] for row in db._conn.execute("PRAGMA table_info(sync_groups)")]
        assert groups == list(schema.GroupRow.__dataclass_fields__)
        ops = {row[1]: row[5] for row in db._conn.execute("PRAGMA table_info(capture_ops)")}
        assert {key: ops[key] for key in ("workspace_id", "actor_id", "client_op_id")} == {
            "workspace_id": 1,
            "actor_id": 2,
            "client_op_id": 3,
        }
        assert "connection_id" not in ops
        assert "listed_messages" in streams
        assert "sync_backfill" in {
            r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        db.close()


def test_archive_segments_use_only_archives_and_inclusive_row_bounds():
    def m(seq, raw, name="", role="system", kind="other"):
        return ThreadMessage(seq, role, kind, "", raw, name=name, at="synthetic-at")

    parsed = m(1, "parsed", role="user", kind="message")
    assert schema.archive_segments([m(0, "snapshot\r\n"), parsed]) == [
        schema.Segment(0, 1, b"snapshot\r\n", "synthetic-at")
    ]
    rows = [
        m(0, "first\r\n", "import.raw"),
        m(1, "metadata", "import.part"),
        m(2, "parsed", role="user", kind="message"),
        m(3, "second", "import.raw"),
        m(4, "metadata", "import.part"),
    ]
    assert schema.archive_segments(rows) == [
        schema.Segment(0, 2, b"first\r\n", "synthetic-at"),
        schema.Segment(3, 4, b"second", "synthetic-at"),
    ]
    assert schema.archive_segments(
        [
            m(0, '{"v":1}', schema.MARKER),
            m(1, "suffix\n", "capture.raw"),
            m(2, "parsed", role="user", kind="message"),
        ]
    ) == [schema.Segment(1, 2, b"suffix\n", "synthetic-at")]
    assert schema.archive_segments([m(0, '{"v":1}', schema.MARKER)]) == []
    assert schema.archive_segments([]) == []
    assert schema.fork_id("usr_a", "stream") == schema.fork_id("usr_a", "stream")
    assert schema.fork_id("usr_a", "stream") != schema.fork_id("usr_a", "other")
    assert schema.fork_id("usr_a", "stream").startswith("capture-fork:usr_a:")


def test_deeply_nested_json_never_escapes_identity_or_tail_helpers():
    nested = "[" * 10000 + "]" * 10000
    assert protocol.native_source("t3", "auto", nested) is None
    assert protocol.native_identity(nested.encode(), "claude-code") is None
    assert protocol.live_tail(nested) is True
