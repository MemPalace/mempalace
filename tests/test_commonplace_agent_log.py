"""Live capture of any agent through the generic agent-log source."""

import hashlib
import io
import json
from datetime import timedelta

import pytest
from test_commonplace_capture import connection, enable, saved, send
from test_commonplace_import import api, surface as surface

from commonplace import agent_log, capture, capture_client, plans, sessions
from commonplace.handoff.transcripts import parse
from commonplace.policy import Policy

WORDS = "  café 🦉\r\nKeep every word.\t"
AT = "2026-11-11T09:30:00Z"


def entry(text=WORDS, *, agent="gemini-cli", session="s-42", role="user", **extra):
    record = {"agent": agent, "session_id": session, "role": role, "content": text, "timestamp": AT}
    return json.dumps({**record, **extra}, ensure_ascii=False) + "\n"


def log_send(s, bearer, raw, *, agent="gemini-cli", session="s-42", **kwargs):
    kwargs.setdefault("identity", f"{agent}:{session}")
    return send(s, bearer, raw, source=agent_log.SOURCE, **kwargs)


def test_new_agent_chat_is_captured_verbatim_with_agent_and_session_provenance(surface):
    s = surface
    bearer = connection(s, agent_log.SOURCE)
    enable(s)
    first = entry() + entry("Second  answer", role="assistant")
    status, result = log_send(s, bearer, first)
    assert status == 200 and result["status"] == "appended" and result["added_messages"] == 2
    rows = saved(s, result["stored_thread_id"])
    assert rows[0]["raw"] == first and rows[0]["name"] == capture.RAW_NAME
    assert [(m["role"], m["text"], m["at"]) for m in rows[1:]] == [
        ("user", WORDS, AT),
        ("assistant", "Second  answer", AT),
    ]
    assert rows[1]["raw"] == entry() and '"agent": "gemini-cli"' in rows[1]["raw"]
    record = api(s, "GET", f"/api/threads/{result['stored_thread_id']}")[1]
    assert record["wing"] == "private_chats"
    assert record["created_by"].startswith("conn:native-")
    assert ":agent-log:gemini-cli:s-42" in record["source_thread_id"]


def test_retry_is_idempotent_and_growth_appends_only_the_suffix(surface, monkeypatch):
    s = surface
    now = plans._now()
    monkeypatch.setattr(plans, "_now", lambda: now)
    bearer = connection(s, agent_log.SOURCE)
    enable(s)
    first = entry()
    _, result = log_send(s, bearer, first)
    stored = result["stored_thread_id"]
    before = saved(s, stored)
    assert log_send(s, bearer, first)[1]["status"] == "duplicate"
    assert saved(s, stored) == before
    suffix = entry("later", role="assistant")
    now += timedelta(seconds=901)
    status, grown = log_send(
        s, bearer, suffix, offset=result["offset"], prefix=result["prefix_sha256"]
    )
    assert status == 200 and grown["stored_thread_id"] == stored
    assert grown["prefix_sha256"] == hashlib.sha256((first + suffix).encode()).hexdigest()
    assert log_send(s, bearer, first)[1]["status"] == "duplicate"  # Stale retry.
    assert saved(s, stored)[: len(before)] == before
    assert log_send(s, bearer, entry("edited"))[0] == 409
    assert len([m for m in saved(s, stored) if m["name"] == capture.RAW_NAME]) == 2


def test_a_private_agent_chat_is_invisible_to_a_non_reader(surface):
    s = surface
    bearer = connection(s, agent_log.SOURCE)
    enable(s)
    stored = log_send(s, bearer, entry("sentinel-private-words"))[1]["stored_thread_id"]
    _, reader = s.db.create_token(
        workspace_ref=s.workspace,
        user_ref=s.user,
        name="reader",
        client="generic",
        policy=Policy.build(
            profile="standard", trust="standard", read_wings=["threads", "private_chats"]
        ),
        actor=s.user,
    )
    # Neither the capture connection itself nor a reader token may read it back.
    for token in (bearer, reader):
        assert api(s, "GET", f"/api/threads/{stored}", cookie=False, token=token)[0] == 404
        assert api(s, "GET", f"/api/threads/{stored}/messages", cookie=False, token=token)[0] == 404
    other = s.db.create_user("other-agent-log@example.com")
    workspace = s.db.create_workspace("isolated-agent-log", other, personal=True)
    cookie = sessions.start_session(s.db, other, workspace)
    assert (
        api(s, "GET", f"/api/threads/{stored}", headers={"Cookie": f"{sessions.COOKIE}={cookie}"})[
            0
        ]
        == 404
    )
    assert api(s, "GET", f"/api/threads/{stored}")[0] == 200  # The filing owner still can.


def test_two_agents_and_two_sessions_stay_separate_streams(surface):
    s = surface
    bearer = connection(s, agent_log.SOURCE)
    enable(s)
    ids = {
        log_send(s, bearer, entry(agent=agent, session=session), agent=agent, session=session)[1][
            "stored_thread_id"
        ]
        for agent, session in (("gemini-cli", "s-1"), ("gemini-cli", "s-2"), ("opencode", "s-1"))
    }
    assert len(ids) == 3


@pytest.mark.parametrize(
    "agent,session,raw",
    [
        ("claude-code", "s-1", entry(agent="claude-code", session="s-1")),  # reserved name
        ("Gemini", "s-1", entry(agent="Gemini", session="s-1")),  # not a slug
        ("gemini-cli", "s-1", entry(agent="gemini-cli", session="other")),  # wrong session
        ("gemini-cli", "s-1", entry(agent="opencode", session="s-1")),  # wrong agent
        ("gemini-cli", "s-1", entry(session="s-1") + '{"role":"user"}\n'),  # no provenance
    ],
)
def test_records_must_carry_the_provenance_of_their_stream(surface, agent, session, raw):
    s = surface
    bearer = connection(s, agent_log.SOURCE)
    enable(s)
    assert log_send(s, bearer, raw, agent=agent, session=session)[0] == 400
    store = s.gateway.handoff.store_for(s.workspace)
    assert store.list_source_range(s.workspace, "capture:", limit=10) == []


def test_only_the_matching_connection_client_may_use_the_source(surface):
    s = surface
    enable(s)
    assert log_send(s, connection(s, "claude-code"), entry())[0] == 403
    assert send(s, connection(s, agent_log.SOURCE), entry())[0] == 403  # wrong source for client
    assert log_send(s, connection(s, agent_log.SOURCE, scopes=["memory:read"]), entry())[0] == 403
    assert log_send(s, connection(s, agent_log.SOURCE), entry())[0] == 200


def test_unicode_line_separators_inside_a_message_do_not_split_the_record(surface):
    s = surface
    bearer = connection(s, agent_log.SOURCE)
    enable(s)
    words = "a\u2028b\u2029c\x85d"
    raw = entry(words)
    assert words in raw  # json.dumps(ensure_ascii=False) keeps them raw
    status, result = log_send(s, bearer, raw)
    assert status == 200 and result["status"] == "appended"
    rows = saved(s, result["stored_thread_id"])
    assert rows[0]["raw"] == raw and rows[1]["text"] == words
    deep = '{"agent":"gemini-cli","session_id":"s-42","role":"user","content":' + "[" * 5000 + "\n"
    assert log_send(s, bearer, deep, session="s-42")[0] == 400  # not a 500


def test_capture_stays_off_until_the_owner_opts_in(surface):
    s = surface
    bearer = connection(s, agent_log.SOURCE)
    assert log_send(s, bearer, entry())[0] == 409
    assert not s.gateway.handoff.has_threads(s.workspace)


def test_native_format_parses_without_loss_and_requires_provenance():
    raw = entry() + entry(
        None, role="assistant", tool_calls=[{"id": "c1", "function": {"name": "f"}}]
    )
    messages = parse(raw, agent_log.SOURCE)
    assert [m.kind for m in messages] == ["message", "tool_call"]
    assert messages[0].text == WORDS and messages[0].raw == entry() and messages[0].at == AT
    with pytest.raises(ValueError):
        parse('{"role":"user","content":"x"}\n', agent_log.SOURCE)


def test_client_identity_comes_from_the_log_and_refuses_files_without_provenance():
    assert capture_client.native_id(io.BytesIO(entry().encode()), agent_log.SOURCE) == (
        "gemini-cli:s-42"
    )
    for raw in (b"", b'{"role":"user","content":"x"}\n', entry(agent="codex").encode()):
        with pytest.raises(capture_client.CaptureError):
            capture_client.native_id(io.BytesIO(raw), agent_log.SOURCE)
