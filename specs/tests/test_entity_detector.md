# Behavior Specification: Entity Detector

This spec describes the observable behavior of the entity-detection subsystem, as
pinned by its test suite `tests/test_entity_detector.py`. The public surface
consists of: `extract_candidates`, `score_entity`, `classify_entity`,
`detect_entities`, `scan_for_detection`, `confirm_entities`, `_print_entity_list`,
`_build_patterns`, `_normalize_langs`, the i18n helper `get_entity_patterns`, the
config accessor `MempalaceConfig.entity_languages`/`set_entity_languages`, the
module constants `STOPWORDS` and `PROSE_EXTENSIONS`, and two shipped data files
(`coca_content_words.json`, `known_systems.json`) (tests/test_entity_detector.py:L9-L19).

## extract_candidates(text, languages=("en",)) -> map<name, count>

Returns a mapping from candidate entity name (string) to its occurrence count
(integer) (tests/test_entity_detector.py:L25-L29).

- A capitalized single word appearing 3 or more times is detected, with its count
  at least equal to the number of occurrences (tests/test_entity_detector.py:L25-L29).
- Stopwords are excluded even when frequent; e.g. "The" repeated many times is not
  returned (tests/test_entity_detector.py:L32-L36).
- A minimum frequency threshold of 3 applies: a name appearing only once is not
  returned (tests/test_entity_detector.py:L39-L44).
- Multi-word names appearing 3 or more times and containing no stopwords are
  detected as a single key, e.g. "Claude Code" (tests/test_entity_detector.py:L47-L51).
- Empty input text returns an empty mapping (tests/test_entity_detector.py:L54-L56).

### COCA content-word filtering

- Common English content words are filtered out even when frequent. A word such as
  "Code" appearing 5 times is NOT returned (tests/test_entity_detector.py:L62-L71).
- The full set of known false positives from a real-world audit must all be
  filtered: "Code", "Brutal", "Phase", "Chat", "Mar", "Backups", "Planning",
  "Line", "Note" (each appearing 5 times) (tests/test_entity_detector.py:L74-L93).
- The filter is case-insensitive: all-caps "CODE" is filtered just as "Code" is
  (tests/test_entity_detector.py:L96-L102).
- Real proper-noun names not in the content-word list are still detected when they
  appear 3+ times, e.g. "Riley" and "Aya" (tests/test_entity_detector.py:L105-L113).
- A multi-word phrase containing a content-word component is still detected even
  though the component word alone would be filtered, e.g. "Claude Code" stays
  despite "code" being a filtered content word (tests/test_entity_detector.py:L116-L130).

### Known-systems compound matching

- Recognized multi-word product names are detected as a single atomic compound,
  not decomposed into their constituent words. "Claude Code" is returned as one key
  with count >= 4 (tests/test_entity_detector.py:L172-L185).
- When a recognized compound appears, its individual component words must NOT
  separately appear in results — "Claude" alone and "Code" alone are absent when
  the text only mentions "Claude Code" (tests/test_entity_detector.py:L188-L203).
- Compound matching is case-insensitive: "claude code", "CLAUDE CODE",
  "Claude code", and "CLaudE coDe" all collapse to the canonical "Claude Code"
  (tests/test_entity_detector.py:L206-L216).
- Single-word real names are not regressed by compound handling: a standalone "Aya"
  appearing 3+ times is still detected (tests/test_entity_detector.py:L219-L226).
- Single-word content words remain filtered regardless of the compound pre-pass:
  "Code" alone is still filtered (tests/test_entity_detector.py:L229-L236).
- Multiple distinct known compounds in the same text are each detected
  independently, e.g. both "Claude Code" and "GitHub Copilot"
  (tests/test_entity_detector.py:L239-L249).
- A two-word proper-noun phrase NOT in the known-systems list is still detected via
  the generic multi-word path, e.g. "Jane Smith" appearing 4 times
  (tests/test_entity_detector.py:L299-L308).

### Language handling

- The default language tuple is `("en",)`; with only English enabled, accented
  names are dropped — "João" repeated 4 times is not returned
  (tests/test_entity_detector.py:L714-L719).
- Passing an additional locale whose candidate pattern includes Latin diacritics
  causes accented names to be detected, e.g. "João" with count >= 3
  (tests/test_entity_detector.py:L721-L737).
- A locale with a Cyrillic candidate pattern detects Cyrillic names, e.g. "Иван"
  (tests/test_entity_detector.py:L739-L754).
- For combining-mark scripts (e.g. Devanagari), a locale providing `boundary_chars`
  extends word boundaries so matra-ending names are extracted in full, e.g.
  "अनीता" with count >= 3 (tests/test_entity_detector.py:L880-L902). Without
  `boundary_chars`, such a name is truncated and not returned
  (tests/test_entity_detector.py:L905-L913).
- English extraction is unaffected by the boundary-chars machinery: "Riley"
  appearing 4 times is still detected with count >= 3 under `languages=("en",)`
  (tests/test_entity_detector.py:L937-L942).

### CJK (Chinese) handling

- A Traditional-Chinese name is extracted when it neighbours whitespace, English
  text, full-width punctuation, or a line start, e.g. "朱宜振" with count >= 3
  under `languages=("zh-TW",)` (tests/test_entity_detector.py:L955-L968).
- A Simplified-Chinese name behaves identically, e.g. "张三" with count >= 3 under
  `languages=("zh-CN",)` (tests/test_entity_detector.py:L1013-L1018).
- Enabling both zh-TW and zh-CN unions the surname sets so a document mixing
  simplified "张三" and traditional "張三" extracts at least one variant
  (tests/test_entity_detector.py:L1021-L1027).
- English names embedded in Chinese text are still captured when both Chinese and
  English locales are enabled, e.g. "Jeffrey Lai" (or "Jeffrey") alongside "朱宜振"
  (tests/test_entity_detector.py:L999-L1010).
- Documented limitation: a name sandwiched between CJK characters on both sides
  with no whitespace or punctuation break is NOT extracted, even at 4 occurrences,
  e.g. "朱宜振" in fully-CJK-flanked context is absent
  (tests/test_entity_detector.py:L1030-L1041).

## score_entity(name, text, lines, languages=("en",)) -> scores object

Returns an object with keys `person_score` (number), `project_score` (number),
`person_signals` (list of strings), and `project_signals` (list of strings)
(tests/test_entity_detector.py:L314-L320).

- Person-verb usage near a name raises `person_score` above 0 and produces at least
  one entry in `person_signals`, e.g. "Riley said/asked/told"
  (tests/test_entity_detector.py:L314-L320).
- Project-verb usage raises `project_score` above 0 with at least one
  `project_signals` entry, e.g. "building/deployed/Install ChromaDB"
  (tests/test_entity_detector.py:L322-L327).
- Dialogue markers (a name followed by a colon at line start) raise `person_score`
  above 0, e.g. lines like "Riley: ..." (tests/test_entity_detector.py:L330-L334).
- Code-file references (name followed by a source extension) raise `project_score`
  above 0, e.g. "ChromaDB.py", "ChromaDB.js" (tests/test_entity_detector.py:L337-L341).
- When no signals are present, both scores are 0
  (tests/test_entity_detector.py:L344-L349).
- Person-verb patterns from non-English locales contribute additively when their
  locale is enabled: scoring with an extra locale yields a strictly higher
  `person_score` than English-only, and produces an "action" signal, e.g.
  Portuguese verbs "disse/falou/riu" for "Maria"
  (tests/test_entity_detector.py:L756-L779).
- For combining-mark scripts, person-verb patterns fire only when the locale
  supplies `boundary_chars`: with it present, "राज" gets `person_score > 0` and an
  "action" signal (tests/test_entity_detector.py:L916-L923); without it,
  `person_score` is 0 (tests/test_entity_detector.py:L926-L934).
- A Traditional-Chinese name with dialogue and verb context yields
  `person_score > 0` (tests/test_entity_detector.py:L970-L982).

## classify_entity(name, mention_count, scores) -> classification

Returns an object with at least keys `type` (one of "person", "project",
"uncertain", "topic") and `name` (the input name); for the mixed-signal case it
also exposes `signals` (a list) (tests/test_entity_detector.py:L355-L364).

- With no signals, classification is "uncertain" and `name` echoes the input
  (tests/test_entity_detector.py:L355-L364).
- Strong project scoring with project signals classifies as "project"
  (tests/test_entity_detector.py:L367-L376).
- A person classification requires at least two distinct signal categories. Two
  person-signal types (e.g. dialogue marker + action) classify as "person"
  (tests/test_entity_detector.py:L378-L390).
- A single pronoun-only signal at low proximity classifies as "uncertain"
  (tests/test_entity_detector.py:L392-L400).
- A high pronoun-proximity ratio overrides the two-category rule: 16 pronoun hits
  out of 30 mentions classifies as "person" even with only the pronoun category
  (tests/test_entity_detector.py:L403-L414).
- A low pronoun-proximity ratio (e.g. 2 pronoun hits over 21 mentions, below ~20%)
  stays "uncertain" (tests/test_entity_detector.py:L417-L427).
- Balanced person and project scores classify as "uncertain", and the final entry
  in `signals` contains the substring "mixed signals"
  (tests/test_entity_detector.py:L430-L439).

## detect_entities(files, max_files=...) -> categorized result

Reads the given files and returns a mapping with keys `people`, `projects`,
`topics`, and `uncertain`, each a list of entity objects carrying a `name`
(tests/test_entity_detector.py:L445-L463).

- A file dominated by person signals yields the person name among detected entities
  across all categories, e.g. "Riley" (tests/test_entity_detector.py:L445-L463).
- A file dominated by project signals yields the project name, e.g. "Lantern". Note
  the single-word candidate pattern targets a leading capital followed by lowercase
  letters, so detection examples use names matching that shape
  (tests/test_entity_detector.py:L465-L483).
- An empty file yields the all-empty result
  `{"people": [], "projects": [], "topics": [], "uncertain": []}`
  (tests/test_entity_detector.py:L486-L490).
- A missing/nonexistent file path is handled without error and yields the same
  all-empty result (tests/test_entity_detector.py:L493-L496).
- A `max_files` argument bounds the number of files read; passing more files than
  the cap still completes successfully and returns a mapping
  (tests/test_entity_detector.py:L499-L508).

## scan_for_detection(root_path, max_files=...) -> list of file paths

Walks a directory tree and returns candidate files for detection
(tests/test_entity_detector.py:L514-L521).

- Prose files (e.g. `.md`, `.txt`) under the root are found
  (tests/test_entity_detector.py:L514-L521).
- The `.git` directory is skipped entirely — no returned path contains ".git"
  (tests/test_entity_detector.py:L524-L531).
- Fallback behavior: when fewer than 3 prose files exist, the scan also includes
  all other readable files (e.g. `.py`, `.js`)
  (tests/test_entity_detector.py:L645-L654).
- The result is capped to at most `max_files` entries
  (tests/test_entity_detector.py:L657-L662).

## Module constants

- `STOPWORDS` contains common words including "the", "import", and "class"
  (tests/test_entity_detector.py:L537-L541).
- `PROSE_EXTENSIONS` includes ".txt" and ".md"
  (tests/test_entity_detector.py:L543-L545).

## _print_entity_list(entities, heading) -> stdout side effect

Prints an entity listing to standard output (tests/test_entity_detector.py:L551-L560).

- Given non-empty entities, the output contains the heading and each entity name,
  e.g. "PEOPLE", "Alice", "Bob" (tests/test_entity_detector.py:L551-L560).
- Given an empty list, the output contains the text "none detected"
  (tests/test_entity_detector.py:L563-L566).

## confirm_entities(detected, yes=...) -> confirmed selection

Takes the categorized detection result and returns a mapping with at least
`people` and `projects` lists of confirmed names (tests/test_entity_detector.py:L572-L581).

- In auto-confirm mode (`yes=True`), all detected people and projects are accepted;
  uncertain entries are not promoted, so `people == ["Alice"]` and
  `projects == ["Acme"]` (tests/test_entity_detector.py:L572-L581).
- In interactive mode, an empty input accepts the proposed entities (e.g. "Alice"
  ends up in `people`) before declining further prompts
  (tests/test_entity_detector.py:L583-L591).
- An "edit" flow lets the user reclassify uncertain entries: choosing "p" promotes
  an entry to a person ("Foo" -> people), and "s" skips it ("Bar" appears in
  neither people nor projects) (tests/test_entity_detector.py:L594-L617).
- An "add" flow lets the user add entirely new entities by name and category: a name
  followed by "p" becomes a person, a name followed by "r" becomes a project, and an
  empty name stops adding (tests/test_entity_detector.py:L620-L639).

## Language normalization and pattern building

- `_normalize_langs` coerces inputs to a tuple of language codes: a bare string
  "en" -> `("en",)`; a list `["en", "pt-br"]` -> `("en", "pt-br")`; `None` ->
  `("en",)`; an empty tuple `()` -> `("en",)`
  (tests/test_entity_detector.py:L820-L827).
- `get_entity_patterns(langs)` returns a patterns object exposing `stopwords` and
  `candidate_patterns`. An unknown language falls back to English defaults
  (non-empty stopwords and candidate patterns)
  (tests/test_entity_detector.py:L782-L788).
- Pattern loading deduplicates across overlapping languages: `("en", "en")`
  produces the same number of `person_verb_patterns` and `stopwords` as `("en",)`
  (tests/test_entity_detector.py:L791-L798).
- `_build_patterns(name, langs)` is keyed by language tuple: building for the same
  name with an extra locale that adds a person-verb pattern yields strictly more
  compiled `person_verbs` entries than English-only
  (tests/test_entity_detector.py:L801-L817).
- The Traditional-Chinese pattern set lower-cases its source stopwords and includes
  common particles/pronouns such as "這個", "我們", "他們", "完成"
  (tests/test_entity_detector.py:L985-L996).

## Config: entity languages

`MempalaceConfig` exposes `entity_languages` (a list) and `set_entity_languages`
(tests/test_entity_detector.py:L830-L870).

- With no config file and no relevant env var, `entity_languages` defaults to
  `["en"]` (tests/test_entity_detector.py:L830-L837).
- The environment variable `MEMPALACE_ENTITY_LANGUAGES` (comma-separated) overrides
  the config file, e.g. "en,pt-br,ru" -> `["en", "pt-br", "ru"]`
  (tests/test_entity_detector.py:L840-L846). An alternate `MEMPAL_ENTITY_LANGUAGES`
  variable is also consulted (tests/test_entity_detector.py:L834-L835).
- `set_entity_languages(list)` persists to disk and is read back by a fresh config
  instance (tests/test_entity_detector.py:L849-L858).
- `set_entity_languages([])` normalizes an empty list to `["en"]`, both as the
  return value and as the stored value (tests/test_entity_detector.py:L861-L870).

## On-disk data file contracts

### coca_content_words.json (ships in the package `data/` directory)

- The file must exist (tests/test_entity_detector.py:L139-L141).
- Top-level field `schema_version` must equal the integer 1
  (tests/test_entity_detector.py:L143-L145).
- Top-level field `words` must be a list with at least 500 entries
  (tests/test_entity_detector.py:L146-L148).
- Every word must be a lowercase string (the filter normalizes candidates to
  lowercase before lookup) (tests/test_entity_detector.py:L149-L151).
- The word list must contain the lowercased known false positives: "code",
  "brutal", "phase", "chat", "mar", "backups", "planning", "line", "note"
  (tests/test_entity_detector.py:L154-L166).

### known_systems.json (ships in the package `data/` directory)

- The file must exist (tests/test_entity_detector.py:L258-L260).
- Top-level field `schema_version` must equal the integer 1
  (tests/test_entity_detector.py:L262-L264).
- Top-level field `compounds` must be a list with at least 20 entries
  (tests/test_entity_detector.py:L265-L269).
- Every entry must be a multi-token compound (containing a space or hyphen); no
  single-word entries are permitted (tests/test_entity_detector.py:L270-L274).
- The list must contain the high-value compounds: "Claude Code", "GitHub Copilot",
  "Visual Studio Code", "Gemini Code Assist", "Docker Desktop", "GitHub Actions"
  (tests/test_entity_detector.py:L277-L296).

## Locale file format (i18n contract)

A locale file is a JSON object placed in the package `i18n/` directory named
`<locale-code>.json`, carrying keys `lang`, `label`, `terms`, `cli`, `aaak`
(with an `instruction`), and `entity` (tests/test_entity_detector.py:L683-L695).
The `entity` section supplies `candidate_pattern`, `multi_word_pattern`,
`person_verb_patterns` (each may use a `{name}` placeholder), `pronoun_patterns`,
`dialogue_patterns`, `project_verb_patterns`, and `stopwords`; it may optionally
supply `boundary_chars` and `direct_address_pattern`
(tests/test_entity_detector.py:L723-L731, tests/test_entity_detector.py:L880-L893).
Caches keyed on locale data (entity cache, compiled patterns, pronoun regex,
stopwords) must be clearable so newly added locales take effect
(tests/test_entity_detector.py:L697-L711).
