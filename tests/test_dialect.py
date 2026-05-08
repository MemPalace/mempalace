"""
test_dialect.py — Tests for the AAAK Dialect compression system.

Covers plain text compression, entity detection, emotion detection,
topic extraction, key sentence extraction, zettel encoding, and stats.
"""

from mempalace.dialect import Dialect


class TestPlainTextCompression:
    def test_compress_basic(self):
        d = Dialect()
        result = d.compress("We decided to use GraphQL instead of REST for the API layer.")
        assert isinstance(result, str)
        assert len(result) > 0
        # AAAK format uses pipe-separated fields
        assert "|" in result

    def test_compress_with_metadata(self):
        d = Dialect()
        result = d.compress(
            "Authentication now uses JWT tokens.",
            metadata={"wing": "project", "room": "backend", "source_file": "auth.py"},
        )
        assert "project" in result
        assert "backend" in result

    def test_compress_produces_entity_codes(self):
        d = Dialect(entities={"Alice": "ALC", "Bob": "BOB"})
        result = d.compress("Alice told Bob about the new deployment strategy.")
        assert "ALC" in result or "BOB" in result

    def test_compress_empty_text(self):
        d = Dialect()
        result = d.compress("")
        assert isinstance(result, str)


class TestEntityDetection:
    def test_known_entities(self):
        d = Dialect(entities={"Alice": "ALC"})
        found = d._detect_entities_in_text("Alice went to the store.")
        assert "ALC" in found

    def test_auto_code_unknown_entities(self):
        d = Dialect()
        found = d._detect_entities_in_text("I spoke with Bernardo about the project today.")
        assert any(code for code in found if len(code) == 3)

    def test_skip_names(self):
        d = Dialect(entities={"Gandalf": "GAN"}, skip_names=["Gandalf"])
        code = d.encode_entity("Gandalf")
        assert code is None


class TestEmotionDetection:
    def test_detect_emotions(self):
        d = Dialect()
        emotions = d._detect_emotions("I'm really excited and happy about this breakthrough!")
        assert len(emotions) > 0

    def test_max_three_emotions(self):
        d = Dialect()
        text = "I feel scared, happy, angry, surprised, disgusted, and confused."
        emotions = d._detect_emotions(text)
        assert len(emotions) <= 3


class TestTopicExtraction:
    def test_extract_topics(self):
        d = Dialect()
        topics = d._extract_topics(
            "The Python authentication server uses PostgreSQL for storage "
            "and Redis for caching sessions."
        )
        assert len(topics) > 0
        assert len(topics) <= 3

    def test_boosts_technical_terms(self):
        d = Dialect()
        topics = d._extract_topics("GraphQL vs REST: we chose GraphQL for the new API endpoint.")
        # "graphql" should appear since it's mentioned twice + capitalized
        topic_lower = [t.lower() for t in topics]
        assert "graphql" in topic_lower


class TestKeySentenceExtraction:
    def test_extract_key_sentence(self):
        d = Dialect()
        text = (
            "The server runs on port 3000. "
            "We decided to use PostgreSQL instead of MongoDB. "
            "The config file needs updating."
        )
        key = d._extract_key_sentence(text)
        assert "decided" in key.lower() or "instead" in key.lower()

    def test_truncates_long_sentences(self):
        d = Dialect()
        text = "a " * 100  # very long
        key = d._extract_key_sentence(text)
        assert len(key) <= 55


class TestCompressionStats:
    def test_stats(self):
        d = Dialect()
        original = "We decided to use GraphQL instead of REST. " * 10
        compressed = d.compress(original)
        stats = d.compression_stats(original, compressed)
        assert stats["size_ratio"] > 1
        assert stats["original_chars"] > stats["summary_chars"]

    def test_count_tokens(self):
        assert Dialect.count_tokens("hello world") == 2

    def test_compression_stats_keys(self):
        """Verify compression_stats() returns the expected key set."""
        d = Dialect()
        stats = d.compression_stats("hello world this is a test", "HW:test")
        expected_keys = {
            "original_chars",
            "summary_chars",
            "original_tokens_est",
            "summary_tokens_est",
            "size_ratio",
            "note",
        }
        assert set(stats.keys()) == expected_keys


class TestZettelEncoding:
    def test_encode_zettel(self):
        d = Dialect(entities={"Alice": "ALC"})
        zettel = {
            "id": "zettel-001",
            "people": ["Alice"],
            "topics": ["memory", "ai"],
            "content": 'She said "I want to remember everything"',
            "emotional_weight": 0.9,
            "emotional_tone": ["joy"],
            "origin_moment": False,
            "sensitivity": "",
            "notes": "",
            "origin_label": "",
            "title": "Test - Memory Discussion",
        }
        result = d.encode_zettel(zettel)
        assert "ALC" in result
        assert "memory" in result

    def test_encode_tunnel(self):
        d = Dialect()
        tunnel = {"from": "zettel-001", "to": "zettel-002", "label": "follows: temporal"}
        result = d.encode_tunnel(tunnel)
        assert "T:" in result
        assert "001" in result
        assert "002" in result


class TestDecode:
    def test_decode_roundtrip(self):
        d = Dialect()
        encoded = (
            '001|ALC+BOB|2025-01-01|test_title\nARC:journey\n001:ALC|memory_ai|"test quote"|0.9|joy'
        )
        decoded = d.decode(encoded)
        assert decoded["header"]["file"] == "001"
        assert decoded["arc"] == "journey"
        assert len(decoded["zettels"]) == 1
