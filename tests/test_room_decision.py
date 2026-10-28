"""Boundary tests for local room inference; no Ollama server is needed."""

import json

import pytest

from mempalace.llm_client import LLMError
from mempalace.room_decision import (
    NONE_ROOM,
    ChatRoomBaseline,
    DecisionRoomDecider,
    LocalOllama,
    drawer_state,
    parse_choice,
    room_criteria,
)
from mempalace.rooms import RoomSet, RoomSpec, plan_rooms
from tests.test_rooms import FakeCollection


def room_set():
    return RoomSet("w", [RoomSpec("release", "Package releases"), RoomSpec("bugs", "Bug fixes")])


class FakeClient:
    def __init__(self):
        self.calls = []
        self.digest = "abc"
        self.answer = {
            "type": "choice",
            "choice": "bugs",
            "probabilities": {"release": 0.1, "bugs": 0.85, NONE_ROOM: 0.05},
            "confidence": 0.2,  # API confidence must not replace the chosen probability.
        }

    def inspect(self, model, capability):
        return {"model": model, "digest": self.digest}

    def request(self, path, body):
        self.calls.append((path, body))
        if path == "/api/chat":
            return {"message": {"content": '{"room":"bugs"}'}}
        return {"answers": {"room": self.answer}}


def test_decision_uses_full_text_closed_options_and_probability():
    client = FakeClient()
    decider = DecisionRoomDecider(room_set(), client, "d3:4b")
    row = {
        "document": "Original\nwords",
        "metadata": {"room": "release"},
        "expected_room": "release",
    }
    assert decider.decide([row]) == [("bugs", 0.85)]
    path, body = client.calls[0]
    assert path == "/v1/systemone"
    assert body["state"] == {"wing": "w", "drawer": "Original\nwords"}
    assert set(body["questions"]["room"]["criteria"]) == {"release", "bugs", NONE_ROOM}
    assert decider.predict(row).margin == pytest.approx(0.75)


def test_reject_means_keep_existing_filing():
    client = FakeClient()
    client.answer.update(
        choice=NONE_ROOM, probabilities={"release": 0.1, "bugs": 0.05, NONE_ROOM: 0.85}
    )
    assert DecisionRoomDecider(room_set(), client, "d1:3b").decide([{"document": "Unknown"}]) == [
        ("", 0.85)
    ]


@pytest.mark.parametrize("text", [None, "", "  ", 42])
def test_missing_text_is_not_silently_classified(text):
    with pytest.raises(ValueError, match="nonempty"):
        drawer_state({"document": text}, "w")


@pytest.mark.parametrize(
    "mutation",
    [
        {"type": "noul"},
        {"choice": "invented"},
        {"choice": []},
        {"choice": "release"},
        {"probabilities": {"release": 0.1, "bugs": 0.9}},
        {"probabilities": {"release": 0.1, "bugs": float("nan"), NONE_ROOM: 0.05}},
        {"probabilities": {"release": 0.1, "bugs": True, NONE_ROOM: 0.05}},
        {"probabilities": {"release": 0.1, "bugs": 2.0, NONE_ROOM: 0.05}},
        {"probabilities": {"release": 0.1, "bugs": 0.2, NONE_ROOM: 0.05}},
    ],
)
def test_invalid_decisions_fail_closed(mutation):
    answer = {**FakeClient().answer, **mutation}
    with pytest.raises(LLMError):
        parse_choice(answer, room_criteria(room_set()))


def test_oversized_input_is_rejected_without_truncation_or_inference():
    client = FakeClient()
    with pytest.raises(LLMError, match="no truncation"):
        DecisionRoomDecider(room_set(), client, "d3:4b").predict({"document": "x" * 65536})
    assert client.calls == []


def test_model_change_during_run_is_refused():
    client = FakeClient()
    decider = DecisionRoomDecider(room_set(), client, "d3:4b")
    decider.predict({"document": "A bug"})
    client.digest = "changed"
    with pytest.raises(LLMError, match="identity changed"):
        decider.predict({"document": "Another bug"})
    assert len(client.calls) == 1


def test_chat_baseline_has_no_invented_confidence_and_disables_truncation():
    client = FakeClient()
    prediction = ChatRoomBaseline(room_set(), client).predict({"document": "A bug"})
    assert prediction.room == "bugs"
    assert prediction.score is None
    assert prediction.probabilities is None
    body = client.calls[0][1]
    assert body["think"] is False
    assert body["truncate"] is False
    assert body["shift"] is False
    assert json.loads(body["messages"][1]["content"])["drawer"] == "A bug"
    assert "Approved rooms:" in body["messages"][0]["content"]
    assert (
        json.dumps(room_criteria(room_set()), ensure_ascii=False) in body["messages"][0]["content"]
    )


def test_planner_fetches_text_for_decision_adapter_without_writing():
    client = FakeClient()
    decider = DecisionRoomDecider(room_set(), client, "d3:4b")
    col = FakeCollection(
        [
            {"id": "one", "doc": "A bug", "meta": {"wing": "w", "room": "general"}, "emb": [1, 0]},
            {"id": "two", "doc": "A bug", "meta": {"wing": "w", "room": "diary"}, "emb": [1, 0]},
        ]
    )
    plan = plan_rooms(col, "w", decider, threshold=0.8)
    assert plan.changes == [("one", "general", "bugs")]
    assert plan.kept == 1
    assert col.updates == []
    assert col.rows["one"]["meta"]["room"] == "general"


def test_decision_planning_cannot_reuse_the_default_cosine_threshold():
    client = FakeClient()
    with pytest.raises(ValueError, match="explicit acceptance threshold"):
        plan_rooms(FakeCollection([]), "w", DecisionRoomDecider(room_set(), client, "d3:4b"))
    assert client.calls == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com",
        "http://192.168.1.2:11434",
        "http://localhost.evil.test",
        "http://user:password@localhost:11434",
        "http://127.0.0.1:11434/path",
        "http://127.0.0.1?x=1",
    ],
)
def test_non_loopback_or_ambiguous_endpoints_are_refused(endpoint):
    with pytest.raises(ValueError, match="loopback"):
        LocalOllama(endpoint)


def test_cloud_names_are_refused_before_contacting_server(monkeypatch):
    client = LocalOllama()
    monkeypatch.setattr(client, "request", lambda *a: pytest.fail("must not contact server"))
    with pytest.raises(LLMError, match="Cloud"):
        client.inspect("clef:cloud", "decision")


@pytest.mark.parametrize(
    "response",
    [
        {"capabilities": ["decision"], "remote_model": "hidden"},
        {"capabilities": ["decision"], "remote_host": "https://ollama.com"},
        {"capabilities": ["completion"]},
        {},
    ],
)
def test_remote_aliases_and_missing_capabilities_are_refused(monkeypatch, response):
    client = LocalOllama()
    calls = []
    monkeypatch.setattr(client, "request", lambda *a: calls.append(a) or response)
    with pytest.raises(LLMError):
        client.inspect("innocent-local-name", "decision")
    assert len(calls) == 1


def test_valid_local_model_requires_installed_digest(monkeypatch):
    client = LocalOllama()

    def request(path, body=None):
        return {"capabilities": ["decision"]} if path == "/api/show" else {"models": []}

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(LLMError, match="digest"):
        client.inspect("local", "decision")


def test_room_options_reserve_space_for_rejection():
    with pytest.raises(ValueError, match="1–25"):
        room_criteria(RoomSet("w", [RoomSpec(str(i), "Definition") for i in range(26)]))


def test_local_http_redirect_is_refused_before_following_location():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "https://example.com/never-contact")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = LocalOllama(f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(LLMError, match="redirect"):
            client.request("/v1/systemone", {"state": "private input"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("failure", ["http", "transport", "json"])
def test_transport_failures_do_not_echo_private_content(failure):
    import io
    from urllib.error import HTTPError, URLError

    secret = "PRIVATE DRAWER CONTENT"

    class Opener:
        def open(self, request, timeout):
            if failure == "http":
                raise HTTPError(request.full_url, 400, secret, {}, io.BytesIO(secret.encode()))
            if failure == "transport":
                raise URLError(secret)
            raise ValueError(secret)

    client = LocalOllama()
    client._opener = Opener()
    with pytest.raises(LLMError) as caught:
        client.request("/v1/systemone", {"state": secret})
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        {"done_reason": "length", "message": {"content": '{"room":"bugs"}'}},
        {"message": {"content": "not json"}},
        {"message": {"content": '{"room":"invented"}'}},
        {"message": {"content": '{"room":"bugs", "extra":"field"}'}},
    ],
)
def test_chat_baseline_refuses_cutoff_or_invalid_labels(response):
    client = FakeClient()
    client.request = lambda *args: response
    with pytest.raises(LLMError):
        ChatRoomBaseline(room_set(), client).predict({"document": "Some evidence"})
