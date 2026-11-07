"""Pure, lossless reference recognition for provenance authorization."""

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

PROVENANCE_FIELDS = {
    "source_thread",
    "source_url",
    "source_file",
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
_THREAD = re.compile(r"thr_[0-9a-f]+", re.IGNORECASE)
_PRIVATE = re.compile(r"(?:capture-fork|capture|import-private):[^\s?]+", re.IGNORECASE)
_PERCENT = re.compile(r"%[0-9a-f]{2}", re.IGNORECASE)


@dataclass(frozen=True)
class References:
    text: str | None
    tokens: tuple[str, ...]


def _controls(text):
    return any(unicodedata.category(char) in {"Cc", "Cf"} for char in text)


def refs(value):
    """Decode at most four rounds, refusing controls before trimming anything."""
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
    text = text.strip()
    # Independent scans also catch thread ids embedded inside private source ids.
    tokens = tuple(
        dict.fromkeys(m[0] for pattern in (_THREAD, _PRIVATE) for m in pattern.finditer(text))
    )
    return References(text, tokens)
