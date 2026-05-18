"""
test_general_extractor.py — Tests for the pure-heuristic memory extractor.

Covers all nine functions in general_extractor.py:
    _score_markers, _get_sentiment, _has_resolution, _disambiguate,
    _is_code_line, _extract_prose, _split_into_segments, _split_by_turns,
    extract_memories.

No external dependencies — all tests run offline without ChromaDB or an API key.
"""

import re

from mempalace.general_extractor import (
    _disambiguate,
    _extract_prose,
    _get_sentiment,
    _has_resolution,
    _is_code_line,
    _score_markers,
    _split_by_turns,
    _split_into_segments,
    extract_memories,
)


# ── _score_markers ────────────────────────────────────────────────────────────


class TestScoreMarkers:
    MARKERS = [r"\bbecause\b", r"\bwe decided\b", r"\barchitecture\b"]

    def test_no_match_returns_zero_score(self):
        score, keywords = _score_markers("unrelated text here", self.MARKERS)
        assert score == 0.0
        assert keywords == []

    def test_single_match_returns_score_one(self):
        score, _ = _score_markers("we decided to proceed", self.MARKERS)
        assert score == 1.0

    def test_multiple_matches_accumulate(self):
        score, _ = _score_markers("we decided because of the architecture", self.MARKERS)
        assert score == 3.0

    def test_repeated_match_counted_per_occurrence(self):
        score, _ = _score_markers("because X and because Y", self.MARKERS)
        assert score == 2.0

    def test_keywords_deduplicated(self):
        _, keywords = _score_markers("because X and because Y", self.MARKERS)
        assert len(keywords) == len(set(keywords))

    def test_empty_text_returns_zero(self):
        score, keywords = _score_markers("", self.MARKERS)
        assert score == 0.0
        assert keywords == []

    def test_case_insensitive_matching(self):
        score, _ = _score_markers("BECAUSE of this", self.MARKERS)
        assert score == 1.0


# ── _get_sentiment ────────────────────────────────────────────────────────────


class TestGetSentiment:
    def test_positive_words_return_positive(self):
        assert _get_sentiment("We are proud and grateful for this breakthrough") == "positive"

    def test_negative_words_return_negative(self):
        assert _get_sentiment("The bug caused a crash and everything is broken") == "negative"

    def test_equal_counts_return_neutral(self):
        # one positive, one negative
        assert _get_sentiment("fixed the bug") == "neutral"

    def test_empty_text_returns_neutral(self):
        assert _get_sentiment("") == "neutral"

    def test_no_sentiment_words_returns_neutral(self):
        assert _get_sentiment("the quick brown fox jumps") == "neutral"


# ── _has_resolution ───────────────────────────────────────────────────────────


class TestHasResolution:
    def test_fixed_is_resolved(self):
        assert _has_resolution("We fixed the authentication issue") is True

    def test_solved_is_resolved(self):
        assert _has_resolution("The team solved the race condition") is True

    def test_it_works_is_resolved(self):
        assert _has_resolution("After the patch, it works correctly") is True

    def test_got_it_working_is_resolved(self):
        assert _has_resolution("Finally got it working after hours of debugging") is True

    def test_pure_problem_is_not_resolved(self):
        assert _has_resolution("The bug keeps crashing production, still broken") is False

    def test_empty_text_is_not_resolved(self):
        assert _has_resolution("") is False


# ── _disambiguate ─────────────────────────────────────────────────────────────


class TestDisambiguate:
    def test_resolved_problem_becomes_milestone(self):
        result = _disambiguate("problem", "The bug was fixed and resolved", {"problem": 3})
        assert result == "milestone"

    def test_resolved_problem_with_positive_emotion_becomes_emotional(self):
        # resolved + emotional score + positive sentiment → emotional
        result = _disambiguate(
            "problem",
            "We finally fixed it, amazing and wonderful breakthrough",
            {"problem": 2, "emotional": 1},
        )
        assert result == "emotional"

    def test_unresolved_problem_stays_problem(self):
        result = _disambiguate("problem", "The bug keeps crashing everything", {"problem": 3})
        assert result == "problem"

    def test_problem_positive_sentiment_with_milestone_score_becomes_milestone(self):
        result = _disambiguate(
            "problem",
            "The amazing breakthrough solved everything beautifully",
            {"problem": 1, "milestone": 2},
        )
        assert result == "milestone"

    def test_non_problem_type_unchanged(self):
        result = _disambiguate("decision", "We decided to use PostgreSQL", {"decision": 2})
        assert result == "decision"

    def test_milestone_type_unchanged(self):
        result = _disambiguate("milestone", "Finally shipped the feature", {"milestone": 3})
        assert result == "milestone"


# ── _is_code_line ─────────────────────────────────────────────────────────────


class TestIsCodeLine:
    def test_shell_prompt_is_code(self):
        assert _is_code_line("$ git commit -m 'message'") is True

    def test_hash_comment_is_code(self):
        assert _is_code_line("# install deps") is True

    def test_pip_command_is_code(self):
        assert _is_code_line("pip install -e '.[dev]'") is True

    def test_import_statement_is_code(self):
        assert _is_code_line("import os") is True

    def test_class_definition_is_code(self):
        assert _is_code_line("class MyClass:") is True

    def test_function_definition_is_code(self):
        assert _is_code_line("def my_function():") is True

    def test_code_fence_is_code(self):
        assert _is_code_line("```python") is True

    def test_plain_prose_is_not_code(self):
        assert _is_code_line("We decided to use PostgreSQL for storage.") is False

    def test_empty_line_is_not_code(self):
        assert _is_code_line("") is False
        assert _is_code_line("   ") is False

    def test_low_alpha_ratio_long_line_is_code(self):
        # Short keys, many symbols — alpha ratio < 0.4 triggers code classification
        assert _is_code_line('{"k": 1, "v": 2, "x": 3, "y": 4, "z": 5, "w": 6}') is True


# ── _extract_prose ────────────────────────────────────────────────────────────


class TestExtractProse:
    def test_pure_prose_returned_unchanged(self):
        text = "We decided to use PostgreSQL.\nIt handles concurrent writes."
        result = _extract_prose(text)
        assert "We decided" in result
        assert "concurrent writes" in result

    def test_fenced_code_block_removed(self):
        text = "Here is the setup:\n```\npip install mempalace\n```\nThat installs it."
        result = _extract_prose(text)
        assert "pip install" not in result
        assert "Here is the setup" in result
        assert "That installs it" in result

    def test_shell_command_lines_filtered(self):
        text = "Run this:\ngit commit -m 'fix'\nThen push the changes."
        result = _extract_prose(text)
        assert "git commit" not in result
        assert "Then push" in result

    def test_all_code_falls_back_to_original(self):
        # Every line matches a code pattern → prose list is empty → original returned
        text = "import os\nfrom pathlib import Path\n$ git status"
        result = _extract_prose(text)
        assert result == text

    def test_mixed_text_keeps_prose_only(self):
        text = "We went with FastAPI because it's fast.\nimport fastapi\nThe decision was final."
        result = _extract_prose(text)
        assert "We went with FastAPI" in result
        assert "The decision was final" in result
        assert "import fastapi" not in result


# ── _split_into_segments ──────────────────────────────────────────────────────


class TestSplitIntoSegments:
    def test_paragraph_split_on_double_newline(self):
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        segments = _split_into_segments(text)
        assert len(segments) == 3
        assert segments[0] == "First paragraph."

    def test_speaker_turns_trigger_turn_split(self):
        text = (
            "> What is memory?\nMemory is persistence.\n"
            "> Why does it matter?\nIt enables continuity.\n"
            "> How do we build it?\nWith structured storage."
        )
        segments = _split_into_segments(text)
        # Each > turn becomes its own segment
        assert len(segments) >= 3

    def test_human_assistant_markers_trigger_turn_split(self):
        text = (
            "Human: What is MemPalace?\n"
            "Assistant: It is a local memory palace.\n"
            "Human: How does it store data?\n"
            "Assistant: Via ChromaDB.\n"
            "Human: Any API keys needed?\n"
            "Assistant: None required."
        )
        segments = _split_into_segments(text)
        assert len(segments) >= 4

    def test_single_giant_block_chunked_by_lines(self):
        # A block with many lines but no double newlines → chunks of 25
        lines = [f"Line {i}." for i in range(50)]
        text = "\n".join(lines)
        segments = _split_into_segments(text)
        assert len(segments) == 2  # 50 lines / 25 = 2 chunks


# ── _split_by_turns ───────────────────────────────────────────────────────────


class TestSplitByTurns:
    PATTERNS = [
        re.compile(r"^>\s"),
        re.compile(r"^(Human|User|Q)\s*:", re.I),
        re.compile(r"^(Assistant|AI|A|Claude)\s*:", re.I),
    ]

    def test_splits_at_each_turn_boundary(self):
        lines = [
            "> First question",
            "First answer here.",
            "> Second question",
            "Second answer here.",
        ]
        segments = _split_by_turns(lines, self.PATTERNS)
        assert len(segments) == 2

    def test_first_segment_has_no_predecessor(self):
        lines = ["> Hello", "Response here.", "> Follow-up", "Answer here."]
        segments = _split_by_turns(lines, self.PATTERNS)
        assert "> Hello" in segments[0]

    def test_content_within_turn_preserved(self):
        lines = ["> Question", "Line one.", "Line two.", "> Next"]
        segments = _split_by_turns(lines, self.PATTERNS)
        assert "Line one." in segments[0]
        assert "Line two." in segments[0]

    def test_empty_lines_returns_empty(self):
        segments = _split_by_turns([], self.PATTERNS)
        assert segments == []


# ── extract_memories ──────────────────────────────────────────────────────────


class TestExtractMemories:
    # Texts are crafted to hit ≥2 markers each so they clear the default
    # min_confidence=0.3 threshold without needing a length bonus.

    DECISION = (
        "We decided to use PostgreSQL over MySQL because it handles concurrent writes better. "
        "The architecture decision was driven by our scaling requirements."
    )
    PREFERENCE = (
        "I prefer snake_case for all variable names. "
        "We always use it here and never use camelCase conventions."
    )
    MILESTONE = (
        "Finally got it working after days of debugging. "
        "The authentication module is now deployed and we shipped the first release."
    )
    PROBLEM = (
        "The authentication bug keeps crashing production. "
        "The error is in the session handler and the whole system is broken."
    )
    EMOTIONAL = (
        "I love working on this project every day. "
        "I feel grateful for the wonderful support from the entire team."
    )

    def test_decision_text_classified_as_decision(self):
        memories = extract_memories(self.DECISION)
        assert any(m["memory_type"] == "decision" for m in memories)

    def test_preference_text_classified_as_preference(self):
        memories = extract_memories(self.PREFERENCE)
        assert any(m["memory_type"] == "preference" for m in memories)

    def test_milestone_text_classified_as_milestone(self):
        memories = extract_memories(self.MILESTONE)
        assert any(m["memory_type"] == "milestone" for m in memories)

    def test_problem_text_classified_as_problem(self):
        memories = extract_memories(self.PROBLEM)
        assert any(m["memory_type"] == "problem" for m in memories)

    def test_emotional_text_classified_as_emotional(self):
        memories = extract_memories(self.EMOTIONAL)
        assert any(m["memory_type"] == "emotional" for m in memories)

    def test_resolved_problem_reclassified_as_milestone(self):
        # This text scores highly on 'problem' but _disambiguate should flip it
        text = (
            "The authentication bug kept crashing production for two days. "
            "We finally solved the issue by patching the broken session handler. "
            "The fix resolved everything and the system is now stable."
        )
        memories = extract_memories(text)
        assert len(memories) >= 1
        # _disambiguate flips resolved problem → milestone
        assert any(m["memory_type"] == "milestone" for m in memories), (
            f"Expected resolved problem to become milestone, got: {[m['memory_type'] for m in memories]}"
        )

    def test_short_paragraphs_filtered_out(self):
        text = "Too short.\n\n" + self.DECISION
        memories = extract_memories(text)
        # "Too short." is 10 chars < 20, so filtered
        assert all(len(m["content"]) >= 20 for m in memories)

    def test_chunk_index_increments(self):
        text = self.DECISION + "\n\n" + self.PREFERENCE + "\n\n" + self.MILESTONE
        memories = extract_memories(text)
        if len(memories) >= 2:
            indices = [m["chunk_index"] for m in memories]
            assert indices == list(range(len(memories)))

    def test_memory_dicts_have_required_keys(self):
        memories = extract_memories(self.DECISION)
        assert len(memories) >= 1
        for m in memories:
            assert "content" in m
            assert "memory_type" in m
            assert "chunk_index" in m

    def test_empty_text_returns_empty_list(self):
        assert extract_memories("") == []

    def test_min_confidence_zero_includes_weak_matches(self):
        # A single-marker short text that wouldn't pass default threshold
        # Use a longer but weakly-matching text
        weak = "The architecture here is fairly straightforward to understand."
        memories_default = extract_memories(weak)
        memories_zero = extract_memories(weak, min_confidence=0.0)
        # Zero threshold should return at least as many results
        assert len(memories_zero) >= len(memories_default)

    def test_content_is_verbatim_original_paragraph(self):
        memories = extract_memories(self.DECISION)
        assert len(memories) >= 1
        assert memories[0]["content"] == self.DECISION.strip()
