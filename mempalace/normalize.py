#!/usr/bin/env python3
"""
normalize.py — Convert any chat export format to MemPalace transcript format.

Supported:
    - Plain text with > markers (pass through)
    - Claude.ai JSON export
    - ChatGPT conversations.json (a single conversation, or the top-level
      array of them that a real data export ships)
    - Claude Code JSONL (with tool_use/tool_result block capture)
    - OpenAI Codex CLI JSONL
    - Gemini CLI JSONL (~/.gemini/tmp/<project_hash>/chats/session-*.jsonl)
    - Pi agent JSONL
    - Gemini CLI / Google AI Studio JSON sessions (contents / messages / flat list)
    - Continue.dev session JSON (~/.continue/sessions/*.json)
    - Slack JSON export
    - Plain text (pass through for paragraph chunking)

No API key. No internet. Everything local.
"""

import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Optional, Tuple

from .fsio import read_source_text


class UnparsedCodexTranscriptError(ValueError):
    """A recognized Codex rollout did not yield a supported conversation."""


# Provenance footer appended to Slack transcript output so downstream consumers
# know the speaker roles are positionally assigned, not verified.
_SLACK_PROVENANCE_FOOTER = (
    "\n[source: slack-export | multi-party chat — speaker roles are positional, not verified]"
)

# The ``NormalizeRun`` of an open ``recording_transforms`` block; None (the
# default) records nothing and keeps user turns as written.
_NORMALIZE_RUN = ContextVar("_NORMALIZE_RUN", default=None)


def _record(transform: str) -> None:
    """Note that the parse in progress applied an RFC 002 transformation."""
    run = _NORMALIZE_RUN.get()
    if run is not None:
        run.applied.add(transform)


# ─── Noise stripping ─────────────────────────────────────────────────────
# Claude Code and other tools inject system tags, hook output, and UI chrome
# into transcripts. These waste drawer space and pollute search results.
#
# Verbatim is sacred — every pattern here is anchored to line boundaries and
# refuses to cross blank lines, so a stray unclosed tag in one message can
# never eat content from neighboring messages. When in doubt, leave text
# alone.

_NOISE_TAGS = (
    "system-reminder",
    "command-message",
    "command-name",
    "task-notification",
    "user-prompt-submit-hook",
    "hook_output",
)


def _tag_pattern(name: str) -> "re.Pattern[str]":
    # Opening tag must begin a line (optionally after a `> ` blockquote marker,
    # since _messages_to_transcript prefixes lines with `> `). Body is lazy but
    # forbidden from crossing a blank line, so a dangling open tag can't span
    # multiple messages. Closing tag eats optional trailing whitespace + newline.
    return re.compile(
        rf"(?m)^(?:> )?<{name}(?:\s[^>]*)?>" rf"(?:(?!\n\s*\n)[\s\S])*?" rf"</{name}>[ \t]*\n?"
    )


_NOISE_TAG_PATTERNS = [_tag_pattern(t) for t in _NOISE_TAGS]

# Strings that identify an entire noise line when found at its start.
# Matched case-sensitively and anchored to line-start so user prose mentioning
# e.g. "current time:" in a sentence is untouched.
_NOISE_LINE_PREFIXES = (
    "CURRENT TIME:",
    "VERIFIED FACTS (do not contradict)",
    "AGENT SPECIALIZATION:",
    "Checking verified facts...",
    "Injecting timestamp...",
    "Starting background pipeline...",
    "Checking emotional weights...",
    "Auto-save reminder...",
    "Checking pipeline...",
    "MemPalace auto-save checkpoint.",
)

_NOISE_LINE_PATTERNS = [
    re.compile(rf"(?m)^(?:> )?{re.escape(p)}.*\n?") for p in _NOISE_LINE_PREFIXES
]

# Claude Code TUI hook-run chrome, e.g. "Ran 2 Stop hook", "Ran 1 PreCompact hook".
# Line-anchored, case-sensitive, explicit hook names — prose like
# "our CI has a stop hook" stays intact.
_HOOK_LINE_RE = re.compile(
    r"(?m)^(?:> )?Ran \d+ (?:Stop|PreCompact|PreToolUse|PostToolUse|UserPromptSubmit|Notification|SessionStart|SessionEnd) hook[s]?.*\n?"
)

# "… +N lines" collapsed-output marker, line-anchored.
_COLLAPSED_LINES_RE = re.compile(r"(?m)^(?:> )?…\s*\+\d+ lines.*\n?")

# Claude Code collapsed-output chrome "[N tokens] (ctrl+o to expand)". Narrow
# shape — a bare "(ctrl+o to expand)" in user prose stays intact.
_CTRL_O_RE = re.compile(r"\s*\[\d+\s+tokens?\]\s*\(ctrl\+o to expand\)")


def strip_noise(text: str) -> str:
    """Remove system tags, hook output, and Claude Code UI chrome from text.

    All patterns are line-anchored. User prose that happens to mention these
    strings inline (e.g., documenting them) is preserved verbatim.
    """
    removed = 0
    for pat in (
        *_NOISE_TAG_PATTERNS,
        *_NOISE_LINE_PATTERNS,
        _HOOK_LINE_RE,
        _COLLAPSED_LINES_RE,
        _CTRL_O_RE,
    ):
        text, n = pat.subn("", text)
        removed += n
    if not removed:
        # Nothing was stripped, so the message keeps its own blank lines.
        return text
    _record("strip_tool_chrome")
    # Collapse the runs of blank lines the removals left behind; one the
    # writer typed is collapsed too, so say so when it happens.
    collapsed = re.sub(r"\n{4,}", "\n\n\n", text)
    if collapsed != text:
        _record("whitespace_collapse_internal")
    return collapsed.strip()


_MAX_TRANSCRIPT_BYTES = 500 * 1024 * 1024  # 500 MB safety limit


def _read_transcript_file(filepath: str) -> str:
    """Read a transcript source file with the same safety checks normalize()
    and normalize_conversations() both need: no symlinks, regular files only,
    size-capped, BOM-tolerant.
    """
    return _read_transcript_file_with_mtime(filepath)[0]


def _read_transcript_file_with_mtime(filepath: str) -> Tuple[str, float]:
    """``_read_transcript_file`` plus the mtime from the ``fstat`` taken
    before the read. A stat after the read can record an mtime for bytes
    the read never saw, and the next mine then skips the appended tail.

    Inside ``recording_transforms`` the codec the bytes decoded with is kept
    on the run as ``encoding``.
    """
    try:
        text, encoding, mtime = read_source_text(filepath, max_bytes=_MAX_TRANSCRIPT_BYTES)
    except OSError as e:
        raise IOError(f"Could not read {filepath}: {e}") from e
    run = _NORMALIZE_RUN.get()
    if run is not None:
        run.encoding = encoding
    return text, mtime


def normalize(filepath: str) -> str:
    """
    Load a file and normalize to transcript format if it's a chat export.
    Plain text files pass through unchanged.
    """
    content = _read_transcript_file(filepath)

    if not content.strip():
        return content

    # Already has > markers — pass through unchanged.
    lines = content.split("\n")
    if sum(1 for line in lines if line.strip().startswith(">")) >= 3:
        return content

    # Try JSON normalization. strip_noise is applied inside the Claude Code
    # JSONL parser (the only format that injects system tags/hook chrome);
    # other formats pass through verbatim.
    ext = Path(filepath).suffix.lower()
    if ext in (".json", ".jsonl") or content.strip()[:1] in ("{", "["):
        normalized = _try_normalize_json(content)
        if normalized:
            return normalized

    return content


def normalize_conversations(filepath: str) -> list:
    """Like normalize(), but keeps each conversation in a bundle export (a
    Claude.ai privacy export, a ChatGPT ``conversations.json``) as a separate
    string instead of joining them into one. Non-bundle formats normalize to a
    one-element list.
    """
    return normalize_conversations_with_mtime(filepath)[0]


def normalize_conversations_with_mtime(filepath: str) -> Tuple[list, float]:
    """``normalize_conversations`` plus the mtime of the bytes it read."""
    content, mtime = _read_transcript_file_with_mtime(filepath)
    return _split_conversations(content, filepath), mtime


def _split_conversations(content: str, filepath: str) -> list:
    if not content.strip():
        return []

    lines = content.split("\n")
    if sum(1 for line in lines if line.strip().startswith(">")) >= 3:
        return [content]

    ext = Path(filepath).suffix.lower()
    if ext in (".json", ".jsonl") or content.strip()[:1] in ("{", "["):
        split = _try_normalize_json_split(content)
        if split:
            return split

    return [content]


def _try_normalize_json(content: str) -> Optional[str]:
    """Try all known JSON chat schemas, joining a multi-conversation bundle
    into one string. See ``_try_normalize_json_split`` for the unjoined form.
    """
    split = _try_normalize_json_split(content)
    if split is None:
        return None
    return "\n\n".join(split)


def _attempt(parser, source):
    """Run one parser, keeping the transformations it recorded only when it
    parsed ``source``: a parser that gives up leaves the text unchanged."""
    run = _NORMALIZE_RUN.get()
    if run is None:
        return parser(source)
    kept, run.applied = run.applied, set()
    try:
        result = parser(source)
    finally:
        tried, run.applied = run.applied, kept
    if result:
        kept |= tried
    return result


def _try_normalize_json_split(content: str) -> Optional[list]:
    """Try all known JSON chat schemas, returning each conversation found as
    a separate list entry (bundle formats) or a single-element list.
    """

    normalized = _attempt(_try_claude_code_jsonl, content)
    if normalized:
        return [normalized]

    normalized = _attempt(_try_codex_jsonl, content)
    if normalized:
        return [normalized]

    # A recognized rollout must never become raw JSON text just because its
    # conversation schema changed (or its first turn is still incomplete).
    # Keep this outside the parser so additional supported schemas can return
    # normally without changing the fallback boundary.
    for line in content.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "session_meta":
            raise UnparsedCodexTranscriptError(
                "Codex rollout contains no complete supported conversation; "
                "refusing raw JSON fallback"
            )

    normalized = _attempt(_try_gemini_jsonl, content)
    if normalized:
        return [normalized]

    normalized = _attempt(_try_pi_jsonl, content)
    if normalized:
        return [normalized]

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    normalized = _attempt(_try_gemini_json, data)
    if normalized:
        return [normalized]

    split = _attempt(_try_claude_ai_json_split, data)
    if split:
        return split

    split = _attempt(_try_chatgpt_export_json_split, data)
    if split:
        return split

    for parser in (_try_chatgpt_json, _try_continue_json, _try_slack_json):
        normalized = _attempt(parser, data)
        if normalized:
            return [normalized]

    return None


def _try_claude_code_jsonl(content: str) -> Optional[str]:
    """Claude Code JSONL sessions."""
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    messages = []
    tool_use_map = {}  # tool_use_id → tool_name

    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        msg_type = entry.get("type", "")
        # system, summary and other entries carry session bookkeeping and UI
        # chrome rather than conversation, so skipping them is not recorded.
        message = entry.get("message", {})
        if not isinstance(message, dict):
            continue
        msg_content = message.get("content", "")

        # Build tool_use_map from assistant messages
        if msg_type == "assistant" and isinstance(msg_content, list):
            for block in msg_content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id", "")
                    if tool_id:
                        tool_use_map[tool_id] = block.get("name", "Unknown")

        if msg_type in ("human", "user"):
            # Check if this message is tool_results only (no user text)
            is_tool_only = isinstance(msg_content, list) and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in msg_content
            )
            text = _extract_content(msg_content, tool_use_map=tool_use_map)
            # Strip Claude Code system-injected noise per message, never across
            # message boundaries — prevents span-eating.
            if text:
                text = strip_noise(text)
            if text:
                if is_tool_only and messages and messages[-1][0] == "assistant":
                    # Append tool results to the previous assistant message
                    prev_role, prev_text = messages[-1]
                    messages[-1] = (prev_role, prev_text + "\n" + text)
                elif not is_tool_only:
                    messages.append(("user", text))
                else:
                    _record("tool_result_omitted")
        elif msg_type == "assistant":
            text = _extract_content(msg_content, tool_use_map=tool_use_map)
            if text:
                text = strip_noise(text)
            if text:
                # If previous message is also assistant (multi-turn tool loop),
                # merge into the same assistant turn
                if messages and messages[-1][0] == "assistant":
                    prev_role, prev_text = messages[-1]
                    messages[-1] = (prev_role, prev_text + "\n" + text)
                else:
                    messages.append(("assistant", text))

    if len(messages) >= 2:
        return _messages_to_transcript(messages)
    return None


def _try_codex_jsonl(content: str) -> Optional[str]:
    """OpenAI Codex CLI sessions (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl).

    Uses only event_msg entries (user_message / agent_message) which represent
    the canonical conversation turns. response_item entries are skipped because
    they include synthetic context injections and duplicate the real messages.
    """
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    messages = []
    has_session_meta = False
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        entry_type = entry.get("type", "")
        if entry_type == "session_meta":
            has_session_meta = True
            continue

        payload = entry.get("payload", {})
        if not isinstance(payload, dict):
            continue

        if entry_type == "response_item":
            # Messages repeat the event_msg turns; tool calls, their output
            # and reasoning appear only here and are not filed.
            item_type = payload.get("type", "")
            if item_type.endswith("_output"):
                _record("tool_result_omitted")
            elif item_type != "message":
                _record("conversations.content_block_drop")
            continue

        if entry_type != "event_msg":
            continue

        payload_type = payload.get("type", "")
        msg = payload.get("message")
        if not isinstance(msg, str):
            continue
        text = msg.strip()
        if not text:
            continue

        if payload_type == "user_message":
            messages.append(("user", text))
        elif payload_type == "agent_message":
            messages.append(("assistant", text))

    if len(messages) >= 2 and has_session_meta:
        return _messages_to_transcript(messages)
    return None


def _try_gemini_jsonl(content: str) -> Optional[str]:
    """Gemini CLI sessions (~/.gemini/tmp/<project_hash>/chats/session-*.jsonl).

    Schema (per google-gemini/gemini-cli#15292): a session_metadata record
    on the first line, then a stream of ``{"type": "user", "content":
    [{"text": "..."}]}`` and ``{"type": "gemini", "content": [...]}``
    records, with optional ``message_update`` records carrying token
    counts only.

    Detection requires a ``session_metadata`` record so this parser does
    not false-positive against Claude Code or Codex JSONL passed through
    the dispatch chain. Any ``user``/``gemini`` lines that appear before
    ``session_metadata`` are discarded — they are treated as preamble
    noise, not conversational turns. ``message_update`` entries are
    skipped — they have no message text. Multiple text blocks within a
    single message's content array are concatenated in order with nothing
    between them, as Gemini's own clients read a message.
    """
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    messages = []
    has_session_metadata = False
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        entry_type = entry.get("type", "")
        if entry_type == "session_metadata":
            has_session_metadata = True
            continue

        # Discard everything (including user/gemini turns) until the
        # session_metadata sentinel has been seen.
        if not has_session_metadata:
            continue

        if entry_type not in ("user", "gemini"):
            # Skips message_update, system events, anything else.
            continue

        content_blocks = entry.get("content", [])
        if not isinstance(content_blocks, list):
            continue

        parts = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
            elif any(key != "text" for key in block):
                # functionCall, functionResponse, inlineData and the like.
                _record("conversations.content_block_drop")
        joined = "".join(parts)
        if not joined.strip():
            continue

        if entry_type == "user":
            messages.append(("user", joined))
        else:  # "gemini"
            messages.append(("assistant", joined))

    if len(messages) >= 2 and has_session_metadata:
        return _messages_to_transcript(messages)
    return None


def _try_pi_jsonl(content: str) -> Optional[str]:
    """Pi agent sessions (~/.config/pi/agent/sessions/{cwd}/{timestamp}_{uuid}.jsonl).

    Pi stores sessions as JSONL with a tree-structured message history.
    User messages have role "user" with content as string or [{type, text}] blocks.
    Assistant messages have role "assistant" with content as [{type, text}] blocks
    (may also include "thinking" blocks which are skipped by _extract_content).
    Tool results (role "toolResult") are skipped — operational, not conversation.

    Format documented at github.com/badlogic/pi-mono session.md.
    """
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    messages = []
    has_session_header = False
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        entry_type = entry.get("type", "")
        if entry_type == "session" and "version" in entry:
            has_session_header = True
            continue

        if entry_type != "message":
            continue

        message = entry.get("message", {})
        if not isinstance(message, dict):
            continue

        role = message.get("role", "")
        if role == "toolResult":
            if message.get("content"):
                _record("tool_result_omitted")
            continue
        text = _extract_content(message.get("content", ""))

        if role == "user" and text:
            messages.append(("user", text))
        elif role == "assistant" and text:
            messages.append(("assistant", text))

    if len(messages) >= 2 and has_session_header:
        return _messages_to_transcript(messages)
    return None


def _try_gemini_json(data) -> Optional[str]:
    """Gemini CLI / Google AI Studio JSON sessions.

    Handles three layouts:

    1. **Gemini API contents format** — used by Gemini CLI session files
       (``~/.gemini/sessions/*.json``):
       ``{"contents": [{"role": "user", "parts": [{"text": "..."}]}, ...]}``

    2. **Messages wrapper** — exports that wrap the conversation under a
       ``messages`` key:
       ``{"messages": [{"role": "user", "content": "..."}, {"role": "model", "content": "..."}]}``

    3. **Flat messages list** — top-level array form:
       ``[{"role": "user", "content": "..."}, {"role": "model", "content": "..."}]``

    Gemini uses ``"model"`` as the assistant role (not ``"assistant"``).
    Detection requires at least one ``role="model"`` entry to disambiguate
    from Claude/ChatGPT exports that use ``"assistant"``. This parser is
    placed *before* ``_try_claude_ai_json`` in the dispatch chain so that
    the layout-2 ``{"messages": [...]}`` wrapper does not get silently
    claimed by the Claude parser, which would drop the model turns.
    """
    contents = None

    # Layout 1: {"contents": [...]}
    if isinstance(data, dict) and "contents" in data:
        contents = data["contents"]
    # Layout 2a: {"messages": [...]}
    elif isinstance(data, dict) and "messages" in data:
        contents = data["messages"]
    # Layout 2b: top-level list
    elif isinstance(data, list):
        contents = data

    if not isinstance(contents, list) or len(contents) < 2:
        return None

    messages = []
    has_model_role = False
    for item in contents:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "")

        # Extract text — try "parts" first (Gemini API), then "content" (flat).
        text = ""
        parts = item.get("parts")
        if isinstance(parts, list):
            text_parts = []
            for p in parts:
                if isinstance(p, str):
                    text_parts.append(p)
                elif isinstance(p, dict) and isinstance(p.get("text"), str):
                    text_parts.append(p["text"])
                elif p:
                    # inlineData, functionCall, functionResponse and the like.
                    _record("conversations.content_block_drop")
            # Gemini's own clients read a message as its text parts
            # concatenated, so that is the message as written.
            text = "".join(text_parts).strip()
        else:
            text = _extract_content(item.get("content", ""))

        if not text:
            continue

        if role == "user":
            messages.append(("user", text))
        elif role == "model":
            messages.append(("assistant", text))
            has_model_role = True
        elif role == "assistant":
            # Defensive: some hand-crafted exports use "assistant" even
            # for Gemini sessions. Accept but don't flip has_model_role.
            messages.append(("assistant", text))
        else:
            # System and other roles are not filed.
            _record("conversations.content_block_drop")

    # Disambiguator: must have seen at least one role="model" entry.
    # This prevents the Gemini parser from claiming Claude/ChatGPT data.
    if not has_model_role:
        return None

    if len(messages) >= 2:
        return _messages_to_transcript(messages)
    return None


def _try_claude_ai_json(data) -> Optional[str]:
    """Claude.ai JSON export: flat messages list or privacy export with chat_messages."""
    split = _try_claude_ai_json_split(data)
    if split is None:
        return None
    return "\n\n".join(split)


def _try_claude_ai_json_split(data) -> Optional[list]:
    """Same as ``_try_claude_ai_json`` but keeps each conversation in a
    privacy export as its own list entry instead of joining them.
    """
    if isinstance(data, dict):
        data = data.get("messages", data.get("chat_messages", []))
    if not isinstance(data, list):
        return None

    # Privacy export: array of conversation objects, each containing its own
    # message list under "chat_messages" or "messages" (both variants seen in the wild).
    if data and isinstance(data[0], dict) and ("chat_messages" in data[0] or "messages" in data[0]):
        transcripts = []
        for convo in data:
            if not isinstance(convo, dict):
                continue
            chat_msgs = convo.get("chat_messages") or convo.get("messages", [])
            transcript = _attempt(_claude_conversation, chat_msgs)
            if transcript:
                transcripts.append(transcript)
        if transcripts:
            return transcripts
        return None

    # Flat messages list
    transcript = _claude_conversation(data)
    return [transcript] if transcript else None


def _claude_conversation(chat_msgs) -> Optional[str]:
    """The transcript of one Claude.ai message list, or None below two turns."""
    messages = _collect_claude_messages(chat_msgs)
    if len(messages) >= 2:
        return _messages_to_transcript(messages)
    return None


def _collect_claude_messages(items) -> list:
    """Extract (role, text) pairs from a Claude.ai message list.

    Accepts both ``role`` (API format) and ``sender`` (privacy export) as the
    author field, and falls back to a top-level ``text`` key when the
    ``content`` blocks are empty or absent.
    """
    messages = []
    for item in items:
        if not isinstance(item, dict):
            continue
        role = item.get("role") or item.get("sender", "")
        text = _extract_content(item.get("content", "")) or (item.get("text") or "").strip()
        if not text:
            continue
        if role in ("user", "human"):
            messages.append(("user", text))
        elif role in ("assistant", "ai"):
            messages.append(("assistant", text))
        else:
            # System and other roles are not filed.
            _record("conversations.content_block_drop")
    return messages


def _try_chatgpt_json(data) -> Optional[str]:
    """ChatGPT conversations.json with mapping tree.

    Every nested shape is type-checked rather than assumed: this parser is
    reached from ``_try_chatgpt_export_json_split`` for each element of any
    top-level JSON array, so it must return None on unrelated payloads that
    merely carry a ``mapping`` key instead of raising.
    """
    if not isinstance(data, dict) or not isinstance(data.get("mapping"), dict):
        return None
    mapping = data["mapping"]
    # The branch the conversation ended on, so drafts and regenerations the
    # user moved away from are not filed. Without a usable current_node, the
    # first-child walk from the root.
    messages = _attempt(
        _chatgpt_conversation, _chatgpt_active_path(mapping, data.get("current_node"))
    ) or _attempt(_chatgpt_conversation, _chatgpt_root_path(mapping))
    if messages:
        return _messages_to_transcript(messages)
    return None


def _chatgpt_conversation(path: list) -> Optional[list]:
    """The messages along ``path``, or None when there are fewer than two."""
    messages = _chatgpt_messages(path)
    return messages if len(messages) >= 2 else None


# Node ids index a dict and a visited set, so anything unhashable (a nested
# object rather than an id) ends a ChatGPT tree walk.


def _chatgpt_active_path(mapping: dict, current_id) -> list:
    path = []
    visited = set()
    while isinstance(current_id, str) and current_id not in visited:
        visited.add(current_id)
        node = mapping.get(current_id)
        if not isinstance(node, dict):
            break
        path.append(node)
        current_id = node.get("parent")
    path.reverse()
    return path


def _chatgpt_root_path(mapping: dict) -> list:
    # Prefer a root with parent=None AND no message (synthetic root).
    root_id = None
    fallback_root = None
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        if node.get("parent") is None:
            if node.get("message") is None:
                root_id = node_id
                break
            if fallback_root is None:
                fallback_root = node_id
    path = []
    visited = set()
    current_id = root_id or fallback_root
    while current_id and current_id not in visited:
        visited.add(current_id)
        node = mapping.get(current_id)
        if not isinstance(node, dict):
            break
        path.append(node)
        children = node.get("children")
        next_id = children[0] if isinstance(children, list) and children else None
        current_id = next_id if isinstance(next_id, str) else None
    return path


def _chatgpt_messages(path: list) -> list:
    messages = []
    for node in path:
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        author = msg.get("author")
        role = author.get("role", "") if isinstance(author, dict) else ""
        content = msg.get("content", {})
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            parts = []
        texts = [p for p in parts if isinstance(p, str) and p]
        if len(texts) < len([p for p in parts if p]):
            # Image pointers and other non-text parts.
            _record("conversations.content_block_drop")
        text = " ".join(texts).strip()
        if not text:
            continue
        if role in ("user", "assistant"):
            if len(texts) > 1:
                _record("line_join_spaces")
            messages.append((role, text))
        elif role == "tool":
            _record("tool_result_omitted")
        else:
            _record("conversations.content_block_drop")
    return messages


def _try_chatgpt_export_json_split(data) -> Optional[list]:
    """ChatGPT data export: top-level array of conversation objects.

    The ``conversations.json`` OpenAI ships is an *array*, while
    ``_try_chatgpt_json`` handles the single conversation object inside it.
    Without this the whole export falls through to the plain-text path and is
    chunked as raw JSON: the drawers hold serialized structure sliced at
    arbitrary offsets, and every speaker turn is gone.

    Each conversation is kept as its own segment rather than concatenated (see
    ``normalize_conversations``); the joined form is reached through
    ``_try_normalize_json``.

    Runs after ``_try_gemini_json`` and ``_try_claude_ai_json_split`` and before
    the ``_try_chatgpt_json``/``_try_continue_json``/``_try_slack_json`` loop.
    That position is safe in both directions: Gemini requires a ``role="model"``
    entry and Claude.ai requires ``chat_messages``/``messages`` on the first
    element, neither of which a ChatGPT conversation object has, while Slack
    entries carry no ``mapping`` and Continue.dev sessions are not arrays at
    all, so this parser declines them and they fall through unchanged.
    """
    if not isinstance(data, list):
        return None

    transcripts = []
    for convo in data:
        transcript = _attempt(_try_chatgpt_json, convo)
        if transcript:
            transcripts.append(transcript)
    # None, not [], so an array of other JSON still reaches the later parsers.
    return transcripts or None


def _try_slack_json(data) -> Optional[str]:
    """
    Slack channel export: [{"type": "message", "user": "...", "text": "..."}]

    Slack exports are multi-party chats where no speaker is inherently the
    "user" or "assistant".  To preserve exchange-pair chunking (which relies
    on ``>`` markers from the ``user`` role), we still alternate roles, but
    prefix each message with the speaker ID so downstream consumers can
    distinguish the original author.  A provenance header marks the
    transcript as a Slack import.
    """
    if not isinstance(data, list):
        return None
    messages = []
    seen_users = {}
    last_role = None
    for item in data:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        # Exports carry "user": null for bots and deleted accounts; file
        # those turns under "unknown" rather than dropping or crashing on them.
        raw_user_id = str(item.get("user") or item.get("username") or "")
        # Sanitize speaker ID: strip brackets, newlines, and control chars
        # to prevent chunk-boundary injection via crafted exports
        user_id = re.sub(r"[\[\]\n\r\x00-\x1f]", "_", raw_user_id).strip() or "unknown"
        if item.get("files") or item.get("attachments"):
            # Uploaded files and link attachments are not filed.
            _record("conversations.content_block_drop")
        text = item.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            continue
        if user_id not in seen_users:
            # Alternate roles so exchange chunking works with any number of speakers
            if not seen_users:
                seen_users[user_id] = "user"
            elif last_role == "user":
                seen_users[user_id] = "assistant"
            else:
                seen_users[user_id] = "user"
        last_role = seen_users[user_id]
        # Prefix with speaker ID so the original author is preserved
        messages.append((seen_users[user_id], f"[{user_id}] {text}"))
    if len(messages) >= 2:
        _record("speaker_role_assignment")
        _record("synthesized_marker")
        return _messages_to_transcript(messages) + _SLACK_PROVENANCE_FOOTER
    return None


def _try_continue_json(data) -> Optional[str]:
    """Continue.dev session JSON (~/.continue/sessions/*.json).

    Sessions contain a ``history`` array of ``{role, content}`` message objects,
    plus optional metadata (``title``, ``sessionId``, ``dateCreated``).
    System messages are skipped.  Tool-call messages (role ``tool``) are
    formatted inline when they contain text content.
    """
    if not isinstance(data, dict) or "history" not in data:
        return None
    history = data["history"]
    if not isinstance(history, list):
        return None

    messages = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "")
        content = item.get("content", "")

        # Extract text from string or list-of-blocks content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    else:
                        _record("conversations.content_block_drop")
                elif isinstance(block, str):
                    parts.append(block)
            text = "\n".join(p for p in parts if p).strip()
        elif isinstance(content, str):
            text = content.strip()
        else:
            continue

        if not text:
            continue

        if role == "user":
            messages.append(("user", text))
        elif role == "assistant":
            messages.append(("assistant", text))
        elif role == "tool":
            # Append tool output to the previous assistant turn if possible
            if messages and messages[-1][0] == "assistant":
                prev_role, prev_text = messages[-1]
                messages[-1] = (prev_role, prev_text + "\n" + f"[tool] {text}")
                _record("synthesized_marker")
            else:
                _record("tool_result_omitted")
        else:
            # System and other roles are not filed.
            _record("conversations.content_block_drop")

    if len(messages) >= 2:
        return _messages_to_transcript(messages)
    return None


def _extract_content(content, tool_use_map: dict = None) -> str:
    """Pull text from content — handles str, list of blocks, or dict.

    Args:
        content: Message content — string, list of content blocks, or dict.
        tool_use_map: Optional mapping of tool_use_id → tool_name, used to
                      select the right formatting strategy for tool_result blocks.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                block_type = item.get("type")
                if block_type == "text":
                    parts.append(item.get("text", ""))
                elif block_type == "tool_use":
                    parts.append(_format_tool_use(item))
                elif block_type == "tool_result":
                    tid = item.get("tool_use_id", "")
                    tname = (tool_use_map or {}).get(tid, "Unknown")
                    result_content = item.get("content", "")
                    formatted = _format_tool_result(result_content, tname)
                    if formatted:
                        parts.append(formatted)
                else:
                    # Thinking, image, document and other blocks are not filed.
                    _record("conversations.content_block_drop")
        return "\n".join(p for p in parts if p).strip()
    if isinstance(content, dict):
        return content.get("text", "").strip()
    return ""


def _format_tool_use(block: dict) -> str:
    """Format a tool_use block into a human-readable one-liner."""
    name = block.get("name", "Unknown")
    inp = block.get("input", {})
    if isinstance(inp, list):
        inp = {}

    if name == "Bash":
        cmd = inp.get("command", "")
        _note_input_left_out(inp, ("command",))
        if len(cmd) > 200:
            _record("conversations.tool_input_summarize")
            cmd = cmd[:200] + "..."
        return f"[Bash] {cmd}"

    if name == "Read":
        path = inp.get("file_path", "?")
        offset = inp.get("offset")
        limit = inp.get("limit")
        if offset is not None and limit is not None:
            _note_input_left_out(inp, ("file_path", "offset", "limit"))
            try:
                return f"[Read {path}:{offset}-{int(offset) + int(limit)}]"
            except (ValueError, TypeError):
                return f"[Read {path}:{offset}+{limit}]"
        _note_input_left_out(inp, ("file_path",))
        return f"[Read {path}]"

    if name == "Grep":
        pattern = inp.get("pattern", "")
        target_key = "path" if inp.get("path") else "glob"
        _note_input_left_out(inp, ("pattern", target_key))
        return f"[Grep] {pattern} in {inp.get(target_key) or ''}"

    if name == "Glob":
        pattern = inp.get("pattern", "")
        _note_input_left_out(inp, ("pattern",))
        return f"[Glob] {pattern}"

    if name in ("Edit", "Write"):
        path = inp.get("file_path", "?")
        _note_input_left_out(inp, ("file_path",))
        return f"[{name} {path}]"

    # Unknown tool — serialize input, truncate
    summary = json.dumps(inp, separators=(",", ":"))
    if len(summary) > 200:
        _record("conversations.tool_input_summarize")
        summary = summary[:200] + "..."
    return f"[{name}] {summary}"


def _note_input_left_out(inp: dict, shown: tuple) -> None:
    """Record a tool call whose one-liner leaves out part of its input."""
    if any(value not in (None, "", [], {}) for key, value in inp.items() if key not in shown):
        _record("conversations.tool_input_summarize")


_TOOL_RESULT_MAX_LINES_BASH = 20  # head and tail line count
_TOOL_RESULT_MAX_MATCHES = 20  # Grep/Glob cap
_TOOL_RESULT_MAX_BYTES = 2048  # fallback cap for unknown tools


def _record_truncation() -> None:
    """A tool result was cut, with a marker string where the rest was."""
    _record("tool_result_truncate")
    _record("synthesized_marker")


def _format_tool_result(content, tool_name: str) -> str:
    """Format a tool_result based on the originating tool's type.

    Args:
        content: Result text (str) or list of content blocks (list of dicts).
        tool_name: Name of the tool that produced this result.

    Returns:
        Formatted string prefixed with ``→ ``, or empty string if omitted.
    """
    # Normalize list-of-blocks to plain text
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                # Images and other non-text blocks of a tool result.
                _record("conversations.content_block_drop")
        text = "\n".join(parts)
    else:
        text = str(content) if content else ""

    text = text.strip()
    if not text:
        return ""

    # Read/Edit/Write — omit result (content is in palace or git)
    if tool_name in ("Read", "Edit", "Write"):
        _record("tool_result_omitted")
        return ""

    lines = text.split("\n")

    # Bash — head + tail
    if tool_name == "Bash":
        n = _TOOL_RESULT_MAX_LINES_BASH
        if len(lines) <= n * 2:
            return "→ " + "\n→ ".join(lines)
        _record_truncation()
        head = lines[:n]
        tail = lines[-n:]
        omitted = len(lines) - 2 * n
        return (
            "→ "
            + "\n→ ".join(head)
            + f"\n→ ... [{omitted} lines omitted] ..."
            + "\n→ "
            + "\n→ ".join(tail)
        )

    # Grep/Glob — cap matches
    if tool_name in ("Grep", "Glob"):
        cap = _TOOL_RESULT_MAX_MATCHES
        if len(lines) <= cap:
            return "→ " + "\n→ ".join(lines)
        _record_truncation()
        kept = lines[:cap]
        remaining = len(lines) - cap
        return "→ " + "\n→ ".join(kept) + f"\n→ ... [{remaining} more matches]"

    # Unknown — byte cap
    if len(text) > _TOOL_RESULT_MAX_BYTES:
        _record_truncation()
        return "→ " + text[:_TOOL_RESULT_MAX_BYTES] + f"... [truncated, {len(text)} chars]"
    return "→ " + text


class NormalizeRun:
    """What one file's parse inside ``recording_transforms`` did: the RFC 002
    transformations it applied (``applied``) and the codec its bytes decoded
    with (``encoding``)."""

    def __init__(self, spellcheck: bool):
        self.spellcheck = spellcheck
        self.applied: set = set()
        self.encoding = "utf-8"

    @property
    def transforms(self) -> str:
        """The ``transforms`` drawer metadata value for ``applied``."""
        # Lazy: importing the sources package loads its adapter registry.
        from mempalace.sources.transforms import format_transforms

        return format_transforms(self.applied)


@contextmanager
def recording_transforms(spellcheck: bool = False):
    """Record the transformations applied to conversations parsed inside this
    block, and spell-correct their user messages when ``spellcheck`` is on.

    Yields a ``NormalizeRun``. Turns are kept as written otherwise.
    """
    run = NormalizeRun(spellcheck)
    token = _NORMALIZE_RUN.set(run)
    try:
        yield run
    finally:
        _NORMALIZE_RUN.reset(token)


def _messages_to_transcript(messages: list) -> str:
    """Convert [(role, text), ...] to transcript format with > markers.

    Turns are kept as written unless the caller opted in to spell correction
    with ``recording_transforms(spellcheck=True)``, which only touches user
    messages.
    """
    run = _NORMALIZE_RUN.get()
    lines = []
    i = 0
    while i < len(messages):
        role, text = messages[i]
        if role == "user":
            if run is not None and run.spellcheck:
                from mempalace.spellcheck import spellcheck_user_text

                corrected = spellcheck_user_text(text)
                if corrected != text:
                    run.applied.add("spellcheck_user")
                text = corrected
            lines.append(f"> {text}")
            if i + 1 < len(messages) and messages[i + 1][0] == "assistant":
                lines.append(messages[i + 1][1])
                i += 2
            else:
                i += 1
        else:
            lines.append(text)
            i += 1
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python normalize.py <filepath>")
        sys.exit(1)
    filepath = sys.argv[1]
    result = normalize(filepath)
    quote_count = sum(1 for line in result.split("\n") if line.strip().startswith(">"))
    print(f"\nFile: {os.path.basename(filepath)}")
    print(f"Normalized: {len(result)} chars | {quote_count} user turns detected")
    print("\n--- Preview (first 20 lines) ---")
    print("\n".join(result.split("\n")[:20]))
