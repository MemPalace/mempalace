"""Reference implementations of the reserved content transformations (RFC 002 §1.4).

Every source adapter declares the set of transformations it applies to source
bytes via ``declared_transformations``. The conformance suite then verifies
that the adapter's output can be reproduced from the source bytes by applying
*only* the declared transformations in declaration order, using these
reference implementations.

Each transformation is a pure function on strings (text content after UTF-8
decoding). ``utf8_replace_invalid`` is the one that operates on bytes.

The invariant the spec enforces: **no transformation is applied that is not
declared in the adapter's set**. Adapters with an empty set are byte-preserving
end-to-end (modulo the initial UTF-8 decode itself, which is captured by
``utf8_replace_invalid`` when applicable).

Adapters MAY add custom transformations beyond the reserved set; third-party
names SHOULD be prefixed with the adapter name (``cursor.composer_ordering``).
Custom transformations MUST expose a reference implementation under
``mempalace.sources.transforms.<adapter_name>_<transform_name>`` so the
conformance suite can locate and apply them.
"""

from __future__ import annotations

import json as _json
import re
from typing import Protocol, Union


class Transformation(Protocol):
    """Callable signature every reserved transformation conforms to.

    Accepts the current stage of the pipeline — ``bytes`` on input
    (``utf8_replace_invalid``) or ``str`` after decoding — and returns ``str``.
    Adapters compose them in declaration order; the first step operates on the
    original source bytes, every subsequent step on the prior step's output.
    """

    def __call__(self, data: Union[bytes, str], /) -> str: ...


# ---------------------------------------------------------------------------
# Reserved transformations
# ---------------------------------------------------------------------------


def utf8_replace_invalid(raw: bytes) -> str:
    """Decode bytes as UTF-8; replace invalid sequences with U+FFFD.

    Equivalent to ``raw.decode("utf-8", errors="replace")``. This is the one
    reserved transformation that operates on bytes rather than decoded text.
    """
    return raw.decode("utf-8", errors="replace")


def newline_normalize(text: str) -> str:
    """Convert CRLF and bare-CR line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def whitespace_trim(text: str) -> str:
    """Strip leading and trailing whitespace at the record boundary only."""
    return text.strip()


_RUN_OF_THREE_OR_MORE_BLANK = re.compile(r"(?:\n[ \t]*){3,}\n")


def whitespace_collapse_internal(text: str) -> str:
    """Collapse runs of three or more blank lines to exactly two blank lines.

    A "blank line" here is a line containing only spaces or tabs. Single and
    double blank-line runs are preserved.
    """
    # Normalise inputs before collapsing: turn internal blank lines with
    # whitespace content into pure \n so the regex matches consistently.
    lines = text.split("\n")
    normalised = "\n".join(line if line.strip() else "" for line in lines)
    return _RUN_OF_THREE_OR_MORE_BLANK.sub("\n\n\n", normalised)


def line_trim(text: str) -> str:
    """Strip leading and trailing whitespace from each individual line."""
    return "\n".join(line.strip() for line in text.split("\n"))


def line_join_spaces(text: str) -> str:
    """Join adjacent non-blank lines with a single space, preserving paragraph breaks.

    Two lines separated by at least one blank line remain on separate lines;
    runs of non-blank lines collapse into a single space-separated line.
    """
    paragraphs = re.split(r"\n[ \t]*\n", text)
    joined = [" ".join(line.strip() for line in p.split("\n") if line.strip()) for p in paragraphs]
    return "\n\n".join(joined)


def blank_line_drop(text: str) -> str:
    """Drop blank lines between non-blank lines, keeping non-blank lines only."""
    return "\n".join(line for line in text.split("\n") if line.strip())


# The following reserved transformations are declared in the spec but are
# deeply adapter-specific. Rather than guess a single reference implementation
# now, we provide identity shims that leave the input unchanged when no
# adapter-specific implementation is available. Adapters that declare these
# MUST either override with a concrete implementation or provide a namespaced
# reference under
# ``mempalace.sources.transforms.<adapter_name>_<transform_name>`` (per the
# module docstring). The conformance suite looks up the adapter-specific
# implementation first, falling back to these identity shims only when none
# exists.


def strip_tool_chrome(text: str) -> str:
    """Adapter-supplied: remove system tags, hook output, tool UI chrome.

    The reference implementation here is intentionally an identity function
    because the noise patterns differ per transcript format (Claude Code,
    Codex, ChatGPT, Slack). The conversations adapter, when migrated, will
    register a concrete reference implementation under
    ``mempalace.sources.transforms.conversations_strip_tool_chrome``.
    """
    return text


def tool_result_truncate(text: str) -> str:
    """Adapter-supplied: head/tail window on tool output with a middle marker."""
    return text


def tool_result_omitted(text: str) -> str:
    """Adapter-supplied: fully omit some tool outputs (e.g., Read/Edit/Write)."""
    return text


def spellcheck_user(text: str) -> str:
    """Adapter-supplied: rewrite user turns via autocorrect.

    Requires the optional ``spellcheck`` extra and a tokenizer; the spec does
    not mandate a specific language model, so the reference is adapter-owned.
    """
    return text


def synthesized_marker(text: str) -> str:
    """Adapter-supplied: adapter inserts its own strings (e.g., '[N lines omitted]')."""
    return text


def speaker_role_assignment(text: str) -> str:
    """Adapter-supplied: multi-party speakers alternately assigned user/assistant."""
    return text


# ---------------------------------------------------------------------------
# Adapter-namespaced reference implementations — OpenClaw
# ---------------------------------------------------------------------------
#
# Per RFC 002 §7.3, custom (non-reserved) transformations declared by an
# adapter MUST expose a reference implementation under
# ``mempalace.sources.transforms.<adapter_name>_<transform_name>`` so the
# conformance suite can locate and apply them by attribute lookup. The
# implementations below are the OpenClaw adapter's; future adapters add their
# own under their own ``<name>_`` prefix.
#
# The OpenClaw adapter's canonical source bytes for one session are the raw
# ``*.trajectory.jsonl`` file contents: one JSON event object per line. The
# transformation chain below converts that into chunked exchange-pair markdown
# compatible with ``mempalace.convo_miner.chunk_exchanges``.


# Regex patterns for user-text cleanup — compiled once at module load.

# Strip the OpenClaw internal context injection block inserted before every prompt.
_OPENCLAW_RUNTIME_BLOCK = re.compile(
    r"OpenClaw runtime context.*?END_OPENCLAW_INTERNAL_CONTEXT>>>",
    re.DOTALL,
)
# Strip fenced code blocks whose body contains OpenClaw routing metadata keys.
_OPENCLAW_META_FENCE = re.compile(
    r"```[a-zA-Z]*\s*\n.*?(?:chat_id|inbound_event_kind|sender_id|\"label\").*?```",
    re.DOTALL,
)
# Strip bare label header lines injected above the metadata fences.
_OPENCLAW_LABEL_LINES = re.compile(
    r"^\s*(?:Conversation info \(untrusted metadata\):"
    r"|Sender \(untrusted metadata\):"
    r"|Conversation info.*:"
    r"|.*untrusted metadata.*:)\s*$",
    re.MULTILINE,
)
# Strip Slack routing header prepended to each Slack-delivered message:
# "[Slack <name> +Nm Mon YYYY-MM-DD HH:MM:SS UTC] <name>: "
# Uses [ \t]+ (not \s+) between ] and the display name so the pattern cannot
# cross line boundaries and accidentally consume the next line's content.
_OPENCLAW_SLACK_HEADER = re.compile(
    r"^\[Slack [^\]]+\][ \t]+[^:\n]+:\s*",
    re.MULTILINE,
)
# Strip [media attached: media://... (mime/type)] annotation lines.
_OPENCLAW_MEDIA_ATTACHED = re.compile(
    r"^\[media attached:[^\]]*\][ \t]*$",
    re.MULTILINE,
)
# Strip [Slack file: <name> (fileId: <id>)] provenance lines.
_OPENCLAW_SLACK_FILE_LINE = re.compile(
    r"^\[Slack file:[^\]]*\][ \t]*$",
    re.MULTILINE,
)
# Strip [slack message id: <ts> channel: <id>] provenance lines.
_OPENCLAW_SLACK_MSG_ID = re.compile(
    r"^\[slack message id:[^\]]*\][ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
# Strip <file name="..." mime="...">...</file> wrapper blocks including any
# <<<EXTERNAL_UNTRUSTED_CONTENT>>> / SECURITY NOTICE scaffolding inside.
# Inner content is tool-injected file data, not human conversation text.
_OPENCLAW_FILE_WRAPPER = re.compile(
    r"<file\b[^>]*>.*?</file>",
    re.DOTALL | re.IGNORECASE,
)


def openclaw_extract_turns(text: str) -> str:  # noqa: D401
    """Parse JSONL trajectory events and emit one role-tab-JSON line per turn.

    Input: newline-separated JSON event objects (raw ``*.trajectory.jsonl``
    file contents).

    Output: lines of the form ``<role>\\t<json_body>`` where ``<json_body>``
    is the JSON-encoded turn text. ``user`` turns come from
    ``prompt.submitted`` (``data.prompt``); ``assistant`` turns come from
    ``model.completed`` (``data.assistantTexts`` joined with ``\\n``).
    All other event types — ``context.compiled``, ``session.started``,
    ``session.ended``, ``trace.*`` etc. — are skipped.

    The JSON encoding of the body means embedded newlines are escaped and
    downstream transforms can split on ``\\n`` without ambiguity.
    """
    out: list[str] = []
    pending_user: str | None = None
    for raw_line in text.split("\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = _json.loads(raw_line)
        except (ValueError, TypeError):
            continue
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "prompt.submitted":
            pending_user = data.get("prompt") or ""
        elif etype == "model.completed":
            texts = data.get("assistantTexts") or []
            assistant = "\n".join(x for x in texts if isinstance(x, str)).strip()
            if pending_user is not None:
                out.append(f"user\t{_json.dumps(pending_user)}")
            if assistant:
                out.append(f"assistant\t{_json.dumps(assistant)}")
            pending_user = None
    return "\n".join(out)


def openclaw_strip_runtime_context(text: str) -> str:  # noqa: D401
    """Strip OpenClaw runtime-context injection blocks from user turns.

    Operates on the role-tab-JSON line format produced by
    :func:`openclaw_extract_turns`. For each ``user`` line, JSON-decodes the
    body, removes the ``OpenClaw runtime context … END_OPENCLAW_INTERNAL_CONTEXT>>>``
    block (inserted by the agent runtime before every prompt), then re-encodes.
    Assistant lines are passed through unchanged.
    """
    out: list[str] = []
    for line in text.split("\n"):
        role, sep, body_json = line.partition("\t")
        if not sep:
            out.append(line)
            continue
        if role == "user":
            try:
                body = _json.loads(body_json)
                body = _OPENCLAW_RUNTIME_BLOCK.sub("", body)
                body_json = _json.dumps(body)
            except (ValueError, TypeError):
                pass
        out.append(f"{role}\t{body_json}")
    return "\n".join(out)


def openclaw_strip_metadata_preamble(text: str) -> str:  # noqa: D401
    """Strip OpenClaw metadata fences and all Slack/media envelope annotations.

    Operates on the role-tab-JSON line format produced by
    :func:`openclaw_extract_turns`. For each ``user`` line, JSON-decodes the
    body and removes:

    * Fenced code blocks whose body contains OpenClaw routing metadata keys
      (``chat_id``, ``sender_id``, ``inbound_event_kind``, ``label``).
    * Bare label lines such as ``Conversation info (untrusted metadata):``
      and ``Sender (untrusted metadata):``.
    * Slack routing header prefix: ``[Slack <name> +Nm ...] <name>:``.
    * ``[media attached: media://... (mime)]`` file-attachment annotation lines.
    * ``[Slack file: <name> (fileId: <id>)]`` file provenance lines.
    * ``[slack message id: <ts> channel: <id>]`` message provenance lines.
    * ``<file name="..." mime="...">...</file>`` wrapper blocks; inner content
      is tool-injected file data (with ``<<<EXTERNAL_UNTRUSTED_CONTENT>>>``
      scaffolding) and is dropped entirely — it is not human conversation text.

    Assistant lines are passed through unchanged.
    """
    out: list[str] = []
    for line in text.split("\n"):
        role, sep, body_json = line.partition("\t")
        if not sep:
            out.append(line)
            continue
        if role == "user":
            try:
                body = _json.loads(body_json)
                body = _OPENCLAW_META_FENCE.sub("", body)
                body = _OPENCLAW_LABEL_LINES.sub("", body)
                body = _OPENCLAW_SLACK_HEADER.sub("", body)
                body = _OPENCLAW_MEDIA_ATTACHED.sub("", body)
                body = _OPENCLAW_SLACK_FILE_LINE.sub("", body)
                body = _OPENCLAW_SLACK_MSG_ID.sub("", body)
                body = _OPENCLAW_FILE_WRAPPER.sub("", body)
                body_json = _json.dumps(body)
            except (ValueError, TypeError):
                pass
        out.append(f"{role}\t{body_json}")
    return "\n".join(out)


def openclaw_format_exchange(text: str) -> str:  # noqa: D401
    """Reformat role-tab-JSON lines as ``convo_miner`` exchange-pair markdown.

    Mirrors :func:`opencode_format_exchange` but JSON-decodes the body first.
    ``user`` lines become ``> <body>``; ``assistant`` lines become the body on
    its own paragraph. Pairs are separated by blank lines. The output is what
    ``mempalace.convo_miner.chunk_exchanges`` recognises as an exchange
    transcript.
    """
    blocks: list[str] = []
    for line in text.split("\n"):
        role, sep, body_json = line.partition("\t")
        if not sep:
            continue
        try:
            body = _json.loads(body_json)
        except (ValueError, TypeError):
            body = body_json
        if not isinstance(body, str):
            body = str(body)
        body = body.strip()
        if not body:
            continue
        if role == "user":
            quoted = "\n".join(f"> {ln}" for ln in body.split("\n"))
            blocks.append(quoted)
        else:
            blocks.append(body)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


# Reserved transformation name → reference implementation.
# Adapters look up by name to compose a round-trip pipeline during testing.
# The value conforms to the :class:`Transformation` protocol above; we type
# it as that Protocol rather than a concrete ``Callable`` so static checkers
# accept both the bytes→str (``utf8_replace_invalid``) and str→str shapes.
RESERVED_TRANSFORMATIONS: dict[str, Transformation] = {
    "utf8_replace_invalid": utf8_replace_invalid,
    "newline_normalize": newline_normalize,
    "whitespace_trim": whitespace_trim,
    "whitespace_collapse_internal": whitespace_collapse_internal,
    "line_trim": line_trim,
    "line_join_spaces": line_join_spaces,
    "blank_line_drop": blank_line_drop,
    "strip_tool_chrome": strip_tool_chrome,
    "tool_result_truncate": tool_result_truncate,
    "tool_result_omitted": tool_result_omitted,
    "spellcheck_user": spellcheck_user,
    "synthesized_marker": synthesized_marker,
    "speaker_role_assignment": speaker_role_assignment,
}


def get_transformation(name: str) -> Transformation:
    """Resolve a reserved transformation by name.

    Raises :class:`KeyError` if the name is neither reserved nor registered as
    an adapter-namespaced reference (``<adapter>_<transform>``). Callers
    looking for adapter-specific references SHOULD ``getattr`` on this module
    first; this helper only covers the reserved names.
    """
    try:
        return RESERVED_TRANSFORMATIONS[name]
    except KeyError as e:
        raise KeyError(
            f"unknown transformation {name!r}; reserved names: {sorted(RESERVED_TRANSFORMATIONS)}"
        ) from e
