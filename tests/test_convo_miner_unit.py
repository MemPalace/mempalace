"""Unit tests for convo_miner pure functions (no chromadb needed)."""

import contextlib
import sys

import pytest

from mempalace.convo_miner import (
    CHUNK_SIZE,
    _emit_bounded,
    _file_chunks_locked,
    chunk_exchanges,
    detect_convo_room,
    scan_convos,
)


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


class TestFileChunksLocked:
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
