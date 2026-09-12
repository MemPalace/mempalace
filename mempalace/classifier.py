"""
classifier.py — Structural content classifier (T0 / T2 / T3).

Ported from ContextCompressionEngine (src/classify.ts, AGPL-3.0).
Decides whether a block of text is:

    T0 — structurally or semantically verbatim-worthy (code, JSON, SQL,
         API keys, URLs, tracebacks, HTTP errors, file paths, hashes,
         numeric-with-units, version strings, direct quotes).
    T2 — short prose (< 20 words).
    T3 — long prose (>= 20 words).

Used by ``closet_llm.py`` to prioritize which blocks survive the
``MAX_CONTENT_CHARS`` truncation window when regenerating closets via an
external LLM. T0 blocks are packed first, then T2, then T3.

Zero dependencies. No network calls.
"""

import math
import re
from dataclasses import dataclass
from typing import List


@dataclass
class ClassifyResult:
    decision: str  # "T0" | "T2" | "T3"
    confidence: float
    reasons: List[str]


# ─── Structural patterns ─────────────────────────────────────────────────────

_CODE_FENCE_RE = re.compile(r"^[ ]{0,3}```[\w]*\n[\s\S]*?\n\s*```", re.MULTILINE)
_INDENT_CODE_RE = re.compile(r"^( {4}|\t).+\n( {4}|\t).+", re.MULTILINE)
_JSON_RE = re.compile(r'^\s*(?:\{\s*"|\[\s*[\[{"0-9\-])')
_YAML_RE = re.compile(r"^[\w-]+:\s+.+\n[\w-]+:\s+.+", re.MULTILINE)
_SPECIAL_CHAR_RE = re.compile(r"[{}\[\]<>|\\;:@#$%^&*()=+`~]")


def _detect_structural(text: str) -> List[str]:
    reasons: List[str] = []
    if _CODE_FENCE_RE.search(text):
        reasons.append("code_fence")
    if _INDENT_CODE_RE.search(text):
        reasons.append("indented_code")
    if _JSON_RE.search(text):
        reasons.append("json_structure")
    if _YAML_RE.search(text):
        reasons.append("yaml_structure")

    lines = text.split("\n")
    if lines:
        lengths = [len(line) for line in lines]
        mean = sum(lengths) / len(lengths)
        if mean > 0 and len(lines) > 3:
            variance = sum((v - mean) ** 2 for v in lengths) / len(lengths)
            cv = math.sqrt(variance) / mean
            if cv > 1.2:
                reasons.append("high_line_length_variance")

    if text:
        specials = len(_SPECIAL_CHAR_RE.findall(text))
        if specials / len(text) > 0.15:
            reasons.append("high_special_char_ratio")

    return reasons


# ─── SQL detection (tiered anchors) ──────────────────────────────────────────

_SQL_ALL_RE = re.compile(
    r"\b(?:SELECT|FROM|WHERE|JOIN|INSERT|INTO|UPDATE|SET|DELETE|CREATE|ALTER|DROP|"
    r"TRUNCATE|MERGE|GRANT|REVOKE|HAVING|UNION|GROUP\s+BY|ORDER\s+BY|DISTINCT|LIMIT|"
    r"OFFSET|VALUES|PRIMARY\s+KEY|FOREIGN\s+KEY|NOT\s+NULL|VARCHAR|INTEGER|BOOLEAN|"
    r"CONSTRAINT|CASCADE|RETURNING|ON\s+CONFLICT|UPSERT|WITH\s+RECURSIVE|"
    r"INNER\s+JOIN|LEFT\s+JOIN|RIGHT\s+JOIN|CROSS\s+JOIN|FULL\s+JOIN|NATURAL\s+JOIN)\b",
    re.IGNORECASE,
)
_SQL_STRONG = {
    "GROUP BY", "ORDER BY", "PRIMARY KEY", "FOREIGN KEY", "NOT NULL",
    "VARCHAR", "INTEGER", "BOOLEAN", "CONSTRAINT", "CASCADE",
    "RETURNING", "ON CONFLICT", "WITH RECURSIVE", "UPSERT",
    "INNER JOIN", "LEFT JOIN", "RIGHT JOIN", "CROSS JOIN",
    "FULL JOIN", "NATURAL JOIN", "TRUNCATE",
}
_SQL_WEAK = {
    "WHERE", "JOIN", "HAVING", "UNION", "DISTINCT", "OFFSET",
    "VALUES", "MERGE", "GRANT", "REVOKE",
}


def _detect_sql(text: str) -> bool:
    matches = _SQL_ALL_RE.findall(text)
    if not matches:
        return False
    distinct = {re.sub(r"\s+", " ", m.upper()) for m in matches}
    if distinct & _SQL_STRONG:
        return True
    if len(distinct) >= 3 and distinct & _SQL_WEAK:
        return True
    return False


# ─── API keys / secrets ──────────────────────────────────────────────────────

_API_KEY_PATTERNS = [
    re.compile(r"(?<![a-zA-Z0-9-])sk-[a-zA-Z0-9_-]{20,}"),  # OpenAI / Anthropic
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),  # AWS
    re.compile(r"\bgh[posrt]_[a-zA-Z0-9]{36,}\b"),  # GitHub
    re.compile(r"\bgithub_pat_[a-zA-Z0-9_]{36,}\b"),
    re.compile(r"\b[sr]k_(?:live|test)_[a-zA-Z0-9]{24,}\b"),  # Stripe
    re.compile(r"\bxox[bpra]-[a-zA-Z0-9-]{20,}\b"),  # Slack
    re.compile(r"\bSG\.[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}\b"),  # SendGrid
    re.compile(r"\bglpat-[a-zA-Z0-9_-]{20,}\b"),  # GitLab
    re.compile(r"\bnpm_[a-zA-Z0-9]{36,}\b"),
    re.compile(r"\bAIza[a-zA-Z0-9_-]{35}\b"),  # Google API
]


def _detect_api_key(text: str) -> bool:
    return any(p.search(text) for p in _API_KEY_PATTERNS)


# ─── Reasoning chains ────────────────────────────────────────────────────────

_REASON_STRONG_RE = re.compile(
    r"^[ \t]*(?:Reasoning|Analysis|Conclusion|Proof|Derivation|Chain of Thought|"
    r"Step[- ]by[- ]step)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_REASON_INFERENCE_RE = re.compile(
    r"\b(?:it follows that|we can (?:conclude|deduce|infer)|"
    r"this (?:implies|proves) that|QED)\b|∴",
    re.IGNORECASE,
)
_REASON_WEAK_RE = re.compile(
    r"\b(?:therefore|hence|thus|consequently|accordingly|this means that|"
    r"as a result|because of this|which (?:implies|means|shows)|"
    r"given that|assuming that|since we know)\b",
    re.IGNORECASE,
)
_NUMBERED_STEP_RE = re.compile(r"(?:^|\n)\s*(?:Step\s+\d+[:.)]|\d+[.)]\s)", re.IGNORECASE)


def _detect_reasoning(text: str) -> bool:
    if _REASON_STRONG_RE.search(text):
        return True
    if _REASON_INFERENCE_RE.search(text):
        return True
    weak = _REASON_WEAK_RE.findall(text)
    distinct_weak = len({re.sub(r"\s+", " ", w.lower()) for w in weak})
    steps = len(_NUMBERED_STEP_RE.findall(text))
    if steps >= 3 and distinct_weak >= 1:
        return True
    if distinct_weak >= 3:
        return True
    return False


# ─── Guardrail signals (tracebacks, HTTP errors, failures) ───────────────────

_ERROR_TRACEBACK_RE = re.compile(
    r"^(?:Traceback \(most recent call last\)|Exception in thread\b|\s+at \w[\w$.]*\()",
    re.MULTILINE,
)
_HTTP_ERROR_RE = re.compile(
    r"\bResponse\s+status\s+code\s+is\s+[45]\d\d\b|"
    r"\bHTTP/\d\.\d\s+[45]\d\d\b|"
    r"\bstatus(?:\s+code)?[:\s]+[45]\d\d\b",
    re.IGNORECASE,
)
_FAILURE_RE = re.compile(
    r"\b(?:Execution\s+failed|Authentication\s+(?:failed|error)|"
    r"Authorization\s+(?:failed|denied)|Connection\s+(?:refused|timeout|timed\s+out)|"
    r"Permission\s+denied)\b",
    re.IGNORECASE,
)


def _detect_guardrail(text: str) -> List[str]:
    reasons: List[str] = []
    if _ERROR_TRACEBACK_RE.search(text):
        reasons.append("error_traceback")
    if _HTTP_ERROR_RE.search(text):
        reasons.append("http_error")
    if _FAILURE_RE.search(text):
        reasons.append("failure_signature")
    return reasons


# ─── Content-type patterns (soft T0 markers) ─────────────────────────────────

_CONTENT_TYPE_PATTERNS = [
    (re.compile(r"https?://[^\s]+"), "url"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE), "email"),
    (re.compile(r"\b(?:v\d+\.\d+(?:\.\d+)?|version\s+\d+)\b", re.IGNORECASE), "version_number"),
    (re.compile(r"[a-f0-9]{40,64}", re.IGNORECASE), "hash_or_sha"),
    (re.compile(r"(?:/[\w.-]+){2,}"), "file_path"),
    (
        re.compile(
            r"\b\d+\.?\d*\s*(?:km|kg|°C|°F|Hz|MHz|GHz|ms|µs|ns|MB|GB|TB)\b",
            re.IGNORECASE,
        ),
        "numeric_with_units",
    ),
]


def _detect_content_types(text: str) -> List[str]:
    reasons: List[str] = []
    if _detect_sql(text):
        reasons.append("sql_content")
    if _detect_api_key(text):
        reasons.append("api_key")
    if _detect_reasoning(text):
        reasons.append("reasoning_chain")
    for pat, label in _CONTENT_TYPE_PATTERNS:
        if pat.search(text):
            reasons.append(label)
    return reasons


# ─── Main entry point ────────────────────────────────────────────────────────

# Reasons that mark content as *hard* T0 — structurally verbatim-worthy.
# Soft T0 (url, email, version_number, file_path, hash_or_sha, numeric_with_units)
# signal "contains reference entities" but the surrounding prose is still
# summarizable. We keep this distinction so a future budget packer can prefer
# hard T0 over soft T0 when space is tight.
HARD_T0_REASONS = frozenset({
    "code_fence",
    "indented_code",
    "json_structure",
    "yaml_structure",
    "high_special_char_ratio",
    "high_line_length_variance",
    "api_key",
    "sql_content",
    "reasoning_chain",
    "error_traceback",
    "http_error",
    "failure_signature",
})


def classify_block(text: str) -> ClassifyResult:
    """Classify a single block of text as T0 / T2 / T3.

    Empty or whitespace-only text → T2 with zero reasons.
    """
    if not text or not text.strip():
        return ClassifyResult(decision="T2", confidence=0.0, reasons=[])

    reasons = (
        _detect_structural(text)
        + _detect_content_types(text)
        + _detect_guardrail(text)
    )

    if reasons:
        confidence = min(0.95, 0.70 + 0.05 * len(reasons))
        return ClassifyResult(decision="T0", confidence=confidence, reasons=reasons)

    words = len(text.split())
    decision = "T2" if words < 20 else "T3"
    return ClassifyResult(decision=decision, confidence=0.65, reasons=[])


def is_hard_t0(result: ClassifyResult) -> bool:
    """True if classification relies on a hard-T0 reason (structural / semantic)."""
    return any(r in HARD_T0_REASONS for r in result.reasons)
