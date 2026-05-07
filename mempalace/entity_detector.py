#!/usr/bin/env python3
"""
entity_detector.py — Auto-detect people and projects from file content.

Two-pass approach:
  Pass 1: scan files, extract entity candidates with signal counts
  Pass 2: score and classify each candidate as person, project, or uncertain

Used by mempalace init before mining begins.
The confirmed entity map feeds the miner as the taxonomy.

Usage:
    from entity_detector import detect_entities, confirm_entities
    candidates = detect_entities(file_paths)
    confirmed = confirm_entities(candidates)  # interactive review
"""

import re
import os
from pathlib import Path
from collections import defaultdict


# ==================== SIGNAL PATTERNS ====================

# Person signals — things people do
PERSON_VERB_PATTERNS = [
    r"\b{name}\s+said\b",
    r"\b{name}\s+asked\b",
    r"\b{name}\s+told\b",
    r"\b{name}\s+replied\b",
    r"\b{name}\s+laughed\b",
    r"\b{name}\s+smiled\b",
    r"\b{name}\s+cried\b",
    r"\b{name}\s+felt\b",
    r"\b{name}\s+thinks?\b",
    r"\b{name}\s+wants?\b",
    r"\b{name}\s+loves?\b",
    r"\b{name}\s+hates?\b",
    r"\b{name}\s+knows?\b",
    r"\b{name}\s+decided\b",
    r"\b{name}\s+pushed\b",
    r"\b{name}\s+wrote\b",
    r"\bhey\s+{name}\b",
    r"\bthanks?\s+{name}\b",
    r"\bhi\s+{name}\b",
    r"\bdear\s+{name}\b",
]

# Person signals — pronouns resolving nearby
PRONOUN_PATTERNS = [
    r"\bshe\b",
    r"\bher\b",
    r"\bhers\b",
    r"\bhe\b",
    r"\bhim\b",
    r"\bhis\b",
    r"\bthey\b",
    r"\bthem\b",
    r"\btheir\b",
]

# Person signals — dialogue markers
DIALOGUE_PATTERNS = [
    r"^>\s*{name}[:\s]",  # > Speaker: ...
    r"^{name}:\s",  # Speaker: ...
    r"^\[{name}\]",  # [Speaker]
    r'"{name}\s+said',
]

# Project signals — things projects have/do
PROJECT_VERB_PATTERNS = [
    r"\bbuilding\s+{name}\b",
    r"\bbuilt\s+{name}\b",
    r"\bship(?:ping|ped)?\s+{name}\b",
    r"\blaunch(?:ing|ed)?\s+{name}\b",
    r"\bdeploy(?:ing|ed)?\s+{name}\b",
    r"\binstall(?:ing|ed)?\s+{name}\b",
    r"\bthe\s+{name}\s+architecture\b",
    r"\bthe\s+{name}\s+pipeline\b",
    r"\bthe\s+{name}\s+system\b",
    r"\bthe\s+{name}\s+repo\b",
    r"\b{name}\s+v\d+\b",  # MemPal v2
    r"\b{name}\.py\b",  # mempalace.py
    r"\b{name}-core\b",  # mempal-core (hyphen only, not underscore)
    r"\b{name}-local\b",
    r"\bimport\s+{name}\b",
    r"\bpip\s+install\s+{name}\b",
]

# Words that are almost certainly NOT entities
STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "as",
    "is",
    "was",
    "are",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "shall",
    "can",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "they",
    "them",
    "their",
    "we",
    "our",
    "you",
    "your",
    "i",
    "my",
    "me",
    "he",
    "she",
    "his",
    "her",
    "who",
    "what",
    "when",
    "where",
    "why",
    "how",
    "which",
    "if",
    "then",
    "so",
    "not",
    "no",
    "yes",
    "ok",
    "okay",
    "just",
    "very",
    "really",
    "also",
    "already",
    "still",
    "even",
    "only",
    "here",
    "there",
    "now",
    "then",
    "too",
    "up",
    "out",
    "about",
    "like",
    "use",
    "get",
    "got",
    "make",
    "made",
    "take",
    "put",
    "come",
    "go",
    "see",
    "know",
    "think",
    "true",
    "false",
    "none",
    "null",
    "new",
    "old",
    "all",
    "any",
    "some",
    "true",
    "false",
    "return",
    "print",
    "def",
    "class",
    "import",
    "from",
    # Common capitalized words in prose that aren't entities
    "step",
    "usage",
    "run",
    "check",
    "find",
    "add",
    "get",
    "set",
    "list",
    "args",
    "dict",
    "str",
    "int",
    "bool",
    "path",
    "file",
    "type",
    "name",
    "note",
    "example",
    "option",
    "result",
    "error",
    "warning",
    "info",
    "every",
    "each",
    "more",
    "less",
    "next",
    "last",
    "first",
    "second",
    "stack",
    "layer",
    "mode",
    "test",
    "stop",
    "start",
    "copy",
    "move",
    "source",
    "target",
    "output",
    "input",
    "data",
    "item",
    "key",
    "value",
    "returns",
    "raises",
    "yields",
    "none",
    "self",
    "cls",
    "kwargs",
    # Common sentence-starting / abstract words that aren't entities
    "world",
    "well",
    "want",
    "topic",
    "choose",
    "social",
    "cars",
    "phones",
    "healthcare",
    "ex",
    "machina",
    "deus",
    "human",
    "humans",
    "people",
    "things",
    "something",
    "nothing",
    "everything",
    "anything",
    "someone",
    "everyone",
    "anyone",
    "way",
    "time",
    "day",
    "life",
    "place",
    "thing",
    "part",
    "kind",
    "sort",
    "case",
    "point",
    "idea",
    "fact",
    "sense",
    "question",
    "answer",
    "reason",
    "number",
    "version",
    "system",
    # Greetings and filler words at sentence starts
    "hey",
    "hi",
    "hello",
    "thanks",
    "thank",
    "right",
    "let",
    "ok",
    # UI/action words that appear in how-to content
    "click",
    "hit",
    "press",
    "tap",
    "drag",
    "drop",
    "open",
    "close",
    "save",
    "load",
    "launch",
    "install",
    "download",
    "upload",
    "scroll",
    "select",
    "enter",
    "submit",
    "cancel",
    "confirm",
    "delete",
    "copy",
    "paste",
    "type",
    "write",
    "read",
    "search",
    "find",
    "show",
    "hide",
    # Common filesystem/technical capitalized words
    "desktop",
    "documents",
    "downloads",
    "users",
    "home",
    "library",
    "applications",
    "system",
    "preferences",
    "settings",
    "terminal",
    # Abstract/topic words
    "actor",
    "vector",
    "remote",
    "control",
    "duration",
    "fetch",
    # Abstract concepts that appear as subjects but aren't entities
    "agents",
    "tools",
    "others",
    "guards",
    "ethics",
    "regulation",
    "learning",
    "thinking",
    "memory",
    "language",
    "intelligence",
    "technology",
    "society",
    "culture",
    "future",
    "history",
    "science",
    "model",
    "models",
    "network",
    "networks",
    "training",
    "inference",
}

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
}


# ==================== CANDIDATE EXTRACTION ====================


def extract_candidates(text: str) -> dict:
    """
    Extract all capitalized proper noun candidates from text.
    Returns {name: frequency} for names appearing 3+ times.
    """
    # Find all capitalized words (not at sentence start — harder, so we use frequency as filter)
    raw = re.findall(r"\b([A-Z][a-z]{1,19})\b", text)

    counts = defaultdict(int)
    for word in raw:
        if word.lower() not in STOPWORDS and len(word) > 1:
            counts[word] += 1

    # Also find multi-word proper nouns (e.g. "Memory Palace", "Claude Code")
    multi = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text)
    for phrase in multi:
        if not any(w.lower() in STOPWORDS for w in phrase.split()):
            counts[phrase] += 1

    # Filter: must appear at least 3 times to be a candidate
    return {name: count for name, count in counts.items() if count >= 3}


# ==================== SIGNAL SCORING ====================


def _build_patterns(name: str) -> dict:
    """Pre-compile all regex patterns for a single entity name."""
    n = re.escape(name)
    return {
        "dialogue": [
            re.compile(p.format(name=n), re.MULTILINE | re.IGNORECASE) for p in DIALOGUE_PATTERNS
        ],
        "person_verbs": [re.compile(p.format(name=n), re.IGNORECASE) for p in PERSON_VERB_PATTERNS],
        "project_verbs": [
            re.compile(p.format(name=n), re.IGNORECASE) for p in PROJECT_VERB_PATTERNS
        ],
        "direct": re.compile(rf"\bhey\s+{n}\b|\bthanks?\s+{n}\b|\bhi\s+{n}\b", re.IGNORECASE),
        "versioned": re.compile(rf"\b{n}[-v]\w+", re.IGNORECASE),
        "code_ref": re.compile(rf"\b{n}\.(py|js|ts|yaml|yml|json|sh)\b", re.IGNORECASE),
    }


def score_entity(name: str, text: str, lines: list) -> dict:
    """
    Score a candidate entity as person vs project.
    Returns scores and the signals that fired.
    """
    patterns = _build_patterns(name)
    person_score = 0
    project_score = 0
    person_signals = []
    project_signals = []

    # --- Person signals ---

    # Dialogue markers (strong signal)
    for rx in patterns["dialogue"]:
        matches = len(rx.findall(text))
        if matches > 0:
            person_score += matches * 3
            person_signals.append(f"dialogue marker ({matches}x)")

    # Person verbs
    for rx in patterns["person_verbs"]:
        matches = len(rx.findall(text))
        if matches > 0:
            person_score += matches * 2
            person_signals.append(f"'{name} ...' action ({matches}x)")

    # Pronoun proximity — pronouns within 3 lines of the name
    name_lower = name.lower()
    name_line_indices = [i for i, line in enumerate(lines) if name_lower in line.lower()]
    pronoun_hits = 0
    for idx in name_line_indices:
        window_text = " ".join(lines[max(0, idx - 2) : idx + 3]).lower()
        for pronoun_pattern in PRONOUN_PATTERNS:
            if re.search(pronoun_pattern, window_text):
                pronoun_hits += 1
                break
    if pronoun_hits > 0:
        person_score += pronoun_hits * 2
        person_signals.append(f"pronoun nearby ({pronoun_hits}x)")

    # Direct address
    direct = len(patterns["direct"].findall(text))
    if direct > 0:
        person_score += direct * 4
        person_signals.append(f"addressed directly ({direct}x)")

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
    _ = signal_categories - {"pronoun"}  # reserved for future thresholds

    if person_ratio >= 0.7 and has_two_signal_types and ps >= 5:
        entity_type = "person"
        confidence = min(0.99, 0.5 + person_ratio * 0.5)
        signals = scores["person_signals"] or [f"appears {frequency}x"]
    elif person_ratio >= 0.7 and (not has_two_signal_types or ps < 5):
        # Pronoun-only match — downgrade to uncertain
        entity_type = "uncertain"
        confidence = 0.4
        signals = scores["person_signals"] + [f"appears {frequency}x — pronoun-only match"]
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


def detect_entities(file_paths: list, max_files: int = 10) -> dict:
    """
    Scan files and detect entity candidates.

    Args:
        file_paths: List of Path objects to scan
        max_files: Max files to read (for speed)

    Returns:
        {
            "people":   [...entity dicts...],
            "projects": [...entity dicts...],
            "uncertain":[...entity dicts...],
        }
    """
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
    candidates = extract_candidates(combined_text)

    if not candidates:
        return {"people": [], "projects": [], "uncertain": []}

    # Score and classify each candidate
    people = []
    projects = []
    uncertain = []

    for name, frequency in sorted(candidates.items(), key=lambda x: x[1], reverse=True):
        scores = score_entity(name, combined_text, all_lines)
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

    # Cap results to most relevant
    return {
        "people": people[:15],
        "projects": projects[:10],
        "uncertain": uncertain[:8],
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
    Returns confirmed {people: [names], projects: [names]}

    Pass yes=True to auto-accept all detected entities without prompting.
    """
    print(f"\n{'=' * 58}")
    print("  MemPalace — Entity Detection")
    print(f"{'=' * 58}")
    print("\n  Scanned your files. Here's what we found:\n")

    _print_entity_list(detected["people"], "PEOPLE")
    _print_entity_list(detected["projects"], "PROJECTS")

    if detected["uncertain"]:
        _print_entity_list(detected["uncertain"], "UNCERTAIN (need your call)")

    confirmed_people = [e["name"] for e in detected["people"]]
    confirmed_projects = [e["name"] for e in detected["projects"]]

    if yes:
        # Auto-accept: include all detected (skip uncertain — ambiguous without user input)
        print(
            f"\n  Auto-accepting {len(confirmed_people)} people, {len(confirmed_projects)} projects."
        )
        return {"people": confirmed_people, "projects": confirmed_projects}

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
                ans = input(f"    {e['name']} — (p)erson, (r)roject, or (s)kip? ").strip().lower()
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
    print(f"{'=' * 58}\n")

    return {
        "people": confirmed_people,
        "projects": confirmed_projects,
    }


# ==================== SCAN HELPER ====================


def scan_for_detection(project_dir: str, max_files: int = 10, pattern: str = None) -> list:
    """
    Collect prose file paths for entity detection.
    Prose only (.txt, .md, .rst, .csv) — code files produce too many false positives.
    Falls back to all readable files if no prose found.
    Optional regex pattern filters by relative path.
    """
    import re

    project_path = Path(project_dir).expanduser().resolve()
    compiled_pattern = re.compile(pattern) if pattern else None
    prose_files = []
    all_files = []

    for root, dirs, filenames in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in filenames:
            filepath = Path(root) / filename
            # Regex pattern filter
            if compiled_pattern is not None:
                try:
                    rel = str(filepath.relative_to(project_path).as_posix())
                    if not compiled_pattern.search(rel):
                        continue
                except ValueError:
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
        print("Usage: python entity_detector.py <directory>")
        sys.exit(1)

    project_dir = sys.argv[1]
    print(f"Scanning: {project_dir}")
    files = scan_for_detection(project_dir)
    print(f"Reading {len(files)} files...")
    detected = detect_entities(files)
    confirmed = confirm_entities(detected)
    print("Confirmed entities:", confirmed)
