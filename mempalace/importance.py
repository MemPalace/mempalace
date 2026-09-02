"""
importance.py — Zero-dependency content scoring for drawer importance

Layer 1 (Essential Story) in ``layers.py`` sorts drawers by an
``importance`` metadata key descending (falling back to ``filed_at``),
but no ingestion path ever wrote an importance value until now — so
the sort was effectively flat at 3.0 for every drawer and wake-up
context was recency-only.

This module provides a small keyword/scorer that writes a sensible
``importance`` value into metadata so the L1 ranking can actually
prioritize critical content (health info, credentials, identity facts)
over trivia. Callers may still override with an explicit numeric
``importance`` (1.0–5.0) — see ``tool_add_drawer``.

The tiers (mirroring the reporter's proposal in #2409):

    5.0  Critical  — health, credentials, safety, life-threatening info
    4.0  Identity  — name, role, employer, location, language, etc.
    3.0  Default   — everything else

Callers write ``metadata["importance"] = score_importance(text)``,
or pass ``importance=<float>`` explicitly where the ingestion path
accepts one.
"""

import re
from typing import Optional

__all__ = ["DEFAULT_IMPORTANCE", "score_importance"]

DEFAULT_IMPORTANCE = 3.0

_CRITICAL_PATTERNS = re.compile(
    r"allerg|medical|medication|health|hospital|disease|emergency|password|"
    r"credential|api.?\s?key|ssn|birth.?\s?date|blood.?\s?type|"
    r"never.?\s?forget|always.?\s?remember|critical|life.?\s?threatening|"
    r"doctor|diagnos|prescription|insurance|symptom",
    re.IGNORECASE,
)

_IDENTITY_PATTERNS = re.compile(
    r"my name (?:is|:)|my job|my role|i work at|work[ ]?for|"
    r"i live in|live in|born in|native language|nationality|"
    r"\bi am (?:a|an|the)\b",
    re.IGNORECASE,
)


def score_importance(text: Optional[str]) -> float:
    """Return an importance score in [3.0, 5.0] from content.

    Uses two regex tiers: critical/identity. Both patterns match
    case-insensitively across the content. Returns ``DEFAULT_IMPORTANCE``
    (3.0) when no pattern matches.

    The tiers are deliberately conservative: false positives land a
    drawer one tier low (still 4.0, near the top of the L1 default
    range), false negatives keep it at the 3.0 default. Neither
    tier's presence is required for correctness — callers can
    always pass an explicit ``importance`` to ``tool_add_drawer``
    (see the caller-facing ``importance`` parameter on add_drawer).
    """
    if not text:
        return DEFAULT_IMPORTANCE
    if _CRITICAL_PATTERNS.search(text):
        return 5.0
    if _IDENTITY_PATTERNS.search(text):
        return 4.0
    return DEFAULT_IMPORTANCE
