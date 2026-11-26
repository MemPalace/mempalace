"""Extra spellcheck tests covering _load_known_names and speller edge cases."""

from pathlib import Path
from unittest.mock import patch

from mempalace.entity_registry import EntityRegistry
from mempalace.spellcheck import (
    _load_known_names,
    spellcheck_user_text,
)


class TestLoadKnownNames:
    def test_returns_names_from_registry(self):
        data = EntityRegistry._empty()
        data["people"] = {
            "Riley": {"aliases": ["Ri"]},
            "Alice": {"canonical": "Alicia", "aliases": []},
        }
        reg = EntityRegistry(data, Path("unused"))
        with patch.object(EntityRegistry, "load", return_value=reg):
            names = _load_known_names()
            assert names == {"riley", "ri", "alice", "alicia"}

    def test_returns_empty_on_exception(self):
        with patch(
            "mempalace.entity_registry.EntityRegistry.load",
            side_effect=Exception("no registry"),
        ):
            names = _load_known_names()
            assert names == set()


class TestSpellerEdgeCases:
    def test_registered_name_not_corrected(self):
        data = EntityRegistry._empty()
        data["people"] = {"Riley": {"aliases": []}}
        reg = EntityRegistry(data, Path("unused"))

        def fake_speller(word):
            corrections = {"riley": "rile", "knoe": "know"}
            return corrections.get(word, word)

        with patch.object(EntityRegistry, "load", return_value=reg):
            with patch("mempalace.spellcheck._get_speller", return_value=fake_speller):
                with patch("mempalace.spellcheck._get_system_words", return_value=set()):
                    assert spellcheck_user_text("riley knoe") == "riley know"

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
