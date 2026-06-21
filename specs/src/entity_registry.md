# Behavior Specification: `entity_registry.py`

A persistent personal entity registry that classifies words as people, projects, places, concepts, or unknown. It distinguishes real names (e.g. "Riley") from common English words (e.g. "ever") using registered data, context patterns, and an optional Wikipedia lookup (mempalace/entity_registry.py:L1-L16).

The registry is built from three prioritized sources: onboarding (explicit user input), learned (inferred from session history), and researched (Wikipedia lookups for unknown words) (mempalace/entity_registry.py:L6-L9).

## On-Disk Format (Observable Contract)

The registry is persisted as a JSON file. The default location is `~/.mempalace/entity_registry.json` (mempalace/entity_registry.py:L277-L299). When a `config_dir` is supplied at load time, the file is `<config_dir>/entity_registry.json` instead (mempalace/entity_registry.py:L308-L309).

An empty/new registry has this exact shape (mempalace/entity_registry.py:L365-L374):
- `version`: integer `1`
- `mode`: string, one of `"work"`, `"personal"`, `"combo"`; default `"personal"`
- `people`: object mapping canonical name → person-info object
- `projects`: array of strings
- `ambiguous_flags`: array of lowercased strings
- `wiki_cache`: object mapping word → wiki-result object

A person-info object contains: `source` (`"onboarding"`/`"learned"`/`"wiki"`), `contexts` (array of strings), `aliases` (array of strings), `relationship` (string), `confidence` (float), and optionally `canonical` (string, for alias entries) and `seen_count` (integer, for learned entries) (mempalace/entity_registry.py:L282-L296, L417-L435, L646-L653).

## Loading

`load(config_dir=None)` resolves the file path, and if the file exists, parses it as JSON and returns a registry wrapping that data (mempalace/entity_registry.py:L307-L316). If the file does not exist, or parsing/reading fails (malformed JSON or IO error), it returns a registry with the empty schema bound to the resolved path; no error is propagated (mempalace/entity_registry.py:L310-L316).

## Saving (Durability Contract)

`save()` creates the parent directory if missing and attempts to set its permissions to `0o700` (failures ignored) (mempalace/entity_registry.py:L318-L323). It performs an atomic write: it serializes the data as JSON with 2-space indentation, writes to a sibling temp file named `<target>.tmp` in the same directory, flushes and fsyncs the file, attempts to set the temp file permissions to `0o600` (failures ignored), then atomically renames the temp file over the target (mempalace/entity_registry.py:L324-L339). If any failure occurs during the write/rename (including interruption), the temp sidecar is removed and the error is re-raised; the previous registry remains intact because the rename is atomic (mempalace/entity_registry.py:L340-L349). After the rename, it fsyncs the parent directory to ensure rename durability across power loss; on filesystems that reject directory file descriptors this step is silently skipped (mempalace/entity_registry.py:L350-L363).

## Properties / Accessors

- `mode` returns the stored mode or `"personal"` if absent (mempalace/entity_registry.py:L378-L380).
- `people` returns the people object or empty object if absent (mempalace/entity_registry.py:L382-L384).
- `projects` returns the projects array or empty array if absent (mempalace/entity_registry.py:L386-L388).
- `ambiguous_flags` returns the flags array or empty array if absent (mempalace/entity_registry.py:L390-L392).

## Seeding from Onboarding

`seed(mode, people, projects, aliases=None)` sets the registry mode and replaces the projects list with a copy of the provided projects (mempalace/entity_registry.py:L396-L405).

`people` is a list of objects with keys `name` (required), `relationship` (default empty string), and `context` (default `"personal"`). `aliases` is a mapping of alias → canonical (e.g. `{"Max": "Maxwell"}`); it is internally reversed to canonical → alias (mempalace/entity_registry.py:L396-L408).

For each person entry: the name is trimmed of surrounding whitespace; entries with an empty name are skipped (mempalace/entity_registry.py:L410-L416). Each registered person gets `source="onboarding"`, `contexts=[context]`, `relationship`, `confidence=1.0`, and `aliases` set to `[alias]` if the name has a known alias else `[]` (mempalace/entity_registry.py:L417-L423). If the name has an alias, a second entry is also created under the alias name with the same fields plus `canonical` pointing back to the original name (mempalace/entity_registry.py:L425-L435).

After registering people, `ambiguous_flags` is recomputed: it becomes the list of lowercased names (of all registered people) that appear in the common-English-words set (mempalace/entity_registry.py:L437-L442). The registry is then saved (mempalace/entity_registry.py:L444).

## Common Word and Context Pattern Data

A fixed set of common English words double as personal names or look like names (e.g. `ever`, `grace`, `will`, `may`, weekday and month names) (mempalace/entity_registry.py:L32-L87). These are matched case-insensitively (compared against `.lower()` of names) (mempalace/entity_registry.py:L440, L613, L654, L700).

`PERSON_CONTEXT_PATTERNS` are regex templates (with `{name}` placeholder) indicating a word is used as a person name, e.g. "X said", "with X", "X's", dialogue prefix "X:" (mempalace/entity_registry.py:L90-L111). `CONCEPT_CONTEXT_PATTERNS` indicate non-name usage, e.g. "have you ever", "ever since", "would ever", "the X of" (mempalace/entity_registry.py:L114-L125).

## Lookup

`lookup(word, context="")` classifies a word and returns an object with keys `type` (`"person"`/`"project"`/`"concept"`/`"unknown"`), `confidence` (float), `source`, `name` (canonical name), and `needs_disambiguation` (bool) (mempalace/entity_registry.py:L448-L460).

Resolution order:
1. Exact match (case-insensitive) against any registered person's canonical name or any of its aliases (mempalace/entity_registry.py:L461-L465). If the matched word is in `ambiguous_flags` AND a non-empty `context` was given, disambiguation runs; if disambiguation resolves, that result is returned (mempalace/entity_registry.py:L466-L470). Otherwise it returns a person result with the person's stored `confidence`, `source`, canonical `name`, `context` (the contexts list, defaulting to `["personal"]`), and `needs_disambiguation=False` (mempalace/entity_registry.py:L471-L478).
2. Project match (case-insensitive against projects list) returns `type="project"`, `confidence=1.0`, `source="onboarding"` (mempalace/entity_registry.py:L480-L489).
3. Wiki cache: a confirmed cached entry matching the word (case-insensitive) returns its `inferred_type`, `confidence`, with `source="wiki"` (mempalace/entity_registry.py:L491-L501). Unconfirmed cache entries do not match here.
4. Fallthrough returns `type="unknown"`, `confidence=0.0`, `source="none"`, `name=word` (mempalace/entity_registry.py:L503-L509).

## Context Disambiguation

`_disambiguate(word, context, person_info)` is used when a word is both a name and a common word (mempalace/entity_registry.py:L511-L515). It counts how many person-context patterns and concept-context patterns match the lowercased context (name escaped for regex) (mempalace/entity_registry.py:L516-L529).

- If person matches exceed concept matches: returns `type="person"`, `confidence=min(0.95, 0.7 + person_score*0.1)`, `source` from the person info, `disambiguated_by="context_patterns"` (mempalace/entity_registry.py:L531-L540).
- If concept matches exceed person matches: returns `type="concept"`, `confidence=min(0.90, 0.7 + concept_score*0.1)`, `source="context_disambiguated"`, `disambiguated_by="context_patterns"` (mempalace/entity_registry.py:L541-L549).
- On a tie (including zero matches each): returns nothing (None), causing the caller to fall through to treating it as a registered person (mempalace/entity_registry.py:L550-L552).

## Research (Wikipedia Lookup)

`research(word, auto_confirm=False, allow_network=False)` is local-only by default (privacy-by-architecture) (mempalace/entity_registry.py:L556-L569). It first checks the wiki cache; if the word is present (exact key match), the cached entry is returned unchanged with no mutation (mempalace/entity_registry.py:L570-L573).

If `allow_network` is false and the word is uncached, it returns an `"unknown"` result (`confidence=0.0`, `confirmed=False`) with a note that network lookup is disabled; nothing is saved (mempalace/entity_registry.py:L575-L584).

If `allow_network` is true and uncached, it performs the Wikipedia lookup, ensures the result has `word` and `confirmed` (set to `auto_confirm`) keys, stores it in the cache, saves the registry, and returns the result (mempalace/entity_registry.py:L586-L594).

### Wikipedia Lookup Behavior (Network Side Effect)

`_wikipedia_lookup(word)` issues an outbound HTTPS GET to `https://en.wikipedia.org/api/rest_v1/page/summary/<url-encoded word>` with `User-Agent: MemPalace/1.0` and a 5-second timeout, parsing a JSON response (mempalace/entity_registry.py:L177-L193). This is the only network side effect in the module and only fires when explicitly opted into via `allow_network=True` (mempalace/entity_registry.py:L183-L188).

Classification from the response (`type`, `extract` lowercased, `title`):
- A `disambiguation` page whose description contains "name"/"given name" → `inferred_type="person"`, `confidence=0.65`; otherwise → `inferred_type="ambiguous"`, `confidence=0.4` (mempalace/entity_registry.py:L199-L215).
- If the extract contains any name-indicator phrase (e.g. "given name", "first name", "is a name") → `inferred_type="person"`; confidence `0.90` when the extract describes the word itself as a name (contains "`<word> is a`" or "`<word> (name`"), else `0.80` (mempalace/entity_registry.py:L133-L159, L217-L233).
- Else if the extract contains a place-indicator phrase (e.g. "city in", "capital of", "river in") → `inferred_type="place"`, `confidence=0.80` (mempalace/entity_registry.py:L161-L174, L235-L242).
- Otherwise → `inferred_type="concept"`, `confidence=0.60` (mempalace/entity_registry.py:L244-L250).

All successful results include `wiki_summary` (extract truncated to 200 characters) and `wiki_title` (mempalace/entity_registry.py:L205-L249).

Errors: HTTP 404 → `inferred_type="unknown"`, `confidence=0.3`, with note "not found in Wikipedia" (mempalace/entity_registry.py:L252-L262). Any other HTTP error → `inferred_type="unknown"`, `confidence=0.0` (mempalace/entity_registry.py:L263). Network/OS/JSON/key errors → `inferred_type="unknown"`, `confidence=0.0` (mempalace/entity_registry.py:L264-L265).

## Confirming Research

`confirm_research(word, entity_type, relationship="", context="personal")` marks a cached word's `confirmed=True` and sets `confirmed_type` to the given type (only if the word is in the cache) (mempalace/entity_registry.py:L596-L603). If `entity_type == "person"`, it adds the word to the people registry with `source="wiki"`, `contexts=[context]`, empty aliases, the relationship, and `confidence=0.90`; if the word is a common English word, it is appended to `ambiguous_flags` (only if not already present) (mempalace/entity_registry.py:L605-L616). The registry is then saved (mempalace/entity_registry.py:L618).

## Learning from Session Text

`learn_from_text(text, min_confidence=0.75, languages=("en",))` scans text for new entity candidates and returns the list of newly discovered candidates (mempalace/entity_registry.py:L622-L630). It extracts candidate names with frequencies, skipping any name already in `people` or `projects` (mempalace/entity_registry.py:L631-L640). Each candidate is scored and classified; only those classified as `type="person"` with `confidence >= min_confidence` are added (mempalace/entity_registry.py:L642-L645).

Added learned people get `source="learned"`, `contexts=[mode]` (or `["personal"]` when mode is `"combo"`), empty aliases, empty relationship, the candidate confidence, and `seen_count` equal to the frequency (mempalace/entity_registry.py:L645-L653). Common-word names are appended to `ambiguous_flags` if not present (mempalace/entity_registry.py:L654-L657). The registry is saved only if at least one candidate was added (mempalace/entity_registry.py:L660-L663). (Classification/scoring/extraction behavior is delegated to `entity_detector` — see mempalace/entity_registry.py:L631-L643.)

## Query Helpers

`extract_people_from_query(query)` returns the list of canonical person names whose canonical name or any alias matches the query as a whole word (case-insensitive, regex word-boundary) (mempalace/entity_registry.py:L667-L678). For names in `ambiguous_flags`, the match is only accepted if context disambiguation classifies it as a person (mempalace/entity_registry.py:L679-L684); otherwise the canonical name is added directly. Each canonical name appears at most once, preserving discovery order (mempalace/entity_registry.py:L682-L688).

`extract_unknown_candidates(query)` finds capitalized candidate words in the query (via `palace._candidate_entity_words`), excludes any that are common English words, and returns the de-duplicated set of those whose `lookup` returns `type="unknown"` (mempalace/entity_registry.py:L690-L705).

## Summary

`summary()` returns a multi-line human-readable string reporting: the mode; the people count plus up to the first 8 people names (with a trailing "..." if more than 8); the comma-joined projects (or "(none)"); the comma-joined ambiguous flags (or "(none)"); and the count of wiki cache entries (mempalace/entity_registry.py:L709-L717).
