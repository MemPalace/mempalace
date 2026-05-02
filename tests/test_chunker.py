"""Unit tests for the meaning-aware chunker.

Spec: a chunk should never start mid-word, code blocks are atomic, and
content dominated by tool-output noise (logs, ps, diffs, truncation
messages) is excluded from indexing.
"""

import pytest

from mempalace.chunker import (
    is_excluded_content,
    smart_split,
)


# ---------------------------------------------------------------------------
# is_excluded_content — the 5 contamination categories
# ---------------------------------------------------------------------------


class TestIsExcludedContent:
    def test_log_dump_excluded(self):
        text = "\n".join(
            f"2026-04-{(i % 28) + 1:02d} 12:42:22,856 [vestige.crystallization] INFO: Cycle {i} ran"
            for i in range(10)
        )
        assert is_excluded_content(text) is True

    def test_logger_format_excluded(self):
        text = "\n".join(
            f"INFO:vestige.recall:Surfaced memory {i} with priority 0.{i:02d}"
            for i in range(8)
        )
        assert is_excluded_content(text) is True

    def test_ps_aux_output_excluded(self):
        text = (
            "jameswinans  93662   5.4  0.5 508859040 323904   ??  S     3:44PM   0:12.34 python\n"
            "jameswinans  93663   2.1  0.2 408859040 123904   ??  S     3:44PM   0:05.21 node\n"
            "jameswinans  93664   1.8  0.1 308859040  63904   ??  S     3:44PM   0:02.10 ruby\n"
            "jameswinans  93665   0.9  0.1 208859040  33904   ??  S     3:44PM   0:01.05 zsh\n"
        )
        assert is_excluded_content(text) is True

    def test_line_numbered_diff_excluded(self):
        text = (
            "  920- # --- Metapath Association ---\n"
            "  921- try:\n"
            "  922:     from vestige.dream.metapath import run_metapath_association\n"
            "  923-\n"
            "  924-     graph_path = _resolve_graph_path()\n"
            "  925-     if graph_path:\n"
        )
        # All lines match the diff pattern (number + +/- prefix)
        assert is_excluded_content(text) is True

    def test_truncation_message_only_excluded(self):
        text = "[truncated, 4905 chars] → <persisted-output> Output too large (52.8KB)."
        assert is_excluded_content(text) is True

    def test_arrow_tool_output_excluded(self):
        # Single visual line with many arrow redirects — pasted shell
        # output that the previous chunker collapsed into one blob.
        text = (
            "→ moved to: ~/.tmp → re-checked → still missing → ran ls "
            "→ found in /tmp → reverted → re-applied → done"
        )
        assert is_excluded_content(text) is True

    def test_multi_line_arrow_listing_excluded(self):
        # Real shape from production palace: a file listing where
        # normalize.py prefixed each line with "→ ". Previously slipped
        # through because the only arrow-rule required <=3 lines.
        text = (
            "→ -rw-------@ 1 jameswinans  staff    3138404 Apr  2 19:11 a.jsonl\n"
            "→ -rw-------@ 1 jameswinans  staff    2626408 Apr  2 19:11 b.jsonl\n"
            "→ -rw-------@ 1 jameswinans  staff    1924120 Apr  2 19:11 c.jsonl\n"
            "→ drwx------@ 5 jameswinans  staff         160 Apr  1 20:21 d/\n"
            "→ drwx------@ 3 jameswinans  staff          96 Apr  2 11:31 e/\n"
        )
        assert is_excluded_content(text) is True

    def test_partial_arrow_content_kept(self):
        # If only a minority of lines are arrow-prefixed (e.g., a few
        # tool outputs embedded in a longer prose response), keep it.
        text = (
            "I checked the directory and found three relevant files. "
            "The listing shows:\n\n"
            "→ a.jsonl  3.1 MB\n"
            "→ b.jsonl  2.6 MB\n\n"
            "Of these, a.jsonl is the one I need to inspect because it "
            "contains the session that was active when the bug fired. "
            "The other two are older sessions that already wrapped."
        )
        assert is_excluded_content(text) is False

    # --- Negative cases: real prose with embedded noise should pass ---

    def test_prose_with_one_log_line_kept(self):
        text = (
            "I noticed the daemon was misbehaving. Looking at the logs:\n\n"
            "2026-04-17 12:42:22 [vestige.recall] INFO: surfaced 3 memories\n\n"
            "That's the only signal I needed. The recall pipeline is alive but "
            "the memories surfacing are the wrong ones — fragment-shaped and "
            "biased to one date. Time to re-design."
        )
        assert is_excluded_content(text) is False

    def test_prose_with_truncation_note_kept(self):
        text = (
            "The full output was too long [truncated, 200 chars] but the "
            "important part is that the consolidation produced 5 entities "
            "instead of the expected 1. That's the bug. The prompt is "
            "over-eager about what counts as a memory, and the LLM is "
            "happily emitting four near-duplicates per session."
        )
        assert is_excluded_content(text) is False

    def test_empty_excluded(self):
        assert is_excluded_content("") is True
        assert is_excluded_content("   \n\n  ") is True

    # --- JSON-blob and Claude Code session-log shapes ---

    def test_jsonl_session_log_excluded(self):
        # Each line is its own JSON object — the canonical Claude Code
        # session log shape. Should be dropped wholesale.
        text = (
            '{"type":"permission-mode","permissionMode":"default","sessionId":"abc"}\n'
            '{"type":"file-history-snapshot","messageId":"90a7","timestamp":"2026-04-02T23:12:57.330Z"}\n'
            '{"parentUuid":null,"isSidechain":false,"type":"user","message":{"role":"user"}}\n'
            '{"type":"assistant","uuid":"158113b8","timestamp":"2026-04-02T23:13:01.448Z"}\n'
        )
        assert is_excluded_content(text) is True

    def test_inline_json_blob_with_transcript_marker_excluded(self):
        # Real production failure shape: a single visual line with high
        # `","key":` density and embedded uuid/timestamp markers, taken
        # verbatim from the b5k5gj8gj.txt tool-results dump that
        # produced 82 mid-line drawers in the 2026-05-02 audit.
        text = (
            'andard","inference_geo":"not_available"}},"requestId":"req_011CZfntLLhXGsFZaH",'
            '"type":"assistant","uuid":"158113b8-5455-4668-8b94-356cd9523bbc",'
            '"timestamp":"2026-04-02T23:13:01.448Z","userType":"external","entrypoint":"cli",'
            '"cwd":"/Users/jameswinans/Documents/Temenos/Ves","sessionId":"1ada0a3a"}'
        )
        assert is_excluded_content(text) is True

    def test_long_quoted_tool_list_excluded(self):
        # The tool-name list shape: comma-separated quoted strings with
        # no transcript marker. High `","key":`-density alone is not
        # enough — but if there's a transcript marker, it should fire.
        # This case has a marker, so excluded.
        text = (
            '"tools":["mcp__library-rag__delete_file","mcp__library-rag__ingest_data",'
            '"mcp__library-rag__ingest_file","mcp__library-rag__list_files",'
            '"mcp__library-rag__query_documents","mcp__library-rag__status"],'
            '"sessionId":"abc","timestamp":"2026-04-02T23:13:01.448Z"'
        )
        assert is_excluded_content(text) is True

    def test_prose_mentioning_uuid_kept(self):
        # Prose that mentions a uuid or session id in passing must
        # survive — only blob density triggers exclusion.
        text = (
            "I traced the issue back to the session with sessionId "
            '"abc-123-def" — that one had a permissionMode mismatch. '
            "The fix is to normalize the JSON envelope before passing "
            "it to the next stage. We've seen this twice before in the "
            "April 17 logs and once in early May; the pattern is "
            "consistent enough that it warrants a regression test."
        )
        assert is_excluded_content(text) is False

    def test_prose_with_one_json_object_kept(self):
        # One inlined JSON example in otherwise prose content stays.
        text = (
            "The handler returns a payload like\n\n"
            '{"status":"ok","count":5,"latency_ms":42}\n\n'
            "which downstream services parse and route. The key thing "
            "to notice is the latency field — it lets us decide whether "
            "to retry on a slow path."
        )
        assert is_excluded_content(text) is False


# ---------------------------------------------------------------------------
# smart_split — boundary preservation, no mid-word cuts
# ---------------------------------------------------------------------------


def _all_chunks_clean_at_starts(chunks: list[str]) -> bool:
    """No chunk starts mid-word (lowercase first char with no leading
    punctuation that would be acceptable, like a quote or list marker).
    """
    for c in chunks:
        if not c:
            continue
        first = c.lstrip()[:1]
        # Acceptable starts: uppercase, digit, opening punctuation, common
        # Markdown markers (#, -, *, >, |, `).
        if first and first[0].islower():
            return False
    return True


def _all_chunks_clean_at_ends(chunks: list[str]) -> bool:
    """No chunk ends mid-word — last char is whitespace or
    sentence/punctuation, not a stranded letter.

    Allowed end chars: any punctuation, whitespace, digit (e.g. URLs
    ending in numbers), closing brackets, or markdown structure.
    """
    for c in chunks:
        if not c:
            continue
        # Strip trailing whitespace; check the last non-whitespace char
        stripped = c.rstrip()
        if not stripped:
            continue
        last = stripped[-1]
        # If the chunk ends with a letter, the *next* chunk must not
        # start with a continuation of that word. Best signal: the
        # original text had a boundary right after this chunk.
        # Simpler check: just allow any end — the key invariant is
        # that the *start* of the next chunk is clean (covered above).
        # So this helper is permissive.
        _ = last
    return True


class TestSmartSplit:
    def test_empty(self):
        assert smart_split("", 800, 1200) == []
        assert smart_split("   \n  ", 800, 1200) == []

    def test_short_under_ceiling_returned_whole(self):
        text = "Short content under the ceiling — no splitting needed."
        chunks = smart_split(text, 800, 1200)
        assert chunks == [text]

    def test_paragraph_boundary_preferred(self):
        # Two paragraphs, each ~500 chars. Target 800, ceiling 1200.
        # Total ~1000 → fits in one chunk. Force splitting by lowering target.
        para1 = "First paragraph. " * 30  # ~510 chars
        para2 = "Second paragraph. " * 30  # ~540 chars
        text = para1.strip() + "\n\n" + para2.strip()

        chunks = smart_split(text, target=400, ceiling=600)
        assert len(chunks) >= 2
        # First chunk should end at the paragraph boundary, not mid-word
        assert chunks[0].rstrip().endswith(".")
        assert _all_chunks_clean_at_starts(chunks)

    def test_no_mid_word_cuts_on_long_prose(self):
        # 3000 chars of prose with sentence boundaries every ~80 chars.
        sentences = [
            "This is sentence number {} of the test. ".format(i)
            for i in range(60)
        ]
        text = "".join(sentences)  # ~3000 chars

        chunks = smart_split(text, target=800, ceiling=1200)
        assert len(chunks) >= 2
        assert _all_chunks_clean_at_starts(chunks)
        # Every chunk should end with a sentence terminator (or be the last)
        for chunk in chunks[:-1]:
            assert chunk.rstrip().endswith((".", "!", "?")), (
                f"Chunk did not end at sentence: {chunk[-50:]!r}"
            )

    def test_no_loss_of_content(self):
        # Round-trip: concatenating all chunks must contain every word
        # from the source (modulo whitespace differences).
        text = "Word " * 500  # 2500 chars, all the same
        chunks = smart_split(text, target=800, ceiling=1200)
        # Total word count preserved
        total_words = sum(c.count("Word") for c in chunks)
        assert total_words == 500

    def test_code_block_atomic(self):
        # A code block longer than target+ceiling should be one chunk,
        # not split mid-line.
        prefix = "Here is a long code listing for the logger:\n\n"
        code_lines = [f"    logger.info('step {i} of the long process')" for i in range(40)]
        code = "```python\n" + "\n".join(code_lines) + "\n```\n"
        suffix = "\n\nThat is the listing. Continuing with the explanation."
        text = prefix + code + suffix

        chunks = smart_split(text, target=400, ceiling=800)
        # Find the chunk containing the code fence
        code_chunks = [c for c in chunks if "```" in c]
        assert len(code_chunks) >= 1
        # The code block should appear with its open and close fence in
        # the same chunk — no orphaned half-blocks
        for c in chunks:
            fence_count = c.count("```")
            assert fence_count % 2 == 0, (
                f"Chunk has unbalanced code fences: {c[:200]!r}"
            )

    def test_no_chunk_exceeds_ceiling_for_normal_prose(self):
        # Pathological prose: long lines, no good boundaries until the very end
        text = ("a " * 600).strip()  # 1199 chars, all single-word lines
        chunks = smart_split(text, target=800, ceiling=1000)
        # Each chunk except potentially an oversized code block must
        # be <= ceiling (allow some slack from boundary skip-chars)
        for c in chunks:
            assert len(c) <= 1010, f"Chunk exceeded ceiling: len={len(c)}"

    def test_minimal_target_ceiling_validation(self):
        with pytest.raises(ValueError):
            smart_split("anything", target=0, ceiling=100)
        with pytest.raises(ValueError):
            smart_split("anything", target=200, ceiling=100)

    def test_real_world_transcript_shape(self):
        """Regression: this mirrors the actual broken pattern observed
        in the live palace — a long AI response that previously got
        split mid-token. With the new chunker, no chunk should start
        mid-word.
        """
        text = (
            "I am going to walk through the diagnostic precision step by "
            "step. Lines 250-256 have the same event query bug we just "
            "fixed in devaluation — querying by raw object_summary instead "
            "of abstractBehaviorId, so content snippets fail to match. "
            "The fix is straightforward: replace the lookup key. "
            "Let me also check whether the metapath association code path "
            "has the same issue. From a quick read it does not, but I "
            "want to verify before claiming that. Running the tests now. "
            "All 301 register tests pass, which means the change is "
            "isolated to the recall path and does not break the steering "
            "telemetry. I will commit and ship this in a single PR. "
        ) * 3  # ~3500 chars

        chunks = smart_split(text, target=800, ceiling=1200)
        assert len(chunks) >= 2
        assert _all_chunks_clean_at_starts(chunks), (
            "Some chunk starts mid-word: "
            + str([c[:60] for c in chunks])
        )
