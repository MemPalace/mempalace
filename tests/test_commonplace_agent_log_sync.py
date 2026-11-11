"""The live-sync daemon captures agent-log files and never duplicates on retry."""

import json

from tests.commonplace_sync_fixtures import hub as hub, reset_reached as reset_reached


def line(text, role="user"):
    record = {"agent": "gemini-cli", "session_id": "s-7", "role": role, "content": text}
    return json.dumps(record, ensure_ascii=False) + "\n"


def test_daemon_captures_an_agent_log_once_and_grows_it_without_duplicates(hub):
    h = hub()
    person = h.signup()
    h.enable_capture(person)
    token, _ = h.pat(person, client="agent-log")
    stranger = h.signup("stranger@example.test")
    root = h.home.parent / "agent-logs"
    root.mkdir()
    log = root / "gemini-s-7.jsonl"
    first = line("  café 🦉 sentinel-one\t") + line("an answer", "assistant")
    log.write_bytes(first.encode())
    env = h.home.parent / "agent.env"
    env.write_text(f"COMMONPLACE_URL={h.origin}\nCOMMONPLACE_CAPTURE_TOKEN_AGENT_LOG={token}\n")
    env.chmod(0o600)
    argv = ["sync", "install", "--enable", "--no-service", "--hub", h.origin]
    assert h.cli([*argv, "--source", f"agent-log:{root}", "--env-file", str(env)]) == 0
    assert h.cli(["sync", "install", "--enable", "--no-service", "--source", "agent-log"]) == 2
    h.at(1)
    assert h.cli(["sync", "run", "--once"]) == 0
    (thread,) = h.stored_threads(person)
    assert thread.source_thread_id.endswith(":agent-log:gemini-cli:s-7")
    assert h.read(person, thread.id) == first.encode()
    texts = [m["text"] for m in h.message_rows(person, thread.id) if m["kind"] == "message"]
    assert texts == ["  café 🦉 sentinel-one\t", "an answer"]
    # A repeated pass changes nothing; growth appends only the new record.
    h.at(2)
    assert h.cli(["sync", "run", "--once"]) == 0
    assert h.read(person, thread.id) == first.encode()
    second = line("one more")
    log.write_bytes((first + second).encode())
    h.at(901)
    assert h.cli(["sync", "run", "--once"]) == 0
    assert [t.id for t in h.stored_threads(person)] == [thread.id]
    assert h.read(person, thread.id) == (first + second).encode()
    # Another account's session cannot read the private chat.
    assert h.get(f"/api/threads/{thread.id}", stranger).status == 404
