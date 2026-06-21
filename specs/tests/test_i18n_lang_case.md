# Behavior Spec: `tests/test_i18n_lang_case.py`

Regression test suite for issue #927. It pins the contract that language-code lookups in the i18n subsystem are case-insensitive, because BCP 47 / RFC 5646 §2.1.1 tags are case-insensitive while the on-disk locale files use mixed case for the region subtag (`pt-br.json` vs `zh-CN.json`). Inputs such as `PT-BR`, `zh-cn`, and `ZH-TW` must resolve to the canonical file rather than silently falling back to English (tests/test_i18n_lang_case.py:L1-L7).

## System Under Test

The suite exercises the i18n module's public/internal surface: `_canonical_lang`, `_load_entity_section`, `available_languages`, `get_entity_patterns`, `load_lang`, plus the module attributes `i18n._entity_cache` and `i18n.current_lang()` (tests/test_i18n_lang_case.py:L11-L18). These names define the contract the implementation under test must expose: a canonicalization function, an entity-section loader, a language-list accessor, an entity-pattern accessor, a language loader, a mutable entity cache, and a current-language accessor.

## Test Fixture / State Reset Contract

An autouse fixture clears the module-level entity cache `i18n._entity_cache` both before yielding to each test and after it completes (tests/test_i18n_lang_case.py:L21-L26). This establishes the invariant that `_entity_cache` is a clearable container keyed per resolved language, and that each test observes a clean cache. The implementation must support clearing this cache as an observable operation.

## `_canonical_lang(code) -> str | None`

Lowercase canonical codes pass through unchanged: `_canonical_lang("en")` returns `"en"` and `_canonical_lang("pt-br")` returns `"pt-br"` (tests/test_i18n_lang_case.py:L29-L31).

Uppercase or mixed-case inputs resolve to the canonical on-disk casing: `_canonical_lang("PT-BR")` returns `"pt-br"`, `_canonical_lang("ZH-CN")` returns `"zh-CN"`, `_canonical_lang("zh-cn")` returns `"zh-CN"`, and `_canonical_lang("Pt-Br")` returns `"pt-br"` (tests/test_i18n_lang_case.py:L34-L38). The canonical form is therefore the exact casing of the corresponding locale file, not a uniformly lowercased string: region subtags that are stored uppercase on disk (`zh-CN`) are returned uppercase, while those stored lowercase (`pt-br`) are returned lowercase. Matching of the input against the canonical set is case-insensitive.

Unknown codes return null: `_canonical_lang("xx")` returns `None` and `_canonical_lang("")` (empty string) returns `None` (tests/test_i18n_lang_case.py:L41-L43). There is no fallback substitution at this layer; an unrecognized or empty code yields the null/absent result.

## `load_lang(code) -> dict`

`load_lang` returns a language dictionary and must be case-insensitive: `load_lang("pt-br")` and `load_lang("PT-BR")` return equal dictionaries — casing of the argument must not change the loaded dict (tests/test_i18n_lang_case.py:L49-L51). The test also loads English via `load_lang("en")` as a reference for distinguishing a real load from a silent English fallback (tests/test_i18n_lang_case.py:L49,L52).

Conditional invariant on current language: if `"pt-br"` is present in `available_languages()` and the loaded pt-br dictionary differs from the English dictionary, then after the load `i18n.current_lang()` must equal `"pt-br"` (tests/test_i18n_lang_case.py:L53-L54). This pins that a successful `load_lang` updates an observable current-language state to the canonical code, and that when a real (non-English) locale exists it is actually loaded rather than falling back to English. `available_languages()` returns a collection that supports membership testing of canonical lowercase codes such as `"pt-br"` (tests/test_i18n_lang_case.py:L53).

## `_load_entity_section(code) -> dict`

`_load_entity_section` reads the entity section for the given language and is case-insensitive: `_load_entity_section("pt-br")` and `_load_entity_section("PT-BR")` return equal results (tests/test_i18n_lang_case.py:L59-L61). An uppercase input must read the `pt-br.json` entity data rather than returning an empty dictionary (tests/test_i18n_lang_case.py:L57-L61), i.e. case must not cause the entity section to be missing.

## `get_entity_patterns(langs: tuple) -> dict`

`get_entity_patterns` accepts a tuple of language codes and returns a patterns dictionary. It is case-insensitive in its result: `get_entity_patterns(("pt-br",))` equals `get_entity_patterns(("PT-BR",))` (tests/test_i18n_lang_case.py:L66-L68).

Cache-sharing invariant: different casings of the same language must hit the same cache entry and not duplicate work. After calling `get_entity_patterns(("zh-CN",))`, snapshotting the current cache keys, and then calling `get_entity_patterns(("ZH-CN",))` and `get_entity_patterns(("zh-cn",))`, the size of `i18n._entity_cache` must remain unchanged from the snapshot — different casings of the same language must not create new cache entries (tests/test_i18n_lang_case.py:L73-L79). This means the cache is keyed by the canonical resolved language, not by the raw input casing.

English fallback for unknown codes (existing contract): a code tuple with no matching locale file falls through to English. `get_entity_patterns(("xx-yy",))` produces a result whose `candidate_patterns` field equals the `candidate_patterns` of `get_entity_patterns(("en",))` (tests/test_i18n_lang_case.py:L84-L86). The returned patterns dictionary therefore contains a `candidate_patterns` key, and an unrecognized language yields the English patterns rather than an error or empty result.

## Observable Contracts Summary

- Canonical codes preserve the on-disk file casing (`pt-br` lowercase, `zh-CN` uppercase region) (tests/test_i18n_lang_case.py:L34-L38).
- Unknown/empty language codes canonicalize to null at `_canonical_lang`, but cause English fallback at `get_entity_patterns` (tests/test_i18n_lang_case.py:L41-L43,L84-L86).
- The entity pattern cache `i18n._entity_cache` is keyed by canonical language so casing variants share entries (tests/test_i18n_lang_case.py:L71-L79).
- `load_lang` updates the observable `current_lang()` to the canonical code on a successful non-fallback load (tests/test_i18n_lang_case.py:L53-L54).
- The entity patterns result exposes a `candidate_patterns` key (tests/test_i18n_lang_case.py:L86).
