"""
fact_checker.py — Verify text against known facts in the palace.

Checks AI responses, diary entries, and new content against the entity
registry and knowledge graph for three classes of issue:

  * similar_name          — text mentions a name that's one/two edits
                            away from *another* registered name, raising
                            the possibility of a typo or mix-up.
  * relationship_mismatch — text asserts a role between two entities
                            (e.g. "Bob is Alice's brother") while the KG
                            records a *different* current role for the
                            same subject/object pair.
  * stale_fact            — text asserts a fact that the KG marks closed
                            (``valid_to`` in the past).

Purely offline. Inputs: entity_registry JSON + KG SQLite. No network.

Usage:
    from mempalace.fact_checker import check_text
    issues = check_text("Bob is Alice's brother", palace_path)

    # CLI
    python -m mempalace.fact_checker "Bob is Alice's brother" \\
        --palace ~/.mempalace/palace
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

# Share miner's mtime-cached registry loader so we don't double-read
# ~/.mempalace/known_entities.json on every check_text call.
from .miner import _load_known_entities_raw

logger = logging.getLogger("mempalace_mcp")


# Narrow detection patterns — parse "X is Y's Z" and "X's Z is Y".
# Names are captured greedily as word sequences (letters + optional
# capitalized follow-ons) so simple multi-token names still work.
# Relationship words are constrained to sane lengths to avoid matching
# arbitrary filler.
_RELATIONSHIP_PATTERNS = [
    # "Bob is Alice's brother"      → subject=Bob, possessor=Alice, role=brother
    re.compile(r"\b([A-Z][\w-]+)\s+is\s+([A-Z][\w-]+)'s\s+([a-z]{3,20})\b"),
    # "Alice's brother is Bob"      → possessor=Alice, role=brother, subject=Bob
    re.compile(r"\b([A-Z][\w-]+)'s\s+([a-z]{3,20})\s+is\s+([A-Z][\w-]+)\b"),
]


def check_text(text: str, palace_path: str = None, config=None) -> list:
    """Return a list of issues detected in ``text``.

    Empty list means "no contradictions found" — absence of evidence, not
    evidence of absence. The detector is deliberately conservative:
    every issue is anchored to a specific KG fact or registry entry.
    """
    if config is None:
        from .config import MempalaceConfig

        config = MempalaceConfig()
    if palace_path is None:
        palace_path = config.palace_path

    if not text:
        return []

    issues: list = []
    entity_names_raw = _load_known_entities_raw()

    issues.extend(_check_entity_confusion(text, entity_names_raw))
    issues.extend(_check_kg_contradictions(text, palace_path))

    return issues


# ── entity-name confusion ────────────────────────────────────────────


def _flatten_names(entity_names_raw: dict) -> set:
    """Flatten a ``{category: [names]}`` or ``{category: {name: meta}}``
    registry into a set of names."""
    flat: set = set()
    for cat in entity_names_raw.values():
        if isinstance(cat, list):
            flat.update(str(n) for n in cat if n)
        elif isinstance(cat, dict):
            flat.update(str(k) for k in cat.keys() if k)
    return flat


def _check_entity_confusion(text: str, entity_names_raw: dict) -> list:
    """Flag names mentioned in the text that are edit-distance ≤ 2 from
    a *different* registered name — a common typo / mix-up pattern.

    Performance note: the original O(n²) pairwise scan over the full
    registry is gone. We first identify which names actually appear in
    the text, then only compute edit distance between *mentioned* names
    and the rest of the registry. This makes the cost O(m × n) where m
    is the handful of names in the text, not the full registry.
    """
    all_names = _flatten_names(entity_names_raw)
    if not all_names:
        return []

    # Which names from the registry actually appear in the text?
    mentioned: list = []
    for name in all_names:
        if re.search(r"\b" + re.escape(name) + r"\b", text, re.IGNORECASE):
            mentioned.append(name)
    if not mentioned:
        return []

    issues: list = []
    seen_pairs: set = set()
    for name_a in mentioned:
        a_lower = name_a.lower()
        for name_b in all_names:
            if name_b == name_a:
                continue
            # Dedupe by unordered pair so we don't double-report.
            pair_key = tuple(sorted((name_a.lower(), name_b.lower())))
            if pair_key in seen_pairs:
                continue
            # Only flag when name_b is a *different* registry entry that
            # was NOT mentioned — otherwise both names in the text is
            # just the user writing about two people.
            if name_b in mentioned:
                seen_pairs.add(pair_key)
                continue
            distance = _edit_distance(a_lower, name_b.lower())
            if 0 < distance <= 2:
                issues.append(
                    {
                        "type": "similar_name",
                        "detail": (
                            f"'{name_a}' mentioned — did you mean "
                            f"'{name_b}'? (edit distance {distance})"
                        ),
                        "names": [name_a, name_b],
                        "distance": distance,
                    }
                )
                seen_pairs.add(pair_key)
    return issues


# ── KG contradictions ────────────────────────────────────────────────


def _extract_claims(text: str) -> list:
    """Yield structured (subject, predicate, object) claims from ``text``.

    The two supported surface forms are "X is Y's Z" and "X's Z is Y",
    both of which resolve to the triple ``(X, Z, Y)`` — ``X`` has role
    ``Z`` with respect to ``Y``. Matches are case-preserving for the
    entity names (KG lookup is case-insensitive on normalized IDs).
    """
    claims: list = []
    for pat in _RELATIONSHIP_PATTERNS:
        for match in pat.finditer(text):
            groups = match.groups()
            if pat is _RELATIONSHIP_PATTERNS[0]:
                subject, possessor, role = groups[0], groups[1], groups[2]
            else:
                possessor, role, subject = groups[0], groups[1], groups[2]
            claims.append(
                {
                    "subject": subject,
                    "predicate": role.lower(),
                    "object": possessor,
                    "span": match.group(0),
                }
            )
    return claims


def _check_kg_contradictions(text: str, palace_path: str) -> list:
    """Compare each claim in ``text`` against the KG.

    For every claim ``(subject, predicate, object)`` parsed from the
    text, look up the subject's current KG triples:

      * ``relationship_mismatch`` fires when the KG records a fact about
        the same ``(subject, object)`` pair but with a *different*
        predicate — e.g. text says "brother" but KG says "husband".
      * ``stale_fact`` fires when the KG has the exact ``(subject,
        predicate, object)`` triple but its ``valid_to`` is in the past,
        meaning the claim is no longer current.
    """
    claims = _extract_claims(text)
    if not claims:
        return []

    try:
        from .knowledge_graph import KnowledgeGraph

        # KG lives alongside the palace collection; mcp_server uses the
        # same convention (see _kg init). Pass ``db_path`` — the previous
        # code passed a nonexistent ``palace_path`` kwarg which raised
        # TypeError, silently swallowed by the outer except and rendered
        # the entire KG-check path dead.
        kg = KnowledgeGraph(db_path=os.path.join(palace_path, "knowledge_graph.sqlite3"))
    except Exception:
        # KG unavailable (brand-new palace, corrupted DB, etc.) — skip.
        return []

    issues: list = []
    for claim in claims:
        subject = claim["subject"]
        claim_pred = claim["predicate"]
        claim_obj = claim["object"]
        try:
            facts = kg.query_entity(subject, direction="outgoing")
        except Exception:
            logger.debug("KG lookup failed for subject %r", subject, exc_info=True)
            continue
        if not facts:
            continue

        current_facts = [f for f in facts if f.get("current")]

        # Mismatch: KG fact about same (subject, object) pair but different predicate.
        for fact in current_facts:
            if not _objects_match(fact.get("object"), claim_obj):
                continue
            kg_pred = (fact.get("predicate") or "").lower()
            if kg_pred and kg_pred != claim_pred:
                issues.append(
                    {
                        "type": "relationship_mismatch",
                        "detail": (
                            f"Text says '{claim['span']}' but KG records "
                            f"{subject} {kg_pred} {fact.get('object')}"
                        ),
                        "entity": subject,
                        "claim": {
                            "predicate": claim_pred,
                            "object": claim_obj,
                        },
                        "kg_fact": {
                            "predicate": kg_pred,
                            "object": fact.get("object"),
                        },
                    }
                )

        # Stale fact: exact match on (subject, predicate, object) but KG
        # closed the window in the past.
        now_iso = datetime.now(timezone.utc).date().isoformat()
        for fact in facts:
            if fact.get("current"):
                continue
            kg_pred = (fact.get("predicate") or "").lower()
            if kg_pred != claim_pred:
                continue
            if not _objects_match(fact.get("object"), claim_obj):
                continue
            valid_to = fact.get("valid_to")
            if valid_to and str(valid_to) < now_iso:
                issues.append(
                    {
                        "type": "stale_fact",
                        "detail": (
                            f"Text says '{claim['span']}' but KG marks "
                            f"this fact closed on {valid_to}"
                        ),
                        "entity": subject,
                        "valid_to": valid_to,
                    }
                )

    return issues


def _objects_match(kg_obj, claim_obj: str) -> bool:
    if kg_obj is None or not claim_obj:
        return False
    return str(kg_obj).strip().lower() == claim_obj.strip().lower()


# ── Levenshtein helper (tight iterative version) ─────────────────────


def _edit_distance(s1: str, s2: str) -> int:
    """Levenshtein distance. O(len(s1) * len(s2)) time, O(len(s2)) space."""
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if not s2:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(
                min(
                    prev[j + 1] + 1,
                    curr[j] + 1,
                    prev[j] + (0 if c1 == c2 else 1),
                )
            )
        prev = curr
    return prev[-1]


def _reconfigure_stdio_utf8_on_windows():
    """Decode --stdin payload as UTF-8 on Windows.

    Thin wrapper around the shared helper in ``mempalace._stdio``. Mirrors
    the primary CLI policy: stdout/stderr use ``replace`` because
    extracted fact text can include surrogate halves round-tripped from
    filenames -- ``strict`` would raise UnicodeEncodeError mid-print.
    stdin keeps the default ``surrogateescape``.
    """
    from ._stdio import reconfigure_stdio_utf8_on_windows

    reconfigure_stdio_utf8_on_windows(stdout_errors="replace", stderr_errors="replace")


if __name__ == "__main__":
    import argparse
    import json
    import sys

    _reconfigure_stdio_utf8_on_windows()

    parser = argparse.ArgumentParser(
        description="Check text against known facts in the MemPalace palace.",
        epilog="Exits 0 when no issues found, 1 when one or more issues detected.",
    )
    parser.add_argument("text", nargs="?", help="Text to check (or use --stdin).")
    parser.add_argument(
        "--palace",
        default=os.path.expanduser("~/.mempalace/palace"),
        help="Path to the palace directory.",
    )
    parser.add_argument("--stdin", action="store_true", help="Read text from stdin.")
    args = parser.parse_args()

    if args.stdin:
        text_in = sys.stdin.read()
    elif args.text:
        text_in = args.text
    else:
        parser.error("Provide text as argument or use --stdin.")

    found = check_text(text_in, palace_path=args.palace)
    if found:
        print(json.dumps(found, indent=2))
        sys.exit(1)
    print("No contradictions found.")
