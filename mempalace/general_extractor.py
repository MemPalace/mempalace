#!/usr/bin/env python3
"""
general_extractor.py — Extract 5 types of memories from text.

Types:
  1. DECISIONS    — "we went with X because Y", choices made
  2. PREFERENCES  — "always use X", "never do Y", "I prefer Z"
  3. MILESTONES   — breakthroughs, things that finally worked
  4. PROBLEMS     — what broke, what fixed it, root causes
  5. EMOTIONAL    — feelings, vulnerability, relationships

No LLM required. Pure keyword/pattern heuristics.
No external dependencies on palace.py, dialect.py, or layers.py.

Usage:
    from general_extractor import extract_memories

    chunks = extract_memories(text)
    # [{"content": "...", "memory_type": "decision", "chunk_index": 0}, ...]
"""

import re
from typing import List, Dict, Tuple

from .config import DEFAULT_CHUNK_SIZE


# =============================================================================
# MARKER SETS — One per memory type
# =============================================================================

DECISION_MARKERS = [
    r"\blet'?s (use|go with|try|pick|choose|switch to)\b",
    r"\bwe (should|decided|chose|went with|picked|settled on)\b",
    r"\bi'?m going (to|with)\b",
    r"\bbetter (to|than|approach|option|choice)\b",
    r"\binstead of\b",
    r"\brather than\b",
    r"\bthe reason (is|was|being)\b",
    r"\bbecause\b",
    r"\btrade-?off\b",
    r"\bpros and cons\b",
    r"\bover\b.*\bbecause\b",
    r"\barchitecture\b",
    r"\bapproach\b",
    r"\bstrategy\b",
    r"\bpattern\b",
    r"\bstack\b",
    r"\bframework\b",
    r"\binfrastructure\b",
    r"\bset (it |this )?to\b",
    r"\bconfigure\b",
    r"\bdefault\b",
]

PREFERENCE_MARKERS = [
    r"\bi prefer\b",
    r"\balways use\b",
    r"\bnever use\b",
    r"\bdon'?t (ever |like to )?(use|do|mock|stub|import)\b",
    r"\bi like (to|when|how)\b",
    r"\bi hate (when|how|it when)\b",
    r"\bplease (always|never|don'?t)\b",
    r"\bmy (rule|preference|style|convention) is\b",
    r"\bwe (always|never)\b",
    r"\bfunctional\b.*\bstyle\b",
    r"\bimperative\b",
    r"\bsnake_?case\b",
    r"\bcamel_?case\b",
    r"\btabs\b.*\bspaces\b",
    r"\bspaces\b.*\btabs\b",
    r"\buse\b.*\binstead of\b",
]

MILESTONE_MARKERS = [
    r"\bit works\b",
    r"\bit worked\b",
    r"\bgot it working\b",
    r"\bfixed\b",
    r"\bsolved\b",
    r"\bbreakthrough\b",
    r"\bfigured (it )?out\b",
    r"\bnailed it\b",
    r"\bcracked (it|the)\b",
    r"\bfinally\b",
    r"\bfirst time\b",
    r"\bfirst ever\b",
    r"\bnever (done|been|had) before\b",
    r"\bdiscovered\b",
    r"\brealized\b",
    r"\bfound (out|that)\b",
    r"\bturns out\b",
    r"\bthe key (is|was|insight)\b",
    r"\bthe trick (is|was)\b",
    r"\bnow i (understand|see|get it)\b",
    r"\bbuilt\b",
    r"\bcreated\b",
    r"\bimplemented\b",
    r"\bshipped\b",
    r"\blaunched\b",
    r"\bdeployed\b",
    r"\breleased\b",
    r"\bprototype\b",
    r"\bproof of concept\b",
    r"\bdemo\b",
    r"\bversion \d",
    r"\bv\d+\.\d+",
    r"\d+x (compression|faster|slower|better|improvement|reduction)",
    r"\d+% (reduction|improvement|faster|better|smaller)",
]

PROBLEM_MARKERS = [
    r"\b(bug|error|crash|fail|broke|broken|issue|problem)\b",
    r"\bdoesn'?t work\b",
    r"\bnot working\b",
    r"\bwon'?t\b.*\bwork\b",
    r"\bkeeps? (failing|crashing|breaking|erroring)\b",
    r"\broot cause\b",
    r"\bthe (problem|issue|bug) (is|was)\b",
    r"\bturns out\b.*\b(was|because|due to)\b",
    r"\bthe fix (is|was)\b",
    r"\bworkaround\b",
    r"\bthat'?s why\b",
    r"\bthe reason it\b",
    r"\bfixed (it |the |by )\b",
    r"\bsolution (is|was)\b",
    r"\bresolved\b",
    r"\bpatched\b",
    r"\bthe answer (is|was)\b",
    r"\b(had|need) to\b.*\binstead\b",
]

EMOTION_MARKERS = [
    r"\blove\b",
    r"\bscared\b",
    r"\bafraid\b",
    r"\bproud\b",
    r"\bhurt\b",
    r"\bhappy\b",
    r"\bsad\b",
    r"\bcry\b",
    r"\bcrying\b",
    r"\bmiss\b",
    r"\bsorry\b",
    r"\bgrateful\b",
    r"\bangry\b",
    r"\bworried\b",
    r"\blonely\b",
    r"\bbeautiful\b",
    r"\bamazing\b",
    r"\bwonderful\b",
    r"i feel",
    r"i'm scared",
    r"i love you",
    r"i'm sorry",
    r"i can't",
    r"i wish",
    r"i miss",
    r"i need",
    r"never told anyone",
    r"nobody knows",
    r"\*[^*]+\*",
]

ALL_MARKERS = {
    "decision": DECISION_MARKERS,
    "preference": PREFERENCE_MARKERS,
    "milestone": MILESTONE_MARKERS,
    "problem": PROBLEM_MARKERS,
    "emotional": EMOTION_MARKERS,
}


# =============================================================================
# SENTIMENT — for disambiguation
# =============================================================================

POSITIVE_WORDS = {
    "pride",
    "proud",
    "joy",
    "happy",
    "love",
    "loving",
    "beautiful",
    "amazing",
    "wonderful",
    "incredible",
    "fantastic",
    "brilliant",
    "perfect",
    "excited",
    "thrilled",
    "grateful",
    "warm",
    "breakthrough",
    "success",
    "works",
    "working",
    "solved",
    "fixed",
    "nailed",
    "heart",
    "hug",
    "precious",
    "adore",
}

NEGATIVE_WORDS = {
    "bug",
    "error",
    "crash",
    "crashing",
    "crashed",
    "fail",
    "failed",
    "failing",
    "failure",
    "broken",
    "broke",
    "breaking",
    "breaks",
    "issue",
    "problem",
    "wrong",
    "stuck",
    "blocked",
    "unable",
    "impossible",
    "missing",
    "terrible",
    "horrible",
    "awful",
    "worse",
    "worst",
    "panic",
    "disaster",
    "mess",
}


def _get_sentiment(text: str) -> str:
    """Quick sentiment: 'positive', 'negative', or 'neutral'."""
    words = set(w.lower() for w in re.findall(r"\b\w+\b", text))
    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)
    if pos > neg:
        return "positive"
    elif neg > pos:
        return "negative"
    return "neutral"


def _has_resolution(text: str) -> bool:
    """Check if text describes a RESOLVED problem."""
    text_lower = text.lower()
    patterns = [
        r"\bfixed\b",
        r"\bsolved\b",
        r"\bresolved\b",
        r"\bpatched\b",
        r"\bgot it working\b",
        r"\bit works\b",
        r"\bnailed it\b",
        r"\bfigured (it )?out\b",
        r"\bthe (fix|answer|solution)\b",
    ]
    return any(re.search(p, text_lower) for p in patterns)


def _disambiguate(memory_type: str, text: str, scores: Dict[str, float]) -> str:
    """Fix misclassifications using sentiment + resolution."""
    sentiment = _get_sentiment(text)

    # Resolved problems are milestones
    if memory_type == "problem" and _has_resolution(text):
        if scores.get("emotional", 0) > 0 and sentiment == "positive":
            return "emotional"
        return "milestone"

    # Problem + positive sentiment => milestone or emotional
    if memory_type == "problem" and sentiment == "positive":
        if scores.get("milestone", 0) > 0:
            return "milestone"
        if scores.get("emotional", 0) > 0:
            return "emotional"

    return memory_type


# =============================================================================
# CODE LINE FILTERING
# =============================================================================

_CODE_LINE_PATTERNS = [
    re.compile(r"^\s*[\$#]\s"),
    re.compile(
        r"^\s*(cd|source|echo|export|pip|npm|git|python|bash|curl|wget|mkdir|rm|cp|mv|ls|cat|grep|find|chmod|sudo|brew|docker)\s"
    ),
    re.compile(r"^\s*```"),
    re.compile(r"^\s*(import|from|def|class|function|const|let|var|return)\s"),
    re.compile(r"^\s*[A-Z_]{2,}="),
    re.compile(r"^\s*\|"),
    re.compile(r"^\s*[-]{2,}"),
    re.compile(r"^\s*[{}\[\]]\s*$"),
    re.compile(r"^\s*(if|for|while|try|except|elif|else:)\b"),
    re.compile(r"^\s*\w+\.\w+\("),
    re.compile(r"^\s*\w+ = \w+\.\w+"),
]


def _is_code_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    for pattern in _CODE_LINE_PATTERNS:
        if pattern.match(stripped):
            return True
    alpha_ratio = sum(1 for c in stripped if c.isalpha()) / max(len(stripped), 1)
    if alpha_ratio < 0.4 and len(stripped) > 10:
        return True
    return False


def _extract_prose(text: str) -> str:
    """Extract only prose lines (skip code) for classification scoring."""
    lines = text.split("\n")
    prose = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not _is_code_line(line):
            prose.append(line)
    result = "\n".join(prose).strip()
    return result if result else text


# =============================================================================
# SCORING
# =============================================================================


def _score_markers(text: str, markers: List[str]) -> Tuple[float, List[str]]:
    """Score text against regex markers. Returns (score, matched_keywords)."""
    text_lower = text.lower()
    score = 0.0
    keywords = []
    for marker in markers:
        matches = re.findall(marker, text_lower)
        if matches:
            score += len(matches)
            keywords.extend(m if isinstance(m, str) else m[0] if m else marker for m in matches)
    return score, list(set(keywords))


# =============================================================================
# MAIN EXTRACTION
# =============================================================================


def extract_memories(
    text: str,
    min_confidence: float = 0.3,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> List[Dict]:
    """
    Extract memories from a text string.

    Args:
        text: The text to extract from (any format).
        min_confidence: Minimum confidence threshold (0.0-1.0).
        chunk_size: Per-memory character cap. Segments exceeding this
            size are sliced verbatim into multiple memories that share
            the same ``memory_type``. Caller (typically ``mine_convos``)
            should pass ``MempalaceConfig.chunk_size`` so config-driven
            sizing reaches this path; the default matches the
            module-level CHUNK_SIZE in ``convo_miner.py``.

    Returns:
        List of dicts: {"content": str, "memory_type": str, "chunk_index": int}
    """
    # Split into paragraphs (double newline or speaker-turn boundaries)
    paragraphs = _split_into_segments(text)
    memories = []

    for para in paragraphs:
        if len(para.strip()) < 20:
            continue

        prose = _extract_prose(para)

        # Score against all types
        scores = {}
        for mem_type, markers in ALL_MARKERS.items():
            score, _ = _score_markers(prose, markers)
            if score > 0:
                scores[mem_type] = score

        if not scores:
            continue

        # Length bonus
        if len(para) > 500:
            length_bonus = 2
        elif len(para) > 200:
            length_bonus = 1
        else:
            length_bonus = 0

        max_type = max(scores, key=scores.get)
        max_score = scores[max_type] + length_bonus

        # Disambiguate
        max_type = _disambiguate(max_type, prose, scores)

        # Confidence
        confidence = min(1.0, max_score / 5.0)
        if confidence < min_confidence:
            continue

        content = para.strip()
        # Slice oversized segments verbatim. ``memory_type`` is propagated
        # to every slice — reclassifying sub-segments risks dropping data
        # (marker may live in only one slice), and the verbatim-store
        # mandate in CLAUDE.md forbids silent loss. Acceptable trade-off:
        # the label is an approximation for sub-slices, not a fresh
        # classification.
        if len(content) <= chunk_size:
            memories.append(
                {
                    "content": content,
                    "memory_type": max_type,
                    "chunk_index": len(memories),
                }
            )
            continue
        for i in range(0, len(content), chunk_size):
            memories.append(
                {
                    "content": content[i : i + chunk_size],
                    "memory_type": max_type,
                    "chunk_index": len(memories),
                }
            )

    return memories


def _split_into_segments(text: str) -> List[str]:
    """
    Split text into segments suitable for memory extraction.

    Tries speaker-turn splitting first (> markers, "Human:", "Assistant:", etc.),
    then falls back to paragraph splitting.
    """
    lines = text.split("\n")

    # Check for speaker-turn markers
    turn_patterns = [
        re.compile(r"^>\s"),  # > quoted user turn
        re.compile(r"^(Human|User|Q)\s*:", re.I),  # Human: / User:
        re.compile(r"^(Assistant|AI|A|Claude|ChatGPT)\s*:", re.I),
    ]

    turn_count = 0
    for line in lines:
        stripped = line.strip()
        for pat in turn_patterns:
            if pat.match(stripped):
                turn_count += 1
                break

    # If enough turn markers, split by turns
    if turn_count >= 3:
        return _split_by_turns(lines, turn_patterns)

    # Fallback: paragraph splitting
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    # If single giant block, chunk by line groups
    if len(paragraphs) <= 1 and len(lines) > 20:
        segments = []
        for i in range(0, len(lines), 25):
            group = "\n".join(lines[i : i + 25]).strip()
            if group:
                segments.append(group)
        return segments

    return paragraphs


def _split_by_turns(lines: List[str], turn_patterns: List[re.Pattern]) -> List[str]:
    """Split lines into segments at each speaker turn boundary."""
    segments = []
    current = []

    for line in lines:
        stripped = line.strip()
        is_turn = any(pat.match(stripped) for pat in turn_patterns)

        if is_turn and current:
            segments.append("\n".join(current))
            current = [line]
        else:
            current.append(line)

    if current:
        segments.append("\n".join(current))

    return segments


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python general_extractor.py <file>")
        print()
        print("Extracts decisions, preferences, milestones, problems, and")
        print("emotional moments from any text file.")
        sys.exit(1)

    filepath = sys.argv[1]
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    memories = extract_memories(text)

    # Summary
    from collections import Counter

    type_counts = Counter(m["memory_type"] for m in memories)
    print(f"Extracted {len(memories)} memories:")
    for mtype in ["decision", "preference", "milestone", "problem", "emotional"]:
        count = type_counts.get(mtype, 0)
        if count:
            print(f"  {mtype:12} {count}")

    print()
    for m in memories[:10]:
        preview = m["content"][:80].replace("\n", " ")
        print(f"  [{m['memory_type']:10}] {preview}...")
