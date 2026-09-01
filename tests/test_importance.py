"""Tests for the importance scoring module."""

from mempalace.importance import score_importance


def test_critical_allergy():
    assert score_importance("I am allergic to peanuts") == 5.0


def test_critical_medication():
    assert score_importance("My medication is metformin") == 5.0


def test_critical_api_key():
    assert score_importance("API key is sk-abc123") == 5.0


def test_critical_never_forget():
    assert score_importance("Never forget the deployment password") == 5.0


def test_critical_blood_type():
    assert score_importance("My blood type is O negative") == 5.0


def test_critical_life_threatening():
    assert score_importance("This is a life-threatening condition") == 5.0


def test_identity_name():
    assert score_importance("My name is Radu and I live in Coventry") == 4.0


def test_identity_work():
    assert score_importance("I work at a tech company") == 4.0


def test_identity_role():
    assert score_importance("My role is senior engineer") == 4.0


def test_normal_preference():
    assert score_importance("I like Ethiopian single origin coffee") == 3.0


def test_normal_weather():
    assert score_importance("The weather was nice today") == 3.0


def test_normal_discussion():
    assert score_importance("We discussed the project architecture") == 3.0


def test_empty_string():
    assert score_importance("") == 3.0


def test_none_like_empty():
    assert score_importance("") == 3.0


def test_critical_outranks_identity():
    """When text matches both critical and identity, critical wins."""
    assert score_importance("My name is allergic-reaction-prone") == 5.0
