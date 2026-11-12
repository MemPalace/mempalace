"""Streaming model transport over loopback fake servers, for both wire formats."""

import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from commonplace.chat import model, model_stream
from commonplace.chat.types import ChatDenied, FrontSettings, ModelError
from tests._chat_helpers import no_outbound

LIMITS = {
    "timeout": 5,
    "max_tokens": 5000,
    "max_input_chars": 10_000,
    "max_response_bytes": 100_000,
}
GATE_WAIT = 3
TOOLS = [
    {
        "name": "find_work",
        "description": "Find work items",
        "schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
    {"name": "list_boards", "description": "List boards", "schema": {"type": "object"}},
]
USER = [{"role": "user", "content": "hello"}]


def anthropic_frame(kind, **data):
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode()


def openai_frame(delta=None, finish=None):
    choice = {"index": 0, "delta": delta or {}, "finish_reason": finish}
    return f"data: {json.dumps({'choices': [choice]})}\n\n".encode()


def a_text(text, index=0):
    return anthropic_frame(
        "content_block_delta", index=index, delta={"type": "text_delta", "text": text}
    )


def a_start(index, call_id, name):
    return anthropic_frame(
        "content_block_start",
        index=index,
        content_block={"type": "tool_use", "id": call_id, "name": name, "input": {}},
    )


def a_json(index, part):
    return anthropic_frame(
        "content_block_delta", index=index, delta={"type": "input_json_delta", "partial_json": part}
    )


def a_stop(index=0):
    return anthropic_frame("content_block_stop", index=index)


def a_end(reason="end_turn"):
    return anthropic_frame("message_delta", delta={"stop_reason": reason})


def o_text(text):
    return openai_frame({"content": text})


def o_call(index, call_id=None, name=None, args=None):
    function = {k: v for k, v in (("name", name), ("arguments", args)) if v is not None}
    part = {"index": index, "function": function}
    if call_id:
        part.update(id=call_id, type="function")
    return openai_frame({"tool_calls": [part]})


@contextmanager
def fake_server(script, status=200, body=b"PROVIDER-SECRET"):
    """Serve `script` as one chunked SSE response.

    Items are bytes (one chunk each), a `threading.Event` (wait for it before going on,
    giving up the connection after GATE_WAIT s) or `"hang"` (hold until the server stops).
    """
    state = {"requests": [], "timed_out": False}
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def chunk(self, data):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            state["requests"].append(
                {
                    "path": self.path,
                    "headers": self.headers,
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            if status != 200:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.flush()
            try:
                for item in script:
                    if item == "hang":
                        stop.wait(GATE_WAIT)
                        return
                    if isinstance(item, threading.Event):
                        if not item.wait(GATE_WAIT):
                            state["timed_out"] = True
                            return
                    else:
                        self.chunk(item)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join()


def settings_for(provider, origin):
    return FrontSettings(
        workspace_id="synthetic-workspace",
        enabled=True,
        provider=provider,
        model="fast-model",
        base_url=origin if provider == "openai_compatible" else None,
        key_ref=None,
        daily_limit=200,
        user_daily_limit=50,
        updated_by=None,
        updated_at=None,
    )


@pytest.fixture(params=["anthropic", "openai_compatible"])
def provider(request, monkeypatch):
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_HOSTS", "127.0.0.1")
    return request.param


def run(provider, monkeypatch, script, *, messages=USER, tools=TOOLS, key="k", **kwargs):
    """Stream `script` through the transport and return (events, state)."""
    connects = no_outbound(monkeypatch)
    with fake_server(script) as (origin, state):
        monkeypatch.setattr(model, "ANTHROPIC_ORIGIN", origin)
        events = list(
            model_stream.stream(
                settings_for(provider, origin),
                key,
                "system",
                messages,
                tools=tools,
                **{**LIMITS, **kwargs},
            )
        )
    assert connects == []
    return events, state


def test_text_deltas_arrive_in_order(provider, monkeypatch):
    if provider == "anthropic":
        script = [a_text("Hel"), a_text("lo "), a_text("there"), a_end()]
    else:
        script = [o_text("Hel"), o_text("lo "), o_text("there"), openai_frame(finish="stop")]
    events, _ = run(provider, monkeypatch, script)
    assert events == [("text", "Hel"), ("text", "lo "), ("text", "there"), ("stop", "end")]


def test_a_frame_split_across_chunks_is_buffered(provider, monkeypatch):
    frame = a_text("whole") if provider == "anthropic" else o_text("whole")
    end = a_end() if provider == "anthropic" else openai_frame(finish="stop")
    script = [frame[:7], frame[7:20], frame[20:], end]
    events, _ = run(provider, monkeypatch, script)
    assert events == [("text", "whole"), ("stop", "end")]


def test_first_text_is_yielded_while_the_server_holds_the_rest(provider, monkeypatch):
    release = threading.Event()
    if provider == "anthropic":
        script = [a_text("first"), release, a_text("second"), a_end()]
    else:
        script = [o_text("first"), release, o_text("second"), openai_frame(finish="stop")]
    no_outbound(monkeypatch)
    with fake_server(script) as (origin, state):
        monkeypatch.setattr(model, "ANTHROPIC_ORIGIN", origin)
        events = model_stream.stream(
            settings_for(provider, origin), "k", "system", USER, tools=TOOLS, **LIMITS
        )
        started = time.monotonic()
        assert next(events) == ("text", "first")
        assert time.monotonic() - started < GATE_WAIT
        assert not release.is_set()
        release.set()
        assert list(events) == [("text", "second"), ("stop", "end")]
    assert not state["timed_out"]


def test_a_tool_call_in_fragments_is_assembled(provider, monkeypatch):
    if provider == "anthropic":
        script = [
            a_text("Looking. "),
            a_stop(0),
            a_start(1, "toolu_1", "find_work"),
            a_json(1, ""),
            a_json(1, '{"q": "inv'),
            a_json(1, 'oice", "n'),
            a_json(1, 'ested": {"a": [1, 2]}}'),
            a_stop(1),
            a_end("tool_use"),
        ]
    else:
        script = [
            o_text("Looking. "),
            o_call(0, "call_1", "find_work", ""),
            o_call(0, args='{"q": "inv'),
            o_call(0, args='oice", "n'),
            o_call(0, args='ested": {"a": [1, 2]}}'),
            openai_frame(finish="tool_calls"),
        ]
    events, _ = run(provider, monkeypatch, script)
    call = {
        "id": "toolu_1" if provider == "anthropic" else "call_1",
        "name": "find_work",
        "arguments": {"q": "invoice", "nested": {"a": [1, 2]}},
    }
    assert events == [("text", "Looking. "), ("tool_call", call), ("stop", "tool")]


def test_two_tool_calls_in_one_response(provider, monkeypatch):
    if provider == "anthropic":
        script = [
            a_start(0, "t1", "find_work"),
            a_json(0, '{"q": "a"}'),
            a_stop(0),
            a_start(1, "t2", "list_boards"),
            a_stop(1),
            a_end("tool_use"),
        ]
    else:
        script = [
            o_call(0, "t1", "find_work", '{"q": '),
            o_call(1, "t2", "list_boards", "{}"),
            o_call(0, args='"a"}'),
            openai_frame(finish="tool_calls"),
        ]
    events, _ = run(provider, monkeypatch, script)
    assert events == [
        ("tool_call", {"id": "t1", "name": "find_work", "arguments": {"q": "a"}}),
        ("tool_call", {"id": "t2", "name": "list_boards", "arguments": {}}),
        ("stop", "tool"),
    ]


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("end_turn", "end"),
        ("stop_sequence", "end"),
        ("tool_use", "tool"),
        ("max_tokens", "length"),
        ("stop", "end"),
        ("tool_calls", "tool"),
        ("length", "length"),
    ],
)
def test_stop_reasons_are_mapped(monkeypatch, reason, expected):
    provider = "openai_compatible" if reason in ("stop", "tool_calls", "length") else "anthropic"
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_HOSTS", "127.0.0.1")
    script = [a_end(reason)] if provider == "anthropic" else [openai_frame(finish=reason)]
    events, _ = run(provider, monkeypatch, script, tools=[])
    assert events == [("stop", expected)]


def test_unknown_stop_reason_is_a_bad_response(provider, monkeypatch):
    script = [a_end("surprise")] if provider == "anthropic" else [openai_frame(finish="surprise")]
    with pytest.raises(ModelError) as exc:
        run(provider, monkeypatch, script)
    assert exc.value.code == "bad_response"


NEUTRAL = [
    {"role": "user", "content": "find the invoice"},
    {
        "role": "assistant",
        "content": "On it.",
        "tool_calls": [
            {"id": "c1", "name": "find_work", "arguments": {"q": "invoice"}},
            {"id": "c2", "name": "list_boards", "arguments": {}},
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": '{"found": 1}'},
    {"role": "tool", "tool_call_id": "c2", "content": '{"boards": []}'},
    {"role": "assistant", "content": "Found one."},
    {"role": "user", "content": "thanks"},
]


def test_request_shape_anthropic(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_HOSTS", "127.0.0.1")
    sent = []
    events, state = run(
        "anthropic",
        monkeypatch,
        [a_end()],
        messages=NEUTRAL,
        on_sent=lambda: sent.append(1),
    )
    assert events == [("stop", "end")] and sent == [1]
    request = state["requests"][0]
    assert request["path"] == "/v1/messages"
    assert request["headers"]["x-api-key"] == "k"
    assert request["headers"]["anthropic-version"] == "2023-06-01"
    body = request["body"]
    assert body == {
        "model": "fast-model",
        "max_tokens": 5000,
        "temperature": 0,
        "stream": True,
        "system": "system",
        "tools": [
            {"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
            for t in TOOLS
        ],
        "messages": [
            {"role": "user", "content": "find the invoice"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "On it."},
                    {
                        "type": "tool_use",
                        "id": "c1",
                        "name": "find_work",
                        "input": {"q": "invoice"},
                    },
                    {"type": "tool_use", "id": "c2", "name": "list_boards", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": '{"found": 1}'},
                    {"type": "tool_result", "tool_use_id": "c2", "content": '{"boards": []}'},
                ],
            },
            {"role": "assistant", "content": "Found one."},
            {"role": "user", "content": "thanks"},
        ],
    }


def test_request_shape_openai_compatible(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_HOSTS", "127.0.0.1")
    events, state = run(
        "openai_compatible", monkeypatch, [openai_frame(finish="stop")], messages=NEUTRAL
    )
    assert events == [("stop", "end")]
    request = state["requests"][0]
    assert request["path"] == "/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer k"
    assert request["body"] == {
        "model": "fast-model",
        "max_tokens": 5000,
        "temperature": 0,
        "stream": True,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["schema"],
                },
            }
            for t in TOOLS
        ],
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "find the invoice"},
            {
                "role": "assistant",
                "content": "On it.",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "find_work", "arguments": '{"q": "invoice"}'},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "list_boards", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"found": 1}'},
            {"role": "tool", "tool_call_id": "c2", "content": '{"boards": []}'},
            {"role": "assistant", "content": "Found one."},
            {"role": "user", "content": "thanks"},
        ],
    }


def test_no_tools_means_no_tools_key_and_no_auth_header_without_a_key(provider, monkeypatch):
    _, state = run(
        provider,
        monkeypatch,
        [a_end()] if provider == "anthropic" else [openai_frame(finish="stop")],
        tools=[],
        key=None,
    )
    request = state["requests"][0]
    assert "tools" not in request["body"]
    assert request["headers"].get("Authorization") is None
    assert request["headers"].get("x-api-key") is None


def test_non_200_is_http_error_without_the_body(provider, monkeypatch):
    no_outbound(monkeypatch)
    with fake_server([], status=500) as (origin, state):
        monkeypatch.setattr(model, "ANTHROPIC_ORIGIN", origin)
        with pytest.raises(ModelError) as exc:
            list(
                model_stream.stream(
                    settings_for(provider, origin), "k", "system", USER, tools=TOOLS, **LIMITS
                )
            )
    assert exc.value.code == "http_error"
    assert len(state["requests"]) == 1  # no retry
    assert "PROVIDER-SECRET" not in str(exc.value) and "PROVIDER-SECRET" not in repr(exc.value)


def bad_scripts(wire):
    if wire == "anthropic":
        return {
            "not_json": [b"data: nope\n\n"],
            "not_an_object": [b"data: [1, 2]\n\n"],
            "error_event": [anthropic_frame("error", error={"message": "PROVIDER-SECRET"})],
            "unknown_tool": [a_start(0, "t", "delete_everything"), a_stop(0), a_end("tool_use")],
            "array_arguments": [
                a_start(0, "t", "find_work"),
                a_json(0, "[1]"),
                a_stop(0),
                a_end("tool_use"),
            ],
            "broken_arguments": [
                a_start(0, "t", "find_work"),
                a_json(0, '{"q": '),
                a_stop(0),
                a_end("tool_use"),
            ],
            "open_block_at_stop": [a_start(0, "t", "find_work"), a_end("tool_use")],
            "no_stop": [a_text("cut off")],
            "missing_field": [anthropic_frame("content_block_delta", index=0)],
        }
    return {
        "not_json": [b"data: nope\n\n"],
        "not_an_object": [b"data: [1, 2]\n\n"],
        "error_object": [b'data: {"error": {"message": "PROVIDER-SECRET"}}\n\n'],
        "unknown_tool": [
            o_call(0, "t", "delete_everything", "{}"),
            openai_frame(finish="tool_calls"),
        ],
        "array_arguments": [o_call(0, "t", "find_work", "[1]"), openai_frame(finish="tool_calls")],
        "broken_arguments": [
            o_call(0, "t", "find_work", '{"q": '),
            openai_frame(finish="tool_calls"),
        ],
        "done_without_finish": [o_text("cut off"), b"data: [DONE]\n\n"],
        "no_stop": [o_text("cut off")],
        "non_string_content": [openai_frame({"content": 5})],
        "missing_field": [b'data: {"id": "x"}\n\n'],
    }


@pytest.mark.parametrize(
    "wire,case",
    [(wire, case) for wire in ("anthropic", "openai_compatible") for case in bad_scripts(wire)],
)
def test_malformed_frames_are_bad_responses(monkeypatch, wire, case):
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_HOSTS", "127.0.0.1")
    no_outbound(monkeypatch)
    with fake_server(bad_scripts(wire)[case]) as (origin, state):
        monkeypatch.setattr(model, "ANTHROPIC_ORIGIN", origin)
        with pytest.raises(ModelError) as exc:
            list(
                model_stream.stream(
                    settings_for(wire, origin), "k", "system", USER, tools=TOOLS, **LIMITS
                )
            )
    assert exc.value.code == "bad_response"
    assert "PROVIDER-SECRET" not in str(exc.value)
    assert len(state["requests"]) == 1  # no retry


def test_events_before_a_bad_frame_are_still_delivered(provider, monkeypatch):
    script = [a_text("fine")] if provider == "anthropic" else [o_text("fine")]
    no_outbound(monkeypatch)
    with fake_server([*script, b"data: nope\n\n"]) as (origin, _):
        monkeypatch.setattr(model, "ANTHROPIC_ORIGIN", origin)
        events = model_stream.stream(
            settings_for(provider, origin), "k", "system", USER, tools=TOOLS, **LIMITS
        )
        assert next(events) == ("text", "fine")
        with pytest.raises(ModelError) as exc:
            next(events)
    assert exc.value.code == "bad_response"


def test_the_deadline_covers_the_whole_call(provider, monkeypatch):
    script = [a_text("a") if provider == "anthropic" else o_text("a"), "hang"]
    no_outbound(monkeypatch)
    with fake_server(script) as (origin, _):
        monkeypatch.setattr(model, "ANTHROPIC_ORIGIN", origin)
        events = model_stream.stream(
            settings_for(provider, origin),
            "k",
            "system",
            USER,
            tools=TOOLS,
            **{**LIMITS, "timeout": 0.4},
        )
        started = time.monotonic()
        assert next(events) == ("text", "a")
        with pytest.raises(ModelError) as exc:
            next(events)
        assert time.monotonic() - started < GATE_WAIT - 1
    assert exc.value.code == "timeout"


def test_cancel_stops_the_call_and_closes_the_socket(provider, monkeypatch):
    script = [a_text("a") if provider == "anthropic" else o_text("a"), "hang"]
    no_outbound(monkeypatch)
    cancel = threading.Event()
    with fake_server(script) as (origin, _):
        monkeypatch.setattr(model, "ANTHROPIC_ORIGIN", origin)
        events = model_stream.stream(
            settings_for(provider, origin),
            "k",
            "system",
            USER,
            tools=TOOLS,
            cancel=cancel,
            **LIMITS,
        )
        assert next(events) == ("text", "a")
        threading.Timer(0.1, cancel.set).start()
        started = time.monotonic()
        with pytest.raises(ModelError) as exc:
            next(events)
        assert time.monotonic() - started < 1.5
    assert exc.value.code == "stopped"


def test_cancel_before_start_sends_nothing(provider, monkeypatch):
    cancel = threading.Event()
    cancel.set()
    no_outbound(monkeypatch)
    with fake_server([a_end()]) as (origin, state):
        monkeypatch.setattr(model, "ANTHROPIC_ORIGIN", origin)
        with pytest.raises(ModelError) as exc:
            list(
                model_stream.stream(
                    settings_for(provider, origin),
                    "k",
                    "system",
                    USER,
                    cancel=cancel,
                    **LIMITS,
                )
            )
    assert exc.value.code == "stopped" and state["requests"] == []


def test_response_bytes_are_bounded_by_the_caller(provider, monkeypatch):
    text = "x" * 500
    script = (
        [a_text(text), a_end()]
        if provider == "anthropic"
        else [o_text(text), openai_frame(finish="stop")]
    )
    with pytest.raises(ModelError) as exc:
        run(provider, monkeypatch, script, max_response_bytes=300)
    assert exc.value.code == "bad_response"
    events, _ = run(provider, monkeypatch, script, max_response_bytes=5000)
    assert events[0] == ("text", text)


def test_input_size_is_bounded_by_the_caller(provider, monkeypatch):
    no_outbound(monkeypatch)
    with fake_server([a_end()]) as (origin, state):
        monkeypatch.setattr(model, "ANTHROPIC_ORIGIN", origin)
        settings = settings_for(provider, origin)
        limits = {**LIMITS, "max_input_chars": 200}
        with pytest.raises(ValueError):
            model_stream.stream(settings, "k", "s" * 201, [], **limits)
        with pytest.raises(ValueError):
            model_stream.stream(
                settings, "k", "", [{"role": "user", "content": "m" * 201}], **limits
            )
        with pytest.raises(ValueError):
            model_stream.stream(
                settings, "k", "", USER, tools=[{**TOOLS[0], "description": "d" * 300}], **limits
            )
    assert state["requests"] == []


def test_a_keyed_host_outside_the_allowlist_is_refused(monkeypatch):
    no_outbound(monkeypatch)
    monkeypatch.delenv("COMMONPLACE_CHAT_KEY_HOSTS", raising=False)
    with fake_server([openai_frame(finish="stop")]) as (origin, state):
        settings = settings_for("openai_compatible", origin)
        with pytest.raises(ChatDenied):
            model_stream.stream(settings, "k", "system", USER, **LIMITS)
        assert state["requests"] == []
        # Without a key the local endpoint needs no allowlist entry.
        events = list(model_stream.stream(settings, None, "system", USER, **LIMITS))
        assert events == [("stop", "end")]


def test_a_local_endpoint_that_resolves_off_loopback_is_refused(monkeypatch):
    attempts = []
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 9))],
    )
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: attempts.append(a))
    settings = settings_for("openai_compatible", "http://127.0.0.1:9")
    with pytest.raises(ModelError) as exc:
        list(model_stream.stream(settings, None, "private message", USER, **LIMITS))
    assert exc.value.code == "http_error"
    assert attempts == []
    assert "private message" not in str(exc.value)


# Stops that must not read as a finished reply


def test_a_paused_anthropic_turn_is_a_bad_response_not_a_final_reply(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_HOSTS", "127.0.0.1")
    with pytest.raises(ModelError) as exc:
        run("anthropic", monkeypatch, [a_text("half an ans"), a_end("pause_turn")])
    assert exc.value.code == "bad_response"


def test_openai_tool_calls_with_a_plain_stop_are_a_bad_response(monkeypatch):
    monkeypatch.setenv("COMMONPLACE_CHAT_KEY_HOSTS", "127.0.0.1")
    script = [o_call(0, "t", "find_work", '{"q": "x"}'), openai_frame(finish="stop")]
    with pytest.raises(ModelError) as exc:
        run("openai_compatible", monkeypatch, script)
    assert exc.value.code == "bad_response"


def test_a_cancel_between_events_of_one_frame_stops_the_stream(monkeypatch):
    no_outbound(monkeypatch)
    calls = [
        {
            "index": n,
            "id": f"t{n}",
            "type": "function",
            "function": {"name": "list_boards", "arguments": "{}"},
        }
        for n in range(2)
    ]
    choice = {"index": 0, "delta": {"tool_calls": calls}, "finish_reason": "tool_calls"}
    frame = f"data: {json.dumps({'choices': [choice]})}\n\n".encode()
    cancel = threading.Event()
    with fake_server([frame, "hang"]) as (origin, _):
        events = model_stream.stream(
            settings_for("openai_compatible", origin),
            None,
            "system",
            USER,
            tools=TOOLS,
            cancel=cancel,
            **LIMITS,
        )
        assert next(events)[0] == "tool_call"
        cancel.set()
        time.sleep(0.3)  # the watcher stops the call
        with pytest.raises(ModelError) as exc:
            next(events)
        events.close()
    assert exc.value.code == "stopped"
