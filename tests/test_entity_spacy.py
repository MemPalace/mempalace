"""Tests for mempalace.entity_spacy — opt-in spaCy NER augmentation.

These tests use mocks for spaCy itself so they pass with or without the
``[nlp]`` extra installed and don't require any model download in CI.
End-to-end verification against the real spaCy + en_core_web_sm model
lives in the live-palace test, not here.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mempalace import entity_spacy


# ── helpers ─────────────────────────────────────────────────────────────


def _make_fake_ent(text: str, label: str) -> SimpleNamespace:
    """Minimal stand-in for a spaCy ``Span`` with .text and .label_ only."""
    return SimpleNamespace(text=text, label_=label)


def _make_fake_doc(ents: list[SimpleNamespace]) -> SimpleNamespace:
    """Minimal stand-in for a spaCy ``Doc`` exposing only .ents."""
    return SimpleNamespace(ents=ents)


def _make_fake_nlp(ents: list[SimpleNamespace]) -> MagicMock:
    """Callable that returns a fake Doc when invoked on text."""
    nlp = MagicMock()
    nlp.return_value = _make_fake_doc(ents)
    return nlp


@pytest.fixture(autouse=True)
def _clear_caches():
    """Drop the lru_caches between tests so env-var + model overrides take effect."""
    entity_spacy._get_spacy_nlp.cache_clear()
    entity_spacy._resolve_model_name.cache_clear()
    yield
    entity_spacy._get_spacy_nlp.cache_clear()
    entity_spacy._resolve_model_name.cache_clear()


# ── _spacy_available ────────────────────────────────────────────────────


def test_spacy_available_returns_false_when_import_fails():
    """If spaCy is not installed, _spacy_available must report False."""
    with patch.dict(sys.modules, {"spacy": None}):
        assert entity_spacy._spacy_available() is False


def test_spacy_available_returns_true_when_import_succeeds():
    """When spaCy is importable, _spacy_available must report True."""
    fake_spacy = MagicMock()
    with patch.dict(sys.modules, {"spacy": fake_spacy}):
        assert entity_spacy._spacy_available() is True


# ── extract_spacy_entities — graceful no-op ─────────────────────────────


def test_extract_returns_empty_when_spacy_missing():
    """ImportError on spaCy must yield an empty dict, NOT raise."""
    with patch.object(entity_spacy, "_spacy_available", return_value=False):
        assert entity_spacy.extract_spacy_entities("Aya met Igor in Paris.") == {}


def test_extract_returns_empty_for_empty_text():
    """Empty input yields empty output without loading the model."""
    assert entity_spacy.extract_spacy_entities("") == {}


# ── label filtering ─────────────────────────────────────────────────────


def test_only_keep_listed_ner_labels():
    """spaCy DATE / CARDINAL / ORDINAL labels must be filtered out.

    Only PERSON, ORG, GPE, PRODUCT, WORK_OF_ART, EVENT pass through.
    """
    fake_ents = [
        _make_fake_ent("Aya", "PERSON"),
        _make_fake_ent("Anthropic", "ORG"),
        _make_fake_ent("Paris", "GPE"),
        _make_fake_ent("2026-05-24", "DATE"),
        _make_fake_ent("three", "CARDINAL"),
        _make_fake_ent("first", "ORDINAL"),
    ]
    fake_nlp = _make_fake_nlp(fake_ents)
    with patch.object(entity_spacy, "_get_spacy_nlp", return_value=fake_nlp):
        result = entity_spacy.extract_spacy_entities("any text")

    assert "Aya" in result
    assert "Anthropic" in result
    assert "Paris" in result
    assert "2026-05-24" not in result
    assert "three" not in result
    assert "first" not in result


def test_kept_labels_include_product_and_work_of_art_and_event():
    """PRODUCT / WORK_OF_ART / EVENT are useful and must be kept."""
    fake_ents = [
        _make_fake_ent("iPhone", "PRODUCT"),
        _make_fake_ent("Mona Lisa", "WORK_OF_ART"),
        _make_fake_ent("World Cup", "EVENT"),
    ]
    fake_nlp = _make_fake_nlp(fake_ents)
    with patch.object(entity_spacy, "_get_spacy_nlp", return_value=fake_nlp):
        result = entity_spacy.extract_spacy_entities("any text")

    assert "iPhone" in result
    assert "Mona Lisa" in result
    assert "World Cup" in result


# ── counts + dedup ──────────────────────────────────────────────────────


def test_extract_counts_repeated_entities():
    """Same surface form mentioned twice must yield count of 2."""
    fake_ents = [
        _make_fake_ent("Aya", "PERSON"),
        _make_fake_ent("Aya", "PERSON"),
        _make_fake_ent("Igor", "PERSON"),
    ]
    fake_nlp = _make_fake_nlp(fake_ents)
    with patch.object(entity_spacy, "_get_spacy_nlp", return_value=fake_nlp):
        result = entity_spacy.extract_spacy_entities("any text")

    assert result["Aya"] == 2
    assert result["Igor"] == 1


def test_extract_strips_whitespace_in_entity_text():
    """spaCy occasionally emits ents with surrounding whitespace; strip them."""
    fake_ents = [
        _make_fake_ent("  Aya  ", "PERSON"),
        _make_fake_ent("\nIgor\t", "PERSON"),
    ]
    fake_nlp = _make_fake_nlp(fake_ents)
    with patch.object(entity_spacy, "_get_spacy_nlp", return_value=fake_nlp):
        result = entity_spacy.extract_spacy_entities("any text")

    assert "Aya" in result
    assert "Igor" in result


def test_extract_drops_empty_or_whitespace_only_entities():
    """An ent with empty or whitespace-only text must not appear in the result."""
    fake_ents = [
        _make_fake_ent("", "PERSON"),
        _make_fake_ent("   ", "ORG"),
        _make_fake_ent("Aya", "PERSON"),
    ]
    fake_nlp = _make_fake_nlp(fake_ents)
    with patch.object(entity_spacy, "_get_spacy_nlp", return_value=fake_nlp):
        result = entity_spacy.extract_spacy_entities("any text")

    assert list(result.keys()) == ["Aya"]


# ── MEMPALACE_SPACY_MODEL env var ───────────────────────────────────────


def test_unset_env_var_returns_empty_string(monkeypatch):
    """When MEMPALACE_SPACY_MODEL is unset, the resolver returns an empty
    string. The default model name is layered on at the call site, NOT
    silently inside the resolver, per project convention on env-var
    parsing (the caller can distinguish "user did not configure" from
    "user explicitly set the default")."""
    monkeypatch.delenv("MEMPALACE_SPACY_MODEL", raising=False)
    assert entity_spacy._resolve_model_name() == ""


def test_env_var_overrides_default_model(monkeypatch):
    """MEMPALACE_SPACY_MODEL must be returned verbatim (after strip)."""
    monkeypatch.setenv("MEMPALACE_SPACY_MODEL", "en_core_web_md")
    assert entity_spacy._resolve_model_name() == "en_core_web_md"


def test_env_var_whitespace_is_stripped(monkeypatch):
    """Surrounding whitespace in env var must not pass through to the
    model name."""
    monkeypatch.setenv("MEMPALACE_SPACY_MODEL", "  en_core_web_lg  \n")
    assert entity_spacy._resolve_model_name() == "en_core_web_lg"


def test_empty_env_var_returns_empty_string(monkeypatch):
    """An explicitly empty MEMPALACE_SPACY_MODEL returns "" — same as
    unset. The caller layers the default via ``... or _DEFAULT_MODEL``."""
    monkeypatch.setenv("MEMPALACE_SPACY_MODEL", "")
    assert entity_spacy._resolve_model_name() == ""


def test_caller_layers_default_when_resolver_returns_empty(monkeypatch):
    """When _resolve_model_name returns "", extract_spacy_entities must
    layer on _DEFAULT_MODEL at the call site, not crash with spacy.load("")."""
    monkeypatch.delenv("MEMPALACE_SPACY_MODEL", raising=False)
    fake_nlp = _make_fake_nlp([_make_fake_ent("Aya", "PERSON")])
    captured_model = {}

    def fake_get(model_name: str):
        captured_model["name"] = model_name
        return fake_nlp

    with patch.object(entity_spacy, "_get_spacy_nlp", side_effect=fake_get):
        entity_spacy.extract_spacy_entities("Aya was here.")

    assert captured_model["name"] == entity_spacy._DEFAULT_MODEL


# ── _get_spacy_nlp cache + lazy download ────────────────────────────────


def test_get_spacy_nlp_is_cached_per_model():
    """The lru_cache must skip a second spacy.load for the same model name."""
    fake_spacy = MagicMock()
    fake_nlp = MagicMock()
    fake_spacy.load.return_value = fake_nlp
    with patch.dict(sys.modules, {"spacy": fake_spacy}):
        first = entity_spacy._get_spacy_nlp("en_core_web_sm")
        second = entity_spacy._get_spacy_nlp("en_core_web_sm")

    assert first is fake_nlp
    assert second is fake_nlp
    # spacy.load called exactly once thanks to the cache
    assert fake_spacy.load.call_count == 1


def test_get_spacy_nlp_returns_none_on_value_error():
    """spacy.load can raise ValueError on an invalid model name. The
    module's design contract is 'never load-bearing,' so this MUST
    return None rather than propagate — the entity pipeline keeps
    working even when spaCy mis-configures."""
    fake_spacy = MagicMock()
    fake_spacy.load.side_effect = ValueError("invalid model name")
    with patch.dict(sys.modules, {"spacy": fake_spacy}):
        result = entity_spacy._get_spacy_nlp("not_a_real_model")
    assert result is None


def test_get_spacy_nlp_returns_none_on_import_error_from_model_package():
    """If a spaCy model package itself is broken (importable as a
    package but raises ImportError on load), the loader must return
    None rather than crash the caller."""
    fake_spacy = MagicMock()
    fake_spacy.load.side_effect = ImportError("broken model package")
    with patch.dict(sys.modules, {"spacy": fake_spacy}):
        result = entity_spacy._get_spacy_nlp("en_core_web_sm")
    assert result is None


def test_get_spacy_nlp_returns_none_on_arbitrary_exception():
    """Any other spaCy-internal failure must NOT crash the caller. The
    broad outer except is the design contract that says 'spaCy is opt-in,
    never load-bearing.'"""
    fake_spacy = MagicMock()
    fake_spacy.load.side_effect = RuntimeError("some spacy internal failure")
    with patch.dict(sys.modules, {"spacy": fake_spacy}):
        result = entity_spacy._get_spacy_nlp("en_core_web_sm")
    assert result is None


def test_get_spacy_nlp_downloads_when_model_missing():
    """When spacy.load raises OSError (model not installed), the loader
    must call spacy.cli.download once, then retry load."""
    fake_spacy = MagicMock()
    fake_nlp = MagicMock()
    # First load raises (model missing); after download, load succeeds.
    fake_spacy.load.side_effect = [OSError("model not found"), fake_nlp]
    fake_spacy.cli.download = MagicMock()
    with patch.dict(sys.modules, {"spacy": fake_spacy}):
        result = entity_spacy._get_spacy_nlp("en_core_web_sm")

    assert result is fake_nlp
    fake_spacy.cli.download.assert_called_once_with("en_core_web_sm")
    assert fake_spacy.load.call_count == 2


def test_get_spacy_nlp_returns_none_when_download_also_fails():
    """If the download itself raises, the loader must return None — never
    crash the caller. spaCy stays opt-in, never load-bearing."""
    fake_spacy = MagicMock()
    fake_spacy.load.side_effect = OSError("model not found")
    fake_spacy.cli.download.side_effect = Exception("network unreachable")
    with patch.dict(sys.modules, {"spacy": fake_spacy}):
        result = entity_spacy._get_spacy_nlp("en_core_web_sm")

    assert result is None


def test_get_spacy_nlp_returns_none_when_spacy_not_installed():
    """ImportError on spaCy itself must yield None, not raise."""
    with patch.dict(sys.modules, {"spacy": None}):
        assert entity_spacy._get_spacy_nlp("en_core_web_sm") is None


# ── hot-path: no compilation / import inside extract_spacy_entities ─────


def test_extract_does_not_call_spacy_load_per_call():
    """Once the model is cached, repeated extract_spacy_entities calls
    must NOT trigger spacy.load again (per the perf rule)."""
    fake_spacy = MagicMock()
    fake_nlp = _make_fake_nlp([_make_fake_ent("Aya", "PERSON")])
    fake_spacy.load.return_value = fake_nlp
    with patch.dict(sys.modules, {"spacy": fake_spacy}):
        # Warm the cache
        entity_spacy.extract_spacy_entities("Aya is here.")
        load_count_after_warm = fake_spacy.load.call_count
        for _ in range(50):
            entity_spacy.extract_spacy_entities("Aya is still here.")
        assert fake_spacy.load.call_count == load_count_after_warm
