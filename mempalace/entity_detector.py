#!/usr/bin/env python3
"""
entity_detector.py — Auto-detect people and projects from file content.

Uses ``from __future__ import annotations`` so PEP 604 union syntax
(``dict | None``) works on the Python 3.9 baseline.

Two-pass approach:
  Pass 1: scan files, extract entity candidates with signal counts
  Pass 2: score and classify each candidate as person, project, or uncertain

Used by mempalace init before mining begins.
The confirmed entity map feeds the miner as the taxonomy.

Multi-language support:
    All lexical patterns (person verbs, pronouns, dialogue markers, project
    verbs, stopwords, and the candidate-extraction character class) live in
    the ``entity`` section of ``mempalace/i18n/<lang>.json``. Every public
    function accepts a ``languages`` tuple and applies the union of the
    requested locales' patterns. The default is ``("en",)`` — existing
    English-only callers behave exactly as before.

    To add a new language: add an ``entity`` section to that locale's JSON.
    No code changes required.

Usage:
    from mempalace.entity_detector import detect_entities, confirm_entities
    candidates = detect_entities(file_paths)                    # English only
    candidates = detect_entities(paths, languages=("en", "pt-br"))
    confirmed = confirm_entities(candidates)  # interactive review
"""

from __future__ import annotations

import re
import os
import functools
from pathlib import Path
from collections import defaultdict

from mempalace.i18n import get_entity_patterns


# ==================== LANGUAGE-AWARE PATTERN LOADING ====================


def _normalize_langs(languages) -> tuple:
    """Coerce a language input into a non-empty hashable tuple."""
    if not languages:
        return ("en",)
    if isinstance(languages, str):
        return (languages,)
    return tuple(languages)


@functools.lru_cache(maxsize=32)
def _get_stopwords(languages: tuple) -> frozenset:
    """Return the union of stopwords across the given languages."""
    patterns = get_entity_patterns(languages)
    return frozenset(patterns["stopwords"])


# ==================== BACKWARD-COMPAT MODULE CONSTANTS ====================
#
# These mirror the old module-level constants so existing imports keep working.
# They reflect the English defaults and are populated at import time from
# ``mempalace/i18n/en.json``. Callers that need multi-language behavior should
# pass the ``languages`` parameter to the public functions below.

_EN = get_entity_patterns(("en",))

PERSON_VERB_PATTERNS = list(_EN["person_verb_patterns"])
PRONOUN_PATTERNS = list(_EN["pronoun_patterns"])
PRONOUN_RE = re.compile("|".join(PRONOUN_PATTERNS), re.IGNORECASE) if PRONOUN_PATTERNS else None
DIALOGUE_PATTERNS = list(_EN["dialogue_patterns"])
PROJECT_VERB_PATTERNS = list(_EN["project_verb_patterns"])
STOPWORDS = set(_EN["stopwords"])


# ==================== EXTENSION POINTS (not language-scoped) ====================

# For entity detection — prose only, no code files
# Code files have too many capitalized names (classes, functions) that aren't entities
PROSE_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".csv",
}

READABLE_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".rst",
    ".toml",
    ".sh",
    ".rb",
    ".go",
    ".rs",
}

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    "coverage",
    ".mempalace",
    ".terraform",
    "vendor",
    "target",
}

# Files whose content is boilerplate prose — poisons entity detection.
# Matched by stem (case-insensitive), with or without an extension.
SKIP_FILENAMES = {
    "license",
    "licence",
    "copying",
    "copyright",
    "notice",
    "authors",
    "patents",
    "third_party_notices",
    "third-party-notices",
}


# ==================== CANDIDATE EXTRACTION ====================


def extract_candidates(text: str, languages=("en",)) -> dict:
    """
    Extract all capitalized proper noun candidates from text.
    Returns {name: frequency} for names appearing 3+ times.

    Each language contributes its own character-class pattern (e.g. ASCII
    for English, Latin+diacritics for pt-br, Cyrillic for Russian,
    Devanagari for Hindi). Matches from all languages are unioned.
    """
    langs = _normalize_langs(languages)
    patterns = get_entity_patterns(langs)
    stopwords = _get_stopwords(langs)

    counts: defaultdict = defaultdict(int)

    # Single-word candidates — one pre-wrapped pattern per language
    for wrapped_pat in patterns["candidate_patterns"]:
        try:
            rx = re.compile(wrapped_pat)
        except re.error:
            continue
        for word in rx.findall(text):
            if word.lower() in stopwords:
                continue
            if len(word) < 2:
                continue
            counts[word] += 1

    # Multi-word candidates — one pre-wrapped pattern per language
    for wrapped_pat in patterns["multi_word_patterns"]:
        try:
            rx = re.compile(wrapped_pat)
        except re.error:
            continue
        for phrase in rx.findall(text):
            if any(w.lower() in stopwords for w in phrase.split()):
                continue
            counts[phrase] += 1

    return {name: count for name, count in counts.items() if count >= 3}


# ==================== SIGNAL SCORING ====================


@functools.lru_cache(maxsize=256)
def _build_patterns(name: str, languages: tuple = ("en",)) -> dict:
    """Pre-compile all regex patterns for a single entity name, per language set."""
    n = re.escape(name)
    langs = _normalize_langs(languages)
    sources = get_entity_patterns(langs)

    def _compile_each(raw_patterns, flags=re.IGNORECASE):
        compiled = []
        for p in raw_patterns:
            try:
                compiled.append(re.compile(p.format(name=n), flags))
            except (re.error, KeyError, IndexError):
                continue
        return compiled

    direct_sources = sources.get("direct_address_patterns") or []
    direct_compiled = []
    for raw in direct_sources:
        try:
            direct_compiled.append(re.compile(raw.format(name=n), re.IGNORECASE))
        except (re.error, KeyError, IndexError):
            continue

    return {
        "dialogue": _compile_each(sources["dialogue_patterns"], re.MULTILINE | re.IGNORECASE),
        "person_verbs": _compile_each(sources["person_verb_patterns"]),
        "project_verbs": _compile_each(sources["project_verb_patterns"]),
        "direct": direct_compiled,
        "versioned": re.compile(rf"\b{n}[-_]v?\d+(?:\.\d+)*\b", re.IGNORECASE),
        "code_ref": re.compile(rf"\b{n}\.(py|js|ts|yaml|yml|json|sh)\b", re.IGNORECASE),
    }


@functools.lru_cache(maxsize=32)
def _pronoun_re(languages: tuple):
    """Compile a combined pronoun regex for the given languages."""
    langs = _normalize_langs(languages)
    patterns = get_entity_patterns(langs)
    pronouns = patterns.get("pronoun_patterns") or []
    if not pronouns:
        return None
    try:
        return re.compile("|".join(pronouns), re.IGNORECASE)
    except re.error:
        return None


def score_entity(name: str, text: str, lines: list, languages=("en",)) -> dict:
    """
    Score a candidate entity as person vs project.
    Returns scores and the signals that fired.
    """
    langs = _normalize_langs(languages)
    patterns = _build_patterns(name, langs)
    pronoun_re = _pronoun_re(langs)
    person_score = 0
    project_score = 0
    person_signals = []
    project_signals = []

    # --- Person signals ---

    # Dialogue markers (strong signal).
    # The bare `^NAME:\s` colon-prefix pattern matches metadata lines like
    # `Created: 2026-04-21`, so we require >= 2 hits for it to count as dialogue
    # (real speaker markers repeat; single-line metadata doesn't).
    for rx in patterns["dialogue"]:
        matches = len(rx.findall(text))
        if matches == 0:
            continue
        is_bare_colon = rx.pattern.endswith(r":\s") and not rx.pattern.endswith(r"[:\s]")
        if is_bare_colon and matches < 2:
            continue
        person_score += matches * 3
        person_signals.append(f"dialogue marker ({matches}x)")

    # Person verbs
    for rx in patterns["person_verbs"]:
        matches = len(rx.findall(text))
        if matches > 0:
            person_score += matches * 2
            person_signals.append(f"'{name} ...' action ({matches}x)")

    # Pronoun proximity — pronouns within 3 lines of the name
    if pronoun_re is not None:
        name_lower = name.lower()
        name_line_indices = [i for i, line in enumerate(lines) if name_lower in line.lower()]
        pronoun_hits = 0
        for idx in name_line_indices:
            window_text = " ".join(lines[max(0, idx - 2) : idx + 3])
            if pronoun_re.search(window_text):
                pronoun_hits += 1
        if pronoun_hits > 0:
            person_score += pronoun_hits * 2
            person_signals.append(f"pronoun nearby ({pronoun_hits}x)")

    # Direct address
    direct_hits = 0
    for rx in patterns["direct"]:
        direct_hits += len(rx.findall(text))
    if direct_hits > 0:
        person_score += direct_hits * 4
        person_signals.append(f"addressed directly ({direct_hits}x)")

    # --- Project signals ---

    for rx in patterns["project_verbs"]:
        matches = len(rx.findall(text))
        if matches > 0:
            project_score += matches * 2
            project_signals.append(f"project verb ({matches}x)")

    versioned = len(patterns["versioned"].findall(text))
    if versioned > 0:
        project_score += versioned * 3
        project_signals.append(f"versioned/hyphenated ({versioned}x)")

    code_ref = len(patterns["code_ref"].findall(text))
    if code_ref > 0:
        project_score += code_ref * 3
        project_signals.append(f"code file reference ({code_ref}x)")

    return {
        "person_score": person_score,
        "project_score": project_score,
        "person_signals": person_signals[:3],
        "project_signals": project_signals[:3],
    }


# ==================== CLASSIFY ====================


def classify_entity(name: str, frequency: int, scores: dict) -> dict:
    """
    Given scores, classify as person / project / uncertain.
    Returns entity dict with confidence.
    """
    ps = scores["person_score"]
    prs = scores["project_score"]
    total = ps + prs

    if total == 0:
        # No strong signals — frequency-only candidate, uncertain
        confidence = min(0.4, frequency / 50)
        return {
            "name": name,
            "type": "uncertain",
            "confidence": round(confidence, 2),
            "frequency": frequency,
            "signals": [f"appears {frequency}x, no strong type signals"],
        }

    person_ratio = ps / total if total > 0 else 0

    # Require TWO different signal categories to confidently classify as a person.
    # One signal type with many hits (e.g. "Click, click, click...") is not enough —
    # it just means that word appears often in a particular syntactic position.
    signal_categories = set()
    for s in scores["person_signals"]:
        if "dialogue" in s:
            signal_categories.add("dialogue")
        elif "action" in s:
            signal_categories.add("action")
        elif "pronoun" in s:
            signal_categories.add("pronoun")
        elif "addressed" in s:
            signal_categories.add("addressed")

    has_two_signal_types = len(signal_categories) >= 2
    # Single-category pronoun signal still classifies as person when the
    # evidence is overwhelming — a diary's main character is referenced
    # with pronouns, not dialogue markers. Require both: many pronoun hits
    # AND a high pronoun-to-frequency ratio so common sentence-start words
    # (Never, Before, etc.) with incidental pronoun proximity don't qualify.
    pronoun_hits = 0
    for s in scores["person_signals"]:
        m = re.search(r"pronoun nearby \((\d+)x\)", s)
        if m:
            pronoun_hits = int(m.group(1))
            break
    strong_pronoun_signal = pronoun_hits >= 5 and frequency > 0 and pronoun_hits / frequency >= 0.2

    if person_ratio >= 0.7 and (has_two_signal_types and ps >= 5 or strong_pronoun_signal):
        entity_type = "person"
        confidence = min(0.99, 0.5 + person_ratio * 0.5)
        signals = scores["person_signals"] or [f"appears {frequency}x"]
    elif person_ratio >= 0.7:
        # Weak single-category person signal — downgrade to uncertain
        entity_type = "uncertain"
        confidence = 0.4
        signals = scores["person_signals"] + [f"appears {frequency}x — weak person signal"]
    elif person_ratio <= 0.3:
        entity_type = "project"
        confidence = min(0.99, 0.5 + (1 - person_ratio) * 0.5)
        signals = scores["project_signals"] or [f"appears {frequency}x"]
    else:
        entity_type = "uncertain"
        confidence = 0.5
        signals = (scores["person_signals"] + scores["project_signals"])[:3]
        signals.append("mixed signals — needs review")

    return {
        "name": name,
        "type": entity_type,
        "confidence": round(confidence, 2),
        "frequency": frequency,
        "signals": signals,
    }


# ==================== MAIN DETECT ====================


def detect_entities(
    file_paths: list,
    max_files: int = 10,
    languages=("en",),
    corpus_origin: dict | None = None,
) -> dict:
    """
    Scan files and detect entity candidates.

    Args:
        file_paths: List of Path objects to scan
        max_files: Max files to read (for speed)
        languages: Tuple of language codes whose entity patterns should be
            applied (union). Defaults to ``("en",)``.
        corpus_origin: Optional corpus-origin context (the dict produced
            by ``mempalace.corpus_origin`` and persisted to
            ``<palace>/.mempalace/origin.json`` by ``mempalace init``).
            When supplied and the corpus is identified as AI-dialogue with
            known agent persona names, candidates whose name matches an
            agent persona are moved out of ``people``/``uncertain`` and
            into a new ``agent_personas`` bucket. Shape:
            ``{"schema_version": 1, "result": {"agent_persona_names": [...], ...}}``.

    Returns:
        {
            "people":   [...entity dicts...],
            "projects": [...entity dicts...],
            "topics":   [...entity dicts...],
            "uncertain":[...entity dicts...],
            # Only present when corpus_origin reclassifies at least one
            # candidate as an agent persona:
            "agent_personas": [...entity dicts...],
        }
    """
    langs = _normalize_langs(languages)

    # Collect text from files
    all_text = []
    all_lines = []
    files_read = 0

    MAX_BYTES_PER_FILE = 5_000  # first 5KB per file — enough to catch recurring entities

    for filepath in file_paths:
        if files_read >= max_files:
            break
        try:
            with open(filepath, encoding="utf-8", errors="replace") as f:
                content = f.read(MAX_BYTES_PER_FILE)
            all_text.append(content)
            all_lines.extend(content.splitlines())
            files_read += 1
        except OSError:
            continue

    combined_text = "\n".join(all_text)

    # Extract candidates
    candidates = extract_candidates(combined_text, languages=langs)

    if not candidates:
        return _apply_corpus_origin(
            {"people": [], "projects": [], "topics": [], "uncertain": []},
            corpus_origin,
        )

    # Score and classify each candidate
    people = []
    projects = []
    uncertain = []

    for name, frequency in sorted(candidates.items(), key=lambda x: x[1], reverse=True):
        scores = score_entity(name, combined_text, all_lines, languages=langs)
        entity = classify_entity(name, frequency, scores)

        if entity["type"] == "person":
            people.append(entity)
        elif entity["type"] == "project":
            projects.append(entity)
        else:
            uncertain.append(entity)

    # Sort by confidence descending
    people.sort(key=lambda x: x["confidence"], reverse=True)
    projects.sort(key=lambda x: x["confidence"], reverse=True)
    uncertain.sort(key=lambda x: x["frequency"], reverse=True)

    detected = {
        "people": people[:15],
        "projects": projects[:10],
        "topics": [],
        "uncertain": uncertain[:8],
    }

    return _apply_corpus_origin(detected, corpus_origin)


def _apply_corpus_origin(detected: dict, corpus_origin: dict | None) -> dict:
    """Reclassify per-candidate buckets using corpus-origin context.

    When the corpus is identified as AI-dialogue with known agent persona
    names, a candidate whose name case-insensitively matches one of those
    personas is moved from ``people``/``uncertain`` into an
    ``agent_personas`` bucket. The candidate's per-entity ``type`` is also
    rewritten to ``"agent_persona"``.

    No-op when ``corpus_origin`` is ``None`` or contains no usable persona
    names. Pure: returns a new dict, does not mutate the input.
    """
    if not corpus_origin:
        return detected

    origin_result = corpus_origin.get("result") or {}
    raw_personas = origin_result.get("agent_persona_names") or []
    persona_lower = {n.lower() for n in raw_personas if isinstance(n, str)}
    if not persona_lower:
        return detected

    agent_personas: list = []
    new_people: list = []
    new_uncertain: list = []

    for entity in detected.get("people", []):
        if entity["name"].lower() in persona_lower:
            agent_personas.append(_tag_as_persona(entity))
        else:
            new_people.append(entity)

    for entity in detected.get("uncertain", []):
        if entity["name"].lower() in persona_lower:
            agent_personas.append(_tag_as_persona(entity))
        else:
            new_uncertain.append(entity)

    if not agent_personas:
        return detected

    agent_personas.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    return {
        **detected,
        "people": new_people,
        "uncertain": new_uncertain,
        "agent_personas": agent_personas,
    }


def _tag_as_persona(entity: dict) -> dict:
    """Return a new entity dict tagged as agent_persona with provenance signal."""
    existing_signals = entity.get("signals", [])
    return {
        **entity,
        "type": "agent_persona",
        "confidence": max(0.95, entity.get("confidence", 0.0)),
        "signals": ["matched corpus_origin agent_persona_names"] + existing_signals[:2],
    }


# ==================== INTERACTIVE CONFIRM ====================


def _print_entity_list(entities: list, label: str):
    print(f"\n  {label}:")
    if not entities:
        print("    (none detected)")
        return
    for i, e in enumerate(entities):
        confidence_bar = "●" * int(e["confidence"] * 5) + "○" * (5 - int(e["confidence"] * 5))
        signals_str = ", ".join(e["signals"][:2]) if e["signals"] else ""
        print(f"    {i + 1:2}. {e['name']:20} [{confidence_bar}] {signals_str}")


def confirm_entities(detected: dict, yes: bool = False) -> dict:
    """
    Interactive confirmation step.
    User reviews detected entities, removes wrong ones, adds missing ones.
    Returns confirmed {people: [names], projects: [names], topics: [names]}.

    Topics are not surfaced for interactive review — they come from the
    LLM-refined ``TOPIC`` bucket and are passed through verbatim. They
    feed cross-wing tunnel computation at mine time (see
    ``palace_graph.compute_topic_tunnels``); a wrong topic at worst adds
    a low-traffic tunnel and never alters drawer storage.

    Pass yes=True to auto-accept all detected entities without prompting.
    """
    print(f"\n{'=' * 58}")
    print("  MemPalace — Entity Detection")
    print(f"{'=' * 58}")
    print("\n  Scanned your files. Here's what we found:\n")

    _print_entity_list(detected["people"], "PEOPLE")
    _print_entity_list(detected["projects"], "PROJECTS")

    if detected.get("topics"):
        _print_entity_list(detected["topics"], "TOPICS (cross-wing tunnel signal)")

    if detected["uncertain"]:
        _print_entity_list(detected["uncertain"], "UNCERTAIN (need your call)")

    confirmed_people = [e["name"] for e in detected["people"]]
    confirmed_projects = [e["name"] for e in detected["projects"]]
    confirmed_topics = [e["name"] for e in detected.get("topics", [])]

    if yes:
        # Auto-accept: include all detected (skip uncertain — ambiguous without user input)
        print(
            f"\n  Auto-accepting {len(confirmed_people)} people, "
            f"{len(confirmed_projects)} projects, "
            f"{len(confirmed_topics)} topics."
        )
        return {
            "people": confirmed_people,
            "projects": confirmed_projects,
            "topics": confirmed_topics,
        }

    print(f"\n{'─' * 58}")
    print("  Options:")
    print("    [enter]  Accept all")
    print("    [edit]   Remove wrong entries or reclassify uncertain")
    print("    [add]    Add missing people or projects")
    print()

    choice = input("  Your choice [enter/edit/add]: ").strip().lower()

    confirmed_people = [e["name"] for e in detected["people"]]
    confirmed_projects = [e["name"] for e in detected["projects"]]

    if choice == "edit":
        # Handle uncertain first
        if detected["uncertain"]:
            print("\n  Uncertain entities — classify each:")
            for e in detected["uncertain"]:
                ans = input(f"    {e['name']} — (p)erson, (r)project, or (s)kip? ").strip().lower()
                if ans == "p":
                    confirmed_people.append(e["name"])
                elif ans == "r":
                    confirmed_projects.append(e["name"])

        # Remove wrong people
        print(f"\n  Current people: {', '.join(confirmed_people) or '(none)'}")
        remove = input(
            "  Numbers to REMOVE from people (comma-separated, or enter to skip): "
        ).strip()
        if remove:
            to_remove = {int(x.strip()) - 1 for x in remove.split(",") if x.strip().isdigit()}
            confirmed_people = [p for i, p in enumerate(confirmed_people) if i not in to_remove]

        # Remove wrong projects
        print(f"\n  Current projects: {', '.join(confirmed_projects) or '(none)'}")
        remove = input(
            "  Numbers to REMOVE from projects (comma-separated, or enter to skip): "
        ).strip()
        if remove:
            to_remove = {int(x.strip()) - 1 for x in remove.split(",") if x.strip().isdigit()}
            confirmed_projects = [p for i, p in enumerate(confirmed_projects) if i not in to_remove]

    if choice == "add" or input("\n  Add any missing? [y/N]: ").strip().lower() == "y":
        while True:
            name = input("  Name (or enter to stop): ").strip()
            if not name:
                break
            kind = input(f"  Is '{name}' a (p)erson or p(r)oject? ").strip().lower()
            if kind == "p":
                confirmed_people.append(name)
            elif kind == "r":
                confirmed_projects.append(name)

    print(f"\n{'=' * 58}")
    print("  Confirmed:")
    print(f"  People:   {', '.join(confirmed_people) or '(none)'}")
    print(f"  Projects: {', '.join(confirmed_projects) or '(none)'}")
    if confirmed_topics:
        print(f"  Topics:   {', '.join(confirmed_topics)}")
    print(f"{'=' * 58}\n")

    return {
        "people": confirmed_people,
        "projects": confirmed_projects,
        "topics": confirmed_topics,
    }


# ==================== SCAN HELPER ====================


def scan_for_detection(project_dir: str, max_files: int = 10) -> list:
    """
    Collect prose file paths for entity detection.
    Prose only (.txt, .md, .rst, .csv) — code files produce too many false positives.
    Falls back to all readable files if no prose found.
    """
    project_path = Path(project_dir).expanduser().resolve()
    prose_files = []
    all_files = []

    for root, dirs, filenames in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in filenames:
            filepath = Path(root) / filename
            if filepath.stem.lower() in SKIP_FILENAMES:
                continue
            ext = filepath.suffix.lower()
            if ext in PROSE_EXTENSIONS:
                prose_files.append(filepath)
            elif ext in READABLE_EXTENSIONS:
                all_files.append(filepath)

    # Prefer prose files — fall back to all readable if too few prose files
    files = prose_files if len(prose_files) >= 3 else prose_files + all_files
    return files[:max_files]


# ==================== CLI ====================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python entity_detector.py <directory> [lang1,lang2,...]")
        sys.exit(1)

    project_dir = sys.argv[1]
    langs = tuple(sys.argv[2].split(",")) if len(sys.argv) >= 3 else ("en",)
    print(f"Scanning: {project_dir} (languages: {', '.join(langs)})")
    files = scan_for_detection(project_dir)
    print(f"Reading {len(files)} files...")
    detected = detect_entities(files, languages=langs)
    confirmed = confirm_entities(detected)
    print("Confirmed entities:", confirmed)
