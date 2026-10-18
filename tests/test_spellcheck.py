"""Tests for mempalace.spellcheck — spell-correction utilities."""

from unittest.mock import patch

from mempalace.spellcheck import (
    _edit_distance,
    _get_system_words,
    _should_skip,
    spellcheck_user_text,
)

# --- _should_skip ---


class TestShouldSkip:
    """Token-level skip logic."""

    def test_short_tokens_skipped(self):
        assert _should_skip("hi", set()) is True
        assert _should_skip("ok", set()) is True
        assert _should_skip("I", set()) is True

    def test_digits_skipped(self):
        assert _should_skip("3am", set()) is True
        assert _should_skip("top10", set()) is True
        assert _should_skip("bge-large-v1.5", set()) is True

    def test_camelcase_skipped(self):
        assert _should_skip("ChromaDB", set()) is True
        assert _should_skip("MemPalace", set()) is True

    def test_allcaps_skipped(self):
        assert _should_skip("NDCG", set()) is True
        assert _should_skip("MAX_RESULTS", set()) is True

    def test_technical_skipped(self):
        assert _should_skip("bge-large", set()) is True
        assert _should_skip("train_test", set()) is True

    def test_url_skipped(self):
        assert _should_skip("https://example.com", set()) is True
        assert _should_skip("www.google.com", set()) is True

    def test_code_or_emoji_skipped(self):
        assert _should_skip("`code`", set()) is True
        assert _should_skip("**bold**", set()) is True

    def test_known_name_skipped(self):
        assert _should_skip("mempalace", {"mempalace"}) is True

    def test_normal_word_not_skipped(self):
        assert _should_skip("hello", set()) is False
        assert _should_skip("question", set()) is False


# --- _edit_distance ---


class TestEditDistance:
    def test_identical(self):
        assert _edit_distance("hello", "hello") == 0

    def test_empty_strings(self):
        assert _edit_distance("", "abc") == 3
        assert _edit_distance("abc", "") == 3
        assert _edit_distance("", "") == 0

    def test_single_edit(self):
        assert _edit_distance("cat", "bat") == 1  # substitution
        assert _edit_distance("cat", "cats") == 1  # insertion
        assert _edit_distance("cats", "cat") == 1  # deletion

    def test_known_distance(self):
        assert _edit_distance("kitten", "sitting") == 3


# --- _get_system_words ---


def test_get_system_words_returns_set():
    result = _get_system_words()
    assert isinstance(result, set)


# --- spellcheck_user_text ---


def test_spellcheck_user_text_passthrough_no_autocorrect():
    """When autocorrect is not installed, text passes through unchanged."""
    with patch("mempalace.spellcheck._get_speller", return_value=None):
        text = "somee misspeledd textt"
        assert spellcheck_user_text(text) == text


def test_spellcheck_user_text_with_speller():
    """When a speller is available, it corrects words."""

    def fake_speller(word):
        corrections = {"knoe": "know", "befor": "before"}
        return corrections.get(word, word)

    with patch("mempalace.spellcheck._get_speller", return_value=fake_speller):
        with patch("mempalace.spellcheck._get_system_words", return_value=set()):
            with patch("mempalace.spellcheck._load_known_names", return_value=set()):
                result = spellcheck_user_text("knoe the question befor")
                assert "know" in result
                assert "before" in result


def test_spellcheck_preserves_technical_terms():
    """Technical terms should never be touched even with a speller."""

    def fake_speller(word):
        return "WRONG"

    with patch("mempalace.spellcheck._get_speller", return_value=fake_speller):
        with patch("mempalace.spellcheck._get_system_words", return_value=set()):
            result = spellcheck_user_text("ChromaDB bge-large", known_names=set())
            assert "ChromaDB" in result
            assert "bge-large" in result
            assert "WRONG" not in result
