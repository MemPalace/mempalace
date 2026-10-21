"""Regression tests for issue #927 — language code lookup must be case-insensitive.

The locale files use mixed case for the region subtag (``pt-br.json`` vs
``zh-CN.json``). BCP 47 tags are case-insensitive (RFC 5646 §2.1.1), so
``--lang PT-BR``, ``--lang zh-cn``, and ``--lang ZH-TW`` must all resolve
to the canonical file rather than silently falling back to English.
"""

import pytest

from mempalace import i18n
from mempalace.i18n import (
    _canonical_lang,
    _load_entity_section,
    available_languages,
    get_entity_patterns,
    load_lang,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset the module-level entity cache between tests."""
    i18n._entity_cache.clear()
    yield
    i18n._entity_cache.clear()


def test_canonical_lang_lowercase_passthrough():
    assert _canonical_lang("en") == "en"
    assert _canonical_lang("pt-br") == "pt-br"


def test_canonical_lang_uppercase_resolves():
    assert _canonical_lang("PT-BR") == "pt-br"
    assert _canonical_lang("ZH-CN") == "zh-CN"
    assert _canonical_lang("zh-cn") == "zh-CN"
    assert _canonical_lang("Pt-Br") == "pt-br"


def test_canonical_lang_unknown_returns_none():
    assert _canonical_lang("xx") is None
    assert _canonical_lang("") is None


def test_load_lang_case_insensitive():
    """`load_lang('PT-BR')` must load the pt-br dictionary, not English."""
    en = load_lang("en")
    pt_lower = load_lang("pt-br")
    pt_upper = load_lang("PT-BR")
    assert pt_lower == pt_upper, "case should not change the loaded dict"
    # If load_lang silently fell back to English, both would equal `en`.
    if "pt-br" in available_languages() and pt_lower != en:
        assert i18n.current_lang() == "pt-br"


def test_entity_section_loads_for_uppercase_input():
    """`_load_entity_section('PT-BR')` must read pt-br.json, not return {}."""
    pt_lower = _load_entity_section("pt-br")
    pt_upper = _load_entity_section("PT-BR")
    assert pt_lower == pt_upper


def test_get_entity_patterns_case_insensitive():
    """Entity patterns must be identical regardless of input case."""
    lower = get_entity_patterns(("pt-br",))
    upper = get_entity_patterns(("PT-BR",))
    assert lower == upper


def test_get_entity_patterns_shares_cache_across_cases():
    """Different casing must hit the same cache entry — not duplicate work."""
    get_entity_patterns(("zh-CN",))
    cache_keys = list(i18n._entity_cache.keys())
    get_entity_patterns(("ZH-CN",))
    get_entity_patterns(("zh-cn",))
    assert len(i18n._entity_cache) == len(cache_keys), (
        "different casings of the same language must not create new cache entries"
    )


def test_unknown_language_still_falls_back_to_english():
    """A code with no matching file must fall through to English (existing contract)."""
    patterns = get_entity_patterns(("xx-yy",))
    en = get_entity_patterns(("en",))
    assert patterns["candidate_patterns"] == en["candidate_patterns"]
