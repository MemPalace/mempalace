"""U8's bounded board reader, independent of the other builders' stubs."""

import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from commonplace.console import output, types
from commonplace.policy import Policy
from mempalace.sqlite_crypto import driver
from tests._console_helpers import console_hub_fixture  # noqa: F401

AT = "2026-11-04T09:00:00Z"
SCHEMA = Path(__file__).parents[1] / "commonplace" / "boards" / "schema.sql"


class OutputCase:
    def __init__(self, root):
        self.root = root
        self.conn = sqlite3.connect(root / "boards.sqlite3", isolation_level=None)
        self.conn.executescript(SCHEMA.read_text())
        self.agent = types.AgentView(
            "conn_a", "old-name", "client", None, "user_a", True, False, AT
        )
        self.principal = SimpleNamespace(
            workspace_id="ws_a",
            user_id="user_a",
            connection="agent-a",
            person=False,
            policy=Policy.build(scopes=["memory:read"], read_wings=["shared"]),
        )
        self.gateway = SimpleNamespace(
            db=SimpleNamespace(authenticate_connection=Mock(return_value=self.principal)),
            supervisor=SimpleNamespace(workspace_dir=Mock(return_value=root)),
            workspace_dir=Mock(side_effect=AssertionError("must not create workspace")),
        )
        self.board(1, "work", "shared")
        self.board(2, "side", "side")
        self.board(3, "archived", "shared", archived=1)
        for number, board in ((42, 1), (43, 2), (44, 3)):
            self.conn.execute(
                "INSERT INTO tasks(id,board_id,title,status,position,stamp_user,stamp_trust,"
                "created_at,updated_at,updated_by) VALUES (?,?,'Card','todo',0,'user_a',"
                "'standard',?,?, 'conn:agent-a')",
                (number, board, AT, AT),
            )

    def board(self, number, slug, wing, *, archived=0):
        self.conn.execute(
            "INSERT INTO boards(id,slug,name,wing,archived,stamp_user,stamp_trust,created_at) "
            "VALUES (?,?,?,?,?,'user_a','standard',?)",
            (number, slug, slug, wing, archived, AT),
        )

    def reply(self, text="reply", *, board=1, user="user_a", connection="agent-a", at=AT):
        return self.conn.execute(
            "INSERT INTO space_messages(board_id,text,stamp_user,stamp_connection,stamp_trust,"
            "flags,created_at) VALUES (?,?,?,?,'standard','[]',?)",
            (board, text, user, connection, at),
        ).lastrowid

    def event(
        self, text="comment", *, task=42, kind="comment", user="user_a", connection="agent-a", at=AT
    ):
        return self.conn.execute(
            "INSERT INTO task_events(task_id,kind,text,context,reason,options,target,flags,"
            "stamp_user,stamp_connection,stamp_trust,created_at) "
            "VALUES (?,?,?,'PRIVATE-CONTEXT','PRIVATE-REASON','PRIVATE-OPTIONS',"
            "'PRIVATE-TARGET','PRIVATE-FLAGS',?,?,'standard',?)",
            (task, kind, text, user, connection, at),
        ).lastrowid


@pytest.fixture
def case(tmp_path, monkeypatch):
    instance = OutputCase(tmp_path)
    monkeypatch.setattr(output.redact, "redact", Mock(side_effect=lambda text: (text, [])))
    try:
        yield instance
    finally:
        instance.conn.close()


def test_queries_use_the_connection_indexes_without_loading_text(case, monkeypatch):
    reply = case.reply()
    comment = case.event()
    statements = []
    original = output.connect

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(output, "connect", traced)
    assert output.latest_ids(case.gateway, case.agent) == (reply, comment)
    queries = [sql for sql in statements if "LIMIT 500" in sql]
    assert len(queries) == 2
    for query, index in zip(
        queries, ("space_messages_by_connection", "task_events_output_by_connection")
    ):
        plan = case.conn.execute("EXPLAIN QUERY PLAN " + query).fetchall()
        assert any(index in row[3] and "SEARCH" in row[3] for row in plan)
        assert "text" not in query
    output.redact.redact.assert_not_called()


@pytest.mark.parametrize("source", ["reply", "event"])
def test_scan_stops_before_older_visible_rows(case, source):
    write = getattr(case, source)
    write("older visible")
    hidden = {"board": 2} if source == "reply" else {"task": 43}
    for _ in range(600):
        write("newer invisible", **hidden)
    assert output.latest_ids(case.gateway, case.agent) == (0, 0)
    assert output.recent(case.gateway, case.agent) == []
    output.redact.redact.assert_not_called()


def test_output_shape_card_number_kinds_and_private_fields(case):
    case.reply()
    for kind in ("comment", "handoff", "ask"):
        case.event(kind, kind=kind)
    case.event("not output", kind="decide")
    rows = output.recent(case.gateway, case.agent)
    assert [row["kind"] for row in rows] == ["ask", "handoff", "reply", "comment"]
    assert [row["id"] for row in rows] == ["c3", "c2", "r1", "c1"]
    assert all(
        row
        == {
            "id": row["id"],
            "at": AT,
            "source": "output",
            "kind": row["kind"],
            "board": "work",
            "card": None if row["kind"] == "reply" else 42,
            "text": row["kind"],
            "truncated": False,
            "redacted": [],
        }
        for row in rows
    )
    assert output.latest_ids(case.gateway, case.agent) == (1, 3)


def test_merge_sorts_timestamp_then_numeric_id_then_reply_and_limits_twenty(case):
    expected = []
    for n in range(1, 26):
        at = f"2026-11-04T09:00:{25 - n:02}Z"
        case.reply(f"r{n}", at=at)
        case.event(f"c{n}", at=at)
        # The source query takes the latest twenty IDs, not the latest times.
        if n >= 6:
            expected.extend([f"r{n}", f"c{n}"])
    assert [row["id"] for row in output.recent(case.gateway, case.agent)] == expected[:20]


@pytest.mark.parametrize("source", ["reply", "event"])
def test_oversized_text_is_omitted_without_feeding_it_to_redactor(case, source):
    getattr(case, source)("x" * 100_000)
    row = output.recent(case.gateway, case.agent)[0]
    assert row["text"] == "[omitted:too_long]"
    assert row["redacted"] == ["too_long"] and row["truncated"]
    assert all(len(call.args[0]) <= 16_000 for call in output.redact.redact.call_args_list)
    assert [call.args[0] for call in output.redact.redact.call_args_list] == ["work"]


@pytest.mark.parametrize("size,truncated", [(16_000, True), (16_001, True), (2000, False)])
def test_text_length_boundaries(case, size, truncated):
    case.reply("x" * size)
    row = output.recent(case.gateway, case.agent)[0]
    assert row["text"] == ("[omitted:too_long]" if size > 16_000 else "x" * min(size, 2000))
    assert row["truncated"] is truncated
    assert all(len(call.args[0]) <= 16_000 for call in output.redact.redact.call_args_list)


@pytest.mark.parametrize("start", [1985, 1998])
@pytest.mark.parametrize("marker", ["[redacted:assignment]", "[omitted:too_long]"])
def test_cut_never_splits_a_redaction_marker(case, start, marker):
    case.reply("x" * start + marker + "tail")
    row = output.recent(case.gateway, case.agent)[0]
    assert row["text"] == "x" * start and row["truncated"]


def test_redacts_whole_text_before_cut_and_redacts_board_slug(case):
    def fake_redact(text):
        clean, count = re.subn(r"password=\S+", "[redacted:assignment]", text)
        return clean, ["assignment"] if count else []

    output.redact.redact.side_effect = fake_redact
    case.conn.execute("UPDATE boards SET slug='password=board-secret' WHERE id=1")
    original = "x" * 1980 + " password=" + "S" * 200 + " tail"
    case.reply(original)
    row = output.recent(case.gateway, case.agent)[0]
    assert row["board"] == "[redacted:assignment]"
    assert row["text"] == "x" * 1980 + " "
    assert row["redacted"] == ["assignment"] and row["truncated"]
    assert any(call.args == (original,) for call in output.redact.redact.call_args_list)
    assert case.conn.execute("SELECT text FROM space_messages").fetchone()[0] == original


def test_current_policy_and_archives_control_visibility(case):
    for board in (1, 2, 3):
        case.reply(f"reply board {board}", board=board)
        case.event(f"comment board {board}", task=41 + board)
    rows = output.recent(case.gateway, case.agent)
    assert {row["board"] for row in rows} == {"work"}
    case.principal.policy = Policy.build(scopes=["memory:read"], read_wings=["side"])
    assert {row["board"] for row in output.recent(case.gateway, case.agent)} == {"side"}
    case.principal.policy = Policy.build(scopes=["memory:read"], read_wings=[])
    assert output.recent(case.gateway, case.agent) == []
    assert output.latest_ids(case.gateway, case.agent) == (0, 0)


def test_person_other_connection_and_wrong_user_rows_never_return(case):
    for source in (case.reply, case.event):
        source("person", connection=None)
        source("another agent", connection="agent-b")
        source("wrong user", user="user_b")
        source("own")
    assert [row["text"] for row in output.recent(case.gateway, case.agent)] == ["own", "own"]


def test_revoked_agent_does_not_resolve_or_open_store(case, monkeypatch):
    case.reply()
    case.gateway.db.authenticate_connection.return_value = None
    opened = Mock(side_effect=AssertionError("revoked agent opened store"))
    monkeypatch.setattr(output, "connect", opened)
    assert output.recent(case.gateway, case.agent) == []
    assert output.latest_ids(case.gateway, case.agent) == (0, 0)
    case.gateway.supervisor.workspace_dir.assert_not_called()
    opened.assert_not_called()


def test_non_own_agent_never_reads_authority_or_store(case):
    agent = replace(case.agent, own=False)
    assert output.recent(case.gateway, agent) == []
    assert output.latest_ids(case.gateway, agent) == (0, 0)
    case.gateway.db.authenticate_connection.assert_not_called()


def test_missing_boards_does_not_create_any_file(case, tmp_path):
    absent = tmp_path / "nonexistent-workspace"
    case.gateway.supervisor.workspace_dir.return_value = absent
    assert output.recent(case.gateway, case.agent) == []
    assert output.latest_ids(case.gateway, case.agent) == (0, 0)
    assert not absent.exists()
    assert list(tmp_path.glob("**/console.sqlite3*")) == []
    case.gateway.workspace_dir.assert_not_called()


def test_reader_uses_non_creating_uri_and_query_only_and_closes(case, monkeypatch):
    case.reply()
    opened = []

    def connect(*args, **kwargs):
        assert args == (case.root.resolve().as_uri() + "/boards.sqlite3?mode=rw",)
        assert kwargs == {"uri": True, "timeout": 0.2, "isolation_level": None}
        conn = sqlite3.connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        opened.append(conn)
        return conn

    statements = []
    monkeypatch.setattr(output, "connect", connect)
    assert output.recent(case.gateway, case.agent)
    assert "PRAGMA query_only=ON" in statements
    assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|ALTER)", sql) for sql in statements)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_busy_boards_read_returns_null_and_recovers(case):
    case.reply()
    case.conn.execute("PRAGMA journal_mode=DELETE")
    case.conn.execute("BEGIN EXCLUSIVE")
    try:
        assert output.latest_ids(case.gateway, case.agent) is None
        assert output.recent(case.gateway, case.agent) is None
    finally:
        case.conn.rollback()
    assert output.latest_ids(case.gateway, case.agent) == (1, 0)
    assert output.recent(case.gateway, case.agent)


@pytest.mark.parametrize("backend", ["sqlite", "sqlcipher"])
@pytest.mark.parametrize("message", ["database is locked", "database table is locked"])
def test_locked_open_returns_null(case, monkeypatch, message, backend):
    if backend == "sqlcipher":
        pytest.importorskip("sqlcipher3")  # same convention as test_workspace_encryption
    module = driver() if backend == "sqlcipher" else sqlite3
    monkeypatch.setattr(output, "connect", Mock(side_effect=module.OperationalError(message)))
    for reader in (output.latest_ids, output.recent):
        try:
            result = reader(case.gateway, case.agent)
        except module.OperationalError as exc:
            result = exc
        assert result is None


def test_sqlcipher_execute_locked_returns_null_and_closes(case, monkeypatch):
    pytest.importorskip("sqlcipher3")  # same convention as test_workspace_encryption
    module = driver()
    opened = []

    class BusyConnection(module.Connection):
        __module__ = "sqlcipher3"

        def execute(self, sql, *args):
            if sql.startswith("SELECT"):
                raise module.OperationalError("database is locked")
            return super().execute(sql, *args)

    def connect(*args, **kwargs):
        conn = module.connect(":memory:", factory=BusyConnection)
        opened.append(conn)
        return conn

    monkeypatch.setattr(output, "connect", connect)
    for reader in (output.latest_ids, output.recent):
        try:
            result = reader(case.gateway, case.agent)
        except module.OperationalError as exc:
            result = exc
        assert result is None
        with pytest.raises(module.ProgrammingError, match="closed"):
            opened[-1].execute("PRAGMA query_only=ON")


@pytest.mark.parametrize("forward", [False, True])
@pytest.mark.parametrize("phase", ["open", "execute", "stream"])
def test_sqlcipher_locked_output_http_keeps_activity_and_retries(
    console_hub, monkeypatch, forward, phase
):
    h = console_hub
    from commonplace.console import store, stream

    pytest.importorskip("sqlcipher3")  # same convention as test_workspace_encryption
    module = driver()
    activity = store.open_activity(h.gateway, h.workspace)
    seq = activity.append(h.a1, "task_list", "ok", 1)
    original = output.connect

    class BusyConnection(module.Connection):
        __module__ = "sqlcipher3"

        def execute(self, sql, *args):
            if sql.startswith("SELECT"):
                raise module.OperationalError("database is locked")
            return super().execute(sql, *args)

    def busy(*args, **kwargs):
        if phase != "execute":
            raise module.OperationalError("database is locked")
        return module.connect(":memory:", factory=BusyConnection)

    with monkeypatch.context() as patch:
        patch.setattr(output, "latest_ids" if phase == "stream" else "connect", busy)
        suffix = "&after=" + stream.encode_cursor(0, 0, 5, 6) if forward else ""
        code, result, _ = h.stream(suffix=suffix)
        assert code == 200
        assert result["output"] is None
        assert [item["id"] for item in result["items"]] == [f"t{seq}"]
        assert stream.decode_cursor(result["cursor"])[2:] == ((5, 6) if forward else (0, 0))
    assert output.connect is original
    code, retry, _ = h.stream(suffix="&after=" + result["cursor"])
    # After the lock clears the output is read again: a tail cursor already holds the
    # current (empty) head, so it is unchanged (null); a stale forward head gets [].
    assert code == 200 and retry["output"] == ([] if forward else None)


def test_broken_workspace_pointer_returns_null(case):
    case.gateway.supervisor.workspace_dir.side_effect = ValueError("broken generation pointer")
    assert output.latest_ids(case.gateway, case.agent) is None
    assert output.recent(case.gateway, case.agent) is None
