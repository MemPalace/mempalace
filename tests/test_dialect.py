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


class TestExpand:
    def test_expand_basic(self):
        d = Dialect(entities={"Alice": "ALC", "Bob": "BOB"})
        aaak = '001|ALC+BOB|2025-01-01|test_title\n001:ALC+BOB|memory_ai|"we decided to use GraphQL"|determ'
        expanded = d.expand(aaak)
        assert "Alice" in expanded or "ALC" in expanded
        assert "memory" in expanded or "ai" in expanded
        assert "we decided to use GraphQL" in expanded

    def test_expand_preserves_entities(self):
        d = Dialect(entities={"Alice": "ALC"})
        aaak = '001|ALC|2025-01-01|meeting\n001:ALC|project_auth|"switched to JWT"|determ+convict'
        expanded = d.expand(aaak)
        assert "Alice" in expanded

    def test_expand_preserves_key_sentence(self):
        d = Dialect()
        aaak = 'project|backend|2025-01-01|auth\n0:???|jwt_tokens|"Tokens expire after 24 hours"'
        expanded = d.expand(aaak)
        assert "Tokens expire after 24 hours" in expanded

    def test_expand_handles_emotions(self):
        d = Dialect()
        aaak = '001|ALC|2025-01-01|talk\n001:ALC|life|"a turning point"|hope+joy'
        expanded = d.expand(aaak)
        assert "hope" in expanded
        assert "joy" in expanded

    def test_expand_empty_input(self):
        d = Dialect()
        expanded = d.expand("")
        assert isinstance(expanded, str)

    def test_expand_uppercase_topic_not_treated_as_emotion(self):
        """Topics like TCP+UDP should not be misclassified as combined emotions."""
        d = Dialect()
        aaak = '001|SRV|2025-01-01|networking\n001:SRV|TCP+UDP|"packet routing"'
        expanded = d.expand(aaak)
        # TCP+UDP should be treated as topic, not emotions
        assert "tcp" in expanded.lower() or "udp" in expanded.lower()

    def test_expand_protocol_names_with_plus(self):
        """Common tech protocol names with + should not be misclassified.

        Regression test suggested by @web3guru888 — protocol names like
        HTTPS+REST and TCP+UDP are common in tech docs and must not be
        treated as combined emotion codes.
        """
        d = Dialect()
        for protocol in ("HTTPS+REST", "TCP+UDP", "HTTP+JSON", "GRPC+PROTOBUF"):
            aaak = f'001|API|2025-01-01|stack\n001:API|{protocol}|"design notes"'
            expanded = d.expand(aaak)
            # Each protocol part should appear in the expanded output
            for part in protocol.split("+"):
                assert part.lower() in expanded.lower(), (
                    f"Protocol part {part!r} missing from expansion of {protocol!r}: {expanded!r}"
                )

    def test_expand_roundtrip_from_compress(self):
        """Compress text then expand — key terms should survive."""
        d = Dialect(entities={"Alice": "ALC"})
        original = "Alice decided to switch from REST to GraphQL for the API layer."
        compressed = d.compress(original)
        expanded = d.expand(compressed)
        # The key concept should survive the round-trip
        assert "graphql" in expanded.lower() or "api" in expanded.lower()


class TestLooksLikeAaak:
    def test_positive_plain_compress(self):
        d = Dialect()
        compressed = d.compress(
            "We decided to use PostgreSQL for the database.",
            metadata={"wing": "project", "room": "backend"},
        )
        assert d.looks_like_aaak(compressed)

    def test_positive_zettel_format(self):
        aaak = '001|ALC|2025-01-01|title\n001:ALC|topic|"quote"|0.9|joy'
        assert Dialect.looks_like_aaak(aaak)

    def test_negative_plain_text(self):
        assert not Dialect.looks_like_aaak("This is just plain English text.")

    def test_negative_empty(self):
        assert not Dialect.looks_like_aaak("")

    def test_negative_pipe_but_no_colon_digit(self):
        assert not Dialect.looks_like_aaak("wing|room|date|file")
