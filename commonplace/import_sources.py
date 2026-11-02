"""Local, lossless readers for one-time conversation imports."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO

from .handoff.models import ThreadMessage
from .handoff.transcripts import parse

SOURCES = ("claude-code", "codex", "cursor", "t3", "grok-bot", "chatgpt", "claude-ai")


def _timestamp(value):
    if isinstance(value, str):
        return value
    if type(value) in (float, int) and math.isfinite(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return ""


def parse_import(source: str, transcript: str, fmt: str = "auto") -> list[ThreadMessage]:
    """Preserve source bytes, all branches and unknown records; never call a model.

    Cursor requires the explicit messages interchange format. T3
    accepts its underlying provider's native transcript, not a truncated view.
    Web exports are one conversation per call; clients split export arrays.
    """
    if source not in SOURCES:
        raise ValueError("Unsupported source")
    if fmt == "messages":
        messages = parse(transcript, fmt)
    elif source == "grok-bot" and fmt in ("auto", "grok-facts"):
        messages = _grok_facts(transcript)
    elif source in ("claude-code", "codex", "t3"):
        expected = source if source != "t3" else "auto"
        if fmt not in ("auto", "claude-code", "codex") or (
            source != "t3" and fmt not in ("auto", expected)
        ):
            raise ValueError("Unsupported transcript format for source")
        messages = parse(transcript, fmt if source == "t3" else expected)
    elif source in ("chatgpt", "claude-ai") and fmt == "auto":
        messages = _web_export(source, transcript)
    else:
        raise ValueError("Use format=messages with a complete role/content JSON export")
    if not messages:
        raise ValueError("Conversation has no messages")
    # Keep wrapper metadata and exact whitespace too, including empty lines
    # around JSON arrays. Index only the readable messages, not serialized JSON.
    archive = ThreadMessage(0, "system", "other", "", transcript)
    return [archive, *(replace(m, seq=i + 1) for i, m in enumerate(messages))]


def _grok_facts(transcript):
    messages = []
    for raw in StringIO(transcript, newline=""):
        if not raw.strip():
            continue  # the exact archive still retains blank lines
        record = json.loads(raw)
        if not isinstance(record, dict) or not all(
            isinstance(record.get(key), str)
            for key in ("source", "memory_type", "learned_date", "text")
        ):
            raise ValueError("Expected Grok memory fact fields")
        if (
            not record["source"].startswith("grok-bot:")
            or not record["source"][len("grok-bot:") :].strip()
            or record["memory_type"]
            not in ("profile", "journal", "shared_profile", "shared_journal")
            or not record["learned_date"].strip()
        ):
            raise ValueError("Invalid Grok memory fact provenance")
        messages.append(
            ThreadMessage(
                len(messages),
                "system",
                "message",
                record["text"],
                raw,
                at=record["learned_date"],
            )
        )
    return messages


def _web_export(source, transcript):
    data = json.loads(transcript)
    if not isinstance(data, dict):
        raise ValueError("Import one conversation object at a time")
    records = []
    if source == "chatgpt":
        mapping = data.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("Expected a ChatGPT conversation mapping")
        # All nodes, including alternate branches; an active-path projection
        # would silently forget messages from the export.
        for node in mapping.values():
            message = node.get("message") if isinstance(node, dict) else None
            if message is None:
                continue
            if not isinstance(message, dict):
                raise ValueError("Invalid ChatGPT message")
            author, content = message.get("author", {}), message.get("content", {})
            parts = content.get("parts", []) if isinstance(content, dict) else []
            if not isinstance(parts, list):
                raise ValueError("Invalid ChatGPT content")
            records.append(
                {
                    "role": author.get("role", "system") if isinstance(author, dict) else "system",
                    "content": [
                        {"type": "text", "text": part} if isinstance(part, str) else part
                        for part in parts
                    ],
                    "timestamp": _timestamp(message.get("create_time")),
                }
            )
    else:
        messages = data.get("chat_messages", data.get("messages"))
        if not isinstance(messages, list):
            raise ValueError("Expected Claude.ai chat_messages")
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("Invalid Claude.ai message")
            role = message.get("sender", message.get("role", "system"))
            records.append(
                {
                    "role": {"human": "user", "ai": "assistant"}.get(role, role),
                    "content": message.get("content") or message.get("text", ""),
                    "timestamp": _timestamp(message.get("created_at")),
                }
            )
    return parse(json.dumps(records, ensure_ascii=False), "messages")
