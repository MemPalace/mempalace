"""
corpus_origin.py — Detect whether a corpus is an AI-dialogue record and,
if so, what platform and what persona names the user has assigned to the
agent.

This is the first question any downstream Pass 2 classification needs
answered. Without it, a drawer like "my three sons" in a Claude Code
dialogue corpus can't be correctly resolved to "three AI instances"
rather than "three biological children."

Two-tier detection:

  Tier 1 — detect_origin_heuristic(samples)
           Cheap, no API. Grep for well-known AI brand terms + turn
           markers. Always runs. Outputs a hypothesis.

  Tier 2 — detect_origin_llm(samples, provider)
           Uses an LLMProvider (typically Haiku via mempalace.llm_client)
           with the model's pre-trained knowledge of Claude/ChatGPT/Gemini
           etc. Confirms platform, extracts agent persona-names the user
           has assigned. One call, ~$0.01 cost.

Design principle:
  Don't make the classifier re-discover what Claude, ChatGPT, Gemini, MCP,
  or other well-known entities ARE — the LLM already knows them from its
  training. Only corpus-specific entities (e.g. the user's persona-name
  for their Claude instance) need discovery.

Default stance (when evidence is thin):
  "This IS an AI-dialogue corpus" — false-negative is catastrophic for
  downstream classification; false-positive is recoverable via per-drawer
  voice-profile detection in later passes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Optional


# ── Well-known AI brand terms (expand as new platforms emerge) ────────────
# Detection is by PATTERN + CONTEXT, not by capitalization or English-language
# rules. Two categories:
#
#   UNAMBIGUOUS — terms that have essentially no meaning outside of AI context.
#     Always counted toward AI-dialogue evidence.
#
#   AMBIGUOUS — terms that share a string with common English words, names,
#     poetry forms, zodiac signs, animals, etc. Counted toward AI-dialogue
#     evidence ONLY when at least one unambiguous AI signal also appears in
#     the corpus (turn marker, unambiguous brand term, or AI infrastructure
#     term). This avoids false-positives on French novels with characters
#     named "Claude", astrology corpora discussing "Gemini", poetry corpora
#     full of "haiku" / "sonnet", etc.
#
# All matching is CASE-INSENSITIVE — users type lowercase constantly.

_AI_UNAMBIGUOUS_TERMS = [
    # Anthropic-specific
    "Anthropic",
    "Claude Code",
    "Claude 3",
    "Claude 4",
    "claude mcp",
    "CLAUDE.md",
    ".claude/",
    # OpenAI-specific
    "ChatGPT",
    "GPT-4",
    "GPT-3",
    "GPT-5",
    "OpenAI",
    "gpt-4o",
    "gpt-4-turbo",
    "o1-preview",
    "o3",
    # Google-specific
    "gemini-pro",
    "gemini-1.5",
    "Google AI",
    # Meta / others (specific model identifiers, not bare common words)
    "Mixtral",
    "Cohere",
    # AI-infrastructure terms with no common-English collision
    "MCP",
    "LLM",
    "RAG",
    "fine-tune",
    "context window",
    "embedding",
]

_AI_AMBIGUOUS_TERMS = [
    # Anthropic — bare brand/model names that collide with names + poetry
    "Claude",  # also a common French masculine name
    "Opus",  # also a musical work, comic strip, magazine
    "Sonnet",  # also a 14-line poem form
    "Haiku",  # also a 17-syllable poem form
    # Google — bare brand that collides with zodiac sign
    "Gemini",  # also the zodiac sign
    "Bard",  # also a poet / Shakespeare
    # Meta / others
    "Llama",  # also the South American animal
    "Mistral",  # also a Mediterranean wind
    # Note: 'prompt', 'completion', 'tokens' previously lived here but were
    # removed: they're suppressed without an unambiguous co-signal anyway,
    # and by the time a co-signal is present the corpus is already flagged.
    # Keeping them just produced noisier evidence strings.
]

# Turn-marker patterns commonly seen in AI-dialogue transcripts
_TURN_MARKERS = [
    r"\buser\s*:\s*",
    r"\bassistant\s*:\s*",
    r"\bhuman\s*:\s*",
    r"\bai\s*:\s*",
    r"\b>>>\s*User\b",
    r"\b>>>\s*Assistant\b",
]


def _brand_pattern(term: str) -> str:
    """Build a regex for a brand term that uses word boundaries
    only on edges where the term itself starts/ends with a word
    character. Without this nuance:
      - 'Claude' would falsely match inside 'Claudette' (no \\b)
      - '.claude/' would fail to match at start of string (\\b
        before non-word char requires preceding word char)
    So we only attach \\b where it actually makes sense."""
    escaped = re.escape(term)
    prefix = r"\b" if term[0].isalnum() or term[0] == "_" else ""
    suffix = r"\b" if term[-1].isalnum() or term[-1] == "_" else ""
    return prefix + escaped + suffix


@dataclass
class CorpusOriginResult:
    """Structured output from corpus-origin detection.

    Fields:
      likely_ai_dialogue — best hypothesis about whether this is AI-dialogue
      confidence — 0.0 to 1.0
      primary_platform — e.g. "Claude Code (Anthropic CLI)" or None
      user_name — the corpus author's name if identifiable from context, else None
      agent_persona_names — names the user has assigned to the AI agent(s)
                            (e.g. ["Echo", "Sparrow"]). Does NOT include the user's own name.
      evidence — human-readable reasons for the classification
    """

    likely_ai_dialogue: bool
    confidence: float
    primary_platform: Optional[str]
    user_name: Optional[str] = None
    agent_persona_names: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Tier 1: cheap heuristic ───────────────────────────────────────────────


def detect_origin_heuristic(samples: list[str]) -> CorpusOriginResult:
    """Fast grep-based detection. No API calls.

    Scores AI-dialogue likelihood by counting:
      - occurrences of well-known AI brand terms
      - turn-marker patterns (user:, assistant:, etc.)

    Returns a CorpusOriginResult with confidence derived from signal density.
    """
    combined = "\n\n".join(samples)
    total_chars = max(1, len(combined))

    # Count UNAMBIGUOUS brand-term hits (case-insensitive — users type
    # lowercase constantly, so 'chatgpt' must trip the same as 'ChatGPT').
    # Word boundaries prevent false in-word matches (see _brand_pattern).
    unambiguous_hits: dict[str, int] = {}
    total_unambiguous = 0
    for term in _AI_UNAMBIGUOUS_TERMS:
        matches = re.findall(_brand_pattern(term), combined, re.IGNORECASE)
        if matches:
            unambiguous_hits[term] = len(matches)
            total_unambiguous += len(matches)

    # Count AMBIGUOUS brand-term hits separately. These will only be
    # counted toward AI-dialogue evidence if the corpus also contains
    # at least one unambiguous AI signal — see co-occurrence rule below.
    ambiguous_hits: dict[str, int] = {}
    total_ambiguous = 0
    for term in _AI_AMBIGUOUS_TERMS:
        matches = re.findall(_brand_pattern(term), combined, re.IGNORECASE)
        if matches:
            ambiguous_hits[term] = len(matches)
            total_ambiguous += len(matches)

    # Count turn-marker hits (case-insensitive — transcripts vary).
    turn_hits = 0
    turn_types_found = set()
    for pattern in _TURN_MARKERS:
        matches = re.findall(pattern, combined, re.IGNORECASE)
        if matches:
            turn_hits += len(matches)
            turn_types_found.add(pattern)

    # Co-occurrence rule for ambiguous terms.
    # Ambiguous terms (e.g. 'Claude' as a French name, 'Gemini' as a zodiac
    # sign, 'Haiku' as a poem form) only count toward brand evidence if
    # the corpus also contains at least one unambiguous AI signal. Otherwise
    # we'd false-positive on French novels, astrology forums, poetry corpora,
    # llama-rancher journals, etc.
    has_ai_context = total_unambiguous > 0 or turn_hits > 0
    counted_brand_hits = total_unambiguous + (total_ambiguous if has_ai_context else 0)

    # Brand-term density per 1000 chars; turn-marker density likewise.
    # Tuned on a small set of examples; these aren't magic numbers and
    # can be revisited as we see more corpora.
    brand_density = counted_brand_hits / (total_chars / 1000)
    turn_density = turn_hits / (total_chars / 1000)

    # Build evidence list
    evidence: list[str] = []
    shown_hits = dict(unambiguous_hits)
    if has_ai_context:
        shown_hits.update(ambiguous_hits)
    if shown_hits:
        top_terms = sorted(shown_hits.items(), key=lambda x: -x[1])[:5]
        evidence.append("AI brand terms: " + ", ".join(f"'{k}' ({v}x)" for k, v in top_terms))
    elif ambiguous_hits and not has_ai_context:
        # Be transparent that we saw ambiguous matches but suppressed them
        # for lack of co-occurring AI context.
        suppressed = sorted(ambiguous_hits.items(), key=lambda x: -x[1])[:3]
        evidence.append(
            "Ambiguous terms present but suppressed (no co-occurring AI signal): "
            + ", ".join(f"'{k}' ({v}x)" for k, v in suppressed)
        )
    if turn_hits:
        evidence.append(
            f"Turn markers detected: {turn_hits} occurrences across {len(turn_types_found)} pattern types"
        )

    # Decision logic:
    #   strong signal (brand OR turn hits both >= threshold) → confident AI-dialogue
    #   MEANINGFUL absence (enough text, zero brand, zero turn) → confident narrative
    #   ambiguous or insufficient text → default stance: AI-dialogue with low confidence
    #
    # Threshold for "meaningful absence": the samples collectively have to
    # be long enough that the absence of AI signals would be expected to
    # surface if the corpus really is narrative. 150 chars is the working
    # floor — below that, we cannot confidently say "this is narrative."
    MEANINGFUL_TEXT_FLOOR = 150

    if brand_density >= 0.5 or turn_density >= 2.0:
        return CorpusOriginResult(
            likely_ai_dialogue=True,
            confidence=min(0.95, 0.6 + 0.1 * (brand_density + turn_density)),
            primary_platform=None,  # tier 2 will refine
            evidence=evidence,
        )
    if counted_brand_hits == 0 and turn_hits == 0 and total_chars >= MEANINGFUL_TEXT_FLOOR:
        # Note: ambiguous-only matches (e.g. a French novel with 'Claude' as
        # a character name) flow through here because counted_brand_hits == 0
        # when no unambiguous AI signal co-occurs. The 'evidence' list still
        # records that the ambiguous matches were seen and suppressed.
        narrative_evidence = list(evidence) + [
            f"no unambiguous AI signal across {total_chars} chars of text — pure narrative"
        ]
        return CorpusOriginResult(
            likely_ai_dialogue=False,
            confidence=0.9,
            primary_platform=None,
            evidence=narrative_evidence,
        )
    # Ambiguous or too-short-to-tell case: default stance is AI-dialogue
    # with explicit low confidence. Tier 2 (LLM) should be called to confirm.
    reason = "weak signal" if (counted_brand_hits or turn_hits) else "insufficient text"
    return CorpusOriginResult(
        likely_ai_dialogue=True,
        confidence=0.4,
        primary_platform=None,
        evidence=evidence
        + [
            f"{reason} — applying default-stance (ai_dialogue=True, low confidence). "
            "Tier 2 LLM check recommended to confirm or override."
        ],
    )


# ── Tier 2: LLM-assisted confirmation + persona extraction ────────────────


_SYSTEM_PROMPT = """You are analyzing a corpus of text to determine whether it is a \
record of conversations with an AI agent (e.g. Claude, ChatGPT, Gemini, custom LLM \
apps), or some other kind of text (personal narrative, story, research notes, \
journal, code, etc.).

Use your pre-existing knowledge of well-known AI platforms. You don't need the \
corpus to explain what Claude or ChatGPT is — you already know. Your job is to \
detect evidence of their presence and identify what persona-names the user has \
assigned to the agent(s) they converse with.

CRITICAL distinction:
  - agent_persona_names are names the USER has assigned to the AI AGENT(S)
    they converse with. Example: "Echo", "Sparrow", "Henry" might be names
    the user calls a Claude instance they're building a relationship with.
  - Do NOT include the USER's own name in agent_persona_names. The user
    is the human author of the corpus, not a persona of the agent. Even
    if the user's name appears frequently in the text (writing about
    themselves), that is NOT an agent persona.
  - If you can identify the user's name from context, put it in user_name
    (separate field). If unclear, leave user_name null.

Respond with JSON only (no prose before or after):
{
  "is_ai_dialogue_corpus": <true|false>,
  "confidence": <0.0 to 1.0>,
  "primary_platform": <"Claude (Anthropic)" | "ChatGPT (OpenAI)" | "Gemini (Google)" | other platform name | null>,
  "user_name": <user's name if clearly identifiable from context, else null>,
  "agent_persona_names": [<names the user has assigned to the AI AGENT(S), NOT the user's own name>],
  "evidence": [<short bullet strings explaining the decision>]
}

Default stance: if evidence is thin or mixed, return is_ai_dialogue_corpus=true \
with low confidence. False-negatives on AI-dialogue detection break downstream \
classification; false-positives are recoverable later.
"""


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of a possibly-messy LLM response."""
    text = text.strip()
    if not text:
        return None
    # Try straight parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to find a {...} block
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


def detect_origin_llm(samples: list[str], provider) -> CorpusOriginResult:
    """LLM-assisted detection. Takes samples (list of drawer-text excerpts)
    and an LLMProvider (mempalace.llm_client.LLMProvider). Returns the
    same CorpusOriginResult shape as the heuristic.

    Falls back conservatively (default-stance ai=True, low confidence)
    on any LLM error or malformed response — never raises.
    """
    # Build the user prompt: concise excerpts, capped so we stay cheap
    max_excerpt_chars = 800
    excerpts = "\n\n---\n\n".join(
        f"[sample {i + 1}]\n{s[:max_excerpt_chars]}" for i, s in enumerate(samples[:20])
    )
    user_prompt = f"CORPUS EXCERPTS:\n\n{excerpts}\n\nAnalyze and respond with JSON."

    try:
        resp = provider.classify(system=_SYSTEM_PROMPT, user=user_prompt, json_mode=True)
        raw = getattr(resp, "text", "") or ""
    except Exception as e:
        return CorpusOriginResult(
            likely_ai_dialogue=True,
            confidence=0.3,
            primary_platform=None,
            evidence=[f"LLM provider error (fallback to default stance): {e}"],
        )

    parsed = _extract_json(raw)
    if not parsed or not isinstance(parsed, dict):
        return CorpusOriginResult(
            likely_ai_dialogue=True,
            confidence=0.3,
            primary_platform=None,
            evidence=["LLM response was not valid JSON (fallback to default stance)"],
        )

    # Pull fields defensively. If the LLM leaked the user_name into
    # agent_persona_names despite the prompt telling it not to, filter it out.
    user_name = parsed.get("user_name") or None
    personas = list(parsed.get("agent_persona_names") or [])
    if user_name:
        personas = [p for p in personas if p.lower() != user_name.lower()]
    return CorpusOriginResult(
        likely_ai_dialogue=bool(parsed.get("is_ai_dialogue_corpus", True)),
        confidence=float(parsed.get("confidence", 0.5)),
        primary_platform=parsed.get("primary_platform") or None,
        user_name=user_name,
        agent_persona_names=personas,
        evidence=list(parsed.get("evidence") or []),
    )
