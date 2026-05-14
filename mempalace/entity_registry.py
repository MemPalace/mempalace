#!/usr/bin/env python3
"""
entity_registry.py — Persistent personal entity registry for MemPalace.

Knows the difference between Riley (a person) and ever (an adverb).
Built from three sources, in priority order:
  1. Onboarding — what the user explicitly told us
  2. Learned — what we inferred from session history with high confidence
  3. Researched — what we looked up via Wikipedia for unknown words

Usage:
    from mempalace.entity_registry import EntityRegistry
    registry = EntityRegistry.load()
    result = registry.lookup("Riley", context="I went with Riley today")
    # → {"type": "person", "confidence": 1.0, "source": "onboarding"}
"""

import json
import os
import re
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Common English words that could be confused with names
# These get flagged as AMBIGUOUS and require context disambiguation
# ─────────────────────────────────────────────────────────────────────────────

COMMON_ENGLISH_WORDS = {
    # Words that are also common personal names
    "ever",
    "grace",
    "will",
    "bill",
    "mark",
    "april",
    "may",
    "june",
    "joy",
    "hope",
    "faith",
    "chance",
    "chase",
    "hunter",
    "dash",
    "flash",
    "star",
    "sky",
    "river",
    "brook",
    "lane",
    "art",
    "clay",
    "gil",
    "nat",
    "max",
    "rex",
    "ray",
    "jay",
    "rose",
    "violet",
    "lily",
    "ivy",
    "ash",
    "reed",
    "sage",
    # Words that look like names at start of sentence
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "january",
    "february",
    "march",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}

# Context patterns that indicate a word is being used as a PERSON name
PERSON_CONTEXT_PATTERNS = [
    r"\b{name}\s+said\b",
    r"\b{name}\s+told\b",
    r"\b{name}\s+asked\b",
    r"\b{name}\s+laughed\b",
    r"\b{name}\s+smiled\b",
    r"\b{name}\s+was\b",
    r"\b{name}\s+is\b",
    r"\b{name}\s+called\b",
    r"\b{name}\s+texted\b",
    r"\bwith\s+{name}\b",
    r"\bsaw\s+{name}\b",
    r"\bcalled\s+{name}\b",
    r"\btook\s+{name}\b",
    r"\bpicked\s+up\s+{name}\b",
    r"\bdrop(?:ped)?\s+(?:off\s+)?{name}\b",
    r"\b{name}(?:'s|s')\b",  # Riley's, Max's
    r"\bhey\s+{name}\b",
    r"\bthanks?\s+{name}\b",
    r"^{name}[:\s]",  # dialogue: "Riley: ..."
    r"\bmy\s+(?:son|daughter|kid|child|brother|sister|friend|partner|colleague|coworker)\s+{name}\b",
]

# Context patterns that indicate a word is NOT being used as a name
CONCEPT_CONTEXT_PATTERNS = [
    r"\bhave\s+you\s+{name}\b",  # "have you ever"
    r"\bif\s+you\s+{name}\b",  # "if you ever"
    r"\b{name}\s+since\b",  # "ever since"
    r"\b{name}\s+again\b",  # "ever again"
    r"\bnot\s+{name}\b",  # "not ever"
    r"\b{name}\s+more\b",  # "ever more"
    r"\bwould\s+{name}\b",  # "would ever"
    r"\bcould\s+{name}\b",  # "could ever"
    r"\bwill\s+{name}\b",  # "will ever"
    r"(?:the\s+)?{name}\s+(?:of|in|at|for|to)\b",  # "the grace of", "the mark of"
]


# ─────────────────────────────────────────────────────────────────────────────
# Wikipedia lookup for unknown words
# ─────────────────────────────────────────────────────────────────────────────

# Phrases in Wikipedia summaries that indicate a personal name
NAME_INDICATOR_PHRASES = [
    "given name",
    "personal name",
    "first name",
    "forename",
    "masculine name",
    "feminine name",
    "boy's name",
    "girl's name",
    "male name",
    "female name",
    "irish name",
    "welsh name",
    "scottish name",
    "gaelic name",
    "hebrew name",
    "arabic name",
    "norse name",
    "old english name",
    "is a name",
    "as a name",
    "name meaning",
    "name derived from",
    "legendary irish",
    "legendary welsh",
    "legendary scottish",
]

PLACE_INDICATOR_PHRASES = [
    "city in",
    "town in",
    "village in",
    "municipality",
    "capital of",
    "district of",
    "county",
    "province",
    "region of",
    "island of",
    "mountain in",
    "river in",
]


def _wikipedia_lookup(word: str) -> dict:
    """
    Look up a word via Wikipedia REST API.
    Returns inferred type (person/place/concept/unknown) + confidence + summary.
    Free, no API key, handles disambiguation pages.

    **Privacy warning:** This function makes an outbound HTTPS request to
    en.wikipedia.org, sending the queried word over the network.  It should
    only be called when the caller has explicitly opted in via
    ``allow_network=True`` in :meth:`EntityRegistry.research`.  The default
    behaviour of ``research()`` is local-only (no network calls).
    """
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(word)}"
        req = urllib.request.Request(url, headers={"User-Agent": "MemPalace/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        page_type = data.get("type", "")
        extract = data.get("extract", "").lower()
        title = data.get("title", word)

        # Disambiguation — look at description
        if page_type == "disambiguation":
            desc = data.get("description", "").lower()
            if any(p in desc for p in ["name", "given name"]):
                return {
                    "inferred_type": "person",
                    "confidence": 0.65,
                    "wiki_summary": extract[:200],
                    "wiki_title": title,
                    "note": "disambiguation page with name entries",
                }
            return {
                "inferred_type": "ambiguous",
                "confidence": 0.4,
                "wiki_summary": extract[:200],
                "wiki_title": title,
            }

        # Check for name indicators
        if any(phrase in extract for phrase in NAME_INDICATOR_PHRASES):
            # Higher confidence if the word itself is described as a name
            confidence = (
                0.90
                if any(
                    f"{word.lower()} is a" in extract or f"{word.lower()} (name" in extract
                    for _ in [1]
                )
                else 0.80
            )
            return {
                "inferred_type": "person",
                "confidence": confidence,
                "wiki_summary": extract[:200],
                "wiki_title": title,
            }

        # Check for place indicators
        if any(phrase in extract for phrase in PLACE_INDICATOR_PHRASES):
            return {
                "inferred_type": "place",
                "confidence": 0.80,
                "wiki_summary": extract[:200],
                "wiki_title": title,
            }

        # Found but doesn't match name/place patterns
        return {
            "inferred_type": "concept",
            "confidence": 0.60,
            "wiki_summary": extract[:200],
            "wiki_title": title,
        }

    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Not in Wikipedia — this tells us nothing definitive about
            # the word.  Return "unknown" so the caller can decide.
            return {
                "inferred_type": "unknown",
                "confidence": 0.3,
                "wiki_summary": None,
                "wiki_title": None,
                "note": "not found in Wikipedia",
            }
        return {"inferred_type": "unknown", "confidence": 0.0, "wiki_summary": None}
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return {"inferred_type": "unknown", "confidence": 0.0, "wiki_summary": None}


# ─────────────────────────────────────────────────────────────────────────────
# Entity Registry
# ─────────────────────────────────────────────────────────────────────────────


class EntityRegistry:
    """
    Persistent personal entity registry.

    Stored at ~/.mempalace/entity_registry.json
    Schema:
    {
      "mode": "personal",   # work | personal | combo
      "version": 1,
      "people": {
        "Riley": {
          "source": "onboarding",
          "contexts": ["personal"],
          "aliases": [],
          "relationship": "daughter",
          "confidence": 1.0
        }
      },
      "projects": ["MemPalace", "Acme"],
      "ambiguous_flags": ["riley", "max"],
      "wiki_cache": {
        "Sam": {"inferred_type": "person", "confidence": 0.9, "confirmed": true, ...}
      }
    }
    """

    DEFAULT_PATH = Path.home() / ".mempalace" / "entity_registry.json"

    def __init__(self, data: dict, path: Path):
        self._data = data
        self._path = path

    # ── Load / Save ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, config_dir: Optional[Path] = None) -> "EntityRegistry":
        path = (Path(config_dir) / "entity_registry.json") if config_dir else cls.DEFAULT_PATH
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return cls(data, path)
            except (json.JSONDecodeError, OSError):
                pass
        return cls(cls._empty(), path)

    def save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._path.parent.chmod(0o700)
        except (OSError, NotImplementedError):
            pass
        # Atomic write: serialize to a sibling temp file in the same dir
        # (so os.replace stays on one filesystem), fsync, then rename over
        # the target. A crash mid-write leaves the previous registry intact
        # instead of a half-written file or an empty file from the truncate.
        payload = json.dumps(self._data, indent=2)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            tmp_path.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
        os.replace(tmp_path, self._path)
        # On ext4 (and similar) the rename's durability across power loss
        # requires an additional fsync on the parent directory. Without it,
        # the kernel can ack the rename and a crash reverts to the state
        # where the temp file is present and the target is at the old version.
        try:
            dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Windows and some special filesystems reject directory fds — they
            # have different durability semantics on rename anyway.
            pass

    @staticmethod
    def _empty() -> dict:
        return {
            "version": 1,
            "mode": "personal",
            "people": {},
            "projects": [],
            "ambiguous_flags": [],
            "wiki_cache": {},
        }

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._data.get("mode", "personal")

    @property
    def people(self) -> dict:
        return self._data.get("people", {})

    @property
    def projects(self) -> list:
        return self._data.get("projects", [])

    @property
    def ambiguous_flags(self) -> list:
        return self._data.get("ambiguous_flags", [])

    # ── Seed from onboarding ─────────────────────────────────────────────────

    def seed(self, mode: str, people: list, projects: list, aliases: dict = None):
        """
        Seed the registry from onboarding data.

        people: list of dicts {"name": str, "relationship": str, "context": str}
        projects: list of str
        aliases: dict {"Max": "Maxwell", ...}
        """
        self._data["mode"] = mode
        self._data["projects"] = list(projects)

        aliases = aliases or {}
        reverse_aliases = {v: k for k, v in aliases.items()}  # Maxwell → Max

        for entry in people:
            name = entry["name"].strip()
            if not name:
                continue
            context = entry.get("context", "personal")
            relationship = entry.get("relationship", "")

            self._data["people"][name] = {
                "source": "onboarding",
                "contexts": [context],
                "aliases": [reverse_aliases[name]] if name in reverse_aliases else [],
                "relationship": relationship,
                "confidence": 1.0,
            }

            # Also register aliases
            if name in reverse_aliases:
                alias = reverse_aliases[name]
                self._data["people"][alias] = {
                    "source": "onboarding",
                    "contexts": [context],
                    "aliases": [name],
                    "relationship": relationship,
                    "confidence": 1.0,
                    "canonical": name,
                }

        # Flag ambiguous names (also common English words)
        ambiguous = []
        for name in self._data["people"]:
            if name.lower() in COMMON_ENGLISH_WORDS:
                ambiguous.append(name.lower())
        self._data["ambiguous_flags"] = ambiguous

        self.save()

    # ── Lookup ───────────────────────────────────────────────────────────────

    def lookup(self, word: str, context: str = "") -> dict:
        """
        Look up a word. Returns entity classification.

        context: surrounding sentence (used for disambiguation of ambiguous words)

        Returns:
            {"type": "person"|"project"|"concept"|"unknown",
             "confidence": float,
             "source": "onboarding"|"learned"|"wiki"|"inferred",
             "name": canonical name if found,
             "needs_disambiguation": bool}
        """
        # 1. Exact match in people registry
        for canonical, info in self.people.items():
            if word.lower() == canonical.lower() or word.lower() in [
                a.lower() for a in info.get("aliases", [])
            ]:
                # Check if this is an ambiguous word
                if word.lower() in self.ambiguous_flags and context:
                    resolved = self._disambiguate(word, context, info)
                    if resolved is not None:
                        return resolved
                return {
                    "type": "person",
                    "confidence": info["confidence"],
                    "source": info["source"],
                    "name": canonical,
                    "context": info.get("contexts", ["personal"]),
                    "needs_disambiguation": False,
                }

        # 2. Project match
        for proj in self.projects:
            if word.lower() == proj.lower():
                return {
                    "type": "project",
                    "confidence": 1.0,
                    "source": "onboarding",
                    "name": proj,
                    "needs_disambiguation": False,
                }

        # 3. Wiki cache
        cache = self._data.get("wiki_cache", {})
        for cached_word, cached_result in cache.items():
            if word.lower() == cached_word.lower() and cached_result.get("confirmed"):
                return {
                    "type": cached_result["inferred_type"],
                    "confidence": cached_result["confidence"],
                    "source": "wiki",
                    "name": word,
                    "needs_disambiguation": False,
                }

        return {
            "type": "unknown",
            "confidence": 0.0,
            "source": "none",
            "name": word,
            "needs_disambiguation": False,
        }

    def _disambiguate(self, word: str, context: str, person_info: dict) -> Optional[dict]:
        """
        When a word is both a name and a common word, check context.
        Returns person result if context suggests a name, None if ambiguous.
        """
        name_lower = word.lower()
        ctx_lower = context.lower()

        # Check person context patterns
        person_score = 0
        for pat in PERSON_CONTEXT_PATTERNS:
            if re.search(pat.format(name=re.escape(name_lower)), ctx_lower):
                person_score += 1

        # Check concept context patterns
        concept_score = 0
        for pat in CONCEPT_CONTEXT_PATTERNS:
            if re.search(pat.format(name=re.escape(name_lower)), ctx_lower):
                concept_score += 1

        if person_score > concept_score:
            return {
                "type": "person",
                "confidence": min(0.95, 0.7 + person_score * 0.1),
                "source": person_info["source"],
                "name": word,
                "context": person_info.get("contexts", ["personal"]),
                "needs_disambiguation": False,
                "disambiguated_by": "context_patterns",
            }
        elif concept_score > person_score:
            return {
                "type": "concept",
                "confidence": min(0.90, 0.7 + concept_score * 0.1),
                "source": "context_disambiguated",
                "name": word,
                "needs_disambiguation": False,
                "disambiguated_by": "context_patterns",
            }

        # Truly ambiguous — return None to fall through to person (registered name)
        return None

    # ── Research unknown words ───────────────────────────────────────────────

    def research(self, word: str, auto_confirm: bool = False, allow_network: bool = False) -> dict:
        """
        Research an unknown word.

        By default this is **local-only**: it checks the wiki cache and
        returns ``"unknown"`` for uncached words.  Pass
        ``allow_network=True`` to explicitly opt in to an outbound
        Wikipedia lookup.  This design honours the project's
        *local-first, zero API* and *privacy by architecture* principles
        — no data leaves the machine unless the caller requests it.

        Caches result.  If *auto_confirm* is ``False``, marks the entry
        as unconfirmed (needs user review).
        """
        # Check cache (read-only — no mutation when allow_network is False)
        cache = self._data.get("wiki_cache", {})
        if word in cache:
            return cache[word]

        if not allow_network:
            return {
                "inferred_type": "unknown",
                "confidence": 0.0,
                "wiki_summary": None,
                "wiki_title": None,
                "word": word,
                "confirmed": False,
                "note": "network lookup disabled — pass allow_network=True to query Wikipedia",
            }

        # Network path — ensure wiki_cache key exists before writing
        cache = self._data.setdefault("wiki_cache", {})
        result = _wikipedia_lookup(word)
        result.setdefault("word", word)
        result.setdefault("confirmed", auto_confirm)

        cache[word] = result
        self.save()
        return result

    def confirm_research(
        self, word: str, entity_type: str, relationship: str = "", context: str = "personal"
    ):
        """Mark a researched word as confirmed and add to people registry."""
        cache = self._data.get("wiki_cache", {})
        if word in cache:
            cache[word]["confirmed"] = True
            cache[word]["confirmed_type"] = entity_type

        if entity_type == "person":
            self._data["people"][word] = {
                "source": "wiki",
                "contexts": [context],
                "aliases": [],
                "relationship": relationship,
                "confidence": 0.90,
            }
            if word.lower() in COMMON_ENGLISH_WORDS:
                flags = self._data.setdefault("ambiguous_flags", [])
                if word.lower() not in flags:
                    flags.append(word.lower())

        self.save()

    # ── Learn from sessions ──────────────────────────────────────────────────

    def learn_from_text(self, text: str, min_confidence: float = 0.75, languages=("en",)) -> list:
        """
        Scan session text for new entity candidates.
        Returns list of newly discovered candidates for review.

        ``languages`` is forwarded to entity detection — pass the user's
        configured ``MempalaceConfig().entity_languages`` to match the
        locales used at ``mempalace init`` time.
        """
        from mempalace.entity_detector import extract_candidates, score_entity, classify_entity

        lines = text.splitlines()
        candidates = extract_candidates(text, languages=languages)
        new_candidates = []

        for name, frequency in candidates.items():
            # Skip if already known
            if name in self.people or name in self.projects:
                continue

            scores = score_entity(name, text, lines, languages=languages)
            entity = classify_entity(name, frequency, scores)

            if entity["type"] == "person" and entity["confidence"] >= min_confidence:
                self._data["people"][name] = {
                    "source": "learned",
                    "contexts": [self.mode if self.mode != "combo" else "personal"],
                    "aliases": [],
                    "relationship": "",
                    "confidence": entity["confidence"],
                    "seen_count": frequency,
                }
                if name.lower() in COMMON_ENGLISH_WORDS:
                    flags = self._data.setdefault("ambiguous_flags", [])
                    if name.lower() not in flags:
                        flags.append(name.lower())
                new_candidates.append(entity)

        if new_candidates:
            self.save()

        return new_candidates

    # ── Query helpers for retrieval ──────────────────────────────────────────

    def extract_people_from_query(self, query: str) -> list:
        """
        Extract known person names from a query string.
        Returns list of canonical names found.
        """
        found = []

        for canonical, info in self.people.items():
            names_to_check = [canonical] + info.get("aliases", [])
            for name in names_to_check:
                # Word boundary match
                if re.search(rf"\b{re.escape(name)}\b", query, re.IGNORECASE):
                    # For ambiguous words, check context
                    if name.lower() in self.ambiguous_flags:
                        result = self._disambiguate(name, query, info)
                        if result and result["type"] == "person":
                            if canonical not in found:
                                found.append(canonical)
                    else:
                        if canonical not in found:
                            found.append(canonical)
        return found

    def extract_unknown_candidates(self, query: str) -> list:
        """
        Find capitalized words in query that aren't in registry or common words.
        These are candidates for Wikipedia research.
        """
        from .palace import _candidate_entity_words

        candidates = _candidate_entity_words(query)
        unknown = []
        for word in set(candidates):
            if word.lower() in COMMON_ENGLISH_WORDS:
                continue
            result = self.lookup(word)
            if result["type"] == "unknown":
                unknown.append(word)
        return unknown

    # ── Summary ──────────────────────────────────────────────────────────────

    def summary(self) -> str:
        lines = [
            f"Mode: {self.mode}",
            f"People: {len(self.people)} ({', '.join(list(self.people.keys())[:8])}{'...' if len(self.people) > 8 else ''})",
            f"Projects: {', '.join(self.projects) or '(none)'}",
            f"Ambiguous flags: {', '.join(self.ambiguous_flags) or '(none)'}",
            f"Wiki cache: {len(self._data.get('wiki_cache', {}))} entries",
        ]
        return "\n".join(lines)
