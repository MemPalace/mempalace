"""
importance.py — Rule-based importance scoring for memory drawers.

Assigns an importance tier (1.0–5.0) to drawer content at ingest time
so Layer 1 (Essential Story) can prioritize critical memories over trivia
in wake-up context.

See: https://github.com/MemPalace/mempalace/issues/2409
"""

import re

# Health, safety, credentials — must always surface
CRITICAL_PATTERNS = re.compile(
    r"allerg|medical|medication|disease|emergency|password|"
    r"credential|api.key|ssn|birth.?date|blood.?type|"
    r"never.?forget|always.?remember|critical|life.?threatening",
    re.IGNORECASE,
)

# Identity facts — important but not safety-critical
IDENTITY_PATTERNS = re.compile(
    r"my name is|i am a|my job|my role|i work at|"
    r"i live in|born in|native language|nationality",
    re.IGNORECASE,
)


def score_importance(text: str) -> float:
    """Score text importance: 5.0=critical, 4.0=identity, 3.0=normal.

    The default of 3.0 matches the fallback in ``layers.py`` L1, so
    drawers ingested before this scorer existed sort identically to
    before — no migration required.
    """
    if not text:
        return 3.0
    if CRITICAL_PATTERNS.search(text):
        return 5.0
    if IDENTITY_PATTERNS.search(text):
        return 4.0
    return 3.0
