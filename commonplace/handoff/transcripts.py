"""Parse agent transcripts into verbatim ``ThreadMessage`` lists. Owner: part T01.

Formats: ``claude-code`` (Claude Code session JSONL), ``codex`` (Codex rollout
JSONL), ``messages`` (JSON list of ``{"role", "content"}``, content a string
or a list of content blocks). ``auto`` detects the format.

Lossless: every input line/object becomes at least one message whose ``raw``
is the exact source text; a line holding several content blocks (text plus
tool_use) becomes one message per block, each carrying the same ``raw``.
Unknown line types become ``kind="other"``. Nothing is dropped or rewritten.
JSONL ``raw`` includes the original line ending; message arrays retain the
exact JSON slice of each message object, including its original escapes.
"""

from __future__ import annotations

import json
import re
from io import StringIO

from .models import ThreadMessage

FORMATS = ("auto", "claude-code", "codex", "messages")
_DECODER = json.JSONDecoder()
_SPACE = re.compile(r"[ \t\r\n]*")
_TEXT_TYPES = {"text", "input_text", "output_text", "summary_text", "reasoning_text"}


def detect_format(text: str) -> str:
    """Return ``claude-code``, ``codex`` or ``messages``. Raises ValueError if none fits."""
    if not isinstance(text, str):
        raise ValueError("Transcript must be text")
    try:
        first, end = _DECODER.raw_decode(text, _space(text, 0))
    except ValueError as error:
        raise ValueError("Unrecognized transcript format") from error
    if isinstance(first, list) or isinstance(first, dict) and "messages" in first:
        if text[end:].strip():
            raise ValueError("Unexpected data after messages")
        _validate_messages(first)
        return "messages"
    fmt = _record_format(first)
    if fmt:
        return fmt
    raise ValueError("Unrecognized transcript format")


def parse(text: str, fmt: str = "auto", *, require_recognized: bool = True) -> list[ThreadMessage]:
    """Parse a whole transcript. ``seq`` is dense from 0. Raises ValueError on garbage."""
    if fmt not in FORMATS:
        raise ValueError("Unknown transcript format")
    if not isinstance(text, str):
        raise ValueError("Transcript must be text")
    if fmt == "auto":
        fmt = detect_format(text)
    messages: list[ThreadMessage] = []
    if fmt == "messages":
        for record, raw in _message_sources(text):
            _message(messages, record, raw, _string(record.get("timestamp")))
        return messages

    source = StringIO(text, newline="")
    recognized = False
    for line_number, raw in enumerate(source, 1):
        if not raw.strip():
            _add(messages, raw, "")
            continue
        try:
            record = json.loads(raw)
        except ValueError as error:
            if source.tell() == len(text) and _incomplete(raw, error):
                _add(messages, raw, "")
                continue
            raise ValueError(f"Invalid transcript line {line_number}") from error
        recognized |= _record_format(record) == fmt
        at = _string(record.get("timestamp")) if isinstance(record, dict) else ""
        if not isinstance(record, dict):
            _add(messages, raw, at)
        elif fmt == "claude-code":
            if _string(record.get("type")) in {"user", "assistant"} and isinstance(
                record.get("message"), dict
            ):
                _message(messages, record["message"], raw, at, record["type"])
            else:
                _add(messages, raw, at)
        else:
            _codex(messages, record, raw, at)
    if not recognized and require_recognized:
        raise ValueError("Unrecognized transcript format")
    return messages


def _space(text: str, start: int) -> int:
    return _SPACE.match(text, start).end()


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _record_format(value: object) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        return ""
    return "codex" if "payload" in value else "claude-code"


def _incomplete(raw: str, error: ValueError) -> bool:
    """Only tolerate a partial object at the live file's unterminated tail."""
    if not raw.lstrip().startswith("{") or raw.endswith(("\n", "\r")):
        return False
    if not isinstance(error, json.JSONDecodeError):
        return False
    tail = raw[error.pos :]
    return (
        error.pos >= len(raw.rstrip())
        or error.msg.startswith("Unterminated string")
        or tail in {"t", "tr", "tru", "f", "fa", "fal", "fals", "n", "nu", "nul"}
        or error.msg == "Expecting value"
        and tail == "-"
        or error.msg == "Expecting ',' delimiter"
        and error.pos > 0
        and raw[error.pos - 1].isdigit()
        and re.fullmatch(r"(?:\.[0-9]*)?(?:[eE][+-]?)?", tail) is not None
        or error.msg.startswith("Invalid \\uXXXX escape")
        and re.fullmatch(r"u[0-9a-fA-F]{0,4}", tail) is not None
    )


def _validate_messages(value: object) -> list[dict]:
    records = value.get("messages") if isinstance(value, dict) else value
    if not isinstance(records, list):
        raise ValueError("Messages must be a JSON array")
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("role"), str):
            raise ValueError("Each message must have a role and content")
        if "content" not in record or not (
            isinstance(record["content"], (str, list))
            or record["content"] is None
            and ("tool_calls" in record or "function_call" in record)
        ):
            raise ValueError("Each message must have text or block content")
    return records


def _message_sources(text: str):
    """Read exact object spans, retaining whitespace and JSON escape spelling."""
    try:
        value = json.loads(text)
    except ValueError as error:
        raise ValueError("Invalid messages JSON") from error
    _validate_messages(value)
    start = _space(text, 0)
    if isinstance(value, dict):
        position = _space(text, start + 1)
        # Locate the last messages key, matching JSON's duplicate-key semantics.
        while text[position] != "}":
            key, end = _DECODER.raw_decode(text, position)
            position = _space(text, _space(text, end) + 1)
            if key == "messages":
                start = position
            _, end = _DECODER.raw_decode(text, position)
            position = _space(text, end)
            if text[position] == "}":
                break
            position = _space(text, position + 1)
    position = _space(text, start + 1)
    while text[position] != "]":
        record, end = _DECODER.raw_decode(text, position)
        yield record, text[position:end]
        position = _space(text, end)
        if text[position] == "]":
            break
        position = _space(text, position + 1)


def _add(
    messages: list[ThreadMessage],
    raw: str,
    at: str,
    role: str = "system",
    kind: str = "other",
    text: str = "",
    name: str = "",
    call_id: str = "",
) -> None:
    messages.append(ThreadMessage(len(messages), role, kind, text, raw, name, call_id, at))


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            if isinstance(block, dict)
            and _string(block.get("type")) in _TEXT_TYPES
            and isinstance(block.get("text"), str)
            else _json(block)
            for block in content
        )
    return _json(content)


def _content(
    messages: list[ThreadMessage],
    content: object,
    raw: str,
    at: str,
    role: str,
    kind: str = "message",
    name: str = "",
    call_id: str = "",
) -> None:
    if isinstance(content, str):
        _add(messages, raw, at, role, kind, content, name, call_id)
    elif isinstance(content, list) and content:
        for block in content:
            _block(messages, block, raw, at, role, kind, name, call_id)
    else:
        _add(
            messages,
            raw,
            at,
            role,
            "tool_result" if kind == "tool_result" else "other",
            "",
            name,
            call_id,
        )


def _block(
    messages: list[ThreadMessage],
    block: object,
    raw: str,
    at: str,
    role: str,
    kind: str,
    name: str,
    call_id: str,
) -> None:
    if kind == "tool_result":
        _add(messages, raw, at, role, kind, _content_text([block]), name, call_id)
        return
    if not isinstance(block, dict):
        _add(messages, raw, at, role, "other", _json(block))
        return
    block_type = _string(block.get("type"))
    if block_type in _TEXT_TYPES and isinstance(block.get("text"), str):
        _add(messages, raw, at, role, kind, block["text"], name, call_id)
    elif block_type == "tool_use":
        _add(
            messages,
            raw,
            at,
            "assistant",
            "tool_call",
            _json(block.get("input", {})),
            _string(block.get("name")),
            _string(block.get("id")),
        )
    elif block_type == "tool_result":
        _add(
            messages,
            raw,
            at,
            "tool",
            "tool_result",
            _content_text(block.get("content", "")),
            _string(block.get("name")),
            _string(block.get("tool_use_id")),
        )
    elif block_type == "thinking":
        _add(messages, raw, at, role, "other", _string(block.get("thinking")))
    else:
        _add(messages, raw, at, role, "other", _json(block))


def _message(
    messages: list[ThreadMessage], record: dict, raw: str, at: str, role_hint: str = "system"
) -> None:
    role = _string(record.get("role")) or role_hint
    role = role if role in {"user", "assistant", "tool", "system"} else "system"
    if record.get("role") == "function":
        role = "tool"
    before = len(messages)
    if record.get("content") is not None:
        _content(
            messages,
            record["content"],
            raw,
            at,
            role,
            "tool_result" if role == "tool" else "message",
            _string(record.get("name")),
            _string(record.get("tool_call_id")),
        )
    calls = record.get("tool_calls", [])
    if not isinstance(calls, list):
        calls = []
    if isinstance(record.get("function_call"), dict):
        calls = [*calls, {"function": record["function_call"]}]
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            _add(messages, raw, at, "assistant", "other", _json(call))
            continue
        _add(
            messages,
            raw,
            at,
            "assistant",
            "tool_call",
            _content_text(function.get("arguments", "")),
            _string(function.get("name")),
            _string(call.get("id")),
        )
    if len(messages) == before:
        _add(messages, raw, at, role)


def _codex(messages: list[ThreadMessage], record: dict, raw: str, at: str) -> None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        _add(messages, raw, at)
        return
    record_type = record.get("type")
    item_type = _string(payload.get("type"))
    if record_type == "response_item":
        if item_type == "message":
            _message(messages, payload, raw, at)
        elif item_type in {"function_call", "custom_tool_call", "local_shell_call"}:
            field = {
                "function_call": "arguments",
                "custom_tool_call": "input",
                "local_shell_call": "action",
            }[item_type]
            _add(
                messages,
                raw,
                at,
                "assistant",
                "tool_call",
                _content_text(payload.get(field, "")),
                _string(payload.get("name"))
                or ("local_shell" if item_type == "local_shell_call" else ""),
                _string(payload.get("call_id")) or _string(payload.get("id")),
            )
        elif item_type in {
            "function_call_output",
            "custom_tool_call_output",
            "local_shell_call_output",
        }:
            _add(
                messages,
                raw,
                at,
                "tool",
                "tool_result",
                _content_text(payload.get("output", "")),
                _string(payload.get("name")),
                _string(payload.get("call_id")),
            )
        elif item_type == "reasoning":
            before = len(messages)
            for field in ("summary", "content"):
                content = payload.get(field)
                if content:
                    _content(messages, content, raw, at, "assistant", "other")
            if before == len(messages):
                _add(messages, raw, at, "assistant")
        else:
            _add(messages, raw, at)
    elif record_type == "event_msg" and item_type in {"user_message", "agent_message"}:
        _content(
            messages,
            payload.get("message", ""),
            raw,
            at,
            "user" if item_type == "user_message" else "assistant",
        )
    else:
        _add(messages, raw, at)
