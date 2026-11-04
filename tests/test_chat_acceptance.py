"""Frozen A1–A12 and A17 contracts; all first feature checks are pin RED."""

import json
import os
from datetime import datetime, timezone

import pytest

from commonplace import events
from tests._chat_helpers import (
    chat_environment,  # noqa: F401
    chat_hub_fixture,  # noqa: F401
    no_outbound,
    optional,
)
from tests._console_helpers import examples
from tests.test_commonplace_boards import service


def test_chat_landing_lists_projects_and_orchestrator(chat_hub):
    h = chat_hub
    status, body, _ = h.get(h.owner, "/api/chat")
    assert status == 200  # RED
    work = next(p for p in body["projects"] if p["slug"] == "work")
    assert work["orchestrator"] is None
    assert body["front"]["provider"] == "none"
    h.set_coordinator("a1")
    body = h.get(h.owner, "/api/chat")[1]
    assert next(p for p in body["projects"] if p["slug"] == "work")["orchestrator"]["name"] == "a1"


def test_person_post_gets_instant_receipt_without_model(chat_hub, monkeypatch):
    h = chat_hub
    calls = h.fake_model(monkeypatch)
    message = h.post(h.owner, "hello")
    status, body, _ = h.chat(h.owner)
    assert status == 200  # RED
    assert len(body["replies"]) == 1
    reply = body["replies"][0]
    assert (reply["kind"], reply["message_id"]) == ("receipt", message)
    assert "within" not in reply["text"]
    h.drain()
    assert calls == []
    assert h.handoffs() == []


@pytest.mark.parametrize("state", ["none", "no_wake", "wake", "gone"])
def test_receipt_codes_follow_orchestrator_state(chat_hub, state):
    h = chat_hub
    if state != "none":
        h.set_coordinator("a1")
    if state == "wake":
        h.subscribe(h.t1)
    if state == "gone":
        h.db.revoke(h.a1, actor=h.admin)
    h.post(h.owner, "hello")
    status, body, _ = h.chat(h.owner)
    assert status == 200  # RED
    assert (
        body["replies"][0]["code"]
        == {
            "none": "no_orchestrator",
            "no_wake": "forwarded_no_wake",
            "wake": "forwarded",
            "gone": "orchestrator_gone",
        }[state]
    )


def test_message_reaches_orchestrator_verbatim(chat_hub):
    h = chat_hub
    h.set_coordinator("a1")
    h.subscribe(h.t1)
    before = len(h.space()["messages"])
    text = "Please fix the login bug\nKeep every word: café —  exact spacing."
    seq = h.seq()
    message = h.post(h.owner, text)
    page = service(h, h.principal(h.t1)).space_read("work", after=message - 1)
    assert page["messages"][0]["text"] == text
    assert len(h.space()["messages"]) == before + 1
    emitted = events.after(h.db, h.workspace, seq, h.seq())
    assert any(e["kind"] == "space_message" and e["data"]["message_id"] == message for e in emitted)
    status, body, _ = h.chat(h.owner)
    assert status == 200  # RED
    assert body["messages"][-1]["text"].encode() == text.encode()


def test_model_answer_uses_workspace_front_setting(chat_hub, monkeypatch):
    h = chat_hub
    calls = h.fake_model(monkeypatch)
    assert h.configure()[0] == 200  # RED
    message = h.post(h.owner, "Please explain the open cards")
    assert h.drain() == 1
    assert len(calls) == 1 and calls[0]["model"] == "fast-model" and calls[0]["key"] is None
    answer = next(r for r in h.chat(h.owner)[1]["replies"] if r["kind"] == "answer")
    assert (answer["message_id"], answer["model"], answer["code"]) == (
        message,
        "openai_compatible/fast-model",
        "ok",
    )


def test_default_is_basic_mode_without_network(chat_hub, monkeypatch):
    h = chat_hub
    status, body, _ = h.settings(h.owner)
    assert status == 200  # RED
    assert body["front"]["provider"] == "none" and body["front"]["key_set"] is False
    connects = no_outbound(monkeypatch)
    h.post(h.owner, "Please fix the login bug")
    h.drain(now=h.late())
    assert connects == []
    assert h.rows("chat_front_jobs") == []


def test_settings_owner_only_and_key_policy(chat_hub, monkeypatch):
    h = chat_hub
    anthropic = {"front": {"provider": "anthropic", "key_ref": "env:COMMONPLACE_CHAT_KEY_TEST"}}
    assert h.settings(h.member, anthropic)[0] == 403  # RED
    assert h.settings(h.t1, anthropic)[0] == 403
    assert h.settings(h.owner, anthropic)[0] == 403
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_WORKSPACES", h.workspace)
    status, body, raw = h.settings(h.owner, anthropic)
    assert status == 200
    assert body["front"]["key_set"] is True
    assert body["front"]["model"] == "claude-haiku-5-5"
    for data in (raw, h.settings(h.member)[2]):
        assert b"env:" not in data and os.environ["COMMONPLACE_CHAT_KEY_TEST"].encode() not in data
    assert (
        h.settings(h.owner, {"front": {**anthropic["front"], "base_url": "https://example.test"}})[
            0
        ]
        == 400
    )
    assert h.configure(key_ref="env:COMMONPLACE_CHAT_KEY_TEST")[0] == 403
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_HOSTS", "127.0.0.1")
    assert h.configure(key_ref="env:COMMONPLACE_CHAT_KEY_TEST")[0] == 200
    assert h.configure(base_url=None)[0] == 400
    assert h.configure(base_url="http://127.0.0.1:9?x=1")[0] == 400
    raw_key = "sk-ant-" + os.environ["COMMONPLACE_CHAT_KEY_TEST"]
    status, _, raw = h.configure(key_ref=raw_key)
    assert status == 400 and raw_key.encode() not in raw
    assert h.configure(unknown="bad")[0] == 400
    assert h.configure(daily_limit=1, user_daily_limit=2)[0] == 400
    calls = h.fake_model(monkeypatch)
    monkeypatch.delenv("COMMONPLACE_CHAT_KEY_WORKSPACES")
    h.post(h.owner, "Please check this")
    assert h.drain() == 1
    assert calls == []
    assert (h.rows("chat_front_jobs")[-1]["state"], h.rows("chat_front_jobs")[-1]["code"]) == (
        "skipped",
        "key_not_allowed",
    )


def test_secrets_redacted_before_model_and_display(chat_hub, monkeypatch):
    h = chat_hub
    h.configure()
    calls = h.fake_model(monkeypatch, reply=lambda system, turns: system + json.dumps(turns))
    corpus = examples({"cmmn_oauth", "anthropic"})
    assert corpus
    secret_text = "\n".join(text for _, text, _ in corpus)
    values = [v for _, _, generated in corpus for v in generated]
    service(h, h.person(h.admin)).task_add(board="work", title=secret_text[:200])
    h.post(h.owner, secret_text)
    status, _, _ = h.chat(h.owner)
    assert status == 200  # RED
    h.drain()
    assert len(calls) == 1
    body = h.chat(h.owner)
    for raw in (json.dumps(calls).encode(), body[2], h.status(h.owner)[2]):
        for value in values:
            assert value.encode() not in raw
    assert next(r for r in body[1]["replies"] if r["kind"] == "answer")["redacted"]


def test_replies_are_private_to_poster(chat_hub):
    h = chat_hub
    owner_message = h.post(h.owner, "hello")
    member_message = h.post(h.member, "hello")
    status, body, _ = h.chat(h.member)
    assert status == 200  # RED
    assert {r["message_id"] for r in body["replies"]} == {member_message}
    assert {r["message_id"] for r in h.chat(h.owner)[1]["replies"]} == {owner_message}
    assert h.chat(h.other)[0] == 404
    cursor = body["reply_cursor"]
    newer = h.post(h.member, "thanks")
    page = h.chat(h.member, after=member_message, replies_after=cursor)[1]
    assert {r["message_id"] for r in page["replies"]} == {newer}
    assert all(r["id"] > cursor for r in page["replies"])


def test_status_reuses_console_authority(chat_hub):
    h = chat_hub
    h.set_coordinator("a1")
    status, body, _ = h.status(h.owner)
    assert status == 200  # RED
    from commonplace.console import authority

    view = authority.agent(h.gateway, h.person(h.admin), h.a1)
    assert body["orchestrator"]["visible"] is True
    assert body["orchestrator"]["status"] == authority.status(
        view.last_used_at, datetime.now(timezone.utc)
    )
    assert body["orchestrator"]["console"] == "#/console"
    assert "activity" not in body
    private = h.status(h.member)[1]["orchestrator"]
    assert private["visible"] is False and private["status"] is None and private["console"] is None


def test_limits_and_failures_keep_receipt(chat_hub, monkeypatch):
    h = chat_hub
    h.fake_model(monkeypatch)
    assert h.configure(daily_limit=2, user_daily_limit=1)[0] == 200  # RED
    for auth, expected in (
        (h.owner, ("done", "ok")),
        (h.owner, ("skipped", "user_daily_limit")),
        (h.member, ("done", "ok")),
        (h.member, ("skipped", "daily_limit")),
    ):
        h.post(auth, "Please check the board")
        assert h.drain() == 1
        job = h.rows("chat_front_jobs")[-1]
        assert (job["state"], job["code"]) == expected
    assert h.configure(daily_limit=10, user_daily_limit=10)[0] == 200
    types = optional("commonplace.chat.types")
    h.fake_model(monkeypatch, raises=types.ModelError("timeout"))
    message = h.post(h.owner, "Please check again")
    assert h.drain() == 1
    replies = [r for r in h.chat(h.owner)[1]["replies"] if r["message_id"] == message]
    assert [r["kind"] for r in replies] == ["receipt"]
    assert h.settings(h.owner)[1]["usage"]["last_error"] == "timeout"


def test_agent_tokens_refused_on_chat_routes(chat_hub):
    h = chat_hub
    for path in ("/api/chat", "/api/chat/work", "/api/chat/work/status", "/api/chat-settings"):
        assert h.get(h.t1, path)[0] == 403  # RED


def test_two_pending_posts_supersede_older(chat_hub, monkeypatch):
    h = chat_hub
    calls = h.fake_model(monkeypatch)
    assert h.configure()[0] == 200  # RED
    p1 = h.post(h.owner, "First request")
    p2 = h.post(h.owner, "Second request")
    assert h.drain() == 2
    jobs = h.rows("chat_front_jobs")
    assert (jobs[0]["message_id"], jobs[0]["state"]) == (p1, "superseded")
    assert (jobs[1]["message_id"], jobs[1]["state"], jobs[1]["code"]) == (p2, "done", "ok")
    assert len(calls) == 1
    assert [r["message_id"] for r in h.chat(h.owner)[1]["replies"] if r["kind"] == "answer"] == [p2]
