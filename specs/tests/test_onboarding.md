# Behavior Spec: `mempalace.onboarding` (derived from tests/test_onboarding.py)

This spec describes the observable behavior of the onboarding module as constrained by its test suite. All claims cite the test file that asserts them.

## Public Surface

The module exposes: `DEFAULT_WINGS`, `_ask`, `_ask_embedding_model`, `_ask_mode`, `_ask_people`, `_ask_projects`, `_ask_wings`, `_auto_detect`, `_generate_aaak_bootstrap`, `_header`, `_hr`, `_warn_ambiguous`, `_yn`, `quick_setup`, and `run_onboarding` (tests/test_onboarding.py:L6-L22). The source file may contain Unicode symbols (hearts/stars), requiring UTF-8 capable output (tests/test_onboarding.py:L24-L25).

## DEFAULT_WINGS

`DEFAULT_WINGS` is a mapping keyed by mode. It contains at least the keys `"work"`, `"personal"`, and `"combo"` (tests/test_onboarding.py:L31-L34). The `"work"` entry includes a `"projects"` wing (tests/test_onboarding.py:L37-L38); the `"personal"` entry includes a `"family"` wing (tests/test_onboarding.py:L41-L42); the `"combo"` entry includes both `"family"` and `"work"` wings (tests/test_onboarding.py:L45-L48). Every value is a list of wing names with at least 3 entries (tests/test_onboarding.py:L51-L54).

## `_warn_ambiguous(people) -> list`

Takes a list of person dicts (each with `"name"` and `"relationship"`) and returns the subset of names that are common English words (potentially ambiguous). Names like "Grace", "May", and "Joy" are flagged and appear in the result (tests/test_onboarding.py:L60-L66, L85-L94). Names that are not common English words, like "Riley" and "Devon", are not flagged (tests/test_onboarding.py:L76-L82, L67-L68). An empty input list returns an empty list (tests/test_onboarding.py:L71-L73).

## `quick_setup(mode, people, projects=..., config_dir, embedding_model=...) -> registry`

Programmatically builds and returns an entity registry. The returned registry has a `mode` attribute equal to the supplied mode (tests/test_onboarding.py:L100-L109, L112-L119), a `people` collection containing the supplied person names (tests/test_onboarding.py:L107-L108, L120), and a `projects` collection containing the supplied project names (tests/test_onboarding.py:L108, L121). With empty `people` and no projects, both collections are empty (tests/test_onboarding.py:L124-L127).

Side effect: it persists `entity_registry.json` into `config_dir` (tests/test_onboarding.py:L130-L136).

Embedding model handling: when `embedding_model` is explicitly provided, `quick_setup` writes a `config.json` into `config_dir` whose `embedding_model` field equals the provided value (tests/test_onboarding.py:L534-L545). When `embedding_model` is NOT provided, `config.json` is not written (back-compat: no surprise config write) (tests/test_onboarding.py:L548-L556).

## `_generate_aaak_bootstrap(people, projects, wings, mode, config_dir)`

Side effect: writes two files into `config_dir` — `aaak_entities.md` and `critical_facts.md` (tests/test_onboarding.py:L142-L153). These files are written even when `people` and `projects` are empty (tests/test_onboarding.py:L181-L184).

`aaak_entities.md` content: includes each person's name, an entity code derived from the name, and each project name. For person "Riley" the code "RIL" appears, and project "MemPalace" appears (tests/test_onboarding.py:L155-L164, all UTF-8 encoded). 

Entity code generation: a person's code is normally the uppercased first 3 letters of the name (e.g. "RIL" for Riley) (tests/test_onboarding.py:L163). When two people would collide on the same 3-letter code, they receive distinct codes — e.g. "Alice" yields "ALI" and "Alison" yields the longer "ALIS" (tests/test_onboarding.py:L187-L196). A person without a `"relationship"` key still produces a valid entry; the entity line takes the form `CODE=Name`, e.g. `BOB=Bob` (tests/test_onboarding.py:L199-L204).

`critical_facts.md` content: includes person names, project names, and the mode string. For a work-mode setup it contains "Alice", "Acme", and "work" (case-insensitive) (tests/test_onboarding.py:L167-L178).

## `_hr()` and `_header(title)`

`_hr()` prints a horizontal rule containing the box-drawing character `─` to standard output (tests/test_onboarding.py:L210-L213). `_header(title)` prints a banner to standard output containing the given title text and `=` characters (tests/test_onboarding.py:L216-L219).

## `_ask(prompt, default=None) -> str`

Prompts for interactive input. If a `default` is supplied and the user enters empty input, the default is returned (tests/test_onboarding.py:L226-L229). If the user enters non-empty input, that input is returned, overriding the default (tests/test_onboarding.py:L232-L235). With no default, the user's input is returned as-is (tests/test_onboarding.py:L238-L241).

## `_yn(prompt, default="y") -> bool`

A yes/no prompt returning a boolean. Default is yes: empty input returns `True` (tests/test_onboarding.py:L247-L249). With `default="n"`, empty input returns `False` (tests/test_onboarding.py:L252-L254). Explicit "yes" returns `True` regardless of default (tests/test_onboarding.py:L257-L259); explicit "no" returns `False` regardless of default (tests/test_onboarding.py:L262-L264).

## `_ask_mode() -> str`

Prompts for a mode selection by number: input `"1"` -> `"work"`, `"2"` -> `"personal"`, `"3"` -> `"combo"` (tests/test_onboarding.py:L270-L282). Invalid inputs cause re-prompting until a valid choice is entered; the first valid input is accepted (tests/test_onboarding.py:L285-L287).

## `_ask_people(mode) -> (people, aliases)`

Returns a tuple of a people list and an aliases mapping. Input lines take the form `"Name, relationship"`.

Personal mode: a line `"Alice, daughter"` produces one person `{"name": "Alice", "relationship": "daughter"}`; entry loop ends on a "done" sentinel (the sequence used is name line, then a blank nickname line, then `"done"`) (tests/test_onboarding.py:L293-L298). Work mode: produced people carry `context` = `"work"` (tests/test_onboarding.py:L301-L306). Combo mode collects both a personal section and a work section, yielding the combined people across both (tests/test_onboarding.py:L309-L321).

Nicknames/aliases: when a nickname is supplied after a person, it is recorded in the returned aliases mapping as `{nickname: name}`, e.g. `{"Ali": "Alice"}` (tests/test_onboarding.py:L324-L327). An empty name line is skipped (produces no person) (tests/test_onboarding.py:L330-L333).

## `_ask_projects(mode) -> list`

In personal mode, returns an empty list without prompting (tests/test_onboarding.py:L339-L341). In work mode, prompts for project names; collected names are returned in entry order, ending on a "done" sentinel (e.g. `["Acme", "BigCo"]`) (tests/test_onboarding.py:L344-L347). An empty input entry also stops collection, returning the projects gathered so far (tests/test_onboarding.py:L350-L353).

## `_ask_wings(mode) -> list`

Empty input accepts the defaults for the given mode, returning `DEFAULT_WINGS[mode]` (tests/test_onboarding.py:L359-L362). A comma-separated input string is parsed into a trimmed list of wing names, e.g. `"alpha, beta, gamma"` -> `["alpha", "beta", "gamma"]` (tests/test_onboarding.py:L365-L368).

## `_auto_detect(directory, known) -> list`

Scans a directory for entities and returns newly detected people. With no scannable files it returns an empty list (tests/test_onboarding.py:L374-L376). It depends on two collaborators, `scan_for_detection` and `detect_entities`; the latter returns a dict with keys `people`, `projects`, `uncertain`, where each detected person has `name`, `confidence`, and `signals` (tests/test_onboarding.py:L381-L392).

Filtering: people already present in `known` (matched by name) are excluded from the result (tests/test_onboarding.py:L379-L396). People whose `confidence` is too low (e.g. 0.5) are excluded; the result is empty when all candidates are below threshold (tests/test_onboarding.py:L399-L410). The threshold lies above 0.5 and at/below 0.8 (0.8 candidate "Bob" is retained, 0.5 candidate is dropped) (tests/test_onboarding.py:L384-L385, L401, L410).

Error handling: any exception raised during scanning/detection is swallowed, and the function returns an empty list (tests/test_onboarding.py:L413-L416).

## `run_onboarding(directory, config_dir, auto_detect) -> registry`

Drives the full interactive flow by composing `_ask_mode`, `_ask_embedding_model`, `_ask_people`, `_ask_projects`, `_ask_wings`, `_yn`, and `_warn_ambiguous`. It returns an entity registry whose `people` contains the answered people and whose `projects` contains the answered projects (tests/test_onboarding.py:L422-L438). Ambiguous names are surfaced via `_warn_ambiguous` during the flow but do not block registration — an ambiguous name like "Grace" is still added to the registry (tests/test_onboarding.py:L441-L455).

Persistence of embedding model: `run_onboarding` always writes `config.json` into `config_dir` with an explicit `embedding_model` field reflecting the user's choice (tests/test_onboarding.py:L481-L505). When the user chooses multilingual, the field is `"embeddinggemma"`; a fresh config load reads back `embeddinggemma` (tests/test_onboarding.py:L489-L506). When the user opts down to English-only, the field is `"minilm"` and is still persisted explicitly (never silently relying on the hard default) (tests/test_onboarding.py:L509-L528).

## `_ask_embedding_model() -> str`

Asks whether to use the multilingual model, defaulting to multilingual. Empty input (just Enter) accepts the default and returns `"embeddinggemma"` (tests/test_onboarding.py:L461-L464). Explicit "y" returns `"embeddinggemma"` (tests/test_onboarding.py:L467-L469). Explicit "n" (opt down to English-only) returns `"minilm"` (tests/test_onboarding.py:L472-L475).

## Related contract: `MempalaceConfig.set_embedding_model`

Though defined in `mempalace.config`, the onboarding suite asserts: `set_embedding_model(value)` persists the value to `config.json`, and a freshly constructed `MempalaceConfig(config_dir=...)` reads it back (tests/test_onboarding.py:L562-L567). The stored value is normalized to lowercase — setting `"MiniLM"` reads back as `"minilm"` (tests/test_onboarding.py:L569-L570).

## Observable on-disk contract summary

Within `config_dir`, onboarding may produce: `entity_registry.json` (always on `quick_setup`) (tests/test_onboarding.py:L136); `aaak_entities.md` and `critical_facts.md` (on `_generate_aaak_bootstrap`) (tests/test_onboarding.py:L151-L152); and `config.json` with an `embedding_model` field (on `run_onboarding`, and on `quick_setup` only when the model is provided) (tests/test_onboarding.py:L502-L505, L544-L545, L556).
