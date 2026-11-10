"""Pure, lossless reference recognition for provenance authorization."""

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

PROVENANCE_FIELDS = {
    "source_thread",
    "source_url",
    "source_file",
    "source_path",
    "source_drawer_id",
    "source_closet",
}
DERIVED_FIELDS = {"origin_id", "origin_link", "approval_link", "source_link"}
NON_PROVENANCE = {
    "subject",
    "predicate",
    "object",
    "old_object",
    "new_object",
    "valid_from",
    "valid_to",
    "ended",
    "at",
    "wing",
    "sensitivity",
    "sensitivities",
    "confidence",
    "single_valued",
    "source_seq_start",
    "source_seq_end",
    "thread_scope",
}
READ_FIELDS = {
    "triple_id",
    "source_thread",
    "source_seq_start",
    "source_seq_end",
    "source_drawer_id",
    "source_file",
    "added_at",
    "origin_kind",
    "origin_id",
    "origin_link",
    "approval_status",
    "approved_by",
    "approval_link",
    "source_closet",
    "source_url",
    "source_link",
    "fact_status",
}
REFUSE = object()
# Overlapping variable-length identities can require quadratic output. Never
# truncate or partially prove a field: bound both decoded and NFKC scan copies.
# Four decoding rounds remain O(input length); scanning is bounded by this cap.
MAX_REFERENCE_CHARS = 16_384
_MAX_REFERENCE_CHARS = MAX_REFERENCE_CHARS
_THREAD = re.compile(r"thr_[0-9a-f]+", re.IGNORECASE)
_PRIVATE = re.compile(r"(?:capture-fork|capture|import-private):[^\s?]+", re.IGNORECASE)
_PERCENT = re.compile(r"%[0-9a-f]{2}", re.IGNORECASE)

# These domains mirror capture.SOURCES and import_sources.SOURCES plus the
# grant-only native-memory route. They constrain new shadows, never legacy ids.
_CAPTURE_SOURCES = frozenset({"claude-code", "codex"})
_IMPORT_SOURCES = _CAPTURE_SOURCES | {
    "cursor",
    "t3",
    "grok-bot",
    "chatgpt",
    "claude-ai",
    "native-memory",
}
_BOUNDARY = re.compile(r"(?<![^ /?&#=(<\"'])")
_START = re.compile(r"(?=capture-fork|capture|import-private)", re.IGNORECASE)
# Omitted separators need an id marker, so ordinary words such as
# "capturing-notes:codex:v2" cannot invent an actor. Real usr_/conn_ ids and
# compact fixture ids (u1) have an underscore or digit.
_ACTOR = (
    r"(?:: *|(?=[A-Za-z0-9])(?=[^\s:/\\?]*[0-9_]))"
    r"(?P<actor>[^\s:/\\?]+): *"
)
_SHADOW_PRIVATE = re.compile(
    r"(?P<namespace>import-private|capture)"
    + _ACTOR
    + r"(?P<source>[^\s:/\\?]+): *(?P<identity>[^\s?]+)",
    re.IGNORECASE,
)
_SHADOW_FORK = re.compile(
    r"(?P<namespace>capture-fork)"
    + _ACTOR
    # A fork's identity is fixed length. Everything after its digest is citation
    # context, even another hex digit; embedded references need their own proof.
    + r"(?P<identity>(?-i:[0-9a-f]{32}))[^\s?]*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class References:
    text: str | None
    tokens: tuple[str, ...]


class ReferenceLimitError(ValueError):
    """A caller-supplied citation exceeds the authorization character limit."""


def _over_limit(raise_on_limit):
    if raise_on_limit:
        raise ReferenceLimitError(
            f"citation length limit of {_MAX_REFERENCE_CHARS} Unicode characters exceeded "
            "(decoded or NFKC-normalized)"
        )
    return REFUSE


def _controls(text):
    return any(unicodedata.category(char) in {"Cc", "Cf"} for char in text)


def _private_matches(text):
    # Advancing by the start, rather than the end, retains overlapping tokens.
    for start in _START.finditer(text):
        match = _PRIVATE.match(text, start.start())
        if match is not None:
            yield match


def label_tokens(text):
    """Scan display text independently, without citation decoding or refusal."""
    tokens = [m[0] for m in _THREAD.finditer(text)]
    tokens.extend(m[0] for m in _private_matches(text))
    return tuple(dict.fromkeys(tokens))


def _authorization_copy(text):
    """Build a bounded NFKC copy and preserve original character offsets."""
    chars, offsets = [], []
    length = 0
    for index, char in enumerate(text):
        # NFKC must not broaden the permitted structural space alphabet.
        normalized = (
            char
            if char.isspace() and char not in {" ", "\u00a0"}
            else unicodedata.normalize("NFKC", char)
        )
        length += len(normalized)
        if length > _MAX_REFERENCE_CHARS:
            return None
        chars.append(normalized)
        offsets.extend([index] * len(normalized))
    return "".join(chars), offsets


def _shadows(shadow, offsets):
    """Return complete authorization candidates and their original text spans."""
    reference_end = 0
    for start in _START.finditer(shadow):
        pos = start.start()
        # Legacy ordinary runs also contain protected spellings. Within a run,
        # no punctuation/word boundary is required for an embedded candidate.
        inside_run = pos < reference_end
        ordinary = _PRIVATE.match(shadow, pos)
        if ordinary is not None:
            reference_end = max(reference_end, ordinary.end())
        if not inside_run and _BOUNDARY.match(shadow, pos) is None:
            continue
        for pattern in (_SHADOW_FORK, _SHADOW_PRIVATE):
            match = pattern.match(shadow, pos)
            if match is None:
                continue
            namespace = match["namespace"].lower()
            actor, identity = match["actor"], match["identity"]
            if namespace == "capture-fork":
                token = f"{namespace}:{actor}:{identity}"
            else:
                source = match["source"].lower()
                domain = _CAPTURE_SOURCES if namespace == "capture" else _IMPORT_SOURCES
                if source not in domain:
                    continue
                token = f"{namespace}:{actor}:{source}:{identity}"
            # Compose compatibility characters across actor/identity boundaries
            # within the token too; per-character normalization above keeps spans.
            token = unicodedata.normalize("NFKC", token)
            reference_end = max(reference_end, match.end())
            yield offsets[match.start()], offsets[match.end() - 1] + 1, token


def _tokens(text):
    copy = _authorization_copy(text)
    if copy is None:
        return None
    shadows = list(_shadows(*copy))
    tokens = dict.fromkeys(match[0] for match in _THREAD.finditer(text))
    for match in _private_matches(text):
        # Complete shadows replace incomplete structural prefixes. Fixed-length
        # forks also replace the ordinary scan's trailing citation context, so a
        # readable fork is proved by its digest without changing displayed bytes.
        # Overlapping scans independently retain every embedded reference.
        if any(
            start == match.start()
            and (
                (
                    match.end() < end
                    and text[match.end()] in {" ", "\u00a0"}
                    and match[0].endswith(":")
                )
                or (match.end() <= end and token.startswith("capture-fork:"))
            )
            for start, end, token in shadows
        ):
            continue
        tokens[match[0]] = None
    for _, _, token in shadows:
        tokens[token] = None
        for match in _THREAD.finditer(token):
            tokens[match[0]] = None
    return tuple(tokens)


def refs(value, *, raise_on_limit=False):
    """Decode four rounds; writes may distinguish length limits from other refusals."""
    if value is None:
        return References(None, ())
    if not isinstance(value, str) or _controls(value):
        return REFUSE
    text = value
    try:
        for _ in range(4):
            decoded = unquote(text, errors="strict")
            if _controls(decoded):
                return REFUSE
            if decoded == text:
                break
            text = decoded
    except (UnicodeError, ValueError):
        return REFUSE
    if _controls(text) or _PERCENT.search(text):
        return REFUSE
    if len(text) > _MAX_REFERENCE_CHARS:
        return _over_limit(raise_on_limit)
    text = text.strip()
    # Independent scans also catch thread ids embedded inside private source ids.
    tokens = _tokens(text)
    return _over_limit(raise_on_limit) if tokens is None else References(text, tokens)
