"""Frozen HTTP acceptance contract: fourteen intentional pin failures."""

import base64
import dataclasses
import json
import sqlite3
import uuid
from urllib.parse import quote

from commonplace.policy import Policy
from tests._console_helpers import (
    CORPUS,
    console_hub_fixture,  # noqa: F401
    examples,
    optional,
    successful,
)
from tests.test_commonplace_boards import mcp, service


def tools(payload):
    return [item for item in payload["items"] if item["source"] == "tool"]


def test_signed_in_person_gets_console_agent_list(console_hub):
    h = console_hub
    assert h.get(h.owner, "/api/me")[0] == 200
    status, payload, _ = h.get(h.owner, "/api/connections")
    assert status == 200 and h.a1 in json.dumps(payload)
    status, payload, _ = h.get(h.owner, "/api/console")
    assert status == 200  # RED
    agent = next(a for a in payload["agents"] if a["id"] == h.a1)
    assert payload["role"] == "owner"
    assert agent["own"] is True and agent["recording"] is False and agent["can_toggle"] is True


def test_own_agent_tool_calls_stream_after_opt_in(console_hub):
    h = console_hub
    assert "task_list" in h.tool_names(h.t1)
    successful(h, h.t1, "task_list")
    assert h.toggle()[0] == 200  # RED
    secret = next(text for row, text, _ in examples({"cmmn_token"}))
    successful(h, h.t1, "task_list")
    mcp(h, h.t1, "task_list", board=secret)
    status, payload, raw = h.stream()
    assert status == 200 and payload["tools"] == "on"
    rows = tools(payload)
    assert len(rows) >= 2 and all(r["name"] == "task_list" for r in rows)
    assert secret.encode() not in raw
    assert all(set(r) == {"id", "at", "source", "name", "outcome", "ms"} for r in rows)


def test_recording_default_off_and_per_connection(console_hub):
    h = console_hub
    for token in (h.t1, h.t2):
        successful(h, token, "task_list")
    status, payload, _ = h.get(h.owner, "/api/console")
    assert status == 200  # RED
    assert next(a for a in payload["agents"] if a["id"] == h.a2)["recording"] is False
    assert h.toggle()[0] == 200
    for token in (h.t1, h.t2):
        successful(h, token, "task_list")
    assert tools(h.stream()[1])
    other = h.stream(h.a2)[1]
    assert other["tools"] == "off" and not tools(other)


def test_board_actions_display_redacted_never_raw(console_hub):
    h = console_hub
    rows = examples(
        {
            "cmmn_token",
            "cmmn_oauth",
            "session_cookie",
            "login_link",
            "push_topic",
            "github_token",
            "bearer",
            "json_secret",
            "random_token",
        }
    )
    title = "Synthetic examples " + " ; ".join(text for _, text, _ in rows)
    # Include JSON inside a JSON string, as stored in real card titles.
    lease = next(text for row, text, _ in rows if '"lease_token"' in row["template"])
    title += " ; " + json.dumps(lease)
    successful(h, h.t1, "task_add", board="work", title=title)
    status, events, _ = h.get(h.owner, "/api/events")
    assert status == 200
    created = next(
        e for e in events["events"] if e["kind"] == "card.created" and e["actor"] == "conn:a1"
    )
    assert created["data"]["title"] == title
    assert all(value in created["data"]["title"] for _, _, values in rows for value in values)
    status, payload, raw = h.stream()
    assert status == 200  # RED
    item = next(
        i for i in payload["items"] if i["source"] == "board" and i["kind"] == "card.created"
    )
    assert all(value.encode() not in raw for _, _, values in rows for value in values)
    expected = {row["kind"] for row, _, _ in rows}
    assert expected <= set(item["redacted"])
    assert all(f"[redacted:{kind}]" in item["text"] for kind in expected)


def test_tool_activity_is_redacted_before_storage(console_hub):
    h = console_hub
    name = next(text for _, text, _ in examples({"cmmn_token"}))
    arg = next(text for _, text, _ in examples({"cmmn_oauth"}))
    response = h.rpc(h.t1, "tools/call", {"name": name, "arguments": {}})
    assert response["error"]["code"] == -32601 and name in response["error"]["message"]
    assert h.toggle()[0] == 200  # RED
    h.rpc(h.t1, "tools/call", {"name": name, "arguments": {}})
    mcp(h, h.t1, "task_list", board=arg)
    status, payload, raw = h.stream()
    assert status == 200
    assert any(i["name"] == "unknown" and i["outcome"] == "denied" for i in tools(payload))
    assert name.encode() not in raw and arg.encode() not in raw
    files = h.activity_files()
    assert files
    for path in files:
        data = path.read_bytes()
        assert name.encode() not in data and arg.encode() not in data
    db_path = next(p for p in files if p.name == "console.sqlite3")
    with sqlite3.connect(db_path) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(console_activity)")}
        assert columns == {"seq", "at", "connection_id", "name", "outcome", "ms"}
        for row in db.execute("SELECT * FROM console_activity"):
            assert name not in repr(row) and arg not in repr(row)


def test_people_see_only_what_they_own(console_hub):
    h = console_hub
    assert h.get(h.member, "/api/connections")[0] == 403
    assert h.get(h.owner, "/api/connections")[0] == 200
    status, payload, _ = h.get(h.member, "/api/console")
    assert status == 200  # RED
    assert [a["id"] for a in payload["agents"]] == [h.m1]
    denied = h.stream(h.a1, h.member)
    unknown = h.stream("conn_0000000000000000", h.member)
    assert denied[0] == unknown[0] == 404 and denied[2] == unknown[2]
    assert h.toggle(h.m1, h.member)[0] == 200
    successful(h, h.tm, "task_list")
    successful(h, h.tm, "task_add", board="work", title="Member card")
    member_status, member_stream, _ = h.stream(h.m1, h.member)
    assert member_status == 200 and tools(member_stream)
    status, private, _ = h.stream(h.m1)
    assert status == 200 and private["tools"] == "private" and private["output"] == []
    assert not tools(private) and any(i["source"] == "board" for i in private["items"])
    agents = h.get(h.owner, "/api/console")[1]["agents"]
    assert next(a for a in agents if a["id"] == h.m1)["recording"] is None
    assert h.toggle(h.m1)[0] == 403
    assert h.stream(h.m1, h.member)[1]["tools"] == "on"
    positions = json.loads(base64.urlsafe_b64decode(member_stream["cursor"] + "==="))
    positions.update(a=0, r=0, c=0)
    forged = base64.urlsafe_b64encode(json.dumps(positions).encode()).decode().rstrip("=")
    private = h.stream(h.m1, suffix="&after=" + quote(forged))[1]
    assert not tools(private) and private["output"] == []
    assert h.stream(h.a1, h.member, "&after=" + quote(member_stream["cursor"]))[0] == 404


def test_agent_tokens_cannot_read_console(console_hub):
    h = console_hub
    token = h.owner_token("reader")
    assert h.get(token, "/api/events")[0] == 200
    assert h.get(token, "/api/console")[0] == 403  # RED
    assert h.get(token, "/api/console/stream")[0] == 403
    assert h.post(token, "/api/console/agents/" + h.a1, {"record": True})[0] == 403


def test_other_workspace_agents_are_not_found(console_hub):
    h = console_hub
    status, me, _ = h.get(h.other, "/api/me")
    assert status == 200 and me["workspace"] == h.other_workspace
    status, payload, _ = h.get(h.other, "/api/console")
    assert status == 200  # RED
    assert not payload["agents"]
    other = h.stream(h.a1, h.other)
    unknown = h.stream("conn_0000000000000000", h.other)
    assert other[0] == unknown[0] == 404 and other[2] == unknown[2]
    assert h.toggle(h.a1, h.other)[0] == 404


def test_stream_bounds_and_cursor_resume(console_hub):
    h = console_hub
    for _ in range(150):
        successful(h, h.t1, "task_list")
    assert h.toggle()[0] == 200  # RED
    status, initial, _ = h.stream()
    assert status == 200
    for _ in range(150):
        successful(h, h.t1, "task_list")
    status, first, raw = h.stream(suffix="&after=" + quote(initial["cursor"]))
    assert status == 200 and len(first["items"]) == 100 and first["more"] is True
    assert len(raw) <= 256 * 1024 + 1024
    status, second, _ = h.stream(suffix="&after=" + quote(first["cursor"]))
    assert status == 200 and len(second["items"]) == 50 and second["more"] is False
    ids = [int(i["id"][1:]) for i in first["items"] + second["items"]]
    assert len(set(ids)) == 150 and ids == list(range(ids[0], ids[0] + 150))
    assert h.stream(suffix="&after=!!")[0] == 400
    assert h.stream(suffix="&limit=101")[0] == 400
    old = (
        base64.urlsafe_b64encode(json.dumps({"v": 2, "a": 0, "e": 0}).encode()).decode().rstrip("=")
    )
    assert h.stream(suffix="&after=" + quote(old))[0] == 400


def test_redactor_covers_every_secret_category():
    corpus = json.loads(CORPUS.read_text())
    generated = examples()
    assert generated and all(values for _, _, values in generated)
    redactor = optional("commonplace.redact")
    assert redactor is not None  # RED
    expected = {row["kind"] for row in corpus["templates"]} | {"too_long"}
    assert expected <= set(redactor.KINDS) and redactor.MAX_CHARS == 16_000
    for row, text, values in generated:
        for context in row["contexts"]:
            if context == "object":
                clean, kinds = redactor.redact_value({"api_token": text})
                rendered = json.dumps(clean)
            else:
                wrapped = {
                    "prose": text,
                    "json": json.dumps({"message": text}),
                    "json_in_string": json.dumps({"message": json.dumps({"message": text})}),
                }[context]
                rendered, kinds = redactor.redact(wrapped)
            assert all(value not in rendered for value in values)
            assert row["kind"] in kinds and f"[redacted:{row['kind']}]" in rendered
    for text in corpus["negative_controls"]:
        assert redactor.redact(text) == (text, [])
    assert redactor.redact("x" * 1_000_000) == ("[omitted:too_long]", ["too_long"])
    assert isinstance(redactor.redact("a." * 8000)[0], str)
    assert isinstance(redactor.redact("-----BEGIN PRIVATE KEY-----\n" * 300)[0], str)


def test_own_agent_output_is_shown_redacted(console_hub):
    h = console_hub
    rows = examples({"cmmn_token", "json_secret", "cmmn_oauth", "random_token"})
    selected = [
        next(r for r in rows if r[0]["kind"] == kind)
        for kind in ("cmmn_token", "json_secret", "cmmn_oauth", "random_token")
    ]
    reply = " ; ".join(r[1] for r in selected[:2])
    comment = " ; ".join(r[1] for r in selected[2:])
    successful(h, h.t1, "space_post", board="work", text=reply)
    card = successful(h, h.t1, "task_add", board="work", title="Output card")["number"]
    successful(h, h.t1, "task_comment", task=card, text=comment)
    owner = service(h, h.person(h.admin))
    assert owner.space_read("work", last=1)["messages"][0]["text"] == reply
    assert any(e["text"] == comment for e in owner.task_show(card)["events"])
    status, payload, raw = h.stream()
    assert status == 200  # RED
    output = payload["output"]
    assert len(output) == 2 and output[0]["kind"] == "comment" and output[1]["kind"] == "reply"
    assert output[0]["card"] == card and output[1]["card"] is None
    assert all(o["board"] == "work" for o in output)
    assert all(
        set(o) == {"id", "at", "source", "kind", "board", "card", "text", "truncated", "redacted"}
        for o in output
    )
    assert all(value.encode() not in raw for _, _, values in selected for value in values)
    expected = {row["kind"] for row, _, _ in selected}
    assert expected <= {kind for o in output for kind in o["redacted"]}
    assert all(f"[redacted:{kind}]".encode() in raw for kind in expected)


def test_output_is_newest_twenty_newest_first(console_hub):
    h = console_hub
    for n in range(1, 26):
        successful(h, h.t1, "space_post", board="work", text=f"reply {n:02}")
    assert len(service(h, h.person(h.admin)).space_read("work", last=25)["messages"]) == 25
    status, payload, _ = h.stream()
    assert status == 200  # RED
    output = payload["output"]
    assert [o["text"] for o in output] == [f"reply {n:02}" for n in range(25, 5, -1)]
    ids = [int(o["id"][1:]) for o in output]
    assert all(a > b for a, b in zip(ids, ids[1:]))
    forward = h.stream(suffix="&after=" + quote(payload["cursor"]))
    assert forward[0] == 200 and forward[1]["output"] is None
    successful(h, h.t1, "space_post", board="work", text="reply 26")
    updated = h.stream(suffix="&after=" + quote(forward[1]["cursor"]))
    assert updated[0] == 200 and updated[1]["output"][0]["text"] == "reply 26"


def test_other_peoples_agent_output_is_not_shown(console_hub):
    h = console_hub
    member_text = "M1-OUT-" + uuid.uuid4().hex
    own_text = "A1-OUT-" + uuid.uuid4().hex
    card = successful(h, h.tm, "task_add", board="work", title="Member output")["number"]
    successful(h, h.tm, "task_comment", task=card, text=member_text)
    assert any(
        e["text"] == member_text for e in service(h, h.person(h.admin)).task_show(card)["events"]
    )
    successful(h, h.t1, "space_post", board="work", text=own_text)
    status, payload, raw = h.stream(h.m1)
    assert status == 200  # RED
    assert payload["output"] == [] and payload["tools"] == "private" and b"M1-OUT-" not in raw
    status, member, _ = h.stream(h.m1, h.member)
    assert status == 200 and member_text in member["output"][0]["text"]
    denied = h.stream(h.a1, h.member)
    unknown = h.stream("conn_0000000000000000", h.member)
    assert (
        denied[0] == unknown[0] == 404 and denied[2] == unknown[2] and b"A1-OUT-" not in denied[2]
    )


def test_output_only_from_boards_the_agent_can_read(console_hub):
    h = console_hub
    owner = service(h, h.person(h.admin))
    owner.board_add(slug="side", wing="side")
    successful(h, h.t1, "space_post", board="work", text="WORK-MARK")
    expanded = dataclasses.replace(
        h.principal(h.t1),
        policy=Policy.build(
            profile="full",
            scopes=["memory:read", "memory:write"],
            read_wings=["shared", "side"],
            write_wings=["shared", "side"],
            trust="standard",
        ),
    )
    assert service(h, expanded).space_post("side", "SIDE-MARK")["stamp_connection"] == "a1"
    assert owner.space_read("side", last=1)["messages"][0]["text"] == "SIDE-MARK"
    assert not mcp(h, h.t1, "space_read", board="side")[0]
    status, payload, raw = h.stream()
    assert status == 200  # RED
    assert any(o["text"] == "WORK-MARK" for o in payload["output"])
    assert b"SIDE-MARK" not in raw
