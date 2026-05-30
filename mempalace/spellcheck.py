#!/usr/bin/env python3
"""
spellcheck.py — Spell-correct user messages before palace filing.

Preserves:
  - Technical terms (words with digits, hyphens, underscores)
  - CamelCase and ALL_CAPS identifiers
  - Known entity names (from EntityRegistry if available)
  - URLs and file paths
  - Words shorter than 3 chars (common abbreviations, pronouns, etc.)
  - Proper nouns already capitalized in context

Corrects:
  - Genuine typos in lowercase, flowing text
  - Common fat-finger words (3am → 3am, knoe → know)

Usage:
    from mempalace.spellcheck import spellcheck_user_text
    corrected = spellcheck_user_text("lsresdy knoe the question befor")
    # → "already know the question before"  (best effort)
"""

import re
from pathlib import Path
from typing import Optional

# Lazy-load autocorrect — not everyone has it installed
_speller = None
_autocorrect_available = None

# System word list — loaded once, used to skip already-valid words
_system_words: Optional[set] = None
_SYSTEM_DICT = Path("/usr/share/dict/words")


def _get_speller():
    global _speller, _autocorrect_available
    if _autocorrect_available is None:
        try:
            from autocorrect import Speller

            _speller = Speller(lang="en")
            _autocorrect_available = True
        except ImportError:
            _autocorrect_available = False
    return _speller if _autocorrect_available else None


def _get_system_words() -> set:
    """Load /usr/share/dict/words once and cache it."""
    global _system_words
    if _system_words is None:
        if _SYSTEM_DICT.exists():
            with open(_SYSTEM_DICT) as f:
                _system_words = {w.strip().lower() for w in f if w.strip()}
        else:
            _system_words = set()
    return _system_words


# ─────────────────────────────────────────────────────────────────────────────
# Patterns that mark a token as "don't touch this"
# ─────────────────────────────────────────────────────────────────────────────

# Matches any token with a digit anywhere in it: 3am, bge-large-v1.5, top-10
_HAS_DIGIT = re.compile(r"\d")

# CamelCase: ChromaDB, MemPalace, LongMemEval
_IS_CAMEL = re.compile(r"[A-Z][a-z]+[A-Z]")

# ALL_CAPS or all-caps with underscores: NDCG, R@5, MAX_RESULTS
_IS_ALLCAPS = re.compile(r"^[A-Z_@#$%^&*()+=\[\]{}|<>?.:/\\]+$")

# Technical token: contains hyphens or underscores (bge-large, train_test)
_IS_TECHNICAL = re.compile(r"[-_]")

# URL-like or file-path-like
_IS_URL = re.compile(r"https?://|www\.|/Users/|~/|\.[a-z]{2,4}$", re.IGNORECASE)

# Code fences, markdown, or emoji-heavy
_IS_CODE_OR_EMOJI = re.compile(r"[`*_#{}[\]\\]")

# Very short tokens — skip (I, a, ok, my, etc. — also avoids ambiguous 3-char typos
# like "kno" which autocorrect resolves as "no" rather than "know")
_MIN_LENGTH = 4


def _should_skip(token: str, known_names: set) -> bool:
    """Return True if this token should be left as-is."""
    if len(token) < _MIN_LENGTH:
        return True
    if _HAS_DIGIT.search(token):
        return True
    if _IS_CAMEL.search(token):
        return True
    if _IS_ALLCAPS.match(token):
        return True
    if _IS_TECHNICAL.search(token):
        return True
    if _IS_URL.search(token):
        return True
    if _IS_CODE_OR_EMOJI.search(token):
        return True
    # Known proper names (entity registry)
    if token.lower() in known_names:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Load known entity names from registry (optional, best-effort)
# ─────────────────────────────────────────────────────────────────────────────


def _load_known_names() -> set:
    """Pull all registered names from EntityRegistry. Returns empty set on failure."""
    try:
        from mempalace.entity_registry import EntityRegistry

        reg = EntityRegistry.load()
        names = set()
        for name, person in reg.people.items():
            if name:
                names.add(name.lower())
            canonical = person.get("canonical", "")
            if canonical:
                names.add(canonical.lower())
            for alias in person.get("aliases", []):
                names.add(alias.lower())
        return names
    except Exception:
        return set()


# ─────────────────────────────────────────────────────────────────────────────
# Edit distance — used to guard against over-aggressive autocorrect
# ─────────────────────────────────────────────────────────────────────────────


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


# ─────────────────────────────────────────────────────────────────────────────
# Core correction
# ─────────────────────────────────────────────────────────────────────────────

# Split on word boundaries but keep punctuation attached to tokens
_TOKEN_RE = re.compile(r"(\S+)")


def spellcheck_user_text(text: str, known_names: Optional[set] = None) -> str:
    """
    Spell-correct a user message.

    Args:
        text: Raw user message text.
        known_names: Set of lowercase names/terms to preserve. If None,
                     attempts to load from EntityRegistry automatically.

    Returns:
        Corrected text. Falls back to original if autocorrect not installed.
    """
    speller = _get_speller()
    if speller is None:
        return text  # autocorrect not installed — pass through unchanged

    if known_names is None:
        known_names = _load_known_names()

    # Process token by token, preserving all whitespace
    sys_words = _get_system_words()

    def _fix(match):
        token = match.group(0)
        # Strip trailing punctuation for checking, reattach after
        stripped = token.rstrip(".,!?;:'\")")
        punct = token[len(stripped) :]

        if not stripped or _should_skip(stripped, known_names):
            return token

        # Only correct lowercase words (capitalized words are likely proper nouns)
        if stripped[0].isupper():
            return token

        # Skip words that are already valid English — prevents "coherently" → "inherently"
        if stripped.lower() in sys_words:
            return token

        corrected = speller(stripped)

        # Guard: don't apply if corrected word is too different from original.
        # Extra safety net for words not in the system dict but also not typos.
        if corrected != stripped:
            dist = _edit_distance(stripped, corrected)
            max_edits = 2 if len(stripped) <= 7 else 3
            if dist > max_edits:
                return token

        return corrected + punct

    return _TOKEN_RE.sub(_fix, text)


def spellcheck_transcript_line(line: str) -> str:
    """
    Spell-correct a single transcript line.
    Only touches lines that start with '>' (user turns).
    Assistant turns are never modified.
    """
    stripped = line.lstrip()
    if not stripped.startswith(">"):
        return line

    # '> actual message here'
    prefix_len = len(line) - len(stripped) + 2  # '> '
    message = line[prefix_len:]
    if not message.strip():
        return line

    corrected = spellcheck_user_text(message)
    return line[:prefix_len] + corrected


def spellcheck_transcript(content: str) -> str:
    """
    Spell-correct all user turns in a full transcript.
    Only lines starting with '>' are touched.
    """
    lines = content.split("\n")
    return "\n".join(spellcheck_transcript_line(line) for line in lines)


# ─────────────────────────────────────────────────────────────────────────────
# Quick test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_cases = [
        "lsresdy knoe the question befor",
        "isn't there meny diferent benchmarks tesing questions?",
        "also can you pleese spell chekc my questions befroe storing",
        "it's realy hard for me to writte coherently at 3am",
        "Mempalace cant be fine-tunned if you alredy kno the question",
        # Should NOT change these:
        "ChromaDB bge-large-en-v1.5 NDCG@10 R@5",
        "Riley picked up Sam from school",
        "hybrid_v4 top-k=50 longmemeval_bench.py",
    ]

    print("Spell-check test\n" + "=" * 50)
    for msg in test_cases:
        result = spellcheck_user_text(msg, known_names={"riley", "sam", "mempalace"})
        changed = " ← CHANGED" if result != msg else ""
        print(f"\nIN:  {msg}")
        if result != msg:
            print(f"OUT: {result}{changed}")
        else:
            print("OUT: (unchanged)")
