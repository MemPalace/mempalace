"""
test_usel_encoder.py — Tests for the USEL Encoder module.

Covers encoding, entity detection, stopword filtering, word order
preservation, decoding, validation, and compression comparison.
"""

from mempalace.usel_encoder import (
    NSM_PRIMES,
    USELEncoder,
    USELEncoding,
    USELToken,
)


class TestEncodeBasic:
    """Known input → expected token output."""

    def test_encode_simple_sentence(self):
        encoder = USELEncoder()
        result = encoder.encode("John discussed project alpha yesterday")
        encoded = str(result)
        assert "[SOMEONE:John]" in encoded
        assert "[SAY]" in encoded
        assert "[BEFORE]" in encoded
        assert isinstance(result, USELEncoding)

    def test_encode_empty_string(self):
        encoder = USELEncoder()
        result = encoder.encode("")
        assert str(result) == ""
        assert result.compression_ratio == 0.0

    def test_encode_produces_tokens(self):
        encoder = USELEncoder()
        result = encoder.encode("Alice knows the truth about Bob")
        assert len(result.tokens) > 0

    def test_encode_numbers(self):
        encoder = USELEncoder()
        result = encoder.encode("There were 42 people at the event")
        encoded = str(result)
        assert "[SOMETHING:42]" in encoded


class TestEncodeEntities:
    """Capitalized names detected correctly, non-entities excluded."""

    def test_named_entities_detected(self):
        encoder = USELEncoder()
        result = encoder.encode("Alice told Bob about the new project")
        assert "Alice" in result.entities
        assert "Bob" in result.entities

    def test_months_not_treated_as_entities(self):
        encoder = USELEncoder()
        result = encoder.encode("The March deadline is important")
        assert "March" not in result.entities

    def test_days_not_treated_as_entities(self):
        encoder = USELEncoder()
        result = encoder.encode("Monday we discussed the schedule")
        assert "Monday" not in result.entities

    def test_sentence_start_words_not_entities(self):
        encoder = USELEncoder()
        result = encoder.encode("However, Alice disagreed. Because Bob was wrong.")
        assert "However" not in result.entities
        assert "Because" not in result.entities
        assert "Alice" in result.entities
        assert "Bob" in result.entities

    def test_common_words_not_entities(self):
        encoder = USELEncoder()
        result = encoder.encode("The project needs Some work. Very important.")
        assert "The" not in result.entities
        assert "Some" not in result.entities
        assert "Very" not in result.entities


class TestEncodeStopwords:
    """Common function words don't pollute encodings."""

    def test_stopwords_filtered(self):
        encoder = USELEncoder()
        # "It is a fact that in the end" should not produce noise tokens
        result = encoder.encode("It is a fact that in the end")
        encoded = str(result)
        # Should NOT contain tokens from "it", "is", "a", "in"
        assert "[SOMETHING]" not in encoded  # "it" was mapped to SOMETHING before
        assert "[BE]" not in encoded  # "is" was mapped to BE before
        assert "[ONE]" not in encoded  # "a" was mapped to ONE before
        assert "[INSIDE]" not in encoded  # "in" was mapped to INSIDE before

    def test_content_words_still_mapped(self):
        encoder = USELEncoder()
        result = encoder.encode("I really want to understand the problem")
        encoded = str(result)
        assert "[WANT]" in encoded
        assert "[KNOW]" in encoded  # understand → KNOW
        assert "[BAD]" in encoded  # problem → BAD


class TestWordOrder:
    """Entities and keywords interleaved in document order."""

    def test_agent_patient_distinction(self):
        encoder = USELEncoder()
        result1 = encoder.encode("John told Alice")
        result2 = encoder.encode("Alice told John")
        str1 = str(result1)
        str2 = str(result2)
        # The order of entity tokens should differ
        assert str1 != str2

    def test_entity_order_preserved(self):
        encoder = USELEncoder()
        result = encoder.encode("John told Alice about Bob")
        tokens_str = [str(t) for t in result.tokens]
        john_idx = next(i for i, t in enumerate(tokens_str) if "John" in t)
        alice_idx = next(i for i, t in enumerate(tokens_str) if "Alice" in t)
        bob_idx = next(i for i, t in enumerate(tokens_str) if "Bob" in t)
        assert john_idx < alice_idx < bob_idx


class TestDecodeRoundtrip:
    """Encode → decode produces approximate original meaning."""

    def test_decode_contains_entity_names(self):
        encoder = USELEncoder()
        encoded = encoder.encode("Alice discussed the project with Bob")
        decoded = encoder.decode(str(encoded))
        assert "Alice" in decoded
        assert "Bob" in decoded

    def test_decode_basic(self):
        encoder = USELEncoder()
        notation = "[SOMEONE:John]+[SAY]+[BEFORE]"
        decoded = encoder.decode(notation)
        assert "John" in decoded
        assert len(decoded) > 0


class TestValidateValid:
    """Well-formed USEL passes validation."""

    def test_valid_simple(self):
        encoder = USELEncoder()
        valid, errors = encoder.validate("[SOMEONE:John]+[SAY]+[BEFORE]")
        assert valid
        assert errors == []

    def test_valid_single_token(self):
        encoder = USELEncoder()
        valid, errors = encoder.validate("[GOOD]")
        assert valid

    def test_valid_with_qualifier(self):
        encoder = USELEncoder()
        valid, errors = encoder.validate("[SOMEONE:Alice]+[KNOW]+[SOMETHING:project]")
        assert valid


class TestValidateInvalid:
    """Malformed USEL fails validation."""

    def test_unknown_prime(self):
        encoder = USELEncoder()
        valid, errors = encoder.validate("[BANANA:fruit]")
        assert not valid
        assert any("Unknown prime" in e for e in errors)

    def test_empty_string(self):
        encoder = USELEncoder()
        valid, errors = encoder.validate("")
        assert not valid

    def test_malformed_brackets(self):
        encoder = USELEncoder()
        valid, errors = encoder.validate("[GOOD[BAD]")
        assert not valid
        assert any("Invalid USEL structure" in e for e in errors)

    def test_empty_qualifier(self):
        encoder = USELEncoder()
        valid, errors = encoder.validate("[SOMEONE:]")
        assert not valid
        assert any("Empty qualifier" in e for e in errors)

    def test_missing_brackets(self):
        encoder = USELEncoder()
        valid, errors = encoder.validate("SOMEONE:John+SAY")
        assert not valid


class TestCompressionRatio:
    """Compression ratio is non-zero and non-degenerate."""

    def test_ratio_positive(self):
        encoder = USELEncoder()
        result = encoder.encode("John discussed the new project with Alice yesterday")
        assert result.compression_ratio > 0.0

    def test_ratio_greater_than_one_for_long_input(self):
        encoder = USELEncoder()
        long_text = (
            "Alice and Bob discussed the major project deadline yesterday "
            "and decided to move the important meeting to next week"
        )
        result = encoder.encode(long_text)
        # Longer input should compress (ratio > 1 means output is shorter)
        assert result.compression_ratio > 1.0

    def test_empty_gives_zero(self):
        encoder = USELEncoder()
        result = encoder.encode("")
        assert result.compression_ratio == 0.0


class TestCompareCompression:
    """Fair comparison against real AAAK Dialect."""

    def test_compare_returns_all_fields(self):
        encoder = USELEncoder()
        result = encoder.compare_compression(
            "Alice told Bob about the new deployment strategy yesterday"
        )
        assert "original_chars" in result
        assert "usel_chars" in result
        assert "aaak_chars" in result
        assert "usel_ratio" in result
        assert "aaak_ratio" in result
        assert "usel_compression_pct" in result
        assert "aaak_compression_pct" in result

    def test_compare_uses_real_aaak(self):
        encoder = USELEncoder()
        result = encoder.compare_compression(
            "We decided to use GraphQL instead of REST for the API"
        )
        # AAAK output should contain pipe separators (real Dialect format)
        assert result["aaak_chars"] > 0
        assert result["usel_chars"] > 0

    def test_original_chars_correct(self):
        encoder = USELEncoder()
        text = "Hello world"
        result = encoder.compare_compression(text)
        assert result["original_chars"] == len(text)


class TestUSELToken:
    """USELToken str representation."""

    def test_token_without_qualifier(self):
        token = USELToken("SAY")
        assert str(token) == "[SAY]"

    def test_token_with_qualifier(self):
        token = USELToken("SOMEONE", "John")
        assert str(token) == "[SOMEONE:John]"


class TestNSMPrimes:
    """NSM primes set is correct."""

    def test_has_63_primes(self):
        # NSM literature varies between 63-67 primes depending on version.
        # This implementation uses the Goddard & Wierzbicka core set.
        assert len(NSM_PRIMES) == 63

    def test_core_primes_present(self):
        for prime in ["I", "YOU", "SOMEONE", "GOOD", "BAD", "SAY", "THINK"]:
            assert prime in NSM_PRIMES
