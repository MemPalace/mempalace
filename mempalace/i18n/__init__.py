"""i18n — Language dictionaries for MemPalace.

Usage:
    from mempalace.i18n import load_lang, t

    load_lang("fr")           # load French
    print(t("cli.mine_start", path="/docs"))  # "Extraction de /docs..."
    print(t("terms.wing"))    # "aile"
    print(t("aaak.instruction"))  # AAAK compression instruction in French

Each locale JSON may include an ``entity`` section with patterns used by
``mempalace.entity_detector``. See ``get_entity_patterns`` for the merge rules
and the README section "Adding a new language" for the schema.
"""

import json
from pathlib import Path
from typing import Optional

_LANG_DIR = Path(__file__).parent
_strings: dict = {}
_current_lang: str = "en"

# Cache: tuple(langs) -> merged entity pattern dict
_entity_cache: dict = {}


def _canonical_lang(lang: str) -> Optional[str]:
    """Resolve a language code to its on-disk canonical filename stem.

    BCP 47 tags are case-insensitive (RFC 5646 §2.1.1), and the locale
    files mix conventions (``pt-br.json`` vs ``zh-CN.json``). Match on
    lowercase so callers can pass ``PT-BR``, ``zh-cn``, ``Pt-Br``, etc.
    Returns ``None`` if no file matches.
    """
    if not lang:
        return None
    target = lang.strip().lower()
    for path in _LANG_DIR.glob("*.json"):
        if path.stem.lower() == target:
            return path.stem
    return None


def available_languages() -> list[str]:
    """Return list of available language codes."""
    return sorted(p.stem for p in _LANG_DIR.glob("*.json"))


def load_lang(lang: str = "en") -> dict:
    """Load a language dictionary. Falls back to English if not found."""
    global _strings, _current_lang
    canonical = _canonical_lang(lang)
    if canonical is None:
        canonical = "en"
    lang_file = _LANG_DIR / f"{canonical}.json"
    _strings = json.loads(lang_file.read_text(encoding="utf-8"))
    _current_lang = canonical
    return _strings


def t(key: str, **kwargs) -> str:
    """Get a translated string by dotted key. Supports {var} interpolation.

    t("cli.mine_complete", closets=5, drawers=20)
    → "Done. 5 closets, 20 drawers created."
    """
    if not _strings:
        load_lang("en")
    parts = key.split(".", 1)
    if len(parts) == 2:
        section, name = parts
        val = _strings.get(section, {}).get(name, key)
    else:
        val = _strings.get(key, key)
    if kwargs and isinstance(val, str):
        try:
            val = val.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return val


def current_lang() -> str:
    """Return current language code."""
    return _current_lang


def get_regex() -> dict:
    """Return the regex patterns for the current language.

    Keys: topic_pattern, stop_words, quote_pattern, action_pattern.
    Returns empty dict if no regex section in the language file.
    """
    if not _strings:
        load_lang("en")
    return _strings.get("regex", {})


def _load_entity_section(lang: str) -> dict:
    """Load the raw entity section for one language. Returns {} if missing."""
    canonical = _canonical_lang(lang)
    if canonical is None:
        return {}
    lang_file = _LANG_DIR / f"{canonical}.json"
    try:
        data = json.loads(lang_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data.get("entity", {}) or {}


def _script_boundary(chars: str) -> str:
    """Build a lookaround-based word boundary expression.

    Python's built-in ``\\b`` is a transition between ``\\w`` and non-``\\w``.
    ``\\w`` covers Unicode Letter and Number categories but NOT Marks (category
    Mc/Mn), so for scripts whose words contain combining vowel signs — Devanagari
    (ा ी ु), Arabic (ـَ ـِ ـُ), Hebrew (ִ ֵ), Thai, Tamil, Burmese, Khmer — the
    default ``\\b`` drops the trailing mark, truncating names like ``अनीता`` to
    ``अनीत`` and failing to match ``\\bकहा\\b`` because the trailing matra is
    not a word character.

    Locales with such scripts declare ``boundary_chars`` in their entity section
    (e.g. ``"\\\\w\\\\u0900-\\\\u097F"`` for Hindi). This function returns a
    regex fragment equivalent to ``\\b`` but where the "word" side is defined
    as any char matching ``[chars]`` rather than just ``\\w``.
    """
    return (
        rf"(?:(?<=[{chars}])(?=[^{chars}])"
        rf"|(?<=[^{chars}])(?=[{chars}])"
        rf"|^(?=[{chars}])"
        rf"|(?<=[{chars}])$)"
    )


def _expand_b(pattern: str, boundary_chars: str) -> str:
    """Replace every literal ``\\b`` in ``pattern`` with a script-aware boundary.

    ``boundary_chars`` is the inside-word character class (without brackets).
    If it's falsy, the pattern is returned unchanged so ``\\b`` keeps its
    default Python ``re`` semantics.
    """
    if not boundary_chars:
        return pattern
    return pattern.replace(r"\b", _script_boundary(boundary_chars))


def _wrap_candidate(raw_pat: str, boundary_chars: str) -> str:
    """Wrap a candidate/multi-word extraction pattern with a capture group
    and word boundaries appropriate for its locale.

    Default: ``\\b(raw)\\b``. With ``boundary_chars``: the script-aware
    equivalent, so names ending in combining marks are matched in full.
    """
    if boundary_chars:
        b = _script_boundary(boundary_chars)
        return f"{b}({raw_pat}){b}"
    return rf"\b({raw_pat})\b"


def _collect_entity_section(section: dict, acc: dict) -> None:
    """Merge one language's entity section into the running accumulator.

    Handles boundary expansion in-place so the caller merges already-expanded
    strings: `candidate_patterns` and `multi_word_patterns` are pre-wrapped
    with the locale's boundary (capture group included, ready to compile);
    every ``\\b`` inside person/pronoun/dialogue/project/direct patterns is
    replaced with the locale's script-aware boundary.
    """
    boundary_chars = section.get("boundary_chars")
    if section.get("candidate_pattern"):
        acc["candidate_patterns"].append(
            _wrap_candidate(section["candidate_pattern"], boundary_chars)
        )
    if section.get("multi_word_pattern"):
        acc["multi_word_patterns"].append(
            _wrap_candidate(section["multi_word_pattern"], boundary_chars)
        )
    if section.get("direct_address_pattern"):
        acc["direct_address"].append(_expand_b(section["direct_address_pattern"], boundary_chars))
    acc["person_verbs"].extend(
        _expand_b(p, boundary_chars) for p in section.get("person_verb_patterns", [])
    )
    acc["pronouns"].extend(
        _expand_b(p, boundary_chars) for p in section.get("pronoun_patterns", [])
    )
    acc["dialogue"].extend(
        _expand_b(p, boundary_chars) for p in section.get("dialogue_patterns", [])
    )
    acc["project_verbs"].extend(
        _expand_b(p, boundary_chars) for p in section.get("project_verb_patterns", [])
    )
    acc["stopwords"].update(w.lower() for w in section.get("stopwords", []))


def get_entity_patterns(languages=("en",)) -> dict:
    """Return merged entity detection patterns for the requested languages.

    Entity detection patterns live under each locale's ``entity`` section.
    This function merges them into a single dict for consumption by
    ``mempalace.entity_detector``.

    Merge rules:
      - List fields (person_verb_patterns, pronoun_patterns, dialogue_patterns,
        project_verb_patterns) are concatenated in the order of ``languages``,
        with duplicates removed while preserving first occurrence.
      - ``stopwords`` is the set union across all languages, returned as a
        sorted list.
      - ``candidate_patterns`` and ``multi_word_patterns`` are returned as
        **fully-wrapped regex strings** (boundary + capture group applied);
        the consumer compiles them directly with no further wrapping.
      - ``direct_address_pattern`` is returned as a list of per-language
        alternation patterns (not concatenated — each is applied separately).

    Locales with combining-mark scripts can declare ``boundary_chars`` in
    their entity section (e.g. ``"\\\\w\\\\u0900-\\\\u097F"`` for Hindi);
    every ``\\b`` inside that locale's patterns — plus the candidate/multi-
    word wrapping — is expanded to a script-aware lookaround boundary that
    treats the declared characters as "inside-word".

    If ``languages`` is empty or no requested language declares entity data,
    English is used as a fallback so callers always get a working config.
    """
    if not languages:
        languages = ("en",)
    # Normalize via canonical filename so callers using different casing
    # (e.g. "PT-BR" vs "pt-br") share the same cache entry and load the
    # same locale file. Unknown codes are kept as-is so the merge loop's
    # "found_any" branch fires the English fallback exactly once.
    languages = tuple(_canonical_lang(lang) or lang for lang in languages)
    key = languages
    if key in _entity_cache:
        return _entity_cache[key]

    acc = {
        "candidate_patterns": [],
        "multi_word_patterns": [],
        "person_verbs": [],
        "pronouns": [],
        "dialogue": [],
        "direct_address": [],
        "project_verbs": [],
        "stopwords": set(),
    }

    found_any = False
    for lang in languages:
        section = _load_entity_section(lang)
        if not section:
            continue
        found_any = True
        _collect_entity_section(section, acc)

    if not found_any:
        # Fallback: load English directly so callers always get a working config.
        _collect_entity_section(_load_entity_section("en"), acc)

    merged = {
        "candidate_patterns": acc["candidate_patterns"],
        "multi_word_patterns": acc["multi_word_patterns"],
        "person_verb_patterns": _dedupe(acc["person_verbs"]),
        "pronoun_patterns": _dedupe(acc["pronouns"]),
        "dialogue_patterns": _dedupe(acc["dialogue"]),
        "direct_address_patterns": acc["direct_address"],
        "project_verb_patterns": _dedupe(acc["project_verbs"]),
        "stopwords": sorted(acc["stopwords"]),
    }
    _entity_cache[key] = merged
    return merged


def _dedupe(items: list) -> list:
    """Remove duplicates while preserving first-occurrence order."""
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# Auto-load English on import
load_lang("en")
