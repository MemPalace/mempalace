"""
query_sanitizer.py — Mitigate system prompt contamination in search queries.

Problem: AI agents sometimes prepend system prompts (2000+ chars) to search queries.
Embedding models represent the concatenated string as a single vector where the
system prompt overwhelms the actual question (typically 10-50 chars), causing
near-total retrieval failure (89.8% → 1.0% R@10).

Approach: "Mitigation" (減災) — not perfect prevention, but prevents the cliff.

Expected recovery:
  Step 1 passthrough (≤200 chars)     → no degradation, ~89.8%
  Step 2 question extraction (？found) → near-full recovery, ~85-89%
  Step 3 tail sentence extraction      → moderate recovery, ~80-89%
  Step 4 tail truncation (fallback)    → minimum viable, ~70-80%

  Without sanitizer: 1.0% (catastrophic silent failure)
  Worst case with sanitizer: ~70-80% (survivable)
"""

import logging
import re

from mempalace.config import strip_lone_surrogates

logger = logging.getLogger(__name__)

# --- Constants ---
MAX_QUERY_LENGTH = 250  # Above this, prompt contamination increasingly dominates
SAFE_QUERY_LENGTH = 200  # Below this, query is almost certainly clean
MIN_QUERY_LENGTH = 10  # Extracted result shorter than this = extraction failed
QUOTE_CHARS = {"'", '"'}

# Sentence splitter: split on . ! ? (including fullwidth) and newlines
_SENTENCE_SPLIT = re.compile(r"[.!?。！？\n]+")

# Question detector: ends with ? or ？ (possibly with trailing whitespace/quotes)
_QUESTION_MARK = re.compile(r'[?？]\s*["\']?\s*$')


def _sanitized(clean_query: str, method: str, original_length: int, replaced: bool) -> dict:
    if method != "passthrough":
        logger.warning(
            "Query sanitized: %d → %d chars (method=%s)",
            original_length,
            len(clean_query),
            method,
        )
    return {
        "clean_query": clean_query,
        "was_sanitized": method != "passthrough" or replaced,
        "original_length": original_length,
        "clean_length": len(clean_query),
        "truncated": method != "passthrough",
        "replaced_lone_surrogates": replaced,
        "method": method,
    }


def sanitizer_report(sanitized: dict):
    """What a search result says about the sanitizer, or None if it changed nothing."""
    if not sanitized["was_sanitized"]:
        return None
    return {
        key: sanitized[key]
        for key in (
            "method",
            "original_length",
            "clean_length",
            "truncated",
            "replaced_lone_surrogates",
            "clean_query",
        )
    }


def sanitize_query(raw_query: str) -> dict:
    """
    Extract the actual search intent from a potentially contaminated query.

    Args:
        raw_query: The raw query string from the AI agent, possibly containing
                   system prompt content prepended to the actual question.

    Returns:
        dict with keys:
            clean_query (str): The sanitized query to use for embedding search
            was_sanitized (bool): Whether any sanitization was applied
            original_length (int): Length of the raw input
            clean_length (int): Length of the sanitized output
            truncated (bool): Whether part of the query was dropped
            replaced_lone_surrogates (bool): Whether lone surrogates were
                replaced with U+FFFD
            method (str): Which extraction method was used
                - "passthrough": query was short enough, no action taken
                - "question_extraction": found and extracted a question sentence
                - "tail_sentence": extracted the last meaningful sentence
                - "tail_truncation": fallback — took the last MAX_QUERY_LENGTH chars
    """
    if not raw_query or not raw_query.strip():
        return _sanitized(raw_query or "", "passthrough", len(raw_query or ""), False)

    # Replace lone surrogates before any further processing so downstream
    # embed calls don't crash on UTF-8 encode.
    original_length = len(raw_query)
    stripped = raw_query.strip()
    raw_query = strip_lone_surrogates(stripped)
    replaced = raw_query != stripped

    def _strip_wrapping_quotes(candidate: str) -> str:
        candidate = candidate.strip()
        while (
            len(candidate) >= 2 and candidate[:1] in QUOTE_CHARS and candidate[:1] == candidate[-1:]
        ):
            candidate = candidate[1:-1].strip()
            if not candidate:
                return ""
        if candidate[:1] in QUOTE_CHARS:
            candidate = candidate[1:].strip()
        if candidate[-1:] in QUOTE_CHARS:
            candidate = candidate[:-1].strip()
        return candidate

    def _trim_candidate(candidate: str) -> str:
        candidate = _strip_wrapping_quotes(candidate)
        if len(candidate) <= MAX_QUERY_LENGTH:
            return candidate

        nested_fragments = [
            _strip_wrapping_quotes(frag)
            for frag in _SENTENCE_SPLIT.split(candidate)
            if frag.strip()
        ]
        for frag in reversed(nested_fragments):
            if MIN_QUERY_LENGTH <= len(frag) <= MAX_QUERY_LENGTH:
                return frag

        return candidate[-MAX_QUERY_LENGTH:].strip()

    # --- Step 1: Short query passthrough ---
    if len(raw_query) <= SAFE_QUERY_LENGTH:
        return _sanitized(raw_query, "passthrough", original_length, replaced)

    # --- Step 2: Question extraction ---
    # Split into sentences and find ones ending with ?
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(raw_query) if s.strip()]

    # Also split on newlines to catch questions on their own line
    all_segments = []
    for s in raw_query.split("\n"):
        s = s.strip()
        if s:
            all_segments.append(s)

    # Look for question marks in segments (prefer later ones = more likely the actual query)
    question_sentences = []
    for seg in reversed(all_segments):
        if _QUESTION_MARK.search(seg):
            question_sentences.append(seg)

    if not question_sentences:
        # Also check the sentence-split results
        for sent in reversed(sentences):
            if "?" in sent or "？" in sent:
                question_sentences.append(sent)

    if question_sentences:
        # Take the last (most recent) question found
        candidate = question_sentences[0].strip()
        if len(candidate) >= MIN_QUERY_LENGTH:
            # Apply length guard
            if len(candidate) > MAX_QUERY_LENGTH:
                candidate = _trim_candidate(candidate)
            return _sanitized(candidate, "question_extraction", original_length, replaced)

    # --- Step 3: Tail sentence extraction ---
    # System prompts are prepended, so the actual query is near the end.
    # Walk backwards through segments to find the last meaningful sentence.
    for seg in reversed(all_segments):
        seg = seg.strip()
        if len(seg) >= MIN_QUERY_LENGTH:
            candidate = _trim_candidate(seg)
            if len(candidate) < MIN_QUERY_LENGTH:
                continue
            return _sanitized(candidate, "tail_sentence", original_length, replaced)

    # --- Step 4: Tail truncation (fallback) ---
    # Nothing worked — just take the last MAX_QUERY_LENGTH characters.
    return _sanitized(
        raw_query[-MAX_QUERY_LENGTH:].strip(), "tail_truncation", original_length, replaced
    )
