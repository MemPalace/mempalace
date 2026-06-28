"""
kg_extractor.py — Auto-populate the Knowledge Graph from palace drawers.
=========================================================================

Reads verbatim drawer content from ChromaDB and extracts entity
relationships using rule-based pattern matching. Populates the KG
automatically so users don't have to manually call kg_add for every fact.

Zero API calls. Zero new dependencies. Pure regex + KG writes.

Extraction pipeline:
  1. Read drawers from ChromaDB (all or filtered by wing/room)
  2. Extract entity names (capitalized words appearing 2+ times)
  3. Match relationship patterns against drawer text
  4. Deduplicate against existing KG triples (idempotent)
  5. Write new triples with source_file provenance

Supported relationship patterns:
  - Works at / employed by:  "Alice works at Acme Corp"
  - Role / position:         "Bob is the lead engineer"
  - Parent / child:          "Alice's daughter Riley" or "Riley is Alice's daughter"
  - Partnership:             "Alice and Bob are married"
  - Uses / works with:       "We use PostgreSQL for the database"
  - Decided / chose:         "We decided to switch to GraphQL"
  - Created / built:         "Alice created the auth module"
  - Loves / enjoys:          "Max loves chess"

Usage:
    from mempalace.kg_extractor import extract_kg

    stats = extract_kg(palace_path="/path/to/palace")
    # => {"drawers_scanned": 150, "triples_added": 42, "entities_found": 12, ...}

CLI:
    mempalace extract-kg
    mempalace extract-kg --wing my_project
    mempalace extract-kg --dry-run
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import chromadb

from mempalace.knowledge_graph import KnowledgeGraph

logger = logging.getLogger("mempalace")


# ── Relationship patterns ────────────────────────────────────────────
# Each pattern returns (subject, predicate, object) tuples.

_NAME = r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)"
_NAME_LOWER = r"([A-Za-z][a-z]+(?:\s[A-Za-z][a-z]+)?)"

# "X works at Y" / "X is employed by Y" / "X joined Y"
_EMPLOYMENT_PATTERNS = [
    re.compile(
        rf"\b{_NAME}\s+(?:works?|worked)\s+(?:at|for)\s+{_NAME}", re.IGNORECASE
    ),
    re.compile(
        rf"\b{_NAME}\s+(?:is|was)\s+employed\s+(?:at|by)\s+{_NAME}", re.IGNORECASE
    ),
    re.compile(
        rf"\b{_NAME}\s+joined\s+{_NAME}", re.IGNORECASE
    ),
]

# "X is a/the Y" (role extraction)
_ROLE_PATTERNS = [
    re.compile(
        rf"\b{_NAME}\s+is\s+(?:a|the|an|our)\s+([\w\s]{{2,30}}?)(?:\s+(?:at|of|for|in|on)\b|[.,;]|$)",
        re.IGNORECASE,
    ),
]

# Role words that confirm it's actually a role
_ROLE_WORDS = {
    "engineer", "developer", "designer", "manager", "lead", "director",
    "architect", "scientist", "analyst", "admin", "founder", "ceo", "cto",
    "intern", "consultant", "specialist", "coordinator", "owner", "chief",
    "head", "senior", "junior", "principal", "staff", "vp",
}

# "X's daughter/son/partner Y" or "Y is X's daughter/son"
# NOTE: These do NOT use IGNORECASE because _NAME relies on case to find proper nouns.
_REL_WORDS = r"(?:daughter|son|child|mother|father|parent|wife|husband|partner|brother|sister|sibling|dog|cat|pet)"
_FAMILY_PATTERNS = [
    # "Alice's daughter Riley"
    re.compile(
        rf"\b([A-Z][a-z]+)'s\s+{_REL_WORDS}\s+([A-Z][a-z]+)",
    ),
    # "Riley is Alice's daughter"
    re.compile(
        rf"\b([A-Z][a-z]+)\s+is\s+([A-Z][a-z]+)'s\s+{_REL_WORDS}",
    ),
]

# "We use X for Y" / "switched to X" / "decided to use X"
_TOOL_PATTERNS = [
    re.compile(
        r"\b(?:we|they|team)\s+(?:use|uses|used|using)\s+([A-Z][\w.+-]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:switched|migrated|moved)\s+to\s+([A-Z][\w.+-]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:decided|chose|choosing)\s+(?:to\s+use\s+)?([A-Z][\w.+-]+)",
        re.IGNORECASE,
    ),
]

# "X created/built/designed Y"
_CREATION_PATTERNS = [
    re.compile(
        rf"\b{_NAME}\s+(?:created|built|designed|wrote|implemented|developed|shipped)\s+(?:the\s+)?(.+?)(?:[.,;]|$)",
        re.IGNORECASE,
    ),
]

# "X loves/enjoys Y"
_INTEREST_PATTERNS = [
    re.compile(
        rf"\b{_NAME}\s+(?:loves?|enjoys?|likes?|is\s+(?:into|passionate\s+about))\s+(.+?)(?:[.,;]|$)",
        re.IGNORECASE,
    ),
]

# Map family relationship words to KG predicates
_FAMILY_PREDICATES = {
    "daughter": ("parent_of", False),   # Alice parent_of Riley
    "son": ("parent_of", False),
    "child": ("parent_of", False),
    "mother": ("parent_of", True),      # Riley parent_of Alice → reversed
    "father": ("parent_of", True),
    "parent": ("parent_of", True),
    "wife": ("married_to", False),
    "husband": ("married_to", False),
    "partner": ("married_to", False),
    "brother": ("sibling_of", False),
    "sister": ("sibling_of", False),
    "sibling": ("sibling_of", False),
    "dog": ("is_pet_of", True),
    "cat": ("is_pet_of", True),
    "pet": ("is_pet_of", True),
}

# Common words that should NOT be treated as entity names
_SKIP_NAMES = {
    "the", "this", "that", "here", "there", "been", "just", "also",
    "very", "much", "many", "some", "most", "each", "every", "both",
    "even", "still", "already", "always", "never", "often", "only",
    "well", "really", "actually", "basically", "certainly", "probably",
    "definitely", "currently", "recently", "finally", "however", "instead",
    "because", "since", "while", "before", "after", "above", "below",
    "now", "then", "when", "where", "why", "how", "what", "which",
    "who", "all", "new", "old", "big", "next", "last", "first",
    "second", "third", "good", "bad", "best", "same", "different",
    "important", "possible", "available", "specific", "general",
    "true", "false", "null", "none", "yes",
}


# ── Extraction result ────────────────────────────────────────────────


@dataclass
class ExtractionResult:
    """Result of KG extraction from palace drawers."""

    drawers_scanned: int = 0
    entities_found: int = 0
    triples_added: int = 0
    triples_skipped: int = 0  # already existed
    patterns_matched: int = 0
    errors: List[str] = field(default_factory=list)
    details: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize for MCP/CLI output."""
        return {
            "drawers_scanned": self.drawers_scanned,
            "entities_found": self.entities_found,
            "triples_added": self.triples_added,
            "triples_skipped": self.triples_skipped,
            "patterns_matched": self.patterns_matched,
            "errors": self.errors[:10],  # cap error list
            "sample_triples": self.details[:20],  # cap detail list
        }


# ── Core extraction ──────────────────────────────────────────────────


def _is_valid_name(name: str) -> bool:
    """Check if a string looks like a real entity name."""
    if not name or len(name) < 2:
        return False
    if name.lower() in _SKIP_NAMES:
        return False
    # Must start with uppercase
    if not name[0].isupper():
        return False
    return True


def _clean_object(text: str, max_len: int = 60) -> str:
    """Clean and truncate an extracted object string."""
    text = text.strip().rstrip(".,;:!?")
    # Remove leading articles
    text = re.sub(r"^(?:the|a|an)\s+", "", text, flags=re.IGNORECASE)
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0]
    return text.strip()


def _has_role_word(text: str) -> bool:
    """Check if a text contains a word that signals a role/position."""
    words = set(text.lower().split())
    return bool(words & _ROLE_WORDS)


def extract_from_text(text: str, source_file: str = "") -> List[dict]:
    """Extract relationship triples from a single text block.

    Returns a list of dicts: {"subject": ..., "predicate": ..., "object": ..., "source": ...}
    """
    triples = []

    # Employment
    for pattern in _EMPLOYMENT_PATTERNS:
        for match in pattern.finditer(text):
            person, org = match.group(1), match.group(2)
            if _is_valid_name(person) and _is_valid_name(org):
                triples.append({
                    "subject": person,
                    "predicate": "works_at",
                    "object": org,
                    "source": source_file,
                    "subject_type": "person",
                    "object_type": "company",
                })

    # Roles — only if the extracted role contains a role-like word
    for pattern in _ROLE_PATTERNS:
        for match in pattern.finditer(text):
            person = match.group(1)
            role = _clean_object(match.group(2))
            if _is_valid_name(person) and role and _has_role_word(role):
                triples.append({
                    "subject": person,
                    "predicate": "has_role",
                    "object": role,
                    "source": source_file,
                    "subject_type": "person",
                    "object_type": "role",
                })

    # Family relationships
    # Extract the relationship word separately since it's non-capturing in the regex
    _rel_word_re = re.compile(
        r"'s\s+(daughter|son|child|mother|father|parent|wife|husband|partner|brother|sister|sibling|dog|cat|pet)\b",
        re.IGNORECASE,
    )
    for i, pattern in enumerate(_FAMILY_PATTERNS):
        for match in pattern.finditer(text):
            person_1, person_2 = match.group(1), match.group(2)
            # Extract the relationship word from the matched span
            span_text = match.group(0)
            rel_match = _rel_word_re.search(span_text)
            if not rel_match:
                continue
            rel_word = rel_match.group(1).lower()

            if rel_word not in _FAMILY_PREDICATES:
                continue
            if not (_is_valid_name(person_1) and _is_valid_name(person_2)):
                continue

            predicate, reverse = _FAMILY_PREDICATES[rel_word]

            if i == 0:
                # Pattern 1: "Alice's daughter Riley" → person_1=Alice, person_2=Riley
                subject, obj = (person_2, person_1) if reverse else (person_1, person_2)
            else:
                # Pattern 2: "Riley is Alice's daughter" → person_1=Riley, person_2=Alice
                # Alice is the parent, Riley is the child
                subject, obj = (person_1, person_2) if reverse else (person_2, person_1)

            triples.append({
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "source": source_file,
            })

    # Tool/technology usage
    for pattern in _TOOL_PATTERNS:
        for match in pattern.finditer(text):
            tool = match.group(1)
            if _is_valid_name(tool) and len(tool) >= 2:
                triples.append({
                    "subject": "team",
                    "predicate": "uses",
                    "object": tool,
                    "source": source_file,
                    "subject_type": "team",
                    "object_type": "tool",
                })

    # Creation
    for pattern in _CREATION_PATTERNS:
        for match in pattern.finditer(text):
            person = match.group(1)
            thing = _clean_object(match.group(2))
            if _is_valid_name(person) and thing and len(thing) >= 3:
                triples.append({
                    "subject": person,
                    "predicate": "created",
                    "object": thing,
                    "source": source_file,
                    "subject_type": "person",
                })

    # Interests
    for pattern in _INTEREST_PATTERNS:
        for match in pattern.finditer(text):
            person = match.group(1)
            interest = _clean_object(match.group(2))
            if _is_valid_name(person) and interest and len(interest) >= 2:
                triples.append({
                    "subject": person,
                    "predicate": "loves",
                    "object": interest,
                    "source": source_file,
                    "subject_type": "person",
                })

    return triples


def _dedupe_triples(triples: List[dict]) -> List[dict]:
    """Remove duplicate triples (same subject+predicate+object).

    TODO: Add fuzzy dedup for entity aliases (e.g. "PostgreSQL" vs "Postgres").
    Suggested approach: hard dedup at cosine >= 0.86, soft dedup at >= 0.55
    flagged for review. Requires embedding similarity — separate PR.
    (Credit: @web3guru888 for the tiered threshold idea)
    """
    seen = set()
    unique = []
    for t in triples:
        key = (t["subject"].lower(), t["predicate"], t["object"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique


# ── Main extraction function ─────────────────────────────────────────


def extract_kg(
    palace_path: str,
    kg: Optional[KnowledgeGraph] = None,
    wing: Optional[str] = None,
    room: Optional[str] = None,
    dry_run: bool = False,
) -> ExtractionResult:
    """Extract relationships from palace drawers and populate the knowledge graph.

    Args:
        palace_path: Path to the palace ChromaDB directory.
        kg: KnowledgeGraph instance (creates default if None).
        wing: Filter drawers by wing (optional).
        room: Filter drawers by room (optional).
        dry_run: If True, extract but don't write to KG.

    Returns:
        ExtractionResult with stats and sample triples.
    """
    result = ExtractionResult()

    # Connect to palace
    try:
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_collection("mempalace_drawers")
    except Exception as e:
        result.errors.append(f"No palace found at {palace_path}: {e}")
        return result

    if kg is None:
        kg = KnowledgeGraph()

    # Build where filter
    where = None
    if wing and room:
        where = {"$and": [{"wing": wing}, {"room": room}]}
    elif wing:
        where = {"wing": wing}
    elif room:
        where = {"room": room}

    # Read drawers in batches
    all_triples = []
    batch_size = 500
    offset = 0

    while True:
        try:
            kwargs = {
                "include": ["documents", "metadatas"],
                "limit": batch_size,
                "offset": offset,
            }
            if where:
                kwargs["where"] = where
            batch = col.get(**kwargs)
        except Exception as e:
            if not all_triples and offset == 0:
                result.errors.append(f"Error reading drawers: {e}")
            break

        docs = batch.get("documents", [])
        metas = batch.get("metadatas", [])
        if not docs:
            break

        for doc, meta in zip(docs, metas):
            result.drawers_scanned += 1
            source = meta.get("source_file", "")
            triples = extract_from_text(doc, source_file=source)
            all_triples.extend(triples)

        offset += len(docs)

    # Deduplicate
    all_triples = _dedupe_triples(all_triples)
    result.patterns_matched = len(all_triples)

    # Track unique entities
    entities = set()
    for t in all_triples:
        entities.add(t["subject"].lower())
        entities.add(t["object"].lower())
    result.entities_found = len(entities)

    # Write to KG
    # Pre-fetch existing triple keys once so we can detect duplicates without
    # relying on stats() comparisons (which can be unreliable under concurrent
    # writes per @web3guru888's review on PR #434).
    existing_keys = set()
    if not dry_run:
        try:
            conn = kg._conn()
            for row in conn.execute(
                "SELECT subject, predicate, object FROM triples WHERE valid_to IS NULL"
            ).fetchall():
                existing_keys.add((row["subject"], row["predicate"], row["object"]))
        except Exception as e:
            result.errors.append(f"Error pre-fetching existing triples: {e}")

    for t in all_triples:
        if dry_run:
            result.triples_added += 1
            result.details.append({
                "subject": t["subject"],
                "predicate": t["predicate"],
                "object": t["object"],
                "source": t["source"],
            })
        else:
            try:
                # Add entities with inferred types based on pattern
                sub_type = t.get("subject_type", "unknown")
                obj_type = t.get("object_type", "unknown")
                if sub_type != "unknown":
                    kg.add_entity(t["subject"], entity_type=sub_type)
                if obj_type != "unknown":
                    kg.add_entity(t["object"], entity_type=obj_type)

                # Compute the normalized key the same way KG does
                sub_key = t["subject"].lower().replace(" ", "_").replace("'", "")
                obj_key = t["object"].lower().replace(" ", "_").replace("'", "")
                pred_key = t["predicate"].lower().replace(" ", "_")
                triple_key = (sub_key, pred_key, obj_key)
                is_new = triple_key not in existing_keys

                triple_id = kg.add_triple(
                    t["subject"],
                    t["predicate"],
                    t["object"],
                    source_file=t["source"],
                )

                if is_new:
                    result.triples_added += 1
                    existing_keys.add(triple_key)
                    result.details.append({
                        "subject": t["subject"],
                        "predicate": t["predicate"],
                        "object": t["object"],
                        "source": t["source"],
                        "triple_id": triple_id,
                    })
                else:
                    result.triples_skipped += 1
            except Exception as e:
                result.errors.append(f"Error adding triple {t}: {e}")

    return result
