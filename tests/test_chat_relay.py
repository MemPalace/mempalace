"""B1, B3–B8, B11–B13: private answers from the poster's own runner."""

import json
import time

import pytest

from tests._console_helpers import examples
from tests._relay_helpers import relay_hub_fixture  # noqa: F401


def test_runner_answers_chat_without_server_key(relay_hub, monkeypatch):
    h = relay_hub
    calls = h.fake_model(monkeypatch)
    attempts = h.no_outbound(monkeypatch)
    m = h.machine()
    h.set_main_model("claude", "claude-haiku-5-5")
    assert h.poll(m)[0] == 204  # RED
    message = h.post(h.owner, "What is open on this board?")
    status, body = h.poll(m)
    assert status == 200
    assert body["cli"] == "claude" and body["model"] == "claude-haiku-5-5"
    assert "What is open on this board?" in body["prompt"]
    from commonplace.chat.worker import FRONT_SYSTEM

    assert FRONT_SYSTEM.splitlines()[0] in body["system"]
    assert h.answer(m, body["job"], body["lease_token"], "Two cards are open.")[0] == 200
    replies = [r for r in h.chat(h.owner)[1]["replies"] if r["message_id"] == message]
    assert [r["kind"] for r in replies] == ["receipt", "answer"]
    assert replies[0]["id"] < replies[1]["id"]
    assert replies[1]["text"] == "Two cards are open."
    assert replies[1]["model"] == "runner:claude/claude-haiku-5-5"
    front = h.settings(h.owner)[1]["front"]
    assert front["provider"] == "none" and front["key_set"] is False
    assert calls == [] and attempts == []


@pytest.mark.parametrize("case", ["stale", "member", "other_cli"])
def test_basic_receipt_when_no_eligible_runner(relay_hub, case):
    h = relay_hub
    m = h.machine()
    h.set_main_model("claude", "claude-haiku-5-5")
    assert h.poll(m)[0] == 204  # RED
    if case == "stale":
        h.age_seen(m, 91)
    elif case == "other_cli":
        assert h.poll(m, ("codex",))[0] == 204
    auth = h.member if case == "member" else h.owner
    message = h.post(auth, "Please check the open work.")
    assert h.jobs() == []
    replies = [r for r in h.chat(auth)[1]["replies"] if r["message_id"] == message]
    assert len(replies) == 1 and replies[0]["kind"] == "receipt"
    if case != "stale":
        assert h.poll(m, ("codex",) if case == "other_cli" else ("claude",))[0] == 204


def test_several_runners_one_answer(relay_hub):
    h = relay_hub
    m1 = h.machine(label="relay-1")
    m2 = h.machine(label="relay-2")
    assert h.poll(m1)[0] == 204  # RED
    assert h.poll(m2)[0] == 204
    message = h.post(h.owner, "Answer this once.")
    status, body = h.poll(m1)
    assert status == 200
    assert h.poll(m2)[0] == 204
    job, token = body["job"], body["lease_token"]
    assert h.answer(m2, job, token, "wrong machine")[0] == 409
    assert h.answer(m1, job, "wrong-token", "wrong token")[0] == 409
    assert h.answer(m1, job, token, "One answer.")[0] == 200
    assert h.answer(m1, job, token, "Repeated answer.")[0] == 409
    replies = h.chat(h.owner)[1]["replies"]
    assert len([r for r in replies if r["message_id"] == message and r["kind"] == "answer"]) == 1


@pytest.mark.parametrize("case", ["fail", "expire"])
def test_relay_lifecycle(relay_hub, case):
    h = relay_hub
    m = h.machine()
    assert h.poll(m)[0] == 204  # RED
    h.post(h.owner, "Please answer.")
    status, body = h.poll(m)
    assert status == 200
    job, token = body["job"], body["lease_token"]
    if case == "fail":
        assert h.fail(m, job, token, "cli_timeout")[0] == 200
        assert (h.jobs()[0]["state"], h.jobs()[0]["code"]) == ("failed", "cli_timeout")
        assert h.status(h.owner)[1]["front"]["runner"]["last_error"] == "cli_timeout"
    else:
        relay = h.relay()
        relay.sweep(h.db, now=time.time() + relay.CHAT_LEASE_S + 1)
        assert (h.jobs()[0]["state"], h.jobs()[0]["code"]) == ("failed", "lease_expired")
        assert h.answer(m, job, token, "late")[0] == 409
        h.post(h.owner, "A second question.")
        assert len(h.jobs()) == 2 and h.jobs()[1]["state"] == "queued"
        relay.sweep(h.db, now=time.time() + relay.CHAT_WAIT_S + 1)
        assert (h.jobs()[1]["state"], h.jobs()[1]["code"]) == ("expired", "no_runner")
    replies = h.chat(h.owner)[1]["replies"]
    assert replies and all(r["kind"] == "receipt" for r in replies)


@pytest.mark.parametrize("action", ["lease", "answer", "fail"])
def test_relay_routes_need_the_machine_credential(relay_hub, action):
    h = relay_hub
    m = h.machine()
    assert h.poll(m)[0] == 204  # RED
    body = {"clis": ["claude"]}
    if action != "lease":
        h.post(h.owner, "Only my own machine may answer.")
        status, lease = h.poll(m)
        assert status == 200
        body = {key: lease[key] for key in ("job", "lease_token")}
        body.update(
            {"text": "Answer", "cli": "claude", "model": "example-model"}
            if action == "answer"
            else {"code": "bad_output"}
        )
    path = "/api/runner/chat/" + action
    for auth in (m["worker_token"], h.a1):
        assert h.request(auth, "POST", path, body)[0] == 404
    assert h.request(h.owner, "POST", path, body)[0] in (403, 404)
    assert h.request(None, "POST", path, body)[0] == 401

    def raw(payload, media="application/json"):
        return h._request_bytes(m["machine_token"], "POST", path, payload, media)[0]

    assert raw(b"{}", "text/plain") == 415
    assert raw(b" " * 20_481) == 413
    malformed = [{**body, "unknown": True}, {}, {**body, next(iter(body)): []}]
    if action == "lease":
        malformed.append({"clis": ["bad label"]})
    for value in malformed:
        assert raw(json.dumps(value).encode()) == 400
    if action != "lease":
        assert h.jobs()[0]["state"] == "leased"
        assert h.fail(m, lease["job"], lease["lease_token"], "bad_output")[0] == 200
    h.post(h.owner, "A question for this machine.")
    other = h.machine(auth=h.other)
    assert h.poll(other)[0] == 204
    assert h.jobs()[-1]["state"] == "queued"
    assert h.request(h.owner, "DELETE", "/api/runners/" + m["machine_id"])[0] == 200
    assert h.poll(m)[0] in (401, 404)


def test_server_provider_takes_precedence_without_fallback(relay_hub):
    h = relay_hub
    m = h.machine()
    assert h.poll(m)[0] == 204  # RED
    assert h.configure()[0] == 200
    h.post(h.owner, "The server provider owns this answer.")
    assert h.jobs() == []
    assert h.settings(h.owner, {"front": {"provider": "none"}})[0] == 200
    h.post(h.owner, "Now use my runner.")
    assert len(h.jobs()) == 1 and h.jobs()[0]["state"] == "queued"
    assert h.settings(h.owner, {"front": {"provider": "none", "enabled": False}})[0] == 200
    h.post(h.owner, "Front answers are off.")
    assert len(h.jobs()) == 1


def test_context_and_answer_are_redacted(relay_hub):
    h = relay_hub
    m = h.machine()
    assert h.poll(m)[0] == 204  # RED
    corpus = examples({"cmmn_token", "github_token", "anthropic_key", "assignment"})
    text = "\n".join(text for _, text, _ in corpus)
    secrets = [value for _, _, values in corpus for value in values]
    assert h.request(h.owner, "POST", "/api/boards/work/cards", {"title": text})[0] == 201
    h.post(h.owner, text)
    status, body = h.poll(m)
    assert status == 200
    assert all(value not in body["prompt"] + body["system"] for value in secrets)
    assert h.answer(m, body["job"], body["lease_token"], corpus[0][1])[0] == 200
    page = h.chat(h.owner)
    answers = [r for r in page[1]["replies"] if r["kind"] == "answer"]
    assert len(answers) == 1
    assert "[redacted:" in answers[0]["text"] and answers[0]["redacted"]
    raw = page[2] + h.status(h.owner)[2]
    assert all(value.encode() not in raw for value in secrets)


def test_active_main_takes_precedence_unless_relay_always(relay_hub):
    h = relay_hub
    m = h.machine()
    assert h.poll(m)[0] == 204  # RED
    status, body = h.connect(h.owner, "work")
    assert status == 201
    token, cid = body["token"], body["connection"]["id"]
    h.subscribe(token)
    assert h.mcp(token, "space_read", board="work")[1]["ok"]
    h.post(h.owner, "Let the active main answer.")
    assert h.jobs() == []
    h.age_connection(cid, 601)
    h.post(h.owner, "The main is idle now.")
    assert len(h.jobs()) == 1 and h.jobs()[0]["state"] == "queued"
    assert h.mcp(token, "space_read", board="work")[1]["ok"]
    assert h.poll(m, relay_always=True)[0] == 200
    h.post(h.owner, "Always relay is enabled.")
    assert len(h.jobs()) == 2 and h.jobs()[1]["state"] == "queued"


def test_minimal_context_by_default_full_by_opt_in(relay_hub):
    h = relay_hub
    m = h.machine()
    assert h.poll(m)[0] == 204  # RED
    h.post(h.member, "member note 7Q")
    assert h.propose_titled("untrusted card 5K")[0] == 201
    assert (
        h.request(h.owner, "POST", "/api/boards/work/cards", {"title": "trusted card 3J"})[0] == 201
    )
    h.post(h.owner, "What is open?")
    status, body = h.poll(m)
    assert status == 200
    name = h.project(h.owner, "work")[1]["name"]
    for text in ("What is open?", name, "trusted card 3J"):
        assert text in body["prompt"]
    assert "member note 7Q" not in body["prompt"] and "untrusted card 5K" not in body["prompt"]
    assert h.fail(m, body["job"], body["lease_token"], "bad_output")[0] == 200
    assert h.poll(m, context="full")[0] == 204
    h.post(h.owner, "What is open?")
    status, body = h.poll(m, context="full")
    assert status == 200
    # Full context is chat.worker.build_context unchanged: other people's chat
    # lines appear; an untrusted proposal is not in the poster's task list in either mode.
    assert "member note 7Q" in body["prompt"]
    assert "untrusted card 5K" not in body["prompt"]


def test_daily_cap_is_enforced_and_visible(relay_hub):
    h = relay_hub
    m = h.machine()
    assert h.poll(m)[0] == 204  # RED
    h.seed_jobs(200)
    message = h.post(h.owner, "One more question.")
    job = next(row for row in h.jobs() if row["message_id"] == message)
    assert (job["state"], job["code"]) == ("skipped", "daily_limit")
    assert h.poll(m)[0] == 204
    state = h.status(h.owner)[1]["front"]["runner"]
    assert state["used_today"] == 200 and state["daily_cap"] == 200
    assert state["last_error"] == "daily_limit"
