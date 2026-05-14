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


class TestLocaleTopicExtraction:
    def test_korean_extracts_hangul_topics(self):
        """ko.json declares a Hangul-aware topic_pattern and stop_words.
        Dialect(lang='ko') must honor both so Korean text yields Korean topics
        instead of falling back to English-borrowing-only output (which was
        the bug before locale regex was wired through _extract_topics)."""
        d = Dialect(lang="ko")
        topics = d._extract_topics(
            "Topic Clustering 파이프라인에서 cosine similarity를 Jaccard로 전환하기로 "
            "결정했습니다. 토큰 집합의 이산적인 특성 때문에 Jaccard가 더 적합하고, "
            "짧은 문서에 대한 cosine 결과는 너무 noisy했습니다."
        )
        # At least one Hangul token must surface — without locale support,
        # all topics would be English borrowings (jaccard, cosine, topic).
        assert any(
            any("가" <= c <= "힯" for c in t) for t in topics
        ), f"expected at least one Hangul topic, got {topics!r}"

    def test_korean_stop_words_are_filtered(self):
        """Locale stop_words from ko.json must be merged with English defaults.
        때문에 (because / due to) is declared in ko.json's stop_words list and
        must be filtered out of extracted topics even when frequent."""
        d = Dialect(lang="ko")
        topics = d._extract_topics("때문에 때문에 때문에 토큰 토큰")
        # 때문에 is in ko.json stop_words; 토큰 (token) is not.
        assert "때문에" not in topics
        assert "토큰" in topics

    def test_russian_extracts_cyrillic_topics(self):
        """Dialect(lang='ru') must surface Cyrillic words alongside Latin
        borrowings. Without ru.json's topic_pattern wired through, Russian
        text yields English-borrowing-only output."""
        d = Dialect(lang="ru")
        topics = d._extract_topics(
            "Я решил, что мы перейдём с cosine similarity на Jaccard в "
            "пайплайне topic-clustering. Дискретная природа множеств токенов "
            "делает Jaccard более подходящей метрикой."
        )
        # At least one Cyrillic token must surface — Cyrillic letters live in
        # the U+0400..U+04FF block (plus the Yo letter Ё / ё).
        assert any(
            any("Ѐ" <= c <= "ӿ" for c in t) for t in topics
        ), f"expected at least one Cyrillic topic, got {topics!r}"

    def test_russian_mid_sentence_lowercase_extracted(self):
        """ru.json's topic_pattern must match mid-sentence lowercase Cyrillic.
        Russian only capitalizes sentence-initial words and proper nouns —
        most content words appear lowercase mid-sentence, so a regex
        requiring an uppercase first char misses the bulk of the signal."""
        d = Dialect(lang="ru")
        topics = d._extract_topics("природа природа природа документов документов токенов")
        # All three Cyrillic content words are lowercase and non-trivial in
        # length. At least one must surface in topics; the bug pre-fix is
        # that the regex requires an uppercase first char and emits [].
        assert any(
            t in {"природа", "документов", "токенов"} for t in topics
        ), f"expected lowercase Cyrillic topic, got {topics!r}"

    def test_russian_stop_words_are_filtered(self):
        """Locale stop_words from ru.json must be merged with English defaults.
        'это' (this) is the first entry in ru.json's stop_words list and must
        be filtered even when it dominates by frequency."""
        d = Dialect(lang="ru")
        topics = d._extract_topics("это это это токен токен")
        # 'это' is in ru.json stop_words; 'токен' (token) is not.
        assert "это" not in topics
        assert "токен" in topics


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
