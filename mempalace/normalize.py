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
    - WorkBuddy / CodeBuddy engine JSONL
    - Plain text (pass through for paragraph chunking)

No API key. No internet. Everything local.
"""

import errno
import json
import os
import re
import stat
from pathlib import Path
from typing import Optional


class UnparsedCodexTranscriptError(ValueError):
    """A recognized Codex rollout did not yield a supported conversation."""


# Provenance footer appended to Slack transcript output so downstream consumers
# know the speaker roles are positionally assigned, not verified.
_SLACK_PROVENANCE_FOOTER = (
    "\n[source: slack-export | multi-party chat — speaker roles are positional, not verified]"
)


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


def _tag_pattern(name: str, cross_blank_lines: bool = False) -> "re.Pattern[str]":
    # Opening tag must begin a line (optionally after a `> ` blockquote marker,
    # since _messages_to_transcript prefixes lines with `> `). Closing tag eats
    # optional trailing whitespace + newline.
    #
    # Default (cross_blank_lines=False) reproduces the upstream safety rule
    # verbatim: the body is lazy but forbidden from crossing a blank line, so
    # a stray unclosed tag in one message can never eat content from a
    # neighbouring message. Upstream states the bias plainly — "Verbatim is
    # sacred ... When in doubt, leave text alone" — and that rule applies to
    # every platform whose injection blocks are single-paragraph.
    #
    # WorkBuddy opts out (cross_blank_lines=True). Its injected blocks
    # (<system-reminder>, <identity_context>, ...) are multi-paragraph, so the
    # blank-line ban makes them unmatchable and files them verbatim: measured on
    # a live corpus, not one of them matches under the ban, and nearly all of
    # them strip once the boundary is swapped (docs/workbuddy-support.md). The
    # opt-out is scoped to `_try_workbuddy_jsonl` via `_WORKBUDDY_TAG_PATTERNS`,
    # so no other platform's parsing changes.
    #
    # ⚠️ KNOWN BOUNDARY (accepted 2026-09-27, WorkBuddy only) — why the guard
    # watches openers but NOT closers, and why that is safe HERE:
    #
    # The guard stops the body at the next line-initial OPENER; it does not
    # recognise `</name>` closers. A dangling opener followed by bare prose and
    # then a DISTANT same-name closer would therefore still be swallowed. That
    # shape DOES reproduce by construction (`<system-reminder>\nSYS\n\nPROSE\n
    # </system-reminder>` -> PROSE eaten). It does NOT occur in real transcripts,
    # and the reason is structural, not luck: every real WorkBuddy turn carries
    # an opener before the user's words (`<user_query>` and friends), so the body
    # hits that opener and stops before any distant closer is reachable.
    # Corpus check (live corpus, line-anchored in the stripper's own domain):
    # the open/close counts pair to within a fraction of a percent and no
    # nesting occurs, so the dangling-opener shape this guard is built around is
    # rare in practice. See docs/workbuddy-support.md for the measured counts.
    #
    # Deliberately NOT fixed by also matching `</…>` as a boundary: that would
    # stop the body at any closer too, and a legitimate multi-paragraph block
    # whose closer sits after a nested same-name mention would then be left
    # un-stripped — trading a no-op-in-practice hole for a real under-stripping
    # regression. Under "verbatim is sacred" the current form errs the safe way.
    # Revisit only if a real WorkBuddy transcript shows prose with NO opener
    # ahead of it AND a distant closer behind it (none seen as of 2026-09-27).
    #
    # If no closer exists before the boundary, the whole match fails and the
    # open tag is left in place (per the module's "verbatim is sacred" bias)
    # rather than guessed at.
    if cross_blank_lines:
        guard = "|".join(re.escape(t) for t in _NOISE_TAGS)
        body = rf"(?:(?!\n(?:> )?<(?:{guard})(?:\s[^>]*)?>)[\s\S])*?"
    else:
        body = r"(?:(?!\n\s*\n)[\s\S])*?"
    return re.compile(rf"(?m)^(?:> )?<{name}(?:\s[^>]*)?>" rf"{body}" rf"</{name}>[ \t]*\n?")


# Default set — upstream behaviour, blank-line ban intact. Used by every
# platform except WorkBuddy.
_NOISE_TAG_PATTERNS = [_tag_pattern(t) for t in _NOISE_TAGS]

# WorkBuddy-only variant — blank-line ban lifted so multi-paragraph injected
# blocks are matchable. Kept as a separate list rather than by mutating
# `_NOISE_TAG_PATTERNS` so the wider body can never leak into another
# platform's parsing (Claude Code ships single-paragraph `<system-reminder>`
# blocks; it keeps the ban).
_WORKBUDDY_TAG_PATTERNS = [_tag_pattern(t, cross_blank_lines=True) for t in _NOISE_TAGS]

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

# WorkBuddy / CodeBuddy text-block signatures. No other supported schema uses
# these names — Claude Code and Pi both emit `text` — so their presence is what
# identifies a WorkBuddy transcript (see `_try_workbuddy_jsonl`).
_WORKBUDDY_TEXT_BLOCKS = frozenset(("input_text", "output_text"))

# WorkBuddy wraps the user's actual utterance in `<user_query>...</user_query>`
# inside the user turn, with system injections (identity files, `<user_info>`,
# `<previous_*>` history echoes) around it. The tag itself is noise, but its
# PAYLOAD is the user's own words and must be preserved.
#
# This is deliberately peeled at the platform layer — during
# `_try_workbuddy_jsonl` — rather than by adding `user_query` to
# `_NOISE_TAGS`. The payload is genuine user speech (long pastes, design
# questions to the assistant), so it must survive; the tag also gets mentioned
# *inline* in prose and in code ("the user's words are in the
# `<user_query>` tag" / `grep -oP '<user_query>(.*?)</user_query>'`), so a
# line-anchored generic strip would eat those mentions too. Peeling here is
# exact: shell gone, payload kept, inline mentions untouched.
_WORKBUDDY_USER_QUERY_RE = re.compile(
    r"(?m)^[ \t]*<user_query(?:\s[^>]*)?>[ \t]*\n?"
    r"([\s\S]*?)"
    r"\n?[ \t]*</user_query>[ \t]*\n?"
)


def _peel_user_query(text: str) -> str:
    """Unwrap WorkBuddy `<user_query>` shells, keeping the user's own words.

    Non-greedy and paired-only: a dangling open tag (or an inline mention) is
    left alone rather than guessed at, matching this module's "verbatim is
    sacred" bias.

    The replacement re-inserts a newline after each payload so that adjacent
    shells do not fuse (`<user_query>a</user_query><user_query>b</user_query>`
    must stay two turns, not become `ab`). The shell's own trailing newline is
    consumed by the pattern, so it is restored here rather than left to
    whatever happened to follow.
    """
    return _WORKBUDDY_USER_QUERY_RE.sub(lambda m: m.group(1) + "\n", text)


# WorkBuddy re-injects the prior conversation history into every user turn,
# wrapped in `<previous_user_message>` / `<previous_assistant_message>` /
# `<previous_tool_call>`. These are COPIES of turns this module has already
# seen (or will see) at their original site, so keeping them files an echo of
# every exchange — measured on a live corpus, these echoes plus the compaction
# summaries are the large majority of the parsed text (see
# docs/workbuddy-support.md). Same class of problem as
# `<user_query>`, opposite fix: the payload is NOT new speech, so tag AND
# payload both go.
#
# Placed at the platform layer rather than in `_NOISE_TAGS` for the same
# reason as `_peel_user_query`: these names are WorkBuddy-specific, and the
# official tag list is other platforms' API surface (see the note above
# `_tag_pattern` about why a shared list must stay untouched).
#
# Pairing measured on a live corpus: the opens and closes differ by a fraction
# of a percent, 0% nesting. The closing tag uses a backreference so a block can
# only close with its OWN name. An independently-enumerated closer (the obvious
# `(?:a|b|c)`) would
# let `<previous_user_message> ... </previous_tool_call>` match and silently
# delete everything between — including real user speech, verified by
# construction. A dangling open tag is left alone rather than guessed at.
_WORKBUDDY_HISTORY_RE = re.compile(
    r"(?m)^[ \t]*<(?P<tag>previous_(?:user_message|assistant_message|tool_call))"
    r"(?:\s[^>]*)?>[\s\S]*?</(?P=tag)>[ \t]*\n?"
)


def _strip_history_echo(text: str) -> str:
    """Drop WorkBuddy's re-injected history blocks (tag + payload).

    Paired-only: an unclosed block survives rather than risk eating real
    content after it — the same conservative bias as `_peel_user_query`.
    """
    return _WORKBUDDY_HISTORY_RE.sub("", text)


# WorkBuddy/CodeBuddy injects its own context-compaction summary into the
# `user` turn's `input_text` block, wrapped in `<cb_summary>...</cb_summary>`
# (older sessions use `<conversation_history_summary>`). The payload opens with
# the fixed line "Summary of the conversation so far:". It is large, and it is
# RE-INJECTED on every subsequent turn after each compaction,
# so the same text gets chunked into the store over and over and retrieval
# returns rooms full of md5-identical duplicate drawers. Like
# `<previous_*>` — and unlike `<user_query>` — the payload is a copy of
# conversation already seen at its original site, so tag AND payload both go.
#
# Two spellings, one paired-only rule. Measured on a live corpus in the
# stripper's own domain (line-anchored): the opens and closes pair to within a
# fraction of a percent for both spellings, with zero nesting. The handful of
# unpaired openers is left verbatim rather than guessed at — a non-paired rule
# would have eaten the real prose that follows them. See
# docs/workbuddy-support.md for the counts.
#
# The closing tag uses a BACKREFERENCE (`</(?P=tag)>`) for the same reason as
# `_WORKBUDDY_HISTORY_RE`: an independently-enumerated closer
# (`(?:cb_summary|conversation_history_summary)`) lets
# `<cb_summary> ... </conversation_history_summary>` cross-pair and silently
# deletes everything between two real blocks. Verified by construction.
#
# Line-anchored for the same reason as the other two strippers: `cb_summary`
# is discussed *inline* in prose and code ("the `<cb_summary>` shell wraps…"),
# and those mentions are real content. Non-greedy `[\s\S]*?` so a block closes
# on the first matching closer rather than the last.
_WORKBUDDY_SUMMARY_RE = re.compile(
    r"(?m)^[ \t]*<(?P<tag>cb_summary|conversation_history_summary)"
    r"(?:\s[^>]*)?>[\s\S]*?</(?P=tag)>[ \t]*\n?"
)


def _strip_summary_echo(text: str) -> str:
    """Drop WorkBuddy's re-injected compaction-summary shell (tag + payload).

    Must run BEFORE `_peel_user_query` and `_strip_history_echo`: the injected
    scaffolding re-emits user turns, so the parsed text holds far more
    `<user_query>` shells than there are user turns (measured in
    docs/workbuddy-support.md). Peeling first would unwrap those copies and
    preserve them as if they were genuine user speech, which is exactly the
    duplicate-drawer problem this strip exists to fix.

    Paired-only: an unclosed shell survives rather than risk eating real
    content after it. The replacement re-inserts a newline so that adjacent
    blocks do not fuse, matching `_peel_user_query`.
    """
    return _WORKBUDDY_SUMMARY_RE.sub("\n", text)


def strip_noise(text: str, tag_patterns=None) -> str:
    """Remove system tags, hook output, and Claude Code UI chrome from text.

    All patterns are line-anchored. User prose that happens to mention these
    strings inline (e.g., documenting them) is preserved verbatim.

    ``tag_patterns`` selects the tag-stripping set. The default
    (``_NOISE_TAG_PATTERNS``) keeps the upstream blank-line ban. WorkBuddy
    passes ``_WORKBUDDY_TAG_PATTERNS``, whose blocks are multi-paragraph and
    would otherwise never match. The line/UI-chrome patterns below are
    shared by both.
    """
    for pat in tag_patterns if tag_patterns is not None else _NOISE_TAG_PATTERNS:
        text = pat.sub("", text)
    for pat in _NOISE_LINE_PATTERNS:
        text = pat.sub("", text)
    text = _HOOK_LINE_RE.sub("", text)
    text = _COLLAPSED_LINES_RE.sub("", text)
    # Strip the Claude Code collapsed-output chrome "[N tokens] (ctrl+o to expand)".
    # Narrow shape — a bare "(ctrl+o to expand)" in user prose stays intact.
    text = re.sub(r"\s*\[\d+\s+tokens?\]\s*\(ctrl\+o to expand\)", "", text)
    # Collapse runs of blank lines created by the removals
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def _read_transcript_file(filepath: str) -> str:
    """Read a transcript source file with the same safety checks normalize()
    and normalize_conversations() both need: no symlinks, regular files only,
    size-capped, BOM-tolerant.
    """
    # O_NONBLOCK keeps the "not a regular file" check below reachable: a
    # blocking open of a FIFO waits in the kernel for a writer, so the
    # S_ISREG test never runs. See ``miner._read_text_no_follow``, including
    # why the EAGAIN branch re-checks the type and retries without the flag.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if os.path.islink(filepath):
        raise IOError(f"Could not read {filepath}: symlinked files are skipped")
    fd = -1
    try:
        try:
            fd = os.open(filepath, flags)
        except OSError as exc:
            if exc.errno != errno.EAGAIN or not stat.S_ISREG(os.lstat(filepath).st_mode):
                raise
            fd = os.open(filepath, flags & ~getattr(os, "O_NONBLOCK", 0))
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            # Text stays prefix-free: this raise is inside the ``try``, so the
            # ``except OSError`` below composes "Could not read <path>: ...".
            raise IOError("not a regular file")
        if file_stat.st_size > 500 * 1024 * 1024:  # 500 MB safety limit
            # Prefix-free for the same reason as the branch above.
            raise IOError(f"file too large ({file_stat.st_size // (1024 * 1024)} MB)")
        with os.fdopen(fd, "r", encoding="utf-8-sig", errors="replace") as f:
            fd = -1
            return f.read()
    except OSError as e:
        raise IOError(f"Could not read {filepath}: {e}") from e
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass


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
    """Like normalize(), but keeps each conversation in a bundle export as a
    separate string instead of joining them into one.

    A Claude.ai privacy export packs every conversation into a single JSON
    file, and normalize() joins them with "\\n\\n".join(...) into one blob.
    That collapses conversation boundaries, so content-hash dedup keyed on
    the whole file breaks the moment the bundle is re-exported with one new
    conversation added — the file-level hash changes even though none of
    the existing conversations did. This returns the pieces un-joined so
    callers can hash and dedup per conversation instead.

    A ChatGPT data export is a bundle for the same reason: its
    ``conversations.json`` is an array of conversations, so it splits per
    conversation too.

    Non-bundle formats (a single Claude Code session, plain text, ...)
    always normalize to one conversation, so this returns a one-element
    list for those — identical dedup granularity to before.
    """
    content = _read_transcript_file(filepath)

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


def _try_normalize_json_split(content: str) -> Optional[list]:
    """Try all known JSON chat schemas, returning each conversation found as
    a separate list entry (bundle formats) or a single-element list.
    """

    normalized = _try_claude_code_jsonl(content)
    if normalized:
        return [normalized]

    normalized = _try_codex_jsonl(content)
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

    normalized = _try_gemini_jsonl(content)
    if normalized:
        return [normalized]

    normalized = _try_pi_jsonl(content)
    if normalized:
        return [normalized]

    normalized = _try_workbuddy_jsonl(content)
    if normalized:
        return [normalized]

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    normalized = _try_gemini_json(data)
    if normalized:
        return [normalized]

    split = _try_claude_ai_json_split(data)
    if split:
        return split

    split = _try_chatgpt_export_json_split(data)
    if split:
        return split

    for parser in (_try_chatgpt_json, _try_continue_json, _try_slack_json):
        normalized = parser(data)
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

        if entry_type != "event_msg":
            continue

        payload = entry.get("payload", {})
        if not isinstance(payload, dict):
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
    single message's content array are concatenated in order, separated
    by newlines.
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
            if isinstance(text, str) and text.strip():
                parts.append(text)
        if not parts:
            continue
        joined = "\n".join(parts)

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
        text = _extract_content(message.get("content", ""))

        if role == "user" and text:
            messages.append(("user", text))
        elif role == "assistant" and text:
            messages.append(("assistant", text))

    if len(messages) >= 2 and has_session_header:
        return _messages_to_transcript(messages)
    return None


def _try_workbuddy_jsonl(content: str) -> Optional[str]:
    """WorkBuddy / CodeBuddy engine session JSONL.

    Format: ``~/.workbuddy/.../<sessionId>.jsonl`` (also mirrored under a
    ``journal/`` export dir). One JSON object per line. Differing from
    Claude Code in three places:

      1. ``role`` / ``content`` sit at the TOP level (no ``"message"`` wrapper).
      2. Text blocks are typed ``input_text`` (user) / ``output_text``
         (assistant) instead of ``text``.
      3. ``timestamp`` is a millisecond integer, not an ISO-8601 string.

    There is NO session header entry: ``session-meta`` carries platform
    metadata only (no conversation), and every other non-``message`` line
    (``reasoning``, ``function_call``, ``function_call_result``,
    ``file-history-snapshot``, ``ai-title``, ``resend-fork-notice``) is
    operational and skipped — matching how ``_try_pi_jsonl`` skips
    ``toolResult``. Detection therefore keys on the block type signature
    (``input_text`` / ``output_text``), which no other supported schema uses.

    A stray ``message`` line whose ``content`` is a bare string (rare, but
    present in real exports) is tolerated via ``_extract_content``.
    """
    lines = [line.strip() for line in content.strip().split("\n") if line.strip()]
    messages = []
    wb_marked = False

    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue

        if entry.get("type", "") != "message":
            continue

        role = entry.get("role", "")
        block_types = _workbuddy_block_types(entry.get("content"))
        if block_types & _WORKBUDDY_TEXT_BLOCKS:
            wb_marked = True

        text = _extract_content(entry.get("content", ""), extra_text_blocks=_WORKBUDDY_TEXT_BLOCKS)
        if not text:
            continue

        if role == "user":
            # Order matters: the compaction-summary shell is stripped FIRST.
            # Its payload holds thousands of `<user_query>` /
            # `<previous_user_message>` COPIES, so peeling before it would
            # unwrap those copies into apparently-genuine user turns.
            #
            # `_WORKBUDDY_TAG_PATTERNS` (blank-line ban lifted) is scoped to
            # this parser only — WorkBuddy's injected blocks are
            # multi-paragraph, unlike every other platform's.
            text = _strip_summary_echo(text)
            text = _strip_history_echo(text)
            text = _peel_user_query(strip_noise(text, _WORKBUDDY_TAG_PATTERNS))
            messages.append(("user", text))
        elif role == "assistant":
            text = _strip_summary_echo(text)
            text = _strip_history_echo(text)
            messages.append(("assistant", strip_noise(text, _WORKBUDDY_TAG_PATTERNS)))

    # Require both the multi-turn shape and the WorkBuddy block signature, so
    # a same-shaped schema cannot be adopted by mistake. Claude Code keeps
    # ``type`` as ``"user"`` / ``"assistant"`` and types text blocks ``text``;
    # only WorkBuddy puts ``type`` at ``"message"`` while naming the blocks
    # ``input_text`` / ``output_text``. The block signature is the load-bearing
    # discriminator here — ``len(messages) >= 2`` alone would also match a
    # bare two-line JSONL, which is why both are required.
    if len(messages) >= 2 and wb_marked:
        return _messages_to_transcript(messages)
    return None


def _workbuddy_block_types(content) -> set:
    """Collect the ``type`` values of top-level content blocks.

    Returns an empty set for a bare-string or plain-dict content, neither of
    which can carry the WorkBuddy signature.
    """
    if not isinstance(content, list):
        return set()
    types = set()
    for item in content:
        if isinstance(item, dict):
            types.add(item.get("type"))
    return types


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
                elif isinstance(p, dict) and "text" in p:
                    text_parts.append(p["text"])
            text = " ".join(text_parts).strip()
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
            messages = _collect_claude_messages(chat_msgs)
            if len(messages) >= 2:
                transcripts.append(_messages_to_transcript(messages))
        if transcripts:
            return transcripts
        return None

    # Flat messages list
    messages = _collect_claude_messages(data)
    if len(messages) >= 2:
        return [_messages_to_transcript(messages)]
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
        if role in ("user", "human") and text:
            messages.append(("user", text))
        elif role in ("assistant", "ai") and text:
            messages.append(("assistant", text))
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
    messages = []
    # Find root: prefer node with parent=None AND no message (synthetic root)
    root_id = None
    fallback_root = None
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            continue
        if node.get("parent") is None:
            if node.get("message") is None:
                root_id = node_id
                break
            elif fallback_root is None:
                fallback_root = node_id
    if not root_id:
        root_id = fallback_root
    if root_id:
        current_id = root_id
        visited = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            node = mapping.get(current_id)
            if not isinstance(node, dict):
                break
            msg = node.get("message")
            if isinstance(msg, dict):
                author = msg.get("author")
                role = author.get("role", "") if isinstance(author, dict) else ""
                content = msg.get("content", {})
                parts = content.get("parts") if isinstance(content, dict) else None
                if not isinstance(parts, list):
                    parts = []
                text = " ".join(str(p) for p in parts if isinstance(p, str) and p).strip()
                if role == "user" and text:
                    messages.append(("user", text))
                elif role == "assistant" and text:
                    messages.append(("assistant", text))
            children = node.get("children")
            next_id = children[0] if isinstance(children, list) and children else None
            # Node ids index a dict and a visited set, so anything unhashable
            # (a nested child object rather than an id) ends the walk.
            current_id = next_id if isinstance(next_id, str) else None
    if len(messages) >= 2:
        return _messages_to_transcript(messages)
    return None


def _try_chatgpt_export_json_split(data) -> Optional[list]:
    """ChatGPT data export: top-level array of conversation objects.

    The ``conversations.json`` OpenAI ships is an *array*, while
    ``_try_chatgpt_json`` handles the single conversation object inside it.
    Without this the whole export falls through to the plain-text path and is
    chunked as raw JSON: the drawers hold serialized structure sliced at
    arbitrary offsets, and every speaker turn is gone.

    Each conversation is kept as its own segment rather than concatenated, so
    per-conversation dedup survives a re-export (see ``normalize_conversations``);
    the joined form is reached through ``_try_normalize_json``.

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
        transcript = _try_chatgpt_json(convo)
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
        raw_user_id = item.get("user", item.get("username", ""))
        # Sanitize speaker ID: strip brackets, newlines, and control chars
        # to prevent chunk-boundary injection via crafted exports
        user_id = re.sub(r"[\[\]\n\r\x00-\x1f]", "_", raw_user_id).strip()
        text = item.get("text", "").strip()
        if not text or not user_id:
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
        # Skip system and other roles

    if len(messages) >= 2:
        return _messages_to_transcript(messages)
    return None


def _extract_content(content, tool_use_map: dict = None, extra_text_blocks=None) -> str:
    """Pull text from content — handles str, list of blocks, or dict.

    Args:
        content: Message content — string, list of content blocks, or dict.
        tool_use_map: Optional mapping of tool_use_id → tool_name, used to
                      select the right formatting strategy for tool_result blocks.
        extra_text_blocks: Optional additional block types to treat exactly like
                      ``text``. Off by default, so every other format keeps its
                      current handling; WorkBuddy passes ``_WORKBUDDY_TEXT_BLOCKS``.
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
                elif extra_text_blocks and block_type in extra_text_blocks:
                    # WorkBuddy / CodeBuddy: input_text (user) / output_text
                    # (assistant). Same shape as `text`, different tag. Opt-in
                    # per call site, so no other format changes behaviour.
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
        if len(cmd) > 200:
            cmd = cmd[:200] + "..."
        return f"[Bash] {cmd}"

    if name == "Read":
        path = inp.get("file_path", "?")
        offset = inp.get("offset")
        limit = inp.get("limit")
        if offset is not None and limit is not None:
            try:
                return f"[Read {path}:{offset}-{int(offset) + int(limit)}]"
            except (ValueError, TypeError):
                return f"[Read {path}:{offset}+{limit}]"
        return f"[Read {path}]"

    if name == "Grep":
        pattern = inp.get("pattern", "")
        target = inp.get("path") or inp.get("glob") or ""
        return f"[Grep] {pattern} in {target}"

    if name == "Glob":
        pattern = inp.get("pattern", "")
        return f"[Glob] {pattern}"

    if name in ("Edit", "Write"):
        path = inp.get("file_path", "?")
        return f"[{name} {path}]"

    # Unknown tool — serialize input, truncate
    summary = json.dumps(inp, separators=(",", ":"))
    if len(summary) > 200:
        summary = summary[:200] + "..."
    return f"[{name}] {summary}"


_TOOL_RESULT_MAX_LINES_BASH = 20  # head and tail line count
_TOOL_RESULT_MAX_MATCHES = 20  # Grep/Glob cap
_TOOL_RESULT_MAX_BYTES = 2048  # fallback cap for unknown tools


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
        text = "\n".join(parts)
    else:
        text = str(content) if content else ""

    text = text.strip()
    if not text:
        return ""

    # Read/Edit/Write — omit result (content is in palace or git)
    if tool_name in ("Read", "Edit", "Write"):
        return ""

    lines = text.split("\n")

    # Bash — head + tail
    if tool_name == "Bash":
        n = _TOOL_RESULT_MAX_LINES_BASH
        if len(lines) <= n * 2:
            return "→ " + "\n→ ".join(lines)
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
        kept = lines[:cap]
        remaining = len(lines) - cap
        return "→ " + "\n→ ".join(kept) + f"\n→ ... [{remaining} more matches]"

    # Unknown — byte cap
    if len(text) > _TOOL_RESULT_MAX_BYTES:
        return "→ " + text[:_TOOL_RESULT_MAX_BYTES] + f"... [truncated, {len(text)} chars]"
    return "→ " + text


def _messages_to_transcript(messages: list, spellcheck: bool = True) -> str:
    """Convert [(role, text), ...] to transcript format with > markers."""
    if spellcheck:
        try:
            from mempalace.spellcheck import spellcheck_user_text

            _fix = spellcheck_user_text
        except ImportError:
            _fix = None
    else:
        _fix = None

    lines = []
    i = 0
    while i < len(messages):
        role, text = messages[i]
        if role == "user":
            if _fix is not None:
                text = _fix(text)
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
