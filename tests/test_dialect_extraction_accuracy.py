"""test_dialect_extraction_accuracy.py — regression coverage for audit Group 8.

Two independent findings in mempalace/dialect.py:

  #53  encode_entity's fallback loop matched a registered name as a bare
       substring of the query (`"ann" in "annabelle"`), so a short registered
       name wrongly claimed a longer, unrelated name's code. The fix requires a
       whole-word match: "Alice" must still match "Alice Cooper", but "Ann"
       must NOT match "Annabelle".

  #50  Dialect loads per-locale regex via get_regex() into self.lang_regex but
       _extract_topics / _detect_entities_in_text / _extract_key_sentence never
       consult it, so non-English (German) content is run through ASCII-only
       English patterns: the tokenizer [a-zA-Z]... splits on umlauts, and the
       English decision-word list scores German text at ~0.
"""

import pytest

from mempalace import i18n
from mempalace.dialect import Dialect


@pytest.fixture(autouse=True)
def _restore_lang():
    """Restore the module-global current language after each test.

    Dialect(lang="de") mutates i18n's process-global _current_lang; without
    this, a later test that constructs Dialect() with no lang would inherit
    German and lose the English decision words (test-isolation leak).
    """
    saved = i18n.current_lang()
    yield
    i18n.load_lang(saved)


class TestEncodeEntityWholeWord:
    def test_short_name_does_not_match_inside_longer_word(self):
        """#53: 'Ann' must not claim 'Annabelle' via substring."""
        d = Dialect(entities={"Ann": "X99"})
        # Annabelle is a different person; must fall through to an auto-code,
        # not steal Ann's distinctive X99.
        assert d.encode_entity("Annabelle") != "X99", (
            "substring collision: 'Ann' wrongly matched inside 'Annabelle'"
        )

    def test_registered_name_still_matches_as_whole_word(self):
        """#53: legitimate whole-word match must survive the fix."""
        d = Dialect(entities={"Alice": "ALC"})
        assert d.encode_entity("Alice Cooper") == "ALC"
        assert d.encode_entity("Alice") == "ALC"


class TestGermanExtraction:
    def test_topic_tokenizer_keeps_umlaut_words_intact(self):
        """#50: German words with umlauts must not be split at the umlaut."""
        d = Dialect(lang="de")
        topics = d._extract_topics("Die Größe der Änderung war wichtig für Müller.")
        # Under the ASCII tokenizer, 'Größe' -> 'Gr'/'e', 'Änderung' -> 'nderung'.
        # A locale-aware tokenizer keeps them whole.
        assert not any(t in ("gr", "nderung", "berpr") for t in topics), (
            f"umlaut word was split by ASCII tokenizer: {topics!r}"
        )

    def test_german_decision_words_outrank_filler(self):
        """#50: German decision words must score, not just sentence length.

        The decision sentence is deliberately LONGER than the filler, so under
        the English-only decision-word list the short filler wins on the length
        bonus. Only if German decision words ('entschieden', 'weil', ...) score
        does the decision sentence win — so this genuinely fails pre-fix.
        """
        d = Dialect(lang="de")
        decision = (
            "Wir haben uns nach langer Diskussion letztlich für die zweite "
            "Variante entschieden weil sie deutlich sicherer war"
        )
        filler = "Es regnete gestern"
        key = d._extract_key_sentence(decision + ". " + filler + ".")
        # _extract_key_sentence truncates to ~55 chars, so assert on the sentence
        # START (proves the decision sentence was SELECTED over the shorter
        # filler), not on a trailing decision word that truncation would drop.
        assert key.startswith("Wir haben"), (
            f"German decision sentence lost to filler (no German scoring): {key!r}"
        )
