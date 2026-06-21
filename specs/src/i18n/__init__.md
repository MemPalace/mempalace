# Spec: `mempalace/i18n/__init__.py` — Internationalization & entity-pattern dictionaries

This module loads per-locale string dictionaries from JSON files, provides a
keyed translation lookup with variable interpolation, and merges per-locale
entity-detection regex patterns into a single configuration consumed by the
entity detector.

## On-disk contract

Locale data lives in JSON files located in the same directory as this module
(`_LANG_DIR` is the module's own directory) (mempalace/i18n/__init__.py:L20-L20).
Each locale is a file named `<stem>.json`; the available language codes are
exactly the file stems of all `*.json` files in that directory
(mempalace/i18n/__init__.py:L45-L47).

A locale JSON document is an object that may contain these top-level sections,
all optional unless stated:
- arbitrary translation sections, each an object mapping string names to string
  values (used by `t`) (mempalace/i18n/__init__.py:L70-L75).
- `regex`: object whose keys include `topic_pattern`, `stop_words`,
  `quote_pattern`, `action_pattern` (mempalace/i18n/__init__.py:L89-L97).
- `entity`: object holding entity-detection patterns (mempalace/i18n/__init__.py:L100-L110).

The `entity` section may contain: `boundary_chars` (string, an inside-word
character class without brackets), `candidate_pattern` (string), `multi_word_pattern`
(string), `direct_address_pattern` (string), `person_verb_patterns` (list of
strings), `pronoun_patterns` (list), `dialogue_patterns` (list),
`project_verb_patterns` (list), and `stopwords` (list of strings)
(mempalace/i18n/__init__.py:L171-L194).

## Module state and import-time behavior

The module holds mutable global state: the currently loaded strings dictionary
(`_strings`), the current language code (`_current_lang`, default `"en"`), and a
cache mapping a tuple of language codes to a merged entity pattern dict
(`_entity_cache`) (mempalace/i18n/__init__.py:L21-L25).

On import, English is loaded automatically by calling `load_lang("en")`, so
after import `_strings` is populated and `current_lang()` returns `"en"`
(mempalace/i18n/__init__.py:L284-L285).

## Language code resolution (canonicalization)

Given a language code, the canonical on-disk stem is found by case-insensitive
match: the input is trimmed of surrounding whitespace and lowercased, then
compared against the lowercased stem of each `*.json` file. The first matching
file's actual (original-case) stem is returned; if none match, the result is
"not found" (mempalace/i18n/__init__.py:L28-L42). An empty or absent code
resolves to "not found" (mempalace/i18n/__init__.py:L36-L37). This makes the
mixed-case file naming conventions (e.g. `pt-br.json` vs `zh-CN.json`)
addressable by callers passing any casing such as `PT-BR`, `zh-cn`, or `Pt-Br`
(mempalace/i18n/__init__.py:L30-L34).

## Public surface

### `available_languages() -> list[str]`
Returns the sorted list of available language codes, i.e. the file stems of all
`*.json` files in the locale directory, in ascending sort order
(mempalace/i18n/__init__.py:L45-L47).

### `load_lang(lang="en") -> dict`
Loads the dictionary for a language and makes it current. The requested code is
canonicalized; if it does not resolve to a file, it falls back to `"en"`
(mempalace/i18n/__init__.py:L50-L55). The file `<canonical>.json` is read as
UTF-8 and parsed as JSON into `_strings`; `_current_lang` is set to the canonical
code; the parsed dictionary is returned (mempalace/i18n/__init__.py:L56-L59).
Side effect: reads the locale file from the filesystem and mutates module global
state (mempalace/i18n/__init__.py:L52-L58).

### `t(key, **kwargs) -> str`
Looks up a translated string by dotted key. If no strings are loaded yet, English
is loaded first (mempalace/i18n/__init__.py:L68-L69). The key is split on the
first `.` into at most two parts: if it has two parts (`section.name`), the value
is `_strings[section][name]`; if either the section or name is missing, the
original `key` string is returned as the value. A key with no `.` is looked up as
a top-level entry, defaulting to the `key` itself when absent
(mempalace/i18n/__init__.py:L70-L75).

If keyword arguments are supplied and the resolved value is a string, `{var}`
placeholders are interpolated using those arguments
(mempalace/i18n/__init__.py:L76-L78). If interpolation fails because a referenced
variable is missing or an index is out of range, the un-interpolated value is
returned instead (no exception is raised) (mempalace/i18n/__init__.py:L79-L80).
The resolved (possibly interpolated) value is returned
(mempalace/i18n/__init__.py:L81-L81).

### `current_lang() -> str`
Returns the current language code (`_current_lang`)
(mempalace/i18n/__init__.py:L84-L86).

### `get_regex() -> dict`
Returns the `regex` section of the currently loaded language; loads English first
if no strings are loaded. Returns an empty dict when the current language has no
`regex` section (mempalace/i18n/__init__.py:L89-L97).

### `get_entity_patterns(languages=("en",)) -> dict`
Returns a merged entity-detection pattern dictionary for the requested languages.

Input normalization: an empty `languages` argument is replaced with `("en",)`
(mempalace/i18n/__init__.py:L225-L226). Each requested code is canonicalized to
its on-disk stem; codes that do not resolve to a file are kept verbatim
(mempalace/i18n/__init__.py:L231-L231). The normalized tuple is used as a cache
key; a cached result is returned directly if present
(mempalace/i18n/__init__.py:L232-L234). Cache entries are stored after computation
(mempalace/i18n/__init__.py:L269-L269). Because canonicalization collapses casing,
callers using different casing of the same locale share a cache entry
(mempalace/i18n/__init__.py:L227-L231).

For each normalized language, the locale's `entity` section is loaded and merged
into an accumulator. Languages whose entity section is empty/missing are skipped.
If no requested language yields any entity data, English's entity section is
merged as a fallback so callers always receive a working configuration
(mempalace/i18n/__init__.py:L247-L257, L255-L257).

The returned object has these keys (mempalace/i18n/__init__.py:L259-L268):
- `candidate_patterns`: list of fully-wrapped regex strings (boundary + capture
  group already applied), one per contributing locale that declared
  `candidate_pattern`; consumers compile them directly without further wrapping
  (mempalace/i18n/__init__.py:L172-L175, L208-L212, L260-L260).
- `multi_word_patterns`: same treatment for each locale's `multi_word_pattern`
  (mempalace/i18n/__init__.py:L176-L179, L261-L261).
- `person_verb_patterns`: concatenation of all locales' `person_verb_patterns`
  (each with `\b` expanded per its locale), deduplicated preserving first
  occurrence (mempalace/i18n/__init__.py:L182-L184, L262-L262).
- `pronoun_patterns`: same treatment for `pronoun_patterns`
  (mempalace/i18n/__init__.py:L185-L187, L263-L263).
- `dialogue_patterns`: same treatment for `dialogue_patterns`
  (mempalace/i18n/__init__.py:L188-L190, L264-L264).
- `direct_address_patterns`: list of per-language alternation patterns (each
  locale's `direct_address_pattern` with `\b` expanded), NOT concatenated and NOT
  deduplicated; each is intended to be applied separately
  (mempalace/i18n/__init__.py:L180-L181, L213-L214, L265-L265).
- `project_verb_patterns`: same treatment as `person_verb_patterns` for
  `project_verb_patterns` (mempalace/i18n/__init__.py:L191-L193, L266-L266).
- `stopwords`: the set union of all locales' `stopwords`, each lowercased,
  returned as a sorted list (mempalace/i18n/__init__.py:L194-L194, L267-L267).

Ordering guarantee: list fields are concatenated in the order of the (normalized)
`languages` argument; deduplication preserves first occurrence
(mempalace/i18n/__init__.py:L204-L209, L248-L253). Concatenation across locales is
done by appending in iteration order over `languages`
(mempalace/i18n/__init__.py:L248-L253).

## Script-aware word boundaries

When an entity section declares `boundary_chars` (an inside-word character class
such as `\wऀ-ॿ` for Devanagari/Hindi), every literal `\b` in that
locale's patterns is replaced with a lookaround-based boundary that treats the
declared characters as "inside-word"; when `boundary_chars` is absent or empty,
patterns are left unchanged so `\b` keeps default behavior
(mempalace/i18n/__init__.py:L113-L146). The boundary fragment matches a transition
between an inside-word char and a non-inside-word char, in either direction, plus
the string start before an inside-word char and the string end after one
(mempalace/i18n/__init__.py:L129-L134). This exists so that names containing
combining marks (Devanagari, Arabic, Hebrew, Thai, Tamil, Burmese, Khmer) are not
truncated at the trailing mark, which default `\b` would drop
(mempalace/i18n/__init__.py:L116-L127).

Candidate and multi-word patterns are wrapped with a single capture group plus
boundaries: with `boundary_chars` the script-aware boundary is placed on both
sides; otherwise the wrapping is `\b(<pattern>)\b`
(mempalace/i18n/__init__.py:L149-L159).

## Error and edge-case behavior

- `_load_entity_section` returns an empty dict when the language does not resolve
  to a file, when the file cannot be read or is not valid JSON, or when the file
  has no (or a falsy) `entity` section — never raising for these cases
  (mempalace/i18n/__init__.py:L100-L110).
- `load_lang` does NOT suppress read/parse errors of a resolved file: a missing
  or malformed file that nevertheless matched canonicalization would propagate an
  error (no try/except around the read/parse) (mempalace/i18n/__init__.py:L56-L57).
- `t` never raises on bad interpolation arguments; it returns the raw value
  instead (mempalace/i18n/__init__.py:L79-L80).
- Unknown language codes passed to `get_entity_patterns` are retained as-is and
  simply contribute no data, allowing the single English fallback to fire exactly
  once (mempalace/i18n/__init__.py:L229-L231, L255-L257).

## Side effects

- Filesystem reads only: locale `*.json` files in the module's own directory are
  globbed and read as UTF-8; no network, process, or environment access occurs
  (mempalace/i18n/__init__.py:L20-L20, L39-L41, L57-L57, L107-L107).
- Module-global mutation: `load_lang` updates `_strings`/`_current_lang`;
  `get_entity_patterns` populates `_entity_cache`; import auto-loads English
  (mempalace/i18n/__init__.py:L52-L58, L269-L269, L284-L285).
