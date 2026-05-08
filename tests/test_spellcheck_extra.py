"""Extra spellcheck tests covering _load_known_names and speller edge cases."""

from unittest.mock import patch, MagicMock

from mempalace.spellcheck import (
    _load_known_names,
    spellcheck_user_text,
)


class TestLoadKnownNames:
    def test_returns_names_from_registry(self):
        mock_reg = MagicMock()
        mock_reg._data = {
            "entities": {
                "e1": {"canonical": "Alice", "aliases": ["ali"]},
                "e2": {"canonical": "Bob", "aliases": []},
            }
        }
        with patch("mempalace.entity_registry.EntityRegistry") as MockER:
            MockER.load.return_value = mock_reg
            names = _load_known_names()
            assert "alice" in names
            assert "ali" in names
            assert "bob" in names

    def test_returns_empty_on_exception(self):
        with patch(
            "mempalace.entity_registry.EntityRegistry.load",
            side_effect=Exception("no registry"),
        ):
            names = _load_known_names()
            assert names == set()


class TestSpellerEdgeCases:
    def test_capitalized_word_skipped(self):
        """Capitalized words (likely proper nouns) are not corrected."""

        def fake_speller(word):
            return "WRONG"

        with patch("mempalace.spellcheck._get_speller", return_value=fake_speller):
            with patch("mempalace.spellcheck._get_system_words", return_value=set()):
                with patch("mempalace.spellcheck._load_known_names", return_value=set()):
                    result = spellcheck_user_text("Alice went home")
                    assert "Alice" in result
                    assert "WRONG" not in result

    def test_system_word_not_corrected(self):
        """Words in system dict should not be corrected."""

        def fake_speller(word):
            return "WRONG"

        with patch("mempalace.spellcheck._get_speller", return_value=fake_speller):
            with patch("mempalace.spellcheck._get_system_words", return_value={"coherently"}):
                with patch("mempalace.spellcheck._load_known_names", return_value=set()):
                    result = spellcheck_user_text("coherently")
                    assert "coherently" in result

    def test_high_edit_distance_rejected(self):
        """Corrections with too many edits are rejected."""

        def fake_speller(word):
            return "completely_different_word"

        with patch("mempalace.spellcheck._get_speller", return_value=fake_speller):
            with patch("mempalace.spellcheck._get_system_words", return_value=set()):
                with patch("mempalace.spellcheck._load_known_names", return_value=set()):
                    result = spellcheck_user_text("hello")
                    assert "hello" in result
