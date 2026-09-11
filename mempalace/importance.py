"""Zero-dependency keyword-based importance scorer.

Classifies drawer content into tiers so L1 (Essential Story) can
prioritise critical facts over trivia at wake-up time.

Tiers:
    5.0  critical  — health, safety, credentials, emergencies
    4.0  identity  — name, occupation, birthdate, nationality
    3.0  normal    — everything else (matches the L1 default)
"""

from __future__ import annotations

import re

# ── patterns ────────────────────────────────────────────────────────

CRITICAL_PATTERNS = re.compile(
    r"\b("
    r"allerg(y|ic|ies)"
    r"|medication|medicated|prescription"
    r"|blood\s*type"
    r"|emergency\s*contact"
    r"|life[\s-]*threatening"
    r"|epipen|insulin|anaphyla"
    r"|password|passphrase|secret\s*key|api[\s_-]*key|credential"
    r"|ssn|social\s*security"
    r"|critical"
    r")\b",
    re.IGNORECASE,
)

IDENTITY_PATTERNS = re.compile(
    r"\b("
    r"my\s+name\s+is"
    r"|i\s+am\s+called"
    r"|i\s+work\s+at|i\s+work\s+for"
    r"|my\s+job\s+is|my\s+occupation"
    r"|born\s+on|birthdate|birthday|date\s+of\s+birth"
    r"|nationality|citizen(ship)?"
    r"|i\s+live\s+in|i\s+live\s+at|my\s+address"
    r"|my\s+email|my\s+phone"
    r")\b",
    re.IGNORECASE,
)


def score_importance(text: str) -> float:
    """Score text importance on a 1.0-5.0 scale.

    Returns:
        5.0 for critical content (health, safety, credentials),
        4.0 for identity content (name, job, birthdate),
        3.0 for everything else.
    """
    if not text:
        return 3.0
    if CRITICAL_PATTERNS.search(text):
        return 5.0
    if IDENTITY_PATTERNS.search(text):
        return 4.0
    return 3.0
