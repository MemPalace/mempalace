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
