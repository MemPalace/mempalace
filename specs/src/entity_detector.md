# Spec: entity_detector

Auto-detect people and projects from file content. A two-pass system: pass 1 scans files and extracts entity candidates with frequency counts; pass 2 scores and classifies each candidate as person, project, or uncertain. Used before mining begins; the confirmed entity map feeds the miner as taxonomy (mempalace/entity_detector.py:L1-L31).

All lexical patterns (person verbs, pronouns, dialogue markers, project verbs, stopwords, candidate character-classes) are loaded from per-language pattern data keyed by locale. Every public function accepts a `languages` argument and applies the union of the requested locales' patterns. The default is `("en",)`, which must reproduce English-only behavior exactly (mempalace/entity_detector.py:L15-L31).

## Data Files and Filters

### COCA content-word filter
A filter set of common English content words that appear capitalized but are not proper nouns (e.g. "Code", "Brutal", "Phase", "Chat", "Note", "Line"). Loaded once from `mempalace/data/coca_content_words.json`, reading the `words` array, keeping only string entries, and lowercasing each. Lookups are case-insensitive (callers lowercase before lookup). If the file is missing or malformed, an empty set is returned and extraction proceeds without filtering rather than failing (mempalace/entity_detector.py:L45-L78).

### Known-systems compound lexicon
Multi-word product/system names (e.g. "Claude Code", "GitHub Copilot") that must be detected as atomic units, not split into constituent words. Loaded once from `mempalace/data/known_systems.json`, reading the `compounds` array, keeping only non-empty string entries. Entries are sorted by length descending so longest-match wins (a longer compound is masked before a shorter one contained within it, preventing double-counting). Each compound is matched case-insensitively with word boundaries on both sides so partial-word matches are excluded (e.g. "GPT-4" must not match inside "GPT-40"). If the file is missing or malformed, an empty lexicon is returned and compounds are then only detectable via the ordinary multi-word path (mempalace/entity_detector.py:L81-L135).

### Known-systems pre-pass
Given input text, scans for every lexicon compound, returning a working copy of the text with each matched span replaced by spaces (same length, preserving subsequent indices), plus a map of `compound -> count`. The count is keyed by the lexicon's canonical casing regardless of how the compound appeared in the source. When the lexicon is empty, the input text is returned unchanged with an empty map. Masking matched spans prevents the later single-word and multi-word passes from re-decomposing the compound (mempalace/entity_detector.py:L138-L170).

## Language Normalization

A language input is coerced into a non-empty tuple: an empty/falsy input yields `("en",)`; a single string yields a one-element tuple of that string; any other iterable is converted to a tuple as-is (mempalace/entity_detector.py:L176-L182).

Stopwords for a language set are the union of stopwords across all requested locales (mempalace/entity_detector.py:L185-L189).

## Extension-Point Constants (not language-scoped)

- Prose extensions (preferred for entity detection, lower false-positive rate): `.txt`, `.md`, `.rst`, `.csv` (mempalace/entity_detector.py:L213-L218).
- Readable extensions (fallback set): `.txt`, `.md`, `.py`, `.js`, `.ts`, `.json`, `.yaml`, `.yml`, `.csv`, `.rst`, `.toml`, `.sh`, `.rb`, `.go`, `.rs` (mempalace/entity_detector.py:L220-L236).
- Skipped directory names during scan: `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `env`, `dist`, `build`, `.next`, `coverage`, `.mempalace`, `.terraform`, `vendor`, `target` (mempalace/entity_detector.py:L238-L253).
- Skipped filenames by stem (case-insensitive, with or without extension), as boilerplate prose that poisons detection: `license`, `licence`, `copying`, `copyright`, `notice`, `authors`, `patents`, `third_party_notices`, `third-party-notices` (mempalace/entity_detector.py:L255-L267).

## Public Surface

### extract_candidates(text, languages=("en",)) -> dict
Returns a map of `{name: frequency}` containing only names appearing 3 or more times (mempalace/entity_detector.py:L273-L331).

Order of operation: (1) run the known-systems pre-pass, adding each detected compound's count into the running tally and obtaining a working text with compounds masked (mempalace/entity_detector.py:L289-L295). (2) For each per-language single-word candidate pattern, find all matches in the working text; skip a word if its lowercase form is a stopword, if its lowercase form is in the COCA filter, or if its length is under 2 characters; otherwise increment its count (mempalace/entity_detector.py:L297-L315). (3) For each per-language multi-word candidate pattern, find all phrases in the working text; skip a phrase if any of its space-separated words is a stopword; otherwise increment the phrase count. The multi-word path is intentionally NOT subject to the COCA filter, so compounds like "Claude Code" remain detectable (mempalace/entity_detector.py:L307-L329). Per-language matches from all locales are unioned. A pattern that fails to compile is silently skipped (mempalace/entity_detector.py:L298-L302, L321-L325). The final result keeps only entries with count >= 3 (mempalace/entity_detector.py:L331).

### score_entity(name, text, lines, languages=("en",)) -> dict
Scores a single candidate as person vs project and returns `{"person_score": int, "project_score": int, "person_signals": [str...], "project_signals": [str...]}`, where each signals list is truncated to at most 3 entries (mempalace/entity_detector.py:L385-L465).

Person signals:
- Dialogue markers (strong): for each dialogue pattern, count matches in text and add `matches * 3` to person score. A bare colon-prefix pattern (one ending in the `NAME:` whitespace form but not the bracketed character-class form) matches metadata lines like `Created: 2026-04-21`, so it only counts when it fires at least twice; a single hit is ignored (mempalace/entity_detector.py:L400-L412).
- Person verbs: for each person-verb pattern, add `matches * 2` (mempalace/entity_detector.py:L414-L419).
- Pronoun proximity: a pronoun appearing within a 5-line window (2 lines before through 2 lines after) of a line containing the name (case-insensitive) counts as one hit per name-bearing line; add `pronoun_hits * 2`. Skipped entirely if no pronoun pattern exists for the language set (mempalace/entity_detector.py:L421-L432).
- Direct address: sum direct-address pattern matches and add `direct_hits * 4` (mempalace/entity_detector.py:L434-L440).

Project signals:
- Project verbs: for each project-verb pattern, add `matches * 2` (mempalace/entity_detector.py:L444-L448).
- Versioned/hyphenated form (`NAME-v1.2`, `NAME_2`, etc.): add `matches * 3` (mempalace/entity_detector.py:L366, L450-L453).
- Code-file reference (`NAME.py`, `.js`, `.ts`, `.yaml`, `.yml`, `.json`, `.sh`): add `matches * 3` (mempalace/entity_detector.py:L367, L455-L458).

Signal strings are human-readable, each embedding its hit count in the form `... (Nx)`, e.g. `"dialogue marker (3x)"`, `"pronoun nearby (5x)"`, `"versioned/hyphenated (2x)"` — these exact substrings are contractually depended on by classification (mempalace/entity_detector.py:L412, L419, L432, L440, L448, L453, L458).

### classify_entity(name, frequency, scores) -> dict
Returns `{"name": str, "type": str, "confidence": float, "frequency": int, "signals": [str...]}` where `type` is one of `"person"`, `"project"`, or `"uncertain"`, and confidence is rounded to 2 decimals (mempalace/entity_detector.py:L471-L546).

Classification rules:
- If `person_score + project_score == 0`: type is `"uncertain"`, confidence is `min(0.4, frequency/50)`, with a single signal `"appears Nx, no strong type signals"` (mempalace/entity_detector.py:L480-L489).
- Otherwise `person_ratio = person_score / total`. Determine distinct person-signal categories by scanning the person-signal strings for the substrings `dialogue`, `action`, `pronoun`, `addressed`; "two signal types" means two or more distinct categories present (mempalace/entity_detector.py:L491-L507).
- A "strong pronoun signal" requires the parsed pronoun-hit count (extracted from the `pronoun nearby (Nx)` string) to be >= 5, frequency > 0, and `pronoun_hits / frequency >= 0.2` (mempalace/entity_detector.py:L513-L519).
- If `person_ratio >= 0.7` AND (two signal types with `person_score >= 5`, OR a strong pronoun signal): type `"person"`, confidence `min(0.99, 0.5 + person_ratio*0.5)`, signals = person signals (or `["appears Nx"]` if empty) (mempalace/entity_detector.py:L521-L524).
- Else if `person_ratio >= 0.7` (weak single-category person evidence): type `"uncertain"`, confidence `0.4`, signals = person signals plus `"appears Nx — weak person signal"` (mempalace/entity_detector.py:L525-L529).
- Else if `person_ratio <= 0.3`: type `"project"`, confidence `min(0.99, 0.5 + (1-person_ratio)*0.5)`, signals = project signals (or `["appears Nx"]` if empty) (mempalace/entity_detector.py:L530-L533).
- Else (ratio between 0.3 and 0.7): type `"uncertain"`, confidence `0.5`, signals = first 3 of combined person+project signals plus `"mixed signals — needs review"` (mempalace/entity_detector.py:L534-L538).

### detect_entities(file_paths, max_files=10, languages=("en",), corpus_origin=None) -> dict
Scans files and returns detected entity buckets (mempalace/entity_detector.py:L552-L646).

Inputs: `file_paths` is a list of file paths; `max_files` caps how many files are read; `corpus_origin` is an optional context dict (see below) (mempalace/entity_detector.py:L552-L585).

File reading: iterate file paths, stopping once `max_files` files have been successfully read. Each file is opened as UTF-8 with malformed bytes replaced, reading only the first 5000 bytes (enough to catch recurring entities). Files that raise an OS error are skipped without aborting. Read content is accumulated both as full text and as split lines (mempalace/entity_detector.py:L588-L607).

Candidate extraction runs on the newline-joined combined text. If no candidates are found, an empty-bucket result `{"people": [], "projects": [], "topics": [], "uncertain": []}` is returned after corpus-origin reclassification (mempalace/entity_detector.py:L607-L616).

Otherwise each candidate is scored and classified, iterating candidates in descending frequency order, and routed into people/projects/uncertain buckets by its type (mempalace/entity_detector.py:L618-L632).

Ordering and truncation guarantees in the output: people sorted by confidence descending and truncated to 15; projects sorted by confidence descending and truncated to 10; topics always empty; uncertain sorted by frequency descending and truncated to 8 (mempalace/entity_detector.py:L634-L644). Each bucket value is a list of entity dicts of the shape produced by `classify_entity`. An `agent_personas` bucket is present only when corpus-origin reclassification moves at least one candidate (mempalace/entity_detector.py:L576-L584, L639-L646).

### Corpus-origin reclassification
Given the detected buckets and a `corpus_origin` dict, when the corpus is identified as AI-dialogue with known agent persona names, any candidate whose name matches (case-insensitively) one of those persona names is moved from people/uncertain into a new `agent_personas` bucket, and that candidate's `type` is rewritten to `"agent_persona"` (mempalace/entity_detector.py:L649-L696).

Persona names are read from `corpus_origin["result"]["agent_persona_names"]` (string entries only, lowercased for comparison). The expected input shape is `{"schema_version": 1, "result": {"agent_persona_names": [...], ...}}` (mempalace/entity_detector.py:L566-L573, L664-L666). The function is a no-op (returns the input unchanged) when `corpus_origin` is falsy, or when there are no usable persona names, or when no candidate matched. It does not mutate its input; it returns a new dict. When reclassification occurs, the resulting `agent_personas` list is sorted by confidence descending (mempalace/entity_detector.py:L661-L696).

A reclassified entity gets `type = "agent_persona"`, `confidence = max(0.95, existing_confidence)`, and signals set to `["matched corpus_origin agent_persona_names"]` followed by up to the first 2 of its existing signals (mempalace/entity_detector.py:L699-L707).

### confirm_entities(detected, yes=False) -> dict
Interactive confirmation step returning `{"people": [names], "projects": [names], "topics": [names]}` — note buckets here are flat lists of name strings, not entity dicts (mempalace/entity_detector.py:L724-L833).

Side effect: prints a formatted report of detected people, projects, optionally topics, and optionally uncertain entities to stdout. Each entity is rendered with a 5-segment confidence bar (filled-circle count = `int(confidence*5)`, remainder hollow) and up to its first 2 signal strings (mempalace/entity_detector.py:L713-L750).

Topics are never surfaced for interactive review; they pass through verbatim from `detected["topics"]` and feed cross-wing tunnel computation at mine time (mempalace/entity_detector.py:L730-L735, L752-L754).

When `yes=True`: auto-accept all detected people, projects, and topics (uncertain are excluded since they need user input), print a one-line summary, and return without prompting (mempalace/entity_detector.py:L756-L767).

When interactive: prompt for a choice among accept/edit/add. Confirmed people and projects start as all detected names (mempalace/entity_detector.py:L769-L779).
- On `edit`: for each uncertain entity, prompt to classify as `p` (person), `r` (project), or skip; then prompt for comma-separated 1-based index numbers to remove from people, then from projects (mempalace/entity_detector.py:L781-L808).
- On `add` (or an affirmative answer to the "Add any missing?" prompt): repeatedly prompt for a name (empty name stops) and whether it is `p` person or `r` project, appending accordingly (mempalace/entity_detector.py:L810-L819).
Finally prints a "Confirmed" summary of people, projects, and (if any) topics, and returns the three flat name lists (mempalace/entity_detector.py:L821-L833).

### scan_for_detection(project_dir, max_files=10) -> list
Collects candidate file paths for detection (mempalace/entity_detector.py:L839-L863). The directory is expanded (user home) and resolved to an absolute path, then walked recursively. Skip-directory names are pruned from traversal. Files whose stem (case-insensitive) is in the skip-filename set are excluded. Files with a prose extension go to the prose list; files with any other readable extension go to the all-files list (mempalace/entity_detector.py:L845-L859). If at least 3 prose files were found, only prose files are used; otherwise the prose files are concatenated with the all-files list. The result is truncated to `max_files` (mempalace/entity_detector.py:L861-L863).

## CLI Contract
When invoked as a program: requires a directory argument; with fewer than 2 args it prints a usage line `Usage: python entity_detector.py <directory> [lang1,lang2,...]` and exits with code 1. An optional second argument is a comma-separated list of language codes (default `("en",)`). It prints the scan target and language list, scans for files, prints how many will be read, runs detection, runs interactive confirmation, and prints the confirmed entities (mempalace/entity_detector.py:L866-L882).

## Edge Cases and Invariants
- Missing/malformed COCA or known-systems data files degrade gracefully to empty filters/lexicons rather than raising (mempalace/entity_detector.py:L62-L78, L95-L135).
- Any regex pattern that fails to compile during candidate extraction or per-name pattern building is silently skipped (mempalace/entity_detector.py:L298-L302, L321-L325, L344-L351, L355-L359).
- Candidate minimum frequency threshold is 3 (mempalace/entity_detector.py:L331); single-word candidates must be at least 2 characters (mempalace/entity_detector.py:L313-L314).
- Per-file read is capped at 5000 bytes and total files at `max_files` (mempalace/entity_detector.py:L593-L601).
- Output bucket caps: people 15, projects 10, uncertain 8, topics always empty (mempalace/entity_detector.py:L640-L644).
- Backward-compatible module constants (`PERSON_VERB_PATTERNS`, `PRONOUN_PATTERNS`, `PRONOUN_RE`, `DIALOGUE_PATTERNS`, `PROJECT_VERB_PATTERNS`, `STOPWORDS`) are populated at import time from the English locale and mirror the English defaults (mempalace/entity_detector.py:L192-L206).
