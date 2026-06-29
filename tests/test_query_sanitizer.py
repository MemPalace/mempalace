"""
Tests for query_sanitizer.py — system prompt contamination mitigation (#333).

Tests cover all 4 pipeline stages:
  Step 1: passthrough (short queries)
  Step 2: question extraction
  Step 3: tail sentence extraction
  Step 4: tail truncation (fallback)
"""

from mempalace.query_sanitizer import (
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
    SAFE_QUERY_LENGTH,
    sanitize_query,
)


class TestPassthrough:
    """Step 1: Queries under SAFE_QUERY_LENGTH pass through unchanged."""

    def test_short_query_unchanged(self):
        result = sanitize_query("What is Rust error handling?")
        assert result["clean_query"] == "What is Rust error handling?"
        assert result["was_sanitized"] is False
        assert result["method"] == "passthrough"

    def test_empty_query(self):
        result = sanitize_query("")
        assert result["clean_query"] == ""
        assert result["was_sanitized"] is False
        assert result["method"] == "passthrough"

    def test_none_query(self):
        result = sanitize_query(None)
        assert result["was_sanitized"] is False
        assert result["method"] == "passthrough"

    def test_exactly_safe_length(self):
        query = "a" * SAFE_QUERY_LENGTH
        result = sanitize_query(query)
        assert result["was_sanitized"] is False
        assert result["method"] == "passthrough"

    def test_one_over_safe_length_triggers_sanitization(self):
        query = "a" * (SAFE_QUERY_LENGTH + 1)
        result = sanitize_query(query)
        # Will go through sanitization pipeline (may or may not change the query)
        assert result["original_length"] == SAFE_QUERY_LENGTH + 1


class TestQuestionExtraction:
    """Step 2: Extract question sentences (ending with ?)."""

    def test_question_at_end_of_long_text(self):
        system_prompt = "You are a helpful assistant. " * 50  # ~1400 chars
        query = system_prompt + "What is the best way to handle errors in Rust?"
        result = sanitize_query(query)
        assert result["was_sanitized"] is True
        assert "error" in result["clean_query"].lower() or "Rust" in result["clean_query"]
        assert result["method"] == "question_extraction"

    def test_japanese_question_mark(self):
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "Rustのエラーハンドリング方法は？"
        result = sanitize_query(query)
        assert result["was_sanitized"] is True
        assert "Rust" in result["clean_query"] or "エラー" in result["clean_query"]
        assert result["method"] == "question_extraction"

    def test_multiple_questions_takes_last(self):
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "What is Python?\nHow does Rust handle errors?"
        result = sanitize_query(query)
        assert "Rust" in result["clean_query"] or "error" in result["clean_query"].lower()

    def test_question_in_system_prompt_ignored_when_real_question_exists(self):
        # System prompt contains a question, but real query also has one
        system_prompt = "Are you ready to help? " * 30 + "\n"
        real_query = "What databases does MemPalace support?"
        query = system_prompt + real_query
        result = sanitize_query(query)
        assert result["was_sanitized"] is True
        assert "MemPalace" in result["clean_query"] or "database" in result["clean_query"].lower()


class TestTailSentence:
    """Step 3: Extract the last meaningful sentence when no question mark found."""

    def test_command_style_query(self):
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "Show me all Rust error handling patterns"
        result = sanitize_query(query)
        assert result["was_sanitized"] is True
        assert "Rust" in result["clean_query"] or "error" in result["clean_query"].lower()
        assert result["method"] in ("tail_sentence", "question_extraction")

    def test_keyword_style_query(self):
        system_prompt = "System configuration loaded. " * 60
        query = system_prompt + "\nMemPalace ChromaDB integration setup"
        result = sanitize_query(query)
        assert result["was_sanitized"] is True
        assert "MemPalace" in result["clean_query"] or "ChromaDB" in result["clean_query"]

    def test_long_candidate_uses_last_sentence_fragment(self):
        query = ("Prompt sentence. " * 30) + "Final search intent for architecture migration"
        result = sanitize_query(query)
        assert result["method"] == "tail_sentence"
        assert result["clean_query"] == "Final search intent for architecture migration"

    def test_long_candidate_strips_wrapping_quotes(self):
        query = ("Prefix text " * 30) + '\n"' + ("x" * 260) + '"'
        result = sanitize_query(query)
        assert result["method"] == "tail_sentence"
        assert result["clean_query"] == "x" * MAX_QUERY_LENGTH
        assert not result["clean_query"].startswith('"')
        assert not result["clean_query"].endswith('"')
        assert len(result["clean_query"]) <= MAX_QUERY_LENGTH


class TestTailTruncation:
    """Step 4: Fallback — take the last MAX_QUERY_LENGTH characters."""

    def test_single_long_line_no_sentences(self):
        # Short lines only — no segment reaches MIN_QUERY_LENGTH; fallback truncates tail
        filler = "\n".join(["ab"] * 200)
        result = sanitize_query(filler)
        assert result["was_sanitized"] is True
        assert len(result["clean_query"]) <= MAX_QUERY_LENGTH
        assert result["method"] == "tail_truncation"

    def test_truncation_preserves_tail(self):
        filler = "x" * 1000 + "IMPORTANT_QUERY_CONTENT"
        result = sanitize_query(filler)
        assert "IMPORTANT_QUERY_CONTENT" in result["clean_query"]

    def test_tail_sentence_fallback_preserves_tail_without_delimiters(self):
        filler = ("x" * 260) + "IMPORTANT_QUERY_CONTENT"
        result = sanitize_query(filler)
        assert result["method"] == "tail_sentence"
        assert "IMPORTANT_QUERY_CONTENT" in result["clean_query"]


class TestLengthGuards:
    """Verify output length constraints."""

    def test_max_query_length_reduced(self):
        assert MAX_QUERY_LENGTH == 250

    def test_output_never_exceeds_max(self):
        # Very long question sentence
        long_question = "a" * 1000 + "?"
        system_prompt = "Context. " * 100
        query = system_prompt + long_question
        result = sanitize_query(query)
        assert len(result["clean_query"]) <= MAX_QUERY_LENGTH

    def test_extraction_too_short_falls_through(self):
        # Question mark found but the sentence is too short
        system_prompt = "You are helpful. " * 50
        query = system_prompt + "\nOK?"
        result = sanitize_query(query)
        # "OK?" is only 3 chars < MIN_QUERY_LENGTH, should fall through
        assert result["was_sanitized"] is True


class TestMetadata:
    """Verify sanitizer metadata is correct."""

    def test_original_length_preserved(self):
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "What is Rust?"
        result = sanitize_query(query)
        assert result["original_length"] == len(query.strip())

    def test_clean_length_matches_clean_query(self):
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "What is Rust?"
        result = sanitize_query(query)
        assert result["clean_length"] == len(result["clean_query"])

    def test_sanitized_flag_true_when_changed(self):
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "What is Rust?"
        result = sanitize_query(query)
        assert result["was_sanitized"] is True

    def test_sanitized_flag_false_when_unchanged(self):
        result = sanitize_query("Short query")
        assert result["was_sanitized"] is False


class TestRealWorldScenarios:
    """Simulate realistic system prompt contamination patterns."""

    def test_mempalace_wakeup_prepended(self):
        """Simulates mempalace wake-up output prepended to a query."""
        wakeup = (
            "MemPalace loaded. Wings: technical, emotions, identity. "
            "Rooms: chromadb-setup, error-handling, project-planning. "
            "Total drawers: 234. Knowledge graph: 89 entities, 156 triples. "
            "AAAK dialect active. Protocol: verify before responding. "
        ) * 5  # ~1000 chars
        real_query = "How did we decide on the database architecture?"
        query = wakeup + real_query
        result = sanitize_query(query)
        assert result["was_sanitized"] is True
        assert len(result["clean_query"]) <= MAX_QUERY_LENGTH
        # Should recover something meaningful
        assert len(result["clean_query"]) >= MIN_QUERY_LENGTH

    def test_memory_md_prepended(self):
        """Simulates MEMORY.md content prepended to a query."""
        memory_md = (
            "# Project Memory\n"
            "## Architecture Decisions\n"
            "- Use ChromaDB for vector storage\n"
            "- MCP protocol for tool integration\n"
            "- AAAK compression for efficient storage\n"
        ) * 10  # ~750 chars
        real_query = "What were the performance benchmarks for the search system?"
        query = memory_md + "\n" + real_query
        result = sanitize_query(query)
        assert result["was_sanitized"] is True
        assert result["method"] in ("question_extraction", "tail_sentence")

    def test_2000_char_system_prompt_with_question(self):
        """The exact scenario from Issue #333 — 2000 chars prepended."""
        system_prompt = "You are an AI assistant with access to tools. " * 45  # ~2000 chars
        real_query = "What is the status of the MemPalace project?"
        query = system_prompt + real_query
        result = sanitize_query(query)
        assert result["was_sanitized"] is True
        assert result["original_length"] > 2000
        assert result["clean_length"] <= MAX_QUERY_LENGTH
        assert result["method"] == "question_extraction"
