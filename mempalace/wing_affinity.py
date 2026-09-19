"""wing_affinity.py — Score which wings are relevant to a query.

Given a search query, computes a relevance score for each wing in the
palace using existing structural signals. ``search_memories`` uses these
scores for additive cross-wing expansion: when an unfiltered baseline
comes back thin, the best-scoring wings get their own scoped queries and
their hits merge into the baseline pool — the baseline itself is never
filtered or reduced.

The scoring is query-time only — no writes to the KG, no sidecar tables,
no synthetic facts.

Structural signals used:

1. **Passive same-room connections** from collection metadata via
   ``find_tunnels()``/``build_graph()`` — a room name that appears in
   multiple wings marks those wings as covering the same topic. These
   are *derived* connections, not the explicit ``tunnels.json`` links.
2. **Hallway entity overlap** from ``hallways.json`` (palace-scoped) —
   query tokens matching entity names boost the wing holding them.
3. **Room name token overlap** within the same graph.

Target isolation: pass ``col`` (the already-opened search collection) so
graph signals derive from the exact search target; ``config`` must be
bound to the target's ``palace_path``/``collection_name`` so the palace
files read are the right ones. ``build_graph``'s warm cache is keyed on
that identity, and an explicit ``col`` bypasses it entirely.
"""

from __future__ import annotations

import logging
import re

from .hallways import list_hallways
from .palace_graph import find_tunnels, build_graph

logger = logging.getLogger("mempalace_mcp")

# How many wings to expand into when no explicit wing is given.
# Too few → miss context. Too many → back to palace-wide noise.
DEFAULT_MAX_WINGS = 3

# Minimum score to include a wing in the expansion. Below this, the wing
# has no structural connection to the query and would only add noise.
DEFAULT_MIN_SCORE = 0.05

_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)


def _tokenize_query(query: str) -> set[str]:
    """Lowercase + alphanumeric tokens of length >= 2.

    Splits on spaces, underscores, hyphens, and other non-alphanumeric
    delimiters so queries like "dual detector docs" match room slugs like
    "dual_detector_docs".
    """
    if not query:
        return set()
    return set(_TOKEN_RE.findall(query.lower()))


def score_wings(
    query: str,
    col=None,
    config=None,
    max_wings: int = DEFAULT_MAX_WINGS,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[tuple[str, float]]:
    """Return [(wing, score), ...] ranked by relevance to the query.

    Scoring signals:
    1. Passive same-room connections — if query tokens match a room name
       that collection metadata places in multiple wings, those wings get
       a boost. Strongest signal: the room name IS the topic, and the
       connection says it spans wings.
    2. Hallway entity overlap — if query tokens match entity names in
       hallway records, the wing containing those hallways gets a boost.
    3. Room name overlap (within-wing) — if a wing has rooms whose names
       match query tokens, that wing is likely relevant even without
       cross-wing connections.

    ``col`` should be the already-opened search collection so the graph is
    built from the actual search target; ``config`` must be bound to the
    target's palace_path/collection_name. Returns at most ``max_wings``
    wings with score >= ``min_score``. If no wings score above the
    threshold, returns an empty list — the caller should skip expansion.
    """
    tokens = _tokenize_query(query)
    if not tokens:
        return []

    scores: dict[str, float] = {}

    # ── Signal 1: Passive same-room connection overlap ──────────────
    # A room like "dual_detector_docs" present in both orkid and
    # past-performance means a query about "dual detector" is relevant
    # to both wings.
    try:
        tunnels = find_tunnels(col=col, config=config)
        for tunnel in tunnels:
            room_name = tunnel.get("room", "")
            room_tokens = (
                set(_TOKEN_RE.findall(room_name.lower().replace("_", " "))) if room_name else set()
            )
            if not room_tokens:
                continue
            overlap = tokens & room_tokens
            if overlap:
                # Score proportional to token overlap fraction,
                # weighted by connection count (more drawers = stronger signal).
                overlap_frac = len(overlap) / len(room_tokens)
                count_weight = min(tunnel.get("count", 1) / 10.0, 1.0)
                signal = overlap_frac * count_weight
                for wing in tunnel.get("wings", []):
                    scores[wing] = scores.get(wing, 0.0) + signal
    except Exception:
        logger.debug("wing_affinity: tunnel scoring failed", exc_info=True)

    # ── Signal 2: Hallway entity overlap ────────────────────────────
    # Hallways connect entity pairs within a wing. If the query mentions
    # an entity that has hallways in a wing, that wing is relevant.
    try:
        hallways = list_hallways(config=config)
        for hallway in hallways:
            wing = hallway.get("wing", "")
            if not wing:
                continue
            entity_a = (hallway.get("entity_a") or "").lower()
            entity_b = (hallway.get("entity_b") or "").lower()
            # Check if any query token appears in entity names
            for entity in (entity_a, entity_b):
                entity_tokens = set(_TOKEN_RE.findall(entity.replace("_", " ")))
                if entity_tokens & tokens:
                    count = hallway.get("co_occurrence_count", 1)
                    signal = min(count / 20.0, 0.5)  # cap hallway signal
                    scores[wing] = scores.get(wing, 0.0) + signal
                    break
    except Exception:
        logger.debug("wing_affinity: hallway scoring failed", exc_info=True)

    # ── Signal 3: Room name token overlap (within-wing) ─────────────
    # Even without connections, if a wing has rooms whose names match
    # query tokens, that wing is likely relevant.
    try:
        nodes, _edges = build_graph(col=col, config=config)
        for room_name, data in nodes.items():
            room_tokens = set(_TOKEN_RE.findall(room_name.lower().replace("_", " ")))
            if not room_tokens:
                continue
            overlap = tokens & room_tokens
            if overlap:
                overlap_frac = len(overlap) / len(room_tokens)
                count_weight = min(data.get("count", 1) / 10.0, 1.0)
                signal = overlap_frac * count_weight * 0.5  # discount vs connections
                for wing in data.get("wings", []):
                    scores[wing] = scores.get(wing, 0.0) + signal
    except Exception:
        logger.debug("wing_affinity: room name scoring failed", exc_info=True)

    # Filter by min_score, sort by score descending, take top max_wings
    ranked = [(w, s) for w, s in scores.items() if s >= min_score]
    ranked.sort(key=lambda x: -x[1])
    return ranked[:max_wings]


def expand_wings(
    query: str,
    col=None,
    config=None,
    max_wings: int = DEFAULT_MAX_WINGS,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[str]:
    """Return a list of wing names to expand into, ranked by relevance.

    Convenience wrapper around ``score_wings`` that returns just the
    wing names. If no wings score above threshold, returns empty list
    (caller skips expansion).
    """
    scored = score_wings(query, col=col, config=config, max_wings=max_wings, min_score=min_score)
    return [wing for wing, _ in scored]
