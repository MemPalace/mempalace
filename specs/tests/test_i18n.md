# Spec: tests/test_i18n.py

Smoke-test suite asserting behavioral contracts of the i18n dictionary subsystem and its integration with the `Dialect` compression class (tests/test_i18n.py:L1-L4). All claims below are contracts the i18n module and `Dialect` must satisfy; the test file is the executable encoding of those contracts.

## Subjects under test (public surface relied upon)

- `available_languages()` → returns an enumerable collection of language codes (tests/test_i18n.py:L3, L12).
- `load_lang(lang)` → loads the dictionary for one language code and (as a side effect) sets module-level "current language" state used by `t()` (tests/test_i18n.py:L3, L16, L30, L82).
- `t(key, **vars)` → looks up a dotted dictionary key against the current language and interpolates named variables into the resulting string (tests/test_i18n.py:L3, L31, L76).
- `get_entity_patterns(langs_tuple)` → returns a merged dictionary of entity-detection pattern lists for the given language codes (tests/test_i18n.py:L93, L95).
- `_load_entity_section(lang)` → returns the raw per-language entity section dictionary (its input schema), or a falsy/empty value when none is declared (tests/test_i18n.py:L145, L148-L149).
- `Dialect(lang=...)` and `Dialect.from_config(path)` → compression class parameterized by language (tests/test_i18n.py:L4, L41, L63, L87).

## Language coverage contract

`available_languages()` MUST return at least 7 language codes (tests/test_i18n.py:L12-L13). The sample set exercised by compression names these codes explicitly: `en`, `fr`, `ko`, `ja`, `es`, `de`, `zh-CN`, `id`, `be` (tests/test_i18n.py:L50-L60).

## Dictionary structure contract (every language)

For every code returned by `available_languages()`, `load_lang(lang)` MUST succeed and return a dictionary containing top-level sections `terms`, `cli`, and `aaak` (tests/test_i18n.py:L9, L15-L18). The `terms` section MUST contain the keys `palace`, `wing`, `closet`, and `drawer`, and each of those term values MUST be a non-empty string (tests/test_i18n.py:L10, L19-L21). The `aaak` section MUST contain a key `instruction` (tests/test_i18n.py:L22).

## Interpolation contract

After `load_lang(lang)`, calling `t("cli.mine_complete", closets=5, drawers=100)` MUST return a string that contains both the substituted values `5` and `100` — i.e. both named variables are interpolated into the message (tests/test_i18n.py:L29-L33).

The Korean (`ko`) message `cli.status_drawers` MUST use the named variable `count` (not `drawers`): after `load_lang("ko")`, `t("cli.status_drawers", count=42)` MUST contain `42` (tests/test_i18n.py:L73-L77). This is a per-locale invariant guarding against a variable-name mismatch between the message template and the call site.

## Dialect language-loading contract

For every language code, constructing `Dialect(lang=lang)` MUST yield an object whose `lang` attribute equals the requested code, and whose `aaak_instruction` attribute is a string longer than 10 characters (tests/test_i18n.py:L40-L43).

## Dialect compression contract

For each sample text (one per language code in the sample set), `Dialect(lang=lang).compress(text)` MUST return a non-empty result (tests/test_i18n.py:L62-L65). The compressed output MUST be shorter than twice the input length — compression must not expand text beyond a 2x bound (tests/test_i18n.py:L66). Sample inputs and their language codes are fixed verbatim in the test (tests/test_i18n.py:L50-L60).

## Module-state isolation contract (`from_config`)

`Dialect.from_config(config_path)` MUST NOT inherit the module-level "current language" state that prior `load_lang(...)` calls set. Specifically: even after `load_lang("ko")` has polluted module state, loading a config file whose JSON content is `{"entities": {}}` (i.e. with no `lang` key) MUST produce a `Dialect` whose `lang` equals `"en"` (the English default), not the previously loaded language (tests/test_i18n.py:L80-L88). The config is read from a file path containing JSON (tests/test_i18n.py:L84-L87).

## Entity-pattern section contract (de / es / fr)

For each of German (`de`), Spanish (`es`), and French (`fr`), `get_entity_patterns((lang,))` MUST return a dictionary in which the following keys are all present and non-empty (truthy): `candidate_patterns`, `multi_word_patterns`, `person_verb_patterns`, `pronoun_patterns`, `dialogue_patterns`, `direct_address_patterns`, `project_verb_patterns` (tests/test_i18n.py:L95-L102, L110-L117, L125-L132). The merged-output key for direct address is the plural `direct_address_patterns` here (tests/test_i18n.py:L101, L116, L131). Additionally, the `stopwords` collection for each of these locales MUST contain more than 50 entries (tests/test_i18n.py:L103, L118, L133).

## Direct-address schema invariant (input vs output schema)

This is a cross-locale schema invariant on the *input* entity section files. The loader reads only the singular key `direct_address_pattern` (a string); the plural `direct_address_patterns` (a list) is the *output* schema of the merged dictionary, never the input schema. Declaring the plural form in a locale's input section silently drops every direct-address pattern for that locale after load (tests/test_i18n.py:L136-L144).

Concretely, for every language code: let `section = _load_entity_section(lang)`. If `section` is empty/falsy, the locale is skipped (tests/test_i18n.py:L147-L150). Otherwise the input section MUST NOT contain the plural key `direct_address_patterns` (tests/test_i18n.py:L151-L155). If it contains the singular key `direct_address_pattern`, that value MUST be a string (not a list or other type) and MUST be non-empty (tests/test_i18n.py:L156-L161).

## Observable side effects

- `load_lang(lang)` mutates module-level "current language" state consumed by later `t()` calls; tests both rely on this (tests/test_i18n.py:L30-L31, L76) and guard against it leaking into `Dialect.from_config` (tests/test_i18n.py:L82, L88).
- `Dialect.from_config` reads a JSON config file from a filesystem path (tests/test_i18n.py:L84-L87).
- Tests emit human-readable `PASS` / per-language progress lines to standard output; these are diagnostic only and not part of any machine contract (tests/test_i18n.py:L24, L35, L45, L67-L70).
