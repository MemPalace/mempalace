"""U5 contract tests, with frozen upstream interfaces faked locally."""

import base64
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from commonplace import events
from commonplace.console import stream, types
from tests._console_helpers import console_hub_fixture  # noqa: F401

BUDGET = 128 * 1024
MAX_POSITION = (1 << 63) - 1
AT = "2026-11-04T10:00:00Z"


def wire(payload):
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def cursor_payload(**changes):
    return dict(v=3, a=0, e=0, r=0, c=0, **changes)


def json_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def tool(seq, *, cid="own", at=AT, name="task_list"):
    return dict(seq=seq, connection_id=cid, at=at, name=name, outcome="ok", ms=12)


class Activity:
    def __init__(self):
        self.rows = []
        self.calls = []

    def after(self, cid, seq, limit):
        self.calls.append(("after", cid, seq, limit))
        return sorted(
            (row for row in self.rows if row["connection_id"] == cid and row["seq"] > seq),
            key=lambda row: row["seq"],
        )[:limit]

    def tail(self, cid, limit):
        self.calls.append(("tail", cid, limit))
        return sorted(
            (row for row in self.rows if row["connection_id"] == cid),
            key=lambda row: row["seq"],
            reverse=True,
        )[:limit]


@pytest.fixture
def console(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(events.SCHEMA)
    db = SimpleNamespace(_conn=conn, _lock=threading.RLock())
    gateway = SimpleNamespace(db=db)
    principal = SimpleNamespace(workspace_id="ws")
    agent = types.AgentView("own", "agent", "test", None, "user", True, True, None)
    activity = Activity()
    state = SimpleNamespace(head=(0, 0), recent=[], calls=[], redactions=[])

    def clean(text):
        assert len(text) <= 16_000
        state.redactions.append(text)
        if "unit-secret" in text:
            return text.replace("unit-secret", "[redacted:assignment]"), ["assignment"]
        return text, []

    def peek(gw, ws):
        assert gw is gateway and ws == "ws"
        state.calls.append("peek")
        return activity

    def latest_ids(gw, selected):
        assert gw is gateway and selected.id == agent.id and selected.own
        state.calls.append("latest")
        return state.head

    def recent(gw, selected):
        assert gw is gateway and selected.id == agent.id and selected.own
        state.calls.append("recent")
        return state.recent

    monkeypatch.setattr(stream.redact, "redact", clean)
    monkeypatch.setattr(stream.store, "peek", peek)
    monkeypatch.setattr(stream.output, "latest_ids", latest_ids)
    monkeypatch.setattr(stream.output, "recent", recent)

    def add(
        *, actor="conn:agent", ws="ws", at=AT, subject="card:184", data=None, kind="card.updated"
    ):
        return conn.execute(
            "INSERT INTO events(at, workspace_id, kind, subject, actor, data) VALUES(?,?,?,?,?,?)",
            (at, ws, kind, subject, actor, json.dumps(data or {}, ensure_ascii=False)),
        ).lastrowid

    def page(cursor=None, limit=None, selected=None):
        return stream.page(gateway, principal, selected or agent, cursor, limit)

    yield SimpleNamespace(
        db=db, gateway=gateway, agent=agent, activity=activity, state=state, add=add, page=page
    )
    conn.close()


@pytest.mark.parametrize("positions", [(0, 0, 0, 0), (1, 2, 3, 4), (MAX_POSITION,) * 4])
def test_cursor_round_trip_and_shape(positions):
    encoded = stream.encode_cursor(*positions)
    assert len(encoded) <= 192
    assert stream.decode_cursor(encoded) == positions
    padded = encoded + "=" * (-len(encoded) % 4)
    assert stream.decode_cursor(padded) == positions
    assert json.loads(base64.urlsafe_b64decode(padded)) == dict(
        zip(("v", "a", "e", "r", "c"), (3, *positions))
    )


@pytest.mark.parametrize("field", ["a", "e", "r", "c"])
@pytest.mark.parametrize("bad", [-1, MAX_POSITION + 1, True, False, 1.0, "1", None, [], {}])
def test_cursor_rejects_invalid_positions(field, bad):
    payload = cursor_payload()
    payload[field] = bad
    with pytest.raises(ValueError, match="^Invalid console cursor$"):
        stream.decode_cursor(wire(json.dumps(payload)))
    positions = [0, 0, 0, 0]
    positions[("a", "e", "r", "c").index(field)] = bad
    with pytest.raises(ValueError, match="^Invalid console cursor$"):
        stream.encode_cursor(*positions)


@pytest.mark.parametrize(
    "payload",
    [
        '{"v":2,"a":0,"e":0}',
        '{"v":2,"a":0,"e":0,"r":0,"c":0}',
        '{"v":3.0,"a":0,"e":0,"r":0,"c":0}',
        '{"v":true,"a":0,"e":0,"r":0,"c":0}',
        '{"a":0,"e":0,"r":0,"c":0}',
        '{"v":3,"a":0,"e":0,"r":0,"c":0,"id":"foreign"}',
        '{"v":3,"a":0,"a":1,"e":0,"r":0,"c":0}',
        '{"v":3,"a":0,"e":0,"r":0}',
        '{"v":3,"a":0,"e":0,"r":0,"c":NaN}',
        "[3,0,0,0,0]",
        "null",
        "{}",
        "false",
        "{",
        '{"v":3,"a":0,"e":0,"r":0,"c":0} {}',
    ],
)
def test_cursor_rejects_every_other_json_shape(payload):
    with pytest.raises(ValueError, match="^Invalid console cursor$"):
        stream.decode_cursor(wire(payload))


@pytest.mark.parametrize(
    "bad", [None, 12, b"e30", "", "!!", "a", "A" * 193, "é", "e30\n", "e30===", "e30=+", "/w", "_w"]
)
def test_cursor_rejects_malformed_encoding(bad):
    with pytest.raises(ValueError, match="^Invalid console cursor$"):
        stream.decode_cursor(bad)


def test_cursor_rejects_noncanonical_padding_bits():
    encoded = stream.encode_cursor(1, 0, 0, 0)
    assert len(encoded) % 4 != 0
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    changed = encoded[:-1] + alphabet[alphabet.index(encoded[-1]) + 1]
    assert base64.urlsafe_b64decode(encoded + "===") == base64.urlsafe_b64decode(changed + "===")
    with pytest.raises(ValueError, match="Invalid console cursor"):
        stream.decode_cursor(changed)


@pytest.mark.parametrize("bad", [0, -1, 101, 1.0, True, "1", "bad", "", [], {}])
def test_limit_is_strict_and_checked_before_reads(console, bad):
    with pytest.raises(ValueError, match="^Invalid limit$"):
        console.page(limit=bad)
    assert console.state.calls == []


def test_invalid_cursor_is_checked_before_reads(console):
    with pytest.raises(ValueError, match="Invalid console cursor"):
        console.page(cursor="invalid!")
    assert console.state.calls == []


def test_empty_page_and_absent_activity_file(console, monkeypatch):
    monkeypatch.setattr(stream.store, "peek", lambda *args: None)
    result = console.page()
    assert result == dict(
        items=[], cursor=stream.encode_cursor(0, 0, 0, 0), more=False, tools="on", output=[]
    )


def test_tail_defaults_to_newest_50_and_forward_defaults_to_100(console):
    console.activity.rows = [tool(seq) for seq in range(1, 152)]
    tail = console.page()
    assert [i["id"] for i in tail["items"]] == [f"t{s}" for s in range(102, 152)]
    assert stream.decode_cursor(tail["cursor"]) == (151, 0, 0, 0)
    assert console.activity.calls == [("tail", "own", 50)]
    first = console.page(stream.encode_cursor(0, 0, 0, 0))
    assert len(first["items"]) == 100 and first["more"] is True
    second = console.page(first["cursor"])
    assert [i["id"] for i in first["items"] + second["items"]] == [f"t{s}" for s in range(1, 152)]
    assert second["more"] is False
    assert console.activity.calls[-2:] == [("after", "own", 0, 101), ("after", "own", 100, 101)]


@pytest.mark.parametrize("limit", [1, 100])
def test_explicit_limit_boundaries(console, limit):
    console.activity.rows = [tool(seq) for seq in range(1, 102)]
    result = console.page(stream.encode_cursor(0, 0, 0, 0), limit=limit)
    assert len(result["items"]) == limit
    assert result["more"] is True


def test_merge_order_and_forward_cursors_advance_only_consumed_source(console):
    first_event = console.add(at="2026-11-04T09:59:59Z")
    second_event = console.add()
    console.activity.rows = [tool(1), tool(2), tool(3, at="2026-11-04T10:00:01Z")]
    cursor = stream.encode_cursor(0, 0, 0, 0)
    first = console.page(cursor, limit=2)
    assert [i["id"] for i in first["items"]] == [f"e{first_event}", "t1"]
    assert stream.decode_cursor(first["cursor"])[:2] == (1, first_event)
    assert first["more"] is True
    second = console.page(first["cursor"], limit=2)
    assert [i["id"] for i in second["items"]] == ["t2", f"e{second_event}"]
    assert stream.decode_cursor(second["cursor"])[:2] == (2, second_event)
    assert second["more"] is True
    third = console.page(second["cursor"], limit=2)
    assert [i["id"] for i in third["items"]] == ["t3"]
    assert third["more"] is False


def test_tail_merges_newest_and_sets_both_fetched_maxima(console):
    e1, e2 = console.add(), console.add()
    console.activity.rows = [tool(1), tool(2)]
    result = console.page(limit=2)
    assert [i["id"] for i in result["items"]] == [f"e{e1}", f"e{e2}"]
    assert stream.decode_cursor(result["cursor"])[:2] == (2, e2)
    forward = console.page(result["cursor"])
    assert forward["items"] == [] and forward["more"] is False


def test_source_queries_are_filtered_by_authorized_agent(console):
    console.activity.rows = [tool(1, cid="foreign"), tool(2)]
    console.add(actor="conn:other")
    console.add(actor="user:user")
    console.add(ws="elsewhere")
    wanted = console.add()
    result = console.page(stream.encode_cursor(0, 0, 0, 0))
    assert [i["id"] for i in result["items"]] == ["t2", f"e{wanted}"]
    assert console.activity.calls == [("after", "own", 0, 101)]


@pytest.mark.parametrize("forward", [False, True])
def test_events_window_is_newest_2000_in_workspace_before_actor_filter(console, forward):
    console.add(subject="too old")
    visible = console.add(subject="inside window")
    for _ in range(1999):
        console.add(actor="conn:other")
    # Hub-wide seq gaps must not shrink the workspace window.
    for _ in range(2100):
        console.add(ws="elsewhere")
    reads = []
    console.db._conn.set_trace_callback(
        lambda sql: reads.append((sql, console.db._lock._is_owned()))
    )
    result = console.page(stream.encode_cursor(0, 0, 0, 0) if forward else None)
    assert [i["id"] for i in result["items"]] == [f"e{visible}"]
    assert reads and all(held for _, held in reads)
    assert all("LIMIT 2000" in sql for sql, _ in reads)


def test_forward_events_window_applies_after_cursor_and_skips_old_events_permanently(console):
    before = console.add()
    console.add(subject="missed after burst")
    for _ in range(2000):
        console.add(actor="conn:other")
    newest = console.add()
    result = console.page(stream.encode_cursor(0, before, 0, 0))
    assert [i["id"] for i in result["items"]] == [f"e{newest}"]
    assert stream.decode_cursor(result["cursor"])[1] == newest
    assert console.page(result["cursor"])["items"] == []


def test_board_data_allowlist_and_tool_shape_redact_before_display(console):
    console.add(
        subject="unit-secret",
        kind="unit-secret",
        data=dict(
            title="unit-secret",
            **{"from": "unit-secret", "to": "done"},
            context="never shown",
            reason="never shown",
        ),
    )
    console.activity.rows = [tool(1, name="unit-secret")]
    result = console.page()
    tool_item, board = result["items"]
    assert set(tool_item) == {"id", "at", "source", "name", "outcome", "ms"}
    assert tool_item["name"] == "[redacted:assignment]"
    assert set(board) == {"id", "at", "source", "kind", "text", "truncated", "redacted"}
    assert (
        board["text"]
        == "[redacted:assignment] · [redacted:assignment] · [redacted:assignment] → done"
    )
    assert board["kind"] == "[redacted:assignment]"
    assert board["redacted"] == ["assignment"] and board["truncated"] is False
    assert "unit-secret" not in json.dumps(result)
    assert "never shown" not in json.dumps(result)


def test_joined_board_text_is_redacted_again(console, monkeypatch):
    calls = []

    def redact_join(text):
        calls.append(text)
        return ("[redacted:assignment]", ["assignment"]) if text == "a · b" else (text, [])

    monkeypatch.setattr(stream.redact, "redact", redact_join)
    console.add(subject="a", data={"title": "b"})
    board = console.page()["items"][0]
    assert board["text"] == "[redacted:assignment]" and board["redacted"] == ["assignment"]
    assert "a" in calls and "b" in calls and "a · b" in calls


@pytest.mark.parametrize(
    "data", [{"from": "a"}, {"to": "b"}, {"from": "a", "to": 1}, {"title": ["ignore"]}, ["ignore"]]
)
def test_board_ignores_nonstring_fields_and_incomplete_transitions(console, data):
    console.add(subject="subject", data=data)
    assert console.page()["items"][0]["text"] == "subject"


def test_invalid_event_json_does_not_display_raw_data(console):
    seq = console.add()
    console.db._conn.execute("UPDATE events SET data='invalid unit-secret' WHERE seq=?", (seq,))
    assert console.page()["items"][0]["text"] == "card:184"


def test_long_board_fields_are_omitted_without_scanning(console):
    console.add(subject="x" * 16001)
    console.add(subject="x" * 16000, data={"title": "y" * 16000})
    result = console.page()
    assert all(item["text"] == "[omitted:too_long]" for item in result["items"])
    assert all(item["truncated"] and "too_long" in item["redacted"] for item in result["items"])
    assert max(map(len, console.state.redactions)) <= 16000


def test_board_text_is_redacted_before_4000_character_cut(console):
    console.add(subject="x" * 3990 + "unit-secret" + "y" * 100)
    item = console.page()["items"][0]
    assert item["truncated"] is True and len(item["text"]) <= 4000
    assert item["redacted"] == ["assignment"]
    assert "unit-secret" not in item["text"] and "[redacted:" not in item["text"]


@pytest.mark.parametrize("forward", [False, True])
def test_private_agent_forged_cursor_never_reads_tools_or_output(console, monkeypatch, forward):
    def forbidden(*args):
        pytest.fail("private source must not be read")

    monkeypatch.setattr(stream.store, "peek", forbidden)
    monkeypatch.setattr(stream.output, "latest_ids", forbidden)
    monkeypatch.setattr(stream.output, "recent", forbidden)
    private = replace(console.agent, own=False, recording=None)
    console.activity.rows = [tool(1)]
    seq = console.add()
    result = console.page(
        stream.encode_cursor(0, 0, 99, MAX_POSITION) if forward else None, selected=private
    )
    assert result["tools"] == "private" and result["output"] == []
    assert [i["id"] for i in result["items"]] == [f"e{seq}"]
    assert stream.decode_cursor(result["cursor"])[2:] == (0, 0)


def test_recording_off_keeps_existing_own_tool_rows(console):
    console.activity.rows = [tool(1)]
    result = console.page(selected=replace(console.agent, recording=False))
    assert result["tools"] == "off" and result["items"][0]["id"] == "t1"


def test_output_tail_always_refreshes_and_forward_skips_unchanged(console):
    console.state.head = (7, 8)
    console.state.recent = [{"id": "r7"}, {"id": "c8"}]
    tail = console.page()
    assert tail["output"] == console.state.recent
    assert stream.decode_cursor(tail["cursor"])[2:] == (7, 8)
    assert console.state.calls == ["peek", "latest", "recent"]
    console.state.calls.clear()
    forward = console.page(tail["cursor"])
    assert forward["output"] is None and console.state.calls == ["peek", "latest"]
    console.state.head = (9, 8)
    console.state.recent = [{"id": "r9"}]
    assert console.page(forward["cursor"])["output"] == [{"id": "r9"}]


def test_output_empty_tail_is_list_and_unchanged_forward_is_null(console):
    tail = console.page()
    assert tail["output"] == []
    assert console.page(tail["cursor"])["output"] is None
    assert console.state.calls.count("recent") == 1


def test_foreign_positions_can_only_move_within_authorized_sources(console):
    console.activity.rows = [tool(1), tool(100, cid="foreign")]
    console.add()
    result = console.page(stream.encode_cursor(MAX_POSITION, MAX_POSITION, 23, 24))
    assert result["items"] == [] and result["more"] is False
    assert result["output"] == []
    assert stream.decode_cursor(result["cursor"]) == (MAX_POSITION, MAX_POSITION, 0, 0)


@pytest.mark.parametrize("which", ["latest_ids", "recent"])
@pytest.mark.parametrize("forward", [False, True])
def test_output_contention_or_broken_pointer_returns_null_and_retries(
    console_hub, monkeypatch, which, forward
):
    h = console_hub
    activity = stream.store.open_activity(h.gateway, h.workspace)
    tool_seq = activity.append(h.a1, "task_list", "ok", 1)
    with h.db._lock:
        event_seq = h.db._conn.execute(
            "INSERT INTO events(at, workspace_id, kind, subject, actor, data) "
            "VALUES (?, ?, 'card.updated', '#184', 'conn:a1', '{}')",
            (AT, h.workspace),
        ).lastrowid

    def latest(*args):
        return 9, 10

    def recent(*args):
        return [{"id": "r9"}]

    monkeypatch.setattr(stream.output, "latest_ids", latest)
    monkeypatch.setattr(stream.output, "recent", recent)
    # Boards busy/locked, a broken pointer and EncryptionError all have this
    # real upstream contract. They do not raise out of latest_ids/recent.
    monkeypatch.setattr(stream.output, which, lambda *args: None)
    suffix = "&after=" + stream.encode_cursor(0, 0, 5, 6) if forward else ""
    code, result, _ = h.stream(suffix=suffix)
    assert code == 200
    assert result["output"] is None
    assert {item["id"] for item in result["items"]} == {f"t{tool_seq}", f"e{event_seq}"}
    assert stream.decode_cursor(result["cursor"])[2:] == ((5, 6) if forward else (0, 0))
    monkeypatch.setattr(stream.output, which, latest if which == "latest_ids" else recent)
    code, retry, _ = h.stream(suffix="&after=" + result["cursor"])
    assert code == 200
    assert retry["output"] == [{"id": "r9"}]
    assert stream.decode_cursor(retry["cursor"])[2:] == (9, 10)


def test_broken_pointer_during_activity_peek_also_preserves_output(console, monkeypatch):
    def broken(*args):
        raise ValueError("broken workspace pointer")

    monkeypatch.setattr(stream.store, "peek", broken)
    seq = console.add()
    result = console.page(stream.encode_cursor(3, 0, 5, 6))
    assert result["output"] is None
    assert [i["id"] for i in result["items"]] == [f"e{seq}"]
    assert stream.decode_cursor(result["cursor"]) == (3, seq, 5, 6)


def test_unrelated_output_database_failure_is_not_silenced(console, monkeypatch):
    def broken(*args):
        raise sqlite3.OperationalError("no such table: space_messages")

    monkeypatch.setattr(stream.output, "latest_ids", broken)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        console.page()


@pytest.mark.parametrize("forward", [False, True])
def test_items_budget_counts_utf8_and_pages_without_gaps(console, forward):
    seqs = [console.add(subject="😀" * 4000) for _ in range(40)]
    result = console.page(stream.encode_cursor(0, 0, 0, 0) if forward else None, limit=100)
    assert 0 < len(result["items"]) < 40 and result["more"] is True
    assert json_size(result["items"]) <= BUDGET
    assert json_size(result["items"] + [result["items"][0]]) > BUDGET
    if not forward:
        assert [i["id"] for i in result["items"]] == [
            f"e{s}" for s in seqs[-len(result["items"]) :]
        ]
        assert stream.decode_cursor(result["cursor"])[1] == seqs[-1]
        return
    seen = [i["id"] for i in result["items"]]
    for _ in range(40):
        if not result["more"]:
            break
        result = console.page(result["cursor"], limit=100)
        seen.extend(i["id"] for i in result["items"])
        assert json_size(result["items"]) <= BUDGET
    assert result["more"] is False and seen == [f"e{s}" for s in seqs]


def test_output_budget_drops_oldest_keeps_order_and_caps_20(console):
    console.state.head = (50, 0)
    console.state.recent = [{"id": f"r{s}", "text": "😀" * 2000} for s in range(50, 20, -1)]
    result = console.page()
    assert 0 < len(result["output"]) < 20
    assert result["output"] == console.state.recent[: len(result["output"])]
    assert json_size(result["output"]) <= BUDGET
    assert json_size(result["output"] + [console.state.recent[len(result["output"])]]) > BUDGET
    console.state.recent = [{"id": f"r{s}"} for s in range(50, 20, -1)]
    assert console.page()["output"] == console.state.recent[:20]


def test_items_and_output_have_separate_budgets_with_small_envelope(console):
    for _ in range(100):
        console.add(subject="😀" * 4000)
    console.state.recent = [{"id": f"r{s}", "text": "😀" * 2000} for s in range(20)]
    console.state.head = (20, 0)
    result = console.page(limit=100)
    assert json_size(result["items"]) <= BUDGET and json_size(result["output"]) <= BUDGET
    assert json_size(result) <= 2 * BUDGET + 1024


@pytest.mark.parametrize("backend", ["sqlite", "sqlcipher"])
@pytest.mark.parametrize("phase", ["open", "execute"])
@pytest.mark.parametrize("forward", [False, True])
def test_busy_activity_http_reads_degrade_and_retry(
    console_hub, monkeypatch, backend, phase, forward
):
    from mempalace.sqlite_crypto import driver

    h = console_hub
    if backend == "sqlcipher":
        pytest.importorskip("sqlcipher3")  # same convention as test_workspace_encryption
    module = driver() if backend == "sqlcipher" else sqlite3
    activity = stream.store.open_activity(h.gateway, h.workspace)
    seq = activity.append(h.a1, "task_list", "ok", 1)
    suffix = "&after=" + stream.encode_cursor(0, 0, 0, 0) if forward else ""
    actual = activity._conn

    class BusyConnection(module.Connection):
        __module__ = "sqlcipher3" if backend == "sqlcipher" else "sqlite3"

        def execute(self, sql, *args):
            raise module.OperationalError("database is busy")

    busy = module.connect(":memory:", factory=BusyConnection, check_same_thread=False)
    try:
        with monkeypatch.context() as patch:
            if phase == "open":
                patch.setattr(activity, "_conn", None)
                patch.setattr(stream.store, "connect", lambda *a, **kw: busy)
            else:
                patch.setattr(activity, "_conn", busy)
            code, result, _ = h.stream(suffix=suffix)
            assert code == 200
            assert result["items"] == []
            assert stream.decode_cursor(result["cursor"])[0] == 0
            code, listing, _ = h.get(h.owner, "/api/console")
            assert code == 200 and any(row["id"] == h.a1 for row in listing["agents"])
        code, retry, _ = h.stream(suffix="&after=" + result["cursor"])
        assert code == 200
        assert [item["id"] for item in retry["items"]] == [f"t{seq}"]
        assert activity._conn is actual
    finally:
        busy.close()


def test_board_actor_name_identifies_one_connection(console_hub):
    h = console_hub
    conn = h.hub.db._conn
    with h.hub.db._lock:
        row = dict(conn.execute("SELECT * FROM connections WHERE id=?", (h.a1,)).fetchone())
        clone = {**row, "id": row["id"] + "-clone", "token_hash": None}
        insert = "INSERT INTO connections ({}) VALUES ({})".format(
            ", ".join(clone), ", ".join("?" for _ in clone)
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, tuple(clone.values()))
        conn.execute("UPDATE connections SET revoked_at='2026-01-01T00:00:00Z' WHERE id=?", (h.a1,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, tuple(clone.values()))
        assert conn.execute("SELECT 1 FROM connections WHERE id=?", (h.a1,)).fetchone()
