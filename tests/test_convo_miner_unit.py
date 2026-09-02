"""Unit tests for convo_miner pure functions (no chromadb needed)."""

import contextlib
import io
import os
import sys
import threading

import pytest

from mempalace.convo_miner import (
    CHUNK_SIZE,
    _emit_bounded,
    _extract_authored_at,
    _file_chunks_locked,
    _source_file_delete_ids,
    _mine_print,
    chunk_exchanges,
    detect_convo_room,
    mine_output_streams,
    scan_convos,
)


def test_mine_output_streams_are_thread_local(capsys):
    output = io.StringIO()
    errors = io.StringIO()

    with mine_output_streams(output, errors):
        _mine_print("mine-progress")
        _mine_print("mine-error", file=sys.stderr)
        peer = threading.Thread(target=lambda: _mine_print("peer-progress"))
        peer.start()
        peer.join(timeout=2)

    captured = capsys.readouterr()
    assert output.getvalue() == "mine-progress\n"
    assert errors.getvalue() == "mine-error\n"
    assert captured.out == "peer-progress\n"
    assert captured.err == ""


class TestChunkExchanges:
    def test_exchange_chunking(self):
        content = (
            "> What is memory?\n"
            "Memory is persistence of information over time.\n\n"
            "> Why does it matter?\n"
            "It enables continuity across sessions and conversations.\n\n"
            "> How do we build it?\n"
            "With structured storage and retrieval mechanisms.\n"
        )
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 2
        assert all("content" in c and "chunk_index" in c for c in chunks)

    def test_paragraph_fallback(self):
        """Content without '>' lines falls back to paragraph chunking."""
        content = (
            "This is a long paragraph about memory systems. " * 10 + "\n\n"
            "This is another paragraph about storage. " * 10 + "\n\n"
            "And a third paragraph about retrieval. " * 10
        )
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 2

    def test_paragraph_line_group_fallback(self):
        """Long content with no paragraph breaks chunks by line groups.

        Each emitted drawer must respect CHUNK_SIZE. Before #1534 the
        fallback chunker emitted one drawer per 25-line group without
        a size cap, so a 25-line group of long lines produced an
        oversized drawer that crashed embedding upsert.
        """
        lines = [f"Line {i}: some content that is meaningful" for i in range(60)]
        content = "\n".join(lines)
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 1
        max_len = max(len(c["content"]) for c in chunks)
        assert max_len <= CHUNK_SIZE, f"oversized chunk: max_len={max_len}"

    def test_line_group_fallback_drops_sub_min_trailing_group(self):
        """A trailing line-group whose stripped length is at or below
        MIN_CHUNK_SIZE must be dropped, not emitted as a tiny drawer."""
        lines = [f"Line {i}" for i in range(51)]
        content = "\n".join(lines)
        chunks = chunk_exchanges(content)
        from mempalace.convo_miner import MIN_CHUNK_SIZE

        assert len(chunks) == 2, (
            f"expected 2 drawers (groups 0-24 and 25-49); got {len(chunks)}; "
            f"the single-line tail group should drop below MIN_CHUNK_SIZE={MIN_CHUNK_SIZE}"
        )

    def test_empty_content(self):
        chunks = chunk_exchanges("")
        assert chunks == []

    def test_short_content_skipped(self):
        chunks = chunk_exchanges("> hi\nbye")
        # Too short to produce chunks (below MIN_CHUNK_SIZE)
        assert isinstance(chunks, list)

    def test_chunk_size_zero_raises_valueerror(self):
        """Reject chunk_size == 0 explicitly.

        Without this guard, `_chunk_by_exchange` enters an infinite loop:
        content[:0] is empty, content[0:] is the whole string, and the
        remainder never shrinks.
        """
        content = (
            "> What is memory?\nMemory is persistence.\n\n" * 4  # force the split branch
        )
        with pytest.raises(ValueError, match="chunk_size must be > 0"):
            chunk_exchanges(content, chunk_size=0)

    def test_chunk_size_negative_raises_valueerror(self):
        """Reject chunk_size < 0. Negative slicing would also loop forever
        (content[:-1] → all but last, remainder[-1:] → last char repeated)."""
        content = "> hi\nsome response text here that is long enough to chunk\n\n" * 4
        with pytest.raises(ValueError, match="chunk_size must be > 0"):
            chunk_exchanges(content, chunk_size=-10)

    def test_min_chunk_size_negative_raises_valueerror(self):
        """Reject min_chunk_size < 0. A negative threshold silently
        breaks the `if len(part.strip()) > min_chunk_size` gate — every
        chunk including empty ones gets appended."""
        with pytest.raises(ValueError, match="min_chunk_size must be >= 0"):
            chunk_exchanges("> hi\nbye", min_chunk_size=-1)

    def test_min_chunk_size_zero_allowed(self):
        """min_chunk_size == 0 is legal — means 'accept any non-empty chunk'."""
        content = "> What is memory?\nMemory is persistence of information.\n" * 3
        chunks = chunk_exchanges(content, min_chunk_size=0)
        assert isinstance(chunks, list)

    def test_long_ai_response_not_truncated(self):
        """AI responses longer than 8 lines must be stored in full (verbatim principle)."""
        lines = [f"Step {i}: important detail that must be stored" for i in range(1, 14)]
        content = "> How do I implement authentication?\n" + "\n".join(lines)
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 1
        stored = chunks[0]["content"]
        # All 13 lines must be present — none silently dropped
        for i in range(1, 14):
            assert f"Step {i}:" in stored, f"Step {i} was truncated and not stored"

    def test_paragraph_loop_enforces_chunk_size(self):
        """A paragraph longer than CHUNK_SIZE must split into multiple
        bounded drawers. Regression for #1534: the paragraph loop in
        ``_chunk_by_paragraph`` used to append each paragraph as a
        single drawer regardless of size, producing one giant chunk
        that crashed embedding upsert with
        ``RuntimeError: Invalid buffer size: ... GiB``.
        """
        big_para = "x" * 5000
        tail = "small paragraph tail of meaningful length"
        content = big_para + "\n\n" + tail
        chunks = chunk_exchanges(content)
        max_len = max(len(c["content"]) for c in chunks)
        assert max_len <= CHUNK_SIZE, f"oversized chunk: max_len={max_len}"
        assert len(chunks) > 1, "5000-char content should produce multiple drawers"
        assert chunks[-1]["content"] == tail, (
            "trailing paragraph must be preserved as the last drawer"
        )

    def test_custom_chunk_size_propagates_to_paragraph_path(self):
        """User-supplied chunk_size must govern the paragraph chunker, not
        only the exchange chunker. Confirms config plumbing reaches both
        paths after the #1534 fix.
        """
        big_para = "y" * 3000
        content = big_para + "\n\ntail paragraph of meaningful length"
        chunks = chunk_exchanges(content, chunk_size=400)
        max_len = max(len(c["content"]) for c in chunks)
        assert max_len <= 400, f"oversized chunk under custom chunk_size=400: {max_len}"

    def test_paragraph_loop_no_content_loss(self):
        """Verbatim principle: every char of a single long paragraph lands
        in some drawer in order. The slicing helper must not drop or
        reorder content."""
        content = "a" * 5000
        chunks = chunk_exchanges(content)
        joined = "".join(c["content"] for c in chunks)
        assert joined == content

    def test_chunk_exactly_at_size_boundary(self):
        """Content length == CHUNK_SIZE produces exactly one drawer of CHUNK_SIZE."""
        content = "z" * CHUNK_SIZE
        chunks = chunk_exchanges(content)
        assert len(chunks) == 1
        assert len(chunks[0]["content"]) == CHUNK_SIZE

    def test_chunk_many_multiples_of_size(self):
        """Content length == 8 * CHUNK_SIZE produces exactly 8 drawers, each
        of length CHUNK_SIZE."""
        content = "w" * (8 * CHUNK_SIZE)
        chunks = chunk_exchanges(content)
        assert len(chunks) == 8
        assert all(len(c["content"]) == CHUNK_SIZE for c in chunks)

    def test_paragraph_loop_preserves_slice_order(self):
        """Slices must appear in source order. Guards against a future
        regression where the helper reverses, shuffles, or duplicates
        slices — the verbatim invariant in CLAUDE.md depends on order
        as well as content."""
        content = "a" * CHUNK_SIZE + "b" * CHUNK_SIZE + "c" * CHUNK_SIZE
        chunks = chunk_exchanges(content)
        assert len(chunks) == 3
        assert chunks[0]["content"] == "a" * CHUNK_SIZE
        assert chunks[1]["content"] == "b" * CHUNK_SIZE
        assert chunks[2]["content"] == "c" * CHUNK_SIZE

    def test_ai_response_preserves_blank_lines(self):
        """Blank lines inside an AI response must survive ingestion (verbatim principle).

        A response with paragraph breaks separates distinct ideas; collapsing the
        blank lines loses that boundary and fuses unrelated content.
        """
        # Three `>` turns route through _chunk_by_exchange (the exchange-pair path).
        content = (
            "> explain the architecture\n"
            "First paragraph introducing the system.\n"
            "\n"
            "Second paragraph about the data layer.\n"
            "\n"
            "Third paragraph about retrieval.\n"
            "\n"
            "> what about caching?\n"
            "Cache lives in memory and is invalidated on write.\n"
            "\n"
            "> and persistence?\n"
            "Persistence lives on disk via SQLite and Chroma.\n"
        )
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 1
        stored = "\n".join(c["content"] for c in chunks)
        # Paragraph break between the three bodies must survive as `\n\n`.
        assert "First paragraph introducing the system.\n\nSecond paragraph" in stored
        assert "Second paragraph about the data layer.\n\nThird paragraph" in stored

    def test_ai_response_preserves_line_structure(self):
        """Line-oriented content (lists, code fences, tables) must keep newlines.

        Joining lines with a single space fuses structurally distinct tokens,
        breaks downstream search, and destroys code blocks.
        """
        content = (
            "> show me the steps\n"
            "1. First step\n"
            "2. Second step\n"
            "3. Third step\n"
            "```python\n"
            "def hello():\n"
            "    return 'world'\n"
            "```\n"
            "\n"
            "> what next?\n"
            "Run the test suite.\n"
            "\n"
            "> anything else?\n"
            "Ship the feature.\n"
        )
        chunks = chunk_exchanges(content)
        assert len(chunks) >= 1
        stored = "\n".join(c["content"] for c in chunks)
        # Each list item keeps its own line (not "1. First step 2. Second step").
        assert "1. First step\n2. Second step\n3. Third step" in stored
        # Code fence survives intact, with indentation preserved.
        assert "```python\ndef hello():\n    return 'world'\n```" in stored


class TestEmitBounded:
    """Direct unit tests for the chunk-size-enforcement helper."""

    def test_emits_no_oversized_chunks(self):
        chunks = []
        _emit_bounded(chunks, "abc" * 20, chunk_size=10, min_chunk_size=0)
        assert all(len(c["content"]) <= 10 for c in chunks)

    def test_assigns_sequential_chunk_indices(self):
        chunks = []
        _emit_bounded(chunks, "x" * 25, chunk_size=10, min_chunk_size=0)
        assert [c["chunk_index"] for c in chunks] == [0, 1, 2]

    def test_continues_existing_chunk_index(self):
        chunks = [{"content": "pre-existing entry", "chunk_index": 0}]
        _emit_bounded(chunks, "y" * 5, chunk_size=10, min_chunk_size=0)
        assert len(chunks) == 2
        assert chunks[1]["chunk_index"] == 1

    def test_empty_content_noop(self):
        chunks = []
        _emit_bounded(chunks, "", chunk_size=10, min_chunk_size=0)
        assert chunks == []

    def test_small_trailing_slice_preserved(self):
        """Once the whole content passes the floor, every slice is emitted
        verbatim so small trailing remainders are not silently dropped.
        Regression test for the data-loss class flagged on PR #1538."""
        chunks = []
        _emit_bounded(chunks, "z" * 23, chunk_size=10, min_chunk_size=5)
        assert len(chunks) == 3
        assert [len(c["content"]) for c in chunks] == [10, 10, 3]
        assert "".join(c["content"] for c in chunks) == "z" * 23

    def test_trailing_whitespace_slice_preserved_when_whole_passes(self):
        """When the whole content passes the floor, a trailing
        whitespace-only slice is preserved verbatim rather than dropped.
        The floor is a noise filter on the WHOLE input, not a per-slice gate."""
        chunks = []
        _emit_bounded(chunks, "a" * 10 + " " * 10, chunk_size=10, min_chunk_size=5)
        assert len(chunks) == 2
        assert chunks[0]["content"] == "a" * 10
        assert chunks[1]["content"] == " " * 10

    def test_whole_content_below_floor_dropped(self):
        """The floor is applied to the stripped whole content. An all-whitespace
        input (stripped length 0) or a too-short input is dropped without slicing."""
        chunks = []
        _emit_bounded(chunks, " " * 100, chunk_size=10, min_chunk_size=5)
        _emit_bounded(chunks, "ab", chunk_size=10, min_chunk_size=5)
        assert chunks == []

    def test_split_805_chars_at_chunk_size_800_preserves_tail(self):
        """805 chars at chunk_size=800 produces a 5-char tail. With the
        whole-content floor (not per-slice), the 5-char tail is preserved
        verbatim. Directly addresses the data-loss scenario raised on PR #1538."""
        chunks = []
        _emit_bounded(chunks, "y" * 805, chunk_size=800, min_chunk_size=30)
        assert len(chunks) == 2
        assert chunks[0]["content"] == "y" * 800
        assert chunks[1]["content"] == "y" * 5
        assert "".join(c["content"] for c in chunks) == "y" * 805


class TestDetectConvoRoom:
    def test_technical_room(self):
        content = "Let me debug this python function and fix the code error in the api"
        assert detect_convo_room(content) == "technical"

    def test_planning_room(self):
        content = "We need to plan the roadmap for the next sprint and set milestone deadlines"
        assert detect_convo_room(content) == "planning"

    def test_architecture_room(self):
        content = "The architecture uses a service layer with component interface and module design"
        assert detect_convo_room(content) == "architecture"

    def test_decisions_room(self):
        content = "We decided to switch and migrated to the new framework after we chose it"
        assert detect_convo_room(content) == "decisions"

    def test_general_fallback(self):
        content = "Hello, how are you doing today? The weather is nice."
        assert detect_convo_room(content) == "general"


class TestScanConvos:
    def test_scan_finds_txt_and_md(self, tmp_path):
        (tmp_path / "chat.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "notes.md").write_text("world", encoding="utf-8")
        (tmp_path / "image.png").write_bytes(b"fake")
        files = scan_convos(str(tmp_path))
        extensions = {f.suffix for f in files}
        assert ".txt" in extensions
        assert ".md" in extensions
        assert ".png" not in extensions

    def test_scan_skips_git_dir(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config.txt").write_text("git stuff", encoding="utf-8")
        (tmp_path / "chat.txt").write_text("hello", encoding="utf-8")
        files = scan_convos(str(tmp_path))
        assert len(files) == 1

    def test_scan_skips_meta_json(self, tmp_path):
        (tmp_path / "chat.meta.json").write_text("{}", encoding="utf-8")
        (tmp_path / "chat.json").write_text("{}", encoding="utf-8")
        files = scan_convos(str(tmp_path))
        names = [f.name for f in files]
        assert "chat.json" in names
        assert "chat.meta.json" not in names

    def test_scan_empty_dir(self, tmp_path):
        files = scan_convos(str(tmp_path))
        assert files == []

    def test_scan_skips_tool_results_dirs(self, tmp_path):
        # Claude Code pages large tool outputs to <session>/tool-results/*.txt
        # inside ~/.claude/projects/<slug>/. These are raw machine dumps
        # referenced from the transcript JSONL, not conversations — mining
        # them floods the palace (12.8k drawers measured in the field, one
        # single file produced 3.6k). The scanner must not descend into them.
        session_dir = tmp_path / "1234-5678-session"
        tool_results = session_dir / "tool-results"
        tool_results.mkdir(parents=True)
        (tool_results / "bipc8jdx0.txt").write_text("raw tool dump " * 100, encoding="utf-8")
        (tmp_path / "session.jsonl").write_text('{"type": "user"}', encoding="utf-8")
        files = scan_convos(str(tmp_path))
        names = [f.name for f in files]
        assert "session.jsonl" in names
        assert "bipc8jdx0.txt" not in names

    def test_scan_keeps_regular_nested_dirs(self, tmp_path):
        # The tool-results skip must not turn into a blanket nested-dir skip.
        nested = tmp_path / "archive"
        nested.mkdir()
        (nested / "old-chat.md").write_text("> q\na\n> q2\na2\n> q3\na3", encoding="utf-8")
        files = scan_convos(str(tmp_path))
        assert [f.name for f in files] == ["old-chat.md"]

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlink creation requires elevated privileges on Windows",
    )
    def test_scan_convos_logs_skipped_symlinks(self, tmp_path, capsys):
        real_target = tmp_path / "outside" / "real.jsonl"
        real_target.parent.mkdir()
        real_target.write_text('{"role":"user","content":"hi"}\n', encoding="utf-8")
        link_root = tmp_path / "link_root"
        link_root.mkdir()
        (link_root / "link.jsonl").symlink_to(real_target)
        (link_root / "regular.jsonl").write_text(
            '{"role":"user","content":"hello"}\n', encoding="utf-8"
        )

        files = scan_convos(str(link_root))

        names = {f.name for f in files}
        assert "link.jsonl" not in names
        assert "regular.jsonl" in names
        err = capsys.readouterr().err
        assert err.count("SKIP:") == 1
        assert "  SKIP:" in err
        assert "link.jsonl" in err
        assert "(symlink)" in err

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlink creation requires elevated privileges on Windows",
    )
    def test_scan_convos_logs_dangling_symlink(self, tmp_path, capsys):
        real_target = tmp_path / "outside" / "ghost.jsonl"
        real_target.parent.mkdir()
        real_target.touch()
        link_root = tmp_path / "link_root"
        link_root.mkdir()
        (link_root / "dangling.jsonl").symlink_to(real_target)
        real_target.unlink()  # target deleted, link dangles

        files = scan_convos(str(link_root))

        assert files == []
        err = capsys.readouterr().err
        assert err.count("SKIP:") == 1
        assert "dangling.jsonl" in err
        assert "(symlink)" in err

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="symlink creation requires elevated privileges on Windows",
    )
    def test_scan_convos_logs_nested_symlink_with_relative_path(self, tmp_path, capsys):
        real_target = tmp_path / "outside" / "real.jsonl"
        real_target.parent.mkdir()
        real_target.write_text('{"x":1}\n', encoding="utf-8")
        link_root = tmp_path / "link_root"
        subdir = link_root / "deep" / "subdir"
        subdir.mkdir(parents=True)
        (subdir / "nested.jsonl").symlink_to(real_target)

        files = scan_convos(str(link_root))

        assert files == []
        err = capsys.readouterr().err
        # Forward slash even on Windows (as_posix) and full relative path,
        # not just the leaf — proves relative_to(convo_path) over .name.
        assert "deep/subdir/nested.jsonl" in err
        assert "(symlink)" in err

    def test_scan_skips_oversized_files(self, tmp_path, capsys, monkeypatch):
        import re

        import mempalace.convo_miner as convo_mod

        monkeypatch.setattr(convo_mod, "MAX_FILE_SIZE", 100)

        (tmp_path / "small.txt").write_text("hello " * 5, encoding="utf-8")
        (tmp_path / "big.txt").write_text("hello " * 100, encoding="utf-8")

        files = scan_convos(str(tmp_path))
        names = [f.name for f in files]
        assert "small.txt" in names
        assert "big.txt" not in names

        err = capsys.readouterr().err
        # SKIP message goes to stderr, matching the existing
        # `SKIP: <rel> (symlink)` line in the same function.
        assert "SKIP: big.txt" in err
        # Validate the full template so a drop of the MB suffix or a
        # regression to bare-substring output trips the test.
        assert re.search(r"SKIP: big\.txt \(\d+\.\d+ MB\) exceeds \d+ MB limit", err), err

    def test_scan_skips_unreadable_files(self, tmp_path, capsys, monkeypatch):
        from pathlib import Path

        # .txt is in CONVO_EXTENSIONS so it reaches the size-check gate.
        (tmp_path / "readable.txt").write_text("hi", encoding="utf-8")
        unreadable = tmp_path / "unreadable.txt"
        unreadable.write_text("hi", encoding="utf-8")

        real_stat = Path.stat

        def selective_stat(self, *args, **kwargs):
            # On Py 3.10+, Path.is_symlink() routes through lstat ->
            # stat(follow_symlinks=False). Only raise for the follow-symlinks
            # call that the actual size-check makes, otherwise the test
            # never reaches the size-check arm we want to exercise.
            if self.name == "unreadable.txt" and kwargs.get("follow_symlinks", True):
                raise PermissionError(13, "Permission denied", str(self))
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", selective_stat)

        files = scan_convos(str(tmp_path))
        names = [f.name for f in files]
        assert "readable.txt" in names
        assert "unreadable.txt" not in names

        err = capsys.readouterr().err
        assert "SKIP: unreadable.txt" in err
        assert "stat error" in err

    def test_scan_skips_subagent_dirs_by_default(self, tmp_path):
        # Mimic Claude Code layout: ~/.claude/projects/<slug>/<session>/subagents/agent-*.jsonl
        session_dir = tmp_path / "session-abc"
        session_dir.mkdir()
        (session_dir / "main.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
        subagents_dir = session_dir / "subagents"
        subagents_dir.mkdir()
        (subagents_dir / "agent-abc.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
        (subagents_dir / "agent-def.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")

        files = scan_convos(str(tmp_path))
        names = [f.name for f in files]

        assert "main.jsonl" in names
        assert "agent-abc.jsonl" not in names
        assert "agent-def.jsonl" not in names

    def test_scan_includes_subagent_dirs_when_opted_in(self, tmp_path):
        session_dir = tmp_path / "session-abc"
        session_dir.mkdir()
        (session_dir / "main.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
        subagents_dir = session_dir / "subagents"
        subagents_dir.mkdir()
        (subagents_dir / "agent-abc.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")

        files = scan_convos(str(tmp_path), include_subagents=True)
        names = [f.name for f in files]

        assert "main.jsonl" in names
        assert "agent-abc.jsonl" in names

    def test_scan_skips_subagent_dirs_at_any_depth(self, tmp_path):
        # The "subagents" name match is by directory name, not by depth: verify
        # both shallow (top-level) and nested subagents/ get skipped.
        (tmp_path / "subagents").mkdir()
        (tmp_path / "subagents" / "agent-top.jsonl").write_text("{}", encoding="utf-8")
        nested = tmp_path / "session" / "subagents"
        nested.mkdir(parents=True)
        (nested / "agent-deep.jsonl").write_text("{}", encoding="utf-8")
        (tmp_path / "session" / "main.jsonl").write_text("{}", encoding="utf-8")

        files = scan_convos(str(tmp_path))
        names = [f.name for f in files]

        assert "main.jsonl" in names
        assert "agent-top.jsonl" not in names
        assert "agent-deep.jsonl" not in names

    def test_scan_does_not_skip_suffix_named_dirs(self, tmp_path):
        # Exact name match only: 'mysubagents' or 'subagentsbackup' must still
        # be mined. Guards against future regression to substring/regex match.
        for dir_name in ("mysubagents", "subagentsbackup", "subagent"):
            d = tmp_path / dir_name
            d.mkdir()
            (d / f"{dir_name}.jsonl").write_text("{}", encoding="utf-8")

        files = scan_convos(str(tmp_path))
        names = {f.name for f in files}

        assert "mysubagents.jsonl" in names
        assert "subagentsbackup.jsonl" in names
        assert "subagent.jsonl" in names

    def test_scan_skips_subagents_case_insensitive(self, tmp_path):
        # On Windows + macOS APFS the filesystem is case-preserving; if Claude
        # Code or a plugin ever emits 'Subagents' (capitalized), the filter
        # must still match. Only one variant per tmp_path because case-
        # insensitive filesystems collapse 'Subagents' and 'SUBAGENTS'.
        d = tmp_path / "Subagents"
        d.mkdir()
        (d / "agent.jsonl").write_text("{}", encoding="utf-8")
        (tmp_path / "main.jsonl").write_text("{}", encoding="utf-8")

        files = scan_convos(str(tmp_path))
        names = {f.name for f in files}

        assert "main.jsonl" in names
        assert "agent.jsonl" not in names

    def test_scan_mines_an_explicitly_named_file_inside_subagents(self, tmp_path):
        # The skip is directory pruning, so it cannot reach a caller who names
        # one file: that path feeds a single synthetic entry with no directories
        # to prune. The split is deliberate -- --include-subagents governs what a
        # directory walk sweeps up, while naming a path is an explicit request
        # and stays honored. Pinned because the two behaviours were written
        # independently and nothing else exercises them together.
        subagents_dir = tmp_path / "session-abc" / "subagents"
        subagents_dir.mkdir(parents=True)
        target = subagents_dir / "agent-abc.jsonl"
        target.write_text('{"type":"user"}\n', encoding="utf-8")

        files = scan_convos(str(target))

        assert [f.name for f in files] == ["agent-abc.jsonl"]


class TestFileChunksLocked:
    def test_access_gate_bounds_backend_reads_and_write_batches(self, monkeypatch):
        import mempalace.convo_miner as convo_miner

        class RecordingGate:
            def __init__(self):
                self.mode = None
                self.events = []

            @contextlib.contextmanager
            def read_lock(self):
                assert self.mode is None
                self.mode = "read"
                self.events.append("read-enter")
                try:
                    yield
                finally:
                    self.events.append("read-exit")
                    self.mode = None

            @contextlib.contextmanager
            def write_lock(self):
                assert self.mode is None
                self.mode = "write"
                self.events.append("write-enter")
                try:
                    yield
                finally:
                    self.events.append("write-exit")
                    self.mode = None

        class FakeCol:
            def __init__(self, gate):
                self.gate = gate
                self.batch_sizes = []

            def get(self, **_kwargs):
                assert self.gate.mode in {"read", "write"}
                return {"ids": [], "metadatas": []}

            def upsert(self, documents, ids, metadatas):
                assert self.gate.mode == "write"
                self.batch_sizes.append(len(documents))

        gate = RecordingGate()
        col = FakeCol(gate)
        chunks = [{"content": f"chunk {i} " * 20, "chunk_index": i} for i in range(3)]
        monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 2)
        monkeypatch.setattr(
            convo_miner, "file_already_mined", lambda collection, source_file, **kwargs: False
        )
        monkeypatch.setattr(convo_miner, "mine_lock", lambda source_file: contextlib.nullcontext())
        monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda content: "conversations")

        drawers, _, skipped = _file_chunks_locked(
            col,
            "chat.txt",
            chunks,
            "wing",
            "general",
            "agent",
            "exchange",
            access_gate=gate,
        )

        assert (drawers, skipped) == (3, False)
        assert col.batch_sizes == [2, 1]
        assert gate.events.count("read-enter") == 2
        assert gate.events.count("write-enter") == 2
        assert gate.mode is None

    def test_uses_bounded_upsert_batches(self, monkeypatch):
        import mempalace.convo_miner as convo_miner

        class FakeCol:
            def __init__(self):
                self.batch_sizes = []

            def delete(self, *args, **kwargs):
                pass

            def get(self, ids=None, include=None, **kwargs):
                # Pre-mining collision scan probes the collection; empty
                # palace under test, so nothing matches.
                return {"ids": [], "metadatas": []}

            def upsert(self, documents, ids, metadatas):
                self.batch_sizes.append(len(documents))

        chunks = [{"content": f"chunk {i} " * 20, "chunk_index": i} for i in range(5)]
        col = FakeCol()
        monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 2)
        monkeypatch.setattr(
            convo_miner, "file_already_mined", lambda collection, source_file, **kwargs: False
        )
        monkeypatch.setattr(convo_miner, "mine_lock", lambda source_file: contextlib.nullcontext())
        monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda content: "conversations")

        drawers, room_counts, skipped = _file_chunks_locked(
            col, "chat.txt", chunks, "wing", "general", "agent", "exchange"
        )

        assert drawers == 5
        assert dict(room_counts) == {}
        assert skipped is False
        assert col.batch_sizes == [2, 2, 1]

    def test_populates_entities_metadata(self, monkeypatch):
        import mempalace.convo_miner as convo_miner

        class FakeCol:
            def __init__(self):
                self.metas = []

            def delete(self, *args, **kwargs):
                pass

            def get(self, ids=None, include=None, **kwargs):
                return {"ids": [], "metadatas": []}

            def upsert(self, documents, ids, metadatas):
                self.metas.extend(metadatas)

        chunks = [
            {
                "content": "We changed `MemoryStack` in rag/foo.py via do_thing_now().",
                "chunk_index": 0,
            }
        ]
        col = FakeCol()
        monkeypatch.setattr(
            convo_miner, "file_already_mined", lambda collection, source_file, **kwargs: False
        )
        monkeypatch.setattr(convo_miner, "mine_lock", lambda source_file: contextlib.nullcontext())
        monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda content: "conversations")

        _file_chunks_locked(col, "chat.txt", chunks, "wing", "general", "agent", "exchange")

        entities = col.metas[0]["entities"].split(";")
        assert "MemoryStack" in entities
        assert "rag/foo.py" in entities
        assert "do_thing_now" in entities

    def test_aborts_when_stale_drawer_purge_fails(self, monkeypatch):
        """#105: a failed purge must abort the mine attempt, not silently
        proceed to upsert on top of it — the same swallow already fixed
        for miner.py's process_file at #23, own instance here."""
        import mempalace.convo_miner as convo_miner

        class FailingPurgeCol:
            def __init__(self):
                self.upsert_called = False

            def get(self, *args, **kwargs):
                raise RuntimeError("simulated transient backend error")

            def delete(self, *args, **kwargs):
                pass

            def upsert(self, documents, ids, metadatas):
                self.upsert_called = True

        chunks = [{"content": f"chunk {i} " * 20, "chunk_index": i} for i in range(3)]
        col = FailingPurgeCol()
        monkeypatch.setattr(
            convo_miner, "file_already_mined", lambda collection, source_file, **kwargs: False
        )
        monkeypatch.setattr(convo_miner, "mine_lock", lambda source_file: contextlib.nullcontext())
        monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda content: "conversations")

        drawers, room_counts, skipped = _file_chunks_locked(
            col, "chat.txt", chunks, "wing", "general", "agent", "exchange"
        )

        assert col.upsert_called is False, (
            "_file_chunks_locked inserted new chunks even though the "
            "stale-drawer purge raised — old and new rows can now coexist "
            "as duplicates/orphans"
        )
        assert drawers == 0
        assert skipped is True

    def test_stamps_chunk_total_for_completion_check(self, monkeypatch):
        """Every convo drawer of one pass must carry chunk_total (#2183)."""
        import mempalace.convo_miner as convo_miner

        class FakeCol:
            def __init__(self):
                self.metas = []

            def delete(self, *args, **kwargs):
                pass

            def get(self, ids=None, include=None, **kwargs):
                return {"ids": [], "metadatas": []}

            def upsert(self, documents, ids, metadatas):
                self.metas.extend(metadatas)

        chunks = [{"content": f"chunk {i} " * 20, "chunk_index": i} for i in range(5)]
        col = FakeCol()
        monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 2)
        monkeypatch.setattr(
            convo_miner, "file_already_mined", lambda collection, source_file, **kwargs: False
        )
        monkeypatch.setattr(convo_miner, "mine_lock", lambda source_file: contextlib.nullcontext())
        monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda content: "conversations")

        _file_chunks_locked(col, "chat.txt", chunks, "wing", "general", "agent", "exchange")

        assert len(col.metas) == 5
        assert all(m.get("chunk_total") == 5 for m in col.metas), (
            "not every convo chunk carries the pass's chunk_total — a mid-file "
            "crash would leave mtime-stamped partials that skip forever (#2183)"
        )

    def test_upsert_failure_leaves_existing_drawers_untouched(self, monkeypatch, tmp_path):
        """A failed later batch must not cost the palace data it already
        held (#2403 defect 2: the old purge-before-refile contract meant an
        interrupted re-mine left a partial index — observed live as 5,765 of
        8,022 drawers after a SIGTERM). Under the incremental contract
        nothing is deleted before the new set is fully written, and the
        interrupted pass's mixed mtime groups each fall short of
        chunk_total, so file_already_mined stays False and the next mine
        repairs (#2183)."""
        import mempalace.convo_miner as convo_miner
        from mempalace.ids import make_convo_drawer_id
        from mempalace.palace import NORMALIZE_VERSION, file_already_mined

        class FailingCol:
            def __init__(self):
                self.records = {}
                self.documents = {}
                self.upsert_calls = 0
                self.deleted_ids = []

            def get(self, where=None, limit=None, offset=0, include=None, ids=None, **kwargs):
                if ids is not None:
                    hits = [(i, self.records[i]) for i in ids if i in self.records]
                    return {
                        "ids": [i for i, _ in hits],
                        "metadatas": [m for _, m in hits],
                    }
                records = list(self.records.items())
                if where and "source_file" in where:
                    records = [
                        (i, m) for i, m in records if m.get("source_file") == where["source_file"]
                    ]
                page = records[offset : offset + (limit or len(records))]
                return {
                    "ids": [i for i, _ in page],
                    "metadatas": [m for _, m in page],
                }

            def delete(self, ids=None, **kwargs):
                self.deleted_ids.extend(ids or [])
                for drawer_id in ids or []:
                    self.records.pop(drawer_id, None)
                    self.documents.pop(drawer_id, None)

            def update(self, ids, metadatas):
                for drawer_id, meta in zip(ids, metadatas):
                    self.records[drawer_id] = meta

            def upsert(self, documents, ids, metadatas):
                self.upsert_calls += 1
                if self.upsert_calls == 2:
                    raise RuntimeError("simulated second-batch failure")
                for drawer_id, document, meta in zip(ids, documents, metadatas):
                    self.records[drawer_id] = meta
                    self.documents[drawer_id] = document

        source = tmp_path / "chat.txt"
        source.write_text("content\n", encoding="utf-8")
        chunks = [{"content": f"chunk {i} " * 20, "chunk_index": i} for i in range(3)]
        col = FailingCol()
        # The palace already holds an OLD complete pass for this source
        # (stale mtime, no chunk_hash — pre-incremental rows).
        for i in range(3):
            old_id = make_convo_drawer_id("wing", "general", str(source), "exchange", i)
            col.records[old_id] = {
                "source_file": str(source),
                "chunk_index": i,
                "extract_mode": "exchange",
                "normalize_version": NORMALIZE_VERSION,
                "source_mtime": 1.0,
                "chunk_total": 3,
            }
            col.documents[old_id] = f"old verbatim chunk {i}"
        pre_existing = set(col.records)
        old_documents = dict(col.documents)
        monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 2)
        monkeypatch.setattr(
            convo_miner, "file_already_mined", lambda collection, source_file, **kwargs: False
        )
        monkeypatch.setattr(convo_miner, "mine_lock", lambda source_file: contextlib.nullcontext())
        monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda content: "conversations")

        with pytest.raises(RuntimeError, match="second-batch failure"):
            _file_chunks_locked(col, str(source), chunks, "wing", "general", "agent", "exchange")

        assert col.deleted_ids == [], (
            "an interrupted incremental pass deleted drawers — the palace "
            "now holds less than before the mine started (#2403 defect 2)"
        )
        assert pre_existing <= set(col.records), (
            "pre-existing drawers vanished during a failed incremental pass"
        )
        assert {
            drawer_id: col.documents[drawer_id] for drawer_id in pre_existing
        } == old_documents, "a failed later batch overwrote old verbatim drawer contents"
        staged_ids = set(col.records) - pre_existing
        assert staged_ids
        assert all(col.records[drawer_id].get("mine_staged") is True for drawer_id in staged_ids)
        # The real completion check must still see the file as unfinished so
        # the next mine repairs it instead of skipping forever (#2183).
        assert not file_already_mined(
            col, str(source), check_mtime=True, extract_mode="exchange"
        ), "an interrupted pass reads as complete — the missing chunks never come back"

    def test_remine_of_grown_file_only_upserts_changed_chunks(self, monkeypatch, tmp_path):
        """#2403 defect 1: re-mining a grown transcript must be incremental.

        An active Claude Code session appends to its own transcript every
        turn; the old purge-and-refile contract re-embedded the entire file
        each auto-save (observed live: >5 min per pass on a 75 MB
        transcript, O(n²) over a session). Unchanged chunks must keep their
        embeddings and only get a metadata refresh; orphaned ids are
        deleted only after the new set is fully written."""
        import mempalace.convo_miner as convo_miner
        from mempalace.ids import make_convo_drawer_id
        from mempalace.palace import file_already_mined

        class FakeCol:
            def __init__(self):
                self.records = {}
                self.upserted_ids = []
                self.updated_ids = []
                self.deleted_ids = []
                self.fail_delete = False

            def get(self, where=None, limit=None, offset=0, include=None, ids=None, **kwargs):
                if ids is not None:
                    hits = [(i, self.records[i]) for i in ids if i in self.records]
                    return {
                        "ids": [i for i, _ in hits],
                        "metadatas": [m for _, m in hits],
                    }
                records = list(self.records.items())
                if where and "source_file" in where:
                    records = [
                        (i, m) for i, m in records if m.get("source_file") == where["source_file"]
                    ]
                page = records[offset : offset + (limit or len(records))]
                return {
                    "ids": [i for i, _ in page],
                    "metadatas": [m for _, m in page],
                }

            def delete(self, ids=None, **kwargs):
                if self.fail_delete:
                    raise RuntimeError("simulated orphan cleanup failure")
                self.deleted_ids.extend(ids or [])
                for drawer_id in ids or []:
                    self.records.pop(drawer_id, None)

            def update(self, ids, metadatas):
                self.updated_ids.extend(ids)
                for drawer_id, meta in zip(ids, metadatas):
                    self.records[drawer_id] = meta

            def upsert(self, documents, ids, metadatas):
                self.upserted_ids.extend(ids)
                for drawer_id, meta in zip(ids, metadatas):
                    self.records[drawer_id] = meta

        source = tmp_path / "chat.txt"
        source.write_text("content\n", encoding="utf-8")
        first_mtime = source.stat().st_mtime
        col = FakeCol()
        monkeypatch.setattr(
            convo_miner, "file_already_mined", lambda collection, source_file, **kwargs: False
        )
        monkeypatch.setattr(convo_miner, "mine_lock", lambda source_file: contextlib.nullcontext())
        monkeypatch.setattr(convo_miner, "_detect_hall_cached", lambda content: "conversations")

        chunks_v1 = [{"content": f"chunk {i} " * 20, "chunk_index": i} for i in range(3)]
        drawers, _, skipped = _file_chunks_locked(
            col, str(source), chunks_v1, "wing", "general", "agent", "exchange"
        )
        assert (drawers, skipped) == (3, False)
        first_pass_ids = list(col.upserted_ids)

        # The transcript grows by two exchanges; the first three are untouched.
        source.write_text("content\nmore\n", encoding="utf-8")
        os.utime(source, (first_mtime + 10, first_mtime + 10))
        col.upserted_ids.clear()
        chunks_v2 = chunks_v1 + [
            {"content": f"chunk {i} " * 20, "chunk_index": i} for i in range(3, 5)
        ]
        drawers, _, skipped = _file_chunks_locked(
            col, str(source), chunks_v2, "wing", "general", "agent", "exchange"
        )

        assert skipped is False
        assert drawers == 2, "unchanged chunks were re-embedded on a grown-file re-mine"
        assert set(col.upserted_ids).isdisjoint(first_pass_ids), (
            "a chunk that did not change was re-upserted"
        )
        assert set(col.updated_ids) == set(first_pass_ids), (
            "unchanged chunks did not get their completion metadata refreshed"
        )
        assert col.deleted_ids == [], "a purely-grown file has no orphans to delete"
        # After the incremental pass the file must read as complete.
        assert all(m.get("chunk_total") == 5 for m in col.records.values())
        assert file_already_mined(col, str(source), check_mtime=True, extract_mode="exchange")

        # A rewrite that drops the tail (e.g. /compact) orphans the extra
        # ids. A transient cleanup failure must leave the target set visibly
        # incomplete so the unchanged source retries on the next mine.
        source.write_text("rewritten\n", encoding="utf-8")
        os.utime(source, (first_mtime + 20, first_mtime + 20))
        col.fail_delete = True
        _, _, skipped = _file_chunks_locked(
            col, str(source), chunks_v1, "wing", "general", "agent", "exchange"
        )
        assert skipped is True
        assert not file_already_mined(
            col, str(source), check_mtime=True, extract_mode="exchange"
        ), "failed orphan cleanup made the rewritten source look complete"

        # The source mtime is unchanged, but the incomplete marker forces a
        # retry; once cleanup succeeds the target rows are finalized current.
        col.fail_delete = False
        drawers, _, skipped = _file_chunks_locked(
            col, str(source), chunks_v1, "wing", "general", "agent", "exchange"
        )
        assert skipped is False
        orphaned = {
            make_convo_drawer_id("wing", "general", str(source), "exchange", i) for i in (3, 4)
        }
        assert set(col.deleted_ids) == orphaned, (
            "a shrunk re-mine must delete exactly the orphaned ids, nothing else"
        )
        assert file_already_mined(col, str(source), check_mtime=True, extract_mode="exchange")


def test_end_of_mine_hallways_read_then_fts_validation_write(tmp_path, monkeypatch):
    import mempalace.convo_miner as convo_miner

    class RecordingGate:
        def __init__(self):
            self.mode = None
            self.events = []

        @contextlib.contextmanager
        def _lock(self, mode):
            assert self.mode is None
            self.mode = mode
            self.events.append(f"{mode}-enter")
            try:
                yield
            finally:
                self.events.append(f"{mode}-exit")
                self.mode = None

        def read_lock(self):
            return self._lock("read")

        def write_lock(self):
            return self._lock("write")

    gate = RecordingGate()
    collection = object()
    monkeypatch.setattr(convo_miner, "scan_convos", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(convo_miner, "_open_convo_collection", lambda *_args, **_kwargs: collection)
    monkeypatch.setattr(convo_miner, "prefetch_mined_set", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(convo_miner, "prefetch_content_hashes", lambda *_args, **_kwargs: {})

    def hallways(*_args, **_kwargs):
        assert gate.mode == "read"

    def validate(*_args, **_kwargs):
        assert gate.mode == "write"

    monkeypatch.setattr(convo_miner, "_compute_hallways_for_wing_safe", hallways)
    monkeypatch.setattr(convo_miner, "_validate_palace_fts5_after_mine", validate)

    convo_miner._mine_convos_impl(
        str(tmp_path),
        str(tmp_path / "palace"),
        wing="test-wing",
        access_gate=gate,
    )

    assert gate.events[-4:] == ["read-enter", "read-exit", "write-enter", "write-exit"]


class TestSourceFileDeleteIds:
    """#104: the sweeper writes drawers with no extract_mode at all
    (ingest_mode="sweep"). convo_miner's default exchange-mode purge
    must not scoop those up — they were never meant to carry
    extract_mode, unlike a genuine legacy pre-schema convo_miner row."""

    def test_excludes_sweeper_rows_from_exchange_mode_purge(self):
        class FakeCol:
            def get(self, where=None, limit=None, offset=0, include=None):
                if offset > 0:
                    return {"ids": [], "metadatas": []}
                return {
                    "ids": ["sweep_1", "exchange_1", "legacy_1"],
                    "metadatas": [
                        {"ingest_mode": "sweep", "session_id": "s1", "role": "user"},
                        {"ingest_mode": "convos", "extract_mode": "exchange"},
                        {"source_file": "chat.txt"},  # pre-ingest_mode legacy row
                    ],
                }

        delete_ids = _source_file_delete_ids(FakeCol(), "chat.txt", "exchange")

        assert "sweep_1" not in delete_ids, (
            "sweeper's drawer was scooped into convo_miner's default "
            "exchange-mode purge and would be deleted on the next re-mine"
        )
        assert "exchange_1" in delete_ids
        assert "legacy_1" in delete_ids


class TestExtractAuthoredAt:
    """authored_at = max per-line ``timestamp`` in a transcript (real authored date,
    independent of mine time). Both Claude Code and Codex JSONL carry a top-level
    ISO-8601 ``timestamp`` per line."""

    def test_returns_latest_timestamp(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text(
            '{"type": "user", "timestamp": "2026-06-21T10:00:00.000Z"}\n'
            '{"type": "assistant", "timestamp": "2026-06-23T14:30:00.000Z"}\n'
            '{"type": "user", "timestamp": "2026-06-22T09:00:00.000Z"}\n'
        )
        assert _extract_authored_at(f) == "2026-06-23T14:30:00.000Z"

    def test_ignores_lines_without_timestamp(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text(
            '{"type": "summary", "summary": "x"}\n'
            '{"type": "assistant", "timestamp": "2026-06-23T14:30:00.000Z"}\n'
        )
        assert _extract_authored_at(f) == "2026-06-23T14:30:00.000Z"

    def test_tolerates_blank_and_malformed_lines(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text(
            "\n"
            "not json\n"
            "[1, 2, 3]\n"  # valid JSON, but no .get()
            '{"timestamp": "2026-06-25T00:00:00.000Z"}\n'
        )
        assert _extract_authored_at(f) == "2026-06-25T00:00:00.000Z"

    def test_none_for_non_jsonl(self, tmp_path):
        f = tmp_path / "notes.md"
        f.write_text("# heading\n")
        assert _extract_authored_at(f) is None

    def test_none_when_no_timestamps(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text('{"type": "user", "content": "hi"}\n')
        assert _extract_authored_at(f) is None

    def test_none_for_missing_file(self, tmp_path):
        assert _extract_authored_at(tmp_path / "absent.jsonl") is None

    def test_non_string_timestamp_does_not_crash(self, tmp_path):
        # A non-string timestamp must be skipped, not raise TypeError on compare.
        f = tmp_path / "session.jsonl"
        f.write_text(
            '{"type": "user", "timestamp": 1234567890}\n'
            '{"type": "assistant", "timestamp": {"nested": true}}\n'
            '{"type": "user", "timestamp": "2026-06-24T00:00:00.000Z"}\n'
        )
        assert _extract_authored_at(f) == "2026-06-24T00:00:00.000Z"

    def test_only_non_string_timestamps_returns_none(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text('{"timestamp": 1}\n{"timestamp": false}\n')
        assert _extract_authored_at(f) is None


def test_scan_convos_accepts_one_file_without_scanning_siblings(
    tmp_path,
):
    selected = tmp_path / "selected.jsonl"
    sibling = tmp_path / "sibling.jsonl"

    selected.write_text(
        '{"type": "user"}\n',
        encoding="utf-8",
    )
    sibling.write_text(
        '{"type": "user"}\n',
        encoding="utf-8",
    )

    assert scan_convos(str(selected)) == [selected.resolve()]
