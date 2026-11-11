"""Streaming, tool-calling model transport: events as the provider sends them.

Shares the connection, deadline and loopback rules of `model.complete`, keeps its
safety rules (no retries, no provider error text, bounded bytes) and takes every limit
from the caller.
"""

import json
import threading

from commonplace.chat import model
from commonplace.chat.types import FrontSettings, ModelError

ANTHROPIC_STOPS = {
    "end_turn": "end",
    "stop_sequence": "end",
    "pause_turn": "end",
    "refusal": "end",
    "tool_use": "tool",
    "max_tokens": "length",
}
OPENAI_STOPS = {
    "stop": "end",
    "content_filter": "end",
    "tool_calls": "tool",
    "function_call": "tool",
    "length": "length",
}
# Parse failures of one provider frame; the frame text is never put in an error.
BAD_FRAME = (KeyError, IndexError, TypeError, ValueError, AttributeError)


def _arguments(parts):
    raw = "".join(parts).strip()
    args = json.loads(raw) if raw else {}
    if not isinstance(args, dict):
        raise ValueError
    return args


def _tool_call(names, call_id, name, parts):
    if not isinstance(call_id, str) or name not in names:
        raise ValueError
    return ("tool_call", {"id": call_id, "name": name, "arguments": _arguments(parts)})


class _Anthropic:
    def __init__(self, names):
        self.names = names
        self.blocks = {}
        self.finished = False

    def feed(self, data):
        event = json.loads(data)
        kind = event["type"]
        if kind == "content_block_start":
            block = event["content_block"]
            if block["type"] == "tool_use":
                if block["name"] not in self.names:
                    raise ValueError
                self.blocks[event["index"]] = [block["id"], block["name"], []]
            elif block["type"] == "text" and block.get("text"):
                return [("text", block["text"])]
        elif kind == "content_block_delta":
            delta = event["delta"]
            if delta["type"] == "text_delta":
                if not isinstance(delta["text"], str):
                    raise ValueError
                return [("text", delta["text"])] if delta["text"] else []
            if delta["type"] == "input_json_delta":
                if not isinstance(delta["partial_json"], str):
                    raise ValueError
                self.blocks[event["index"]][2].append(delta["partial_json"])
        elif kind == "content_block_stop":
            block = self.blocks.pop(event["index"], None)
            if block is not None:
                return [_tool_call(self.names, *block)]
        elif kind == "message_delta":
            if self.blocks:
                raise ValueError
            self.finished = True
            return [("stop", ANTHROPIC_STOPS[event["delta"]["stop_reason"]])]
        elif kind == "error":
            raise ValueError
        return []


class _OpenAI:
    def __init__(self, names):
        self.names = names
        self.calls = {}
        self.finished = False

    def feed(self, data):
        if data == "[DONE]":
            raise ValueError
        chunk = json.loads(data)
        if "error" in chunk:
            raise ValueError
        if not chunk["choices"]:
            return []
        choice = chunk["choices"][0]
        delta = choice.get("delta") or {}
        out = []
        text = delta.get("content")
        if text is not None:
            if not isinstance(text, str):
                raise ValueError
            if text:
                out.append(("text", text))
        for part in delta.get("tool_calls") or []:
            slot = self.calls.setdefault(part["index"], {"id": None, "name": None, "args": []})
            slot["id"] = part.get("id") or slot["id"]
            function = part.get("function") or {}
            slot["name"] = function.get("name") or slot["name"]
            if isinstance(function.get("arguments"), str):
                slot["args"].append(function["arguments"])
        reason = choice.get("finish_reason")
        if reason:
            for index in sorted(self.calls):
                slot = self.calls[index]
                # Some local servers send no call id; the turn only needs a unique one.
                out.append(
                    _tool_call(
                        self.names, slot["id"] or f"call_{index}", slot["name"], slot["args"]
                    )
                )
            self.finished = True
            out.append(("stop", OPENAI_STOPS[reason]))
        return out


def _data_lines(call, max_bytes):
    """Yield the `data:` payload of each complete SSE line as it arrives."""
    buffer = bytearray()
    total = 0
    while True:
        chunk = call.read(min(8192, max_bytes + 1 - total))
        if call.stopped.is_set():
            raise ModelError("stopped")
        total += len(chunk)
        if total > max_bytes:
            raise ModelError("bad_response")
        buffer.extend(chunk)
        end = not chunk
        while (cut := buffer.find(b"\n")) >= 0 or (end and buffer):
            line = bytes(buffer if cut < 0 else buffer[:cut]).rstrip(b"\r")
            del buffer[: cut + 1 if cut >= 0 else len(buffer)]
            if line.startswith(b"data:"):
                try:
                    yield line[5:].decode().strip()
                except UnicodeDecodeError:
                    raise ModelError("bad_response") from None
        if end:
            return


def _events(settings, endpoint, payload, names, timeout, max_bytes, on_sent, cancel):
    parser = (_Anthropic if endpoint.anthropic else _OpenAI)(names)
    with model.Call(settings, endpoint, timeout) as call:
        if cancel is not None:
            call.watch(cancel)
            if cancel.is_set():
                raise ModelError("stopped")
        call.open(payload, on_sent)
        for data in _data_lines(call, max_bytes):
            try:
                events = parser.feed(data)
            except BAD_FRAME:
                raise ModelError("bad_response") from None
            yield from events
            if parser.finished:
                return
        raise ModelError("bad_response")


def _anthropic_messages(messages):
    out = []
    for message in messages:
        role = message["role"]
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message["tool_call_id"],
                "content": message["content"],
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant" and message.get("tool_calls"):
            blocks = [{"type": "text", "text": message["content"]}] if message["content"] else []
            blocks += [
                {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                for c in message["tool_calls"]
            ]
            out.append({"role": "assistant", "content": blocks})
        elif role in ("user", "assistant"):
            out.append({"role": role, "content": message["content"]})
        else:
            raise ValueError("invalid model message")
    return out


def _openai_messages(system, messages):
    out = [{"role": "system", "content": system}]
    for message in messages:
        role = message["role"]
        if role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message["tool_call_id"],
                    "content": message["content"],
                }
            )
        elif role == "assistant" and message.get("tool_calls"):
            calls = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                }
                for c in message["tool_calls"]
            ]
            out.append(
                {"role": "assistant", "content": message["content"] or None, "tool_calls": calls}
            )
        elif role in ("user", "assistant"):
            out.append({"role": role, "content": message["content"]})
        else:
            raise ValueError("invalid model message")
    return out


def stream(
    settings: FrontSettings,
    key: str | None,
    system: str,
    messages: list[dict],
    *,
    tools=(),
    timeout: float,
    max_tokens: int,
    max_input_chars: int,
    max_response_bytes: int,
    on_sent=None,
    cancel: threading.Event | None = None,
):
    """Yield events as the provider sends them:

    ("text", str)                                              reply text, in order
    ("tool_call", {"id": str, "name": str, "arguments": dict})  one complete call
    ("stop", "end" | "tool" | "length")                         exactly once, last

    Input checks (size, provider, origin, key host) raise at call time; the connection is
    made when the first event is requested. Errors are `ModelError` with the codes
    `timeout`, `http_error`, `bad_response` and `stopped` (`cancel` set). `timeout` covers
    the whole call, and closing the generator closes the connection.
    """
    tools = list(tools)
    if (
        len(system)
        + len(json.dumps(messages, ensure_ascii=False))
        + len(json.dumps(tools, ensure_ascii=False))
        > max_input_chars
    ):
        raise ValueError("model input too long")
    endpoint = model.resolve_endpoint(settings, key)
    body = {"model": settings.model, "max_tokens": max_tokens, "temperature": 0, "stream": True}
    if endpoint.anthropic:
        body.update(system=system, messages=_anthropic_messages(messages))
        if tools:
            body["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
                for t in tools
            ]
    else:
        body["messages"] = _openai_messages(system, messages)
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["schema"],
                    },
                }
                for t in tools
            ]
    payload = json.dumps(body).encode()
    names = {t["name"] for t in tools}
    return _events(settings, endpoint, payload, names, timeout, max_response_bytes, on_sent, cancel)
