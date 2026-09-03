"""Tests for mempalace.importance — keyword-based importance scorer."""

import pytest

from mempalace.importance import score_importance, CRITICAL_PATTERNS, IDENTITY_PATTERNS


# ── critical tier (5.0) ─────────────────────────────────────────────


def test_allergy_scores_critical():
    assert score_importance("I have a severe peanut allergy") == 5.0


def test_medication_scores_critical():
    assert score_importance("Takes insulin twice daily") == 5.0


def test_blood_type_scores_critical():
    assert score_importance("Blood type is O negative") == 5.0


def test_emergency_contact_scores_critical():
    assert score_importance("Emergency contact: Jane, 555-1234") == 5.0


def test_api_key_scores_critical():
    assert score_importance("The api key for the service is abc123") == 5.0


def test_password_scores_critical():
    assert score_importance("My password for the portal is hunter2") == 5.0


def test_epipen_scores_critical():
    assert score_importance("Always carry an epipen") == 5.0


# ── identity tier (4.0) ─────────────────────────────────────────────


def test_name_scores_identity():
    assert score_importance("My name is Radu") == 4.0


def test_work_scores_identity():
    assert score_importance("I work at RunPod") == 4.0


def test_birthday_scores_identity():
    assert score_importance("Born on March 15th 1990") == 4.0


def test_nationality_scores_identity():
    assert score_importance("Romanian nationality") == 4.0


def test_address_scores_identity():
    assert score_importance("I live in Coventry") == 4.0


# ── normal tier (3.0) ───────────────────────────────────────────────


def test_mundane_scores_normal():
    assert score_importance("Had coffee this morning, it was good") == 3.0


def test_weather_scores_normal():
    assert score_importance("The weather is nice today") == 3.0


def test_empty_scores_normal():
    assert score_importance("") == 3.0


def test_none_like_empty():
    """Empty string returns default, not crash."""
    assert score_importance("") == 3.0


# ── precedence: critical beats identity ──────────────────────────────


def test_critical_beats_identity():
    """If text matches both critical and identity, critical wins."""
    text = "My name is John and I have a severe allergy to shellfish"
    assert score_importance(text) == 5.0


# ── case insensitivity ──────────────────────────────────────────────


def test_case_insensitive_critical():
    assert score_importance("ALLERGIC to cats") == 5.0


def test_case_insensitive_identity():
    assert score_importance("MY NAME IS Alice") == 4.0
