"""Tests for the ``mempalace read`` CLI verb.

The verb consumes a closet pointer (or a shorthand date+line+source) and
returns the surgical line-range slice from the referenced drawer(s).
Resurrects the original ``read.py`` concept Aya designed with Lumi —
opening only the slice the closet pointer matched, never the whole
drawer.

Tests are negative-first — the failure contract is pinned BEFORE the
happy-path positives.
"""

from unittest.mock import MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# parse_pointer — pure parsing, no I/O
# ─────────────────────────────────────────────────────────────────────────────


class TestParsePointer:
    """``parse_pointer(s)`` accepts:

    - Full Tier 6a closet pointer: ``topic|entities|YYYY-MM-DD:Lstart-Lend|→ids``
    - Legacy 3-segment closet pointer: ``topic|entities|→ids``
    - Shorthand:                       ``YYYY-MM-DD:Lstart-Lend source_file``

    Rejects anything the resolver couldn't act on (empty, no source AND
    no drawer ids, malformed line range, etc.).
    """

    # ── Negative cases (RED-first) ────────────────────────────────────

    def test_empty_string_raises(self):
        from mempalace.reader import parse_pointer

        with pytest.raises(ValueError, match="empty"):
            parse_pointer("")

    def test_whitespace_only_raises(self):
        from mempalace.reader import parse_pointer

        with pytest.raises(ValueError, match="empty"):
            parse_pointer("   \n\t  ")

    def test_garbage_string_raises(self):
        from mempalace.reader import parse_pointer

        with pytest.raises(ValueError, match="parse"):
            parse_pointer("totally not a pointer")

    def test_date_only_no_line_no_source_raises(self):
        from mempalace.reader import parse_pointer

        with pytest.raises(ValueError, match="parse"):
            parse_pointer("2024-11-08")

    def test_source_only_no_date_no_line_raises(self):
        from mempalace.reader import parse_pointer

        with pytest.raises(ValueError, match="parse"):
            parse_pointer("just_a_filename.md")

    def test_line_range_with_end_less_than_start_raises(self):
        from mempalace.reader import parse_pointer

        with pytest.raises(ValueError, match=r"end.*start|line range"):
            parse_pointer("2024-11-08:L78-L42 source.md")

    def test_line_start_zero_raises(self):
        from mempalace.reader import parse_pointer

        with pytest.raises(ValueError, match=r"line.*positive|line range"):
            parse_pointer("2024-11-08:L0-L42 source.md")

    def test_pointer_with_arrow_but_no_drawer_ids_raises(self):
        from mempalace.reader import parse_pointer

        with pytest.raises(ValueError, match=r"drawer|parse"):
            parse_pointer("topic|entities|→")

    # ── Positive cases ────────────────────────────────────────────────

    def test_full_4_segment_pointer_with_date_and_drawers(self):
        from mempalace.reader import parse_pointer

        parsed = parse_pointer(
            "brands of dog food|Aya;Lumi|2024-11-08:L42-L78|→drawer_a,drawer_b,drawer_c"
        )
        assert parsed.date == "2024-11-08"
        assert parsed.line_start == 42
        assert parsed.line_end == 78
        assert parsed.drawer_ids == ["drawer_a", "drawer_b", "drawer_c"]

    def test_legacy_3_segment_pointer_no_date(self):
        from mempalace.reader import parse_pointer

        parsed = parse_pointer("brands of dog food|Aya;Lumi|→drawer_a,drawer_b")
        assert parsed.date is None
        assert parsed.line_start is None
        assert parsed.line_end is None
        assert parsed.drawer_ids == ["drawer_a", "drawer_b"]

    def test_shorthand_date_line_source(self):
        from mempalace.reader import parse_pointer

        parsed = parse_pointer("2024-11-08:L42-L78 lumi-session.md")
        assert parsed.date == "2024-11-08"
        assert parsed.line_start == 42
        assert parsed.line_end == 78
        assert parsed.source_file == "lumi-session.md"
        assert parsed.drawer_ids == []

    def test_shorthand_strips_trailing_whitespace(self):
        from mempalace.reader import parse_pointer

        parsed = parse_pointer("  2024-11-08:L42-L78 lumi-session.md  \n")
        assert parsed.source_file == "lumi-session.md"

    def test_shorthand_source_path_with_directory(self):
        from mempalace.reader import parse_pointer

        parsed = parse_pointer("2024-11-08:L42-L78 sub/dir/file.md")
        assert parsed.source_file == "sub/dir/file.md"

    def test_single_drawer_in_pointer_returns_list_of_one(self):
        from mempalace.reader import parse_pointer

        parsed = parse_pointer("topic|ent|2024-11-08:L1-L10|→solo_drawer")
        assert parsed.drawer_ids == ["solo_drawer"]


# ─────────────────────────────────────────────────────────────────────────────
# resolve_drawers — find drawer records matching the parsed pointer
# ─────────────────────────────────────────────────────────────────────────────


def _fake_collection(drawers_by_id):
    """Build a MagicMock collection whose .get supports the patterns
    used in ``reader.py``: by-id, by-where, and paginated full-scan via
    ``limit``+``offset``. ``col.count()`` returns the total drawer count.

    ``drawers_by_id`` is a dict {drawer_id: {"document": ..., "metadata": ...}}.
    """
    col = MagicMock()
    col.count.return_value = len(drawers_by_id)

    def fake_get(ids=None, where=None, include=None, limit=None, offset=None, **kwargs):
        if ids is not None:
            found_ids = []
            found_docs = []
            found_metas = []
            for did in ids:
                if did in drawers_by_id:
                    found_ids.append(did)
                    found_docs.append(drawers_by_id[did]["document"])
                    found_metas.append(drawers_by_id[did]["metadata"])
            return {"ids": found_ids, "documents": found_docs, "metadatas": found_metas}
        if where is not None:
            # Simple "$eq" support for source_file
            target = (where.get("source_file") or {}).get("$eq")
            if target is None:
                target = where.get("source_file")
            ids_out, docs_out, metas_out = [], [], []
            for did, rec in drawers_by_id.items():
                if rec["metadata"].get("source_file") == target:
                    ids_out.append(did)
                    docs_out.append(rec["document"])
                    metas_out.append(rec["metadata"])
            return {"ids": ids_out, "documents": docs_out, "metadatas": metas_out}
        # Paginated full scan: honor limit/offset over the dict's insertion order.
        all_ids = list(drawers_by_id.keys())
        start = offset or 0
        end = start + limit if limit is not None else len(all_ids)
        slice_ids = all_ids[start:end]
        return {
            "ids": slice_ids,
            "documents": [drawers_by_id[d]["document"] for d in slice_ids],
            "metadatas": [drawers_by_id[d]["metadata"] for d in slice_ids],
        }

    col.get.side_effect = fake_get
    return col


class TestResolveDrawers:
    def test_resolve_by_drawer_ids_finds_all(self):
        from mempalace.reader import ParsedPointer, resolve_drawers

        col = _fake_collection(
            {
                "drawer_a": {
                    "document": "content a",
                    "metadata": {"source_file": "/p/x.md", "chunk_index": 0},
                },
                "drawer_b": {
                    "document": "content b",
                    "metadata": {"source_file": "/p/x.md", "chunk_index": 1},
                },
            }
        )
        parsed = ParsedPointer(
            date=None,
            line_start=None,
            line_end=None,
            source_file=None,
            drawer_ids=["drawer_a", "drawer_b"],
        )
        result = resolve_drawers(col, parsed)
        assert len(result) == 2
        assert {r.drawer_id for r in result} == {"drawer_a", "drawer_b"}

    def test_resolve_missing_drawer_ids_returns_only_found(self):
        from mempalace.reader import ParsedPointer, resolve_drawers

        col = _fake_collection(
            {
                "drawer_a": {
                    "document": "content",
                    "metadata": {"source_file": "/p/x.md"},
                }
            }
        )
        parsed = ParsedPointer(
            date=None,
            line_start=None,
            line_end=None,
            source_file=None,
            drawer_ids=["drawer_a", "drawer_nope"],
        )
        result = resolve_drawers(col, parsed)
        assert len(result) == 1
        assert result[0].drawer_id == "drawer_a"

    def test_resolve_all_drawer_ids_missing_returns_empty(self):
        from mempalace.reader import ParsedPointer, resolve_drawers

        col = _fake_collection({})
        parsed = ParsedPointer(
            date=None,
            line_start=None,
            line_end=None,
            source_file=None,
            drawer_ids=["nope_a", "nope_b"],
        )
        result = resolve_drawers(col, parsed)
        assert result == []

    def test_resolve_by_source_basename_matches_all_chunks(self):
        from mempalace.reader import ParsedPointer, resolve_drawers

        col = _fake_collection(
            {
                "drawer_chunk0": {
                    "document": "first chunk",
                    "metadata": {
                        "source_file": "/proj/2024-11-08-lumi.md",
                        "chunk_index": 0,
                    },
                },
                "drawer_chunk1": {
                    "document": "second chunk",
                    "metadata": {
                        "source_file": "/proj/2024-11-08-lumi.md",
                        "chunk_index": 1,
                    },
                },
                "drawer_other": {
                    "document": "different file",
                    "metadata": {
                        "source_file": "/proj/different.md",
                        "chunk_index": 0,
                    },
                },
            }
        )
        parsed = ParsedPointer(
            date="2024-11-08",
            line_start=42,
            line_end=78,
            source_file="2024-11-08-lumi.md",
            drawer_ids=[],
        )
        result = resolve_drawers(col, parsed)
        assert len(result) == 2
        # Should return only chunks of the matched source file
        assert all(r.source_file == "/proj/2024-11-08-lumi.md" for r in result)

    def test_resolve_by_source_no_match_returns_empty(self):
        from mempalace.reader import ParsedPointer, resolve_drawers

        col = _fake_collection(
            {
                "drawer_a": {
                    "document": "content",
                    "metadata": {"source_file": "/proj/other.md", "chunk_index": 0},
                }
            }
        )
        parsed = ParsedPointer(
            date="2024-11-08",
            line_start=1,
            line_end=10,
            source_file="missing.md",
            drawer_ids=[],
        )
        result = resolve_drawers(col, parsed)
        assert result == []


# ─────────────────────────────────────────────────────────────────────────────
# format_drawer_menu — the interactive picker rendering
# ─────────────────────────────────────────────────────────────────────────────


class TestFormatDrawerMenu:
    def _make_candidate(self, drawer_id, source_file, chunk_index, line_start, line_end, document):
        from mempalace.reader import DrawerCandidate

        return DrawerCandidate(
            drawer_id=drawer_id,
            source_file=source_file,
            chunk_index=chunk_index,
            line_start=line_start,
            line_end=line_end,
            document=document,
        )

    def test_menu_shows_chunk_index_and_line_range(self):
        from mempalace.reader import format_drawer_menu

        cand = self._make_candidate(
            "drawer_a", "/p/x.md", 2, 42, 78, "Aya: hey Lumi, what about dog food?"
        )
        out = format_drawer_menu([cand])
        assert "[1]" in out
        assert "chunk 2" in out or "chunk_index 2" in out
        assert "42" in out and "78" in out

    def test_menu_includes_snippet_preview(self):
        from mempalace.reader import format_drawer_menu

        cand = self._make_candidate(
            "drawer_a",
            "/p/x.md",
            2,
            42,
            78,
            "Aya: what brands have you heard good things about?",
        )
        out = format_drawer_menu([cand])
        assert "what brands" in out

    def test_menu_truncates_long_snippets(self):
        from mempalace.reader import format_drawer_menu

        long_doc = "A" * 500
        cand = self._make_candidate("drawer_a", "/p/x.md", 0, 1, 50, long_doc)
        out = format_drawer_menu([cand])
        # The "AAA..." line in the menu should not be 500 chars long
        for line in out.split("\n"):
            assert len(line) < 200, f"menu line too long: {len(line)} chars"

    def test_menu_handles_multiple_candidates_with_indices(self):
        from mempalace.reader import format_drawer_menu

        cand1 = self._make_candidate("drawer_a", "/p/x.md", 2, 42, 78, "snippet one")
        cand2 = self._make_candidate("drawer_b", "/p/x.md", 4, 110, 145, "snippet two")
        cand3 = self._make_candidate("drawer_c", "/p/x.md", 7, 200, 230, "snippet three")
        out = format_drawer_menu([cand1, cand2, cand3])
        assert "[1]" in out
        assert "[2]" in out
        assert "[3]" in out
        assert "snippet one" in out
        assert "snippet three" in out


# ─────────────────────────────────────────────────────────────────────────────
# read_slice — call extract_line_range on a candidate's document
# ─────────────────────────────────────────────────────────────────────────────


class TestReadSlice:
    def test_returns_line_numbered_slice_within_drawer(self):
        from mempalace.reader import DrawerCandidate, read_slice

        doc = "\n".join(f"line {i}" for i in range(1, 101))  # 100 lines
        cand = DrawerCandidate(
            drawer_id="d",
            source_file="/p/x.md",
            chunk_index=0,
            line_start=1,
            line_end=100,
            document=doc,
        )
        out = read_slice(cand, requested_line_start=5, requested_line_end=10)
        assert "[5] line 5" in out
        assert "[10] line 10" in out
        assert "[4]" not in out
        assert "[11]" not in out

    def test_falls_back_to_drawer_lines_when_request_is_none(self):
        """If the caller didn't ask for a specific line range, use the
        drawer's own metadata line range (the chunk's natural span)."""
        from mempalace.reader import DrawerCandidate, read_slice

        doc = "alpha\nbeta\ngamma"
        cand = DrawerCandidate(
            drawer_id="d",
            source_file="/p/x.md",
            chunk_index=0,
            line_start=10,
            line_end=12,
            document=doc,
        )
        out = read_slice(cand, requested_line_start=None, requested_line_end=None)
        assert "[10] alpha" in out
        assert "[12] gamma" in out

    def test_returns_empty_string_for_empty_drawer(self):
        from mempalace.reader import DrawerCandidate, read_slice

        cand = DrawerCandidate(
            drawer_id="d",
            source_file="/p/x.md",
            chunk_index=0,
            line_start=1,
            line_end=10,
            document="",
        )
        out = read_slice(cand, requested_line_start=1, requested_line_end=10)
        assert out == ""


# Gemini PR #1588 review fixes — negative-first per /adversarial-review


class TestResolveBySourceTwoStepFetch:
    """Fallback scan must paginate metadatas-only, then chunked-fetch
    documents only for matched IDs. The contract is asserted against
    the actual call shape produced by the new paginated + chunked
    implementation (mirrors palace.bulk_check_mined for the scan +
    _chunked_get for the docs).
    """

    def test_metadata_scan_calls_do_not_include_documents(self):
        """Every paginated scan call (col.get with offset=N, no ids=)
        must NOT request 'documents' — that's the whole point of the
        two-step pattern."""
        from mempalace.reader import ParsedPointer, resolve_drawers

        col = _fake_collection(
            {
                "d1": {
                    "document": "chunk content",
                    "metadata": {"source_file": "/proj/chat.md", "chunk_index": 0},
                },
            }
        )
        parsed = ParsedPointer(
            date=None,
            line_start=None,
            line_end=None,
            source_file="chat.md",
            drawer_ids=[],
        )
        resolve_drawers(col, parsed)

        # Inspect every scan-shape call (no `ids=`, no `where=`, has offset/limit).
        scan_calls = [
            c
            for c in col.get.call_args_list
            if c.kwargs.get("ids") is None and c.kwargs.get("where") is None
        ]
        assert scan_calls, "expected at least one paginated scan call"
        for c in scan_calls:
            include = c.kwargs.get("include", [])
            assert "documents" not in include, (
                f"scan call must NOT request documents; got include={include!r}"
            )
            assert "metadatas" in include

    def test_docs_fetch_requests_only_matched_ids(self):
        """The chunked docs-fetch (col.get with ids=...) must request
        only the IDs whose metadata matched, never the non-matching ones."""
        from mempalace.reader import ParsedPointer, resolve_drawers

        col = _fake_collection(
            {
                "d1": {
                    "document": "match chunk 0",
                    "metadata": {"source_file": "/proj/match.md", "chunk_index": 0},
                },
                "d2": {
                    "document": "other content",
                    "metadata": {"source_file": "/proj/other.md", "chunk_index": 0},
                },
                "d3": {
                    "document": "match chunk 1",
                    "metadata": {"source_file": "/proj/match.md", "chunk_index": 1},
                },
            }
        )
        parsed = ParsedPointer(
            date=None,
            line_start=None,
            line_end=None,
            source_file="match.md",
            drawer_ids=[],
        )
        result = resolve_drawers(col, parsed)

        # Identify the docs-fetch calls (col.get with ids=).
        docs_calls = [c for c in col.get.call_args_list if c.kwargs.get("ids") is not None]
        assert docs_calls, "expected at least one docs-fetch call"
        # Aggregate all IDs requested across chunks.
        all_requested = set()
        for c in docs_calls:
            for did in c.kwargs.get("ids") or []:
                all_requested.add(did)
            assert "documents" in c.kwargs.get("include", []), (
                "docs-fetch call must request documents"
            )
        assert all_requested == {"d1", "d3"}, (
            f"docs-fetch must request only matched IDs (d1, d3); got {all_requested!r}"
        )
        assert len(result) == 2
        assert [c.chunk_index for c in result] == [0, 1]

    def test_no_docs_fetch_when_no_basename_matches(self):
        """When the metadata scan finds zero matches, we must NOT issue
        any docs-fetch (col.get with ids=). Saves a pointless I/O."""
        from mempalace.reader import ParsedPointer, resolve_drawers

        col = _fake_collection(
            {
                "d1": {
                    "document": "irrelevant",
                    "metadata": {"source_file": "/proj/different.md", "chunk_index": 0},
                },
            }
        )
        parsed = ParsedPointer(
            date=None,
            line_start=None,
            line_end=None,
            source_file="missing.md",
            drawer_ids=[],
        )
        result = resolve_drawers(col, parsed)
        assert result == []
        docs_calls = [c for c in col.get.call_args_list if c.kwargs.get("ids") is not None]
        assert docs_calls == [], (
            f"no docs-fetch should happen on zero matches; got {len(docs_calls)} calls"
        )


class TestCmdReadTTYHandling:
    """When pointer is missing/dash AND stdin is a TTY, the CLI must error
    and exit 1 instead of blocking on stdin.read().
    """

    def test_no_pointer_and_tty_stdin_exits_with_error(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from mempalace.cli import cmd_read

        args = SimpleNamespace(pointer=None, drawer=None, all=False, palace=None)
        with patch("mempalace.cli.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.read.side_effect = AssertionError("stdin.read() must NOT be called when TTY")
            with pytest.raises(SystemExit) as excinfo:
                cmd_read(args)
            assert excinfo.value.code == 1

    def test_dash_pointer_and_tty_stdin_exits_with_error(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from mempalace.cli import cmd_read

        args = SimpleNamespace(pointer="-", drawer=None, all=False, palace=None)
        with patch("mempalace.cli.sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.read.side_effect = AssertionError(
                "stdin.read() must NOT be called when TTY with '-' pointer"
            )
            with pytest.raises(SystemExit) as excinfo:
                cmd_read(args)
            assert excinfo.value.code == 1

    def test_no_pointer_and_none_stdin_exits_cleanly(self):
        """``sys.stdin`` is None on Windows pythonw and some detached
        daemon contexts. Calling ``.isatty()`` on None would raise
        AttributeError — must instead exit 1 like the TTY case.
        Gemini Pro adversarial review finding (#2)."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from mempalace.cli import cmd_read

        args = SimpleNamespace(pointer=None, drawer=None, all=False, palace=None)
        with patch("mempalace.cli.sys.stdin", None):
            with pytest.raises(SystemExit) as excinfo:
                cmd_read(args)
            assert excinfo.value.code == 1

    def test_dash_pointer_and_none_stdin_exits_cleanly(self):
        """Same None-stdin guard applies to explicit ``-`` pointer."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from mempalace.cli import cmd_read

        args = SimpleNamespace(pointer="-", drawer=None, all=False, palace=None)
        with patch("mempalace.cli.sys.stdin", None):
            with pytest.raises(SystemExit) as excinfo:
                cmd_read(args)
            assert excinfo.value.code == 1


class TestTypeAnnotations:
    """Specific type hints on public reader.py surface — list[str] /
    list[DrawerCandidate] rather than bare list."""

    def test_parsed_pointer_drawer_ids_field_typed_as_list_of_str(self):
        from mempalace.reader import ParsedPointer

        ann_str = str(ParsedPointer.__annotations__.get("drawer_ids"))
        assert "str" in ann_str, f"drawer_ids must be list[str], got {ann_str!r}"

    def test_resolve_drawers_return_type_annotation_is_specific(self):
        from mempalace.reader import resolve_drawers

        ann_str = str(resolve_drawers.__annotations__.get("return"))
        assert "DrawerCandidate" in ann_str, (
            f"resolve_drawers must return list[DrawerCandidate]; got {ann_str!r}"
        )


class TestCmdReadThreadsLineRange:
    """Igor's blocker on PR #1588 (cli.py:755): cmd_read called
    ``read_slice(candidates[0])`` with NO line range — so
    ``parsed.line_start`` / ``parsed.line_end`` (the entire point of
    'surgical slice') were dropped on the floor and the whole chunk
    came back.

    These tests pin the contract: every code path in cmd_read that
    calls read_slice MUST forward parsed.line_start and parsed.line_end.
    """

    def _make_candidates(self, n=1):
        from mempalace.reader import DrawerCandidate

        return [
            DrawerCandidate(
                drawer_id=f"d{i}",
                source_file="/p/chat.md",
                chunk_index=i,
                line_start=1,
                line_end=100,
                document="x" * 50,
            )
            for i in range(n)
        ]

    def _patch_cmd_read_deps(self, candidates, parsed):
        """Patch the open_collection + resolve_drawers + parse_pointer
        chain so cmd_read can be invoked in isolation."""
        from unittest.mock import patch, MagicMock

        return [
            patch("mempalace.palace._open_collection_or_explain", return_value=MagicMock()),
            patch("mempalace.reader.parse_pointer", return_value=parsed),
            patch("mempalace.reader.resolve_drawers", return_value=candidates),
        ]

    def test_single_candidate_path_passes_line_range(self):
        """``len(candidates) == 1 and not args.all`` path — cli.py:755."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from mempalace.cli import cmd_read
        from mempalace.reader import ParsedPointer

        parsed = ParsedPointer(
            date="2024-11-08",
            line_start=1,
            line_end=3,
            source_file="chat.md",
            drawer_ids=[],
        )
        candidates = self._make_candidates(1)
        args = SimpleNamespace(
            pointer="2024-11-08:L1-L3 chat.md",
            drawer=None,
            all=False,
            palace=None,
        )
        patches = self._patch_cmd_read_deps(candidates, parsed)
        with patch("mempalace.reader.read_slice") as mock_read_slice:
            mock_read_slice.return_value = "stub output"
            for p in patches:
                p.start()
            try:
                cmd_read(args)
            finally:
                for p in patches:
                    p.stop()
            mock_read_slice.assert_called_once()
            call_args = mock_read_slice.call_args
            # Accept either positional or keyword form.
            kwargs = call_args.kwargs
            positional = call_args.args
            line_start = kwargs.get(
                "requested_line_start", positional[1] if len(positional) > 1 else None
            )
            line_end = kwargs.get(
                "requested_line_end", positional[2] if len(positional) > 2 else None
            )
            assert line_start == 1, (
                f"single-candidate path must pass line_start=1; got {line_start!r}"
            )
            assert line_end == 3, f"single-candidate path must pass line_end=3; got {line_end!r}"

    def test_drawer_flag_path_passes_line_range(self):
        """``--drawer N`` non-interactive path — cli.py:771."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from mempalace.cli import cmd_read
        from mempalace.reader import ParsedPointer

        parsed = ParsedPointer(
            date="2024-11-08",
            line_start=5,
            line_end=7,
            source_file="chat.md",
            drawer_ids=[],
        )
        candidates = self._make_candidates(3)
        args = SimpleNamespace(
            pointer="2024-11-08:L5-L7 chat.md",
            drawer=2,
            all=False,
            palace=None,
        )
        patches = self._patch_cmd_read_deps(candidates, parsed)
        with patch("mempalace.reader.read_slice") as mock_read_slice:
            mock_read_slice.return_value = "stub output"
            for p in patches:
                p.start()
            try:
                cmd_read(args)
            finally:
                for p in patches:
                    p.stop()
            mock_read_slice.assert_called_once()
            kwargs = mock_read_slice.call_args.kwargs
            positional = mock_read_slice.call_args.args
            line_start = kwargs.get(
                "requested_line_start", positional[1] if len(positional) > 1 else None
            )
            line_end = kwargs.get(
                "requested_line_end", positional[2] if len(positional) > 2 else None
            )
            assert line_start == 5, f"--drawer path must pass line_start=5; got {line_start!r}"
            assert line_end == 7, f"--drawer path must pass line_end=7; got {line_end!r}"

    def test_all_flag_path_passes_line_range_to_each_candidate(self):
        """``--all`` path — cli.py:759 (calls _print_all_candidates which
        loops over read_slice). Helper must accept and forward parsed
        line range to every candidate."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from mempalace.cli import cmd_read
        from mempalace.reader import ParsedPointer

        parsed = ParsedPointer(
            date="2024-11-08",
            line_start=10,
            line_end=20,
            source_file="chat.md",
            drawer_ids=[],
        )
        candidates = self._make_candidates(3)
        args = SimpleNamespace(
            pointer="2024-11-08:L10-L20 chat.md",
            drawer=None,
            all=True,
            palace=None,
        )
        patches = self._patch_cmd_read_deps(candidates, parsed)
        with patch("mempalace.reader.read_slice") as mock_read_slice:
            mock_read_slice.return_value = "stub output"
            for p in patches:
                p.start()
            try:
                cmd_read(args)
            finally:
                for p in patches:
                    p.stop()
            assert mock_read_slice.call_count == 3, (
                f"--all must call read_slice once per candidate; got {mock_read_slice.call_count}"
            )
            for i, call in enumerate(mock_read_slice.call_args_list):
                kwargs = call.kwargs
                positional = call.args
                line_start = kwargs.get(
                    "requested_line_start", positional[1] if len(positional) > 1 else None
                )
                line_end = kwargs.get(
                    "requested_line_end", positional[2] if len(positional) > 2 else None
                )
                assert line_start == 10, (
                    f"--all call #{i} must pass line_start=10; got {line_start!r}"
                )
                assert line_end == 20, f"--all call #{i} must pass line_end=20; got {line_end!r}"


# Gemini PR #1588 re-review fixes (chunking + pagination + Igor items)


class TestChunkedGet:
    """gemini HIGH-priority + fresh-Claude V3 plan #1.

    ``_chunked_get(col, ids, include, batch=500)`` splits an arbitrarily
    long ID list into ``col.get`` calls of at most ``batch`` IDs each,
    then merges the results. This avoids ``sqlite3.OperationalError:
    too many SQL variables`` on the SQLite-backed ChromaDB store, whose
    ``SQLITE_MAX_VARIABLE_NUMBER`` default is 999.
    """

    def test_empty_input_returns_empty_dict_with_no_calls(self):
        from unittest.mock import MagicMock
        from mempalace.reader import _chunked_get

        col = MagicMock()
        result = _chunked_get(col, [], include=["documents", "metadatas"])
        assert result == {"ids": [], "documents": [], "metadatas": []}
        assert col.get.call_count == 0, "empty input must NOT call col.get"

    def test_single_chunk_when_input_below_batch_size(self):
        from unittest.mock import MagicMock
        from mempalace.reader import _chunked_get

        col = MagicMock()
        col.get.return_value = {
            "ids": ["a", "b", "c"],
            "documents": ["doc_a", "doc_b", "doc_c"],
            "metadatas": [{"x": 1}, {"x": 2}, {"x": 3}],
        }
        result = _chunked_get(col, ["a", "b", "c"], include=["documents", "metadatas"])
        assert col.get.call_count == 1
        assert result["ids"] == ["a", "b", "c"]
        assert result["documents"] == ["doc_a", "doc_b", "doc_c"]
        assert result["metadatas"] == [{"x": 1}, {"x": 2}, {"x": 3}]

    def test_chunks_input_larger_than_batch_into_multiple_calls(self):
        """1500 IDs with batch=500 must produce 3 col.get calls of 500 each."""
        from unittest.mock import MagicMock
        from mempalace.reader import _chunked_get

        col = MagicMock()

        def fake_get(ids, include):
            return {
                "ids": list(ids),
                "documents": [f"doc_{i}" for i in ids],
                "metadatas": [{"i": i} for i in ids],
            }

        col.get.side_effect = fake_get

        big_ids = [f"id_{i}" for i in range(1500)]
        result = _chunked_get(col, big_ids, include=["documents", "metadatas"], batch=500)

        assert col.get.call_count == 3, (
            f"1500 IDs / batch=500 must produce 3 calls; got {col.get.call_count}"
        )
        for i, call in enumerate(col.get.call_args_list):
            chunk = call.kwargs.get("ids") or call.args[0]
            assert len(chunk) == 500, f"call #{i}: expected 500 IDs, got {len(chunk)}"

        # Aggregation: all 1500 IDs in the merged result, in chunk order.
        assert len(result["ids"]) == 1500
        assert result["ids"] == big_ids
        assert len(result["documents"]) == 1500
        assert len(result["metadatas"]) == 1500

    def test_chunks_input_with_partial_last_batch(self):
        """1200 IDs with batch=500 → 500 + 500 + 200."""
        from unittest.mock import MagicMock
        from mempalace.reader import _chunked_get

        col = MagicMock()

        def fake_get(ids, include):
            return {
                "ids": list(ids),
                "documents": [f"doc_{i}" for i in ids],
                "metadatas": [{"i": i} for i in ids],
            }

        col.get.side_effect = fake_get
        result = _chunked_get(
            col, [f"id_{i}" for i in range(1200)], include=["documents", "metadatas"], batch=500
        )
        assert col.get.call_count == 3
        sizes = [len(call.kwargs.get("ids") or call.args[0]) for call in col.get.call_args_list]
        assert sizes == [500, 500, 200]
        assert len(result["ids"]) == 1200


class TestCmdReadCLISubprocess:
    """End-to-end smoke tests that mine a tiny fixture and shell out
    to ``mempalace read``. The unit suite can pass while the headline
    feature is broken (e.g., the verb returns a whole chunk instead
    of the requested line range) because the CLI wiring is untested
    by mocks alone. These subprocess tests close that gap.
    """

    def test_read_with_line_range_returns_only_requested_lines(self, tmp_path):
        """Mine a 12-line file. ``mempalace read "<date>:L3-L7 chat.md"``
        must return lines [3] through [7] only — no [1] [2] [8] [9] etc."""
        import subprocess
        import sys

        # Build a tiny source corpus.
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        # Use ASCII-only fixture content. The em-dash decorations that
        # were here originally trip Windows' source-encoding handling
        # in pytest assertion messages, causing the assertion-string
        # literal to mismatch the (correctly UTF-8) subprocess stdout.
        (src_dir / "chat.md").write_text(
            "\n".join(
                [
                    "line 1 hello",
                    "line 2 world",
                    "line 3 alpha",
                    "line 4 beta",
                    "line 5 gamma",
                    "line 6 delta",
                    "line 7 epsilon",
                    "line 8 zeta",
                    "line 9 eta",
                    "line 10 theta",
                    "line 11 iota",
                    "line 12 kappa",
                ]
            ),
            encoding="utf-8",
        )
        palace = tmp_path / "palace"

        # Mine the fixture. Allow generous timeout: cold-cache invocations
        # download a ~79 MB ONNX embedding model on first run.
        mine = subprocess.run(
            [sys.executable, "-m", "mempalace", "--palace", str(palace), "mine", str(src_dir)],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        assert mine.returncode == 0, f"mine failed: {mine.stderr}"

        # Run the verb under test.
        read = subprocess.run(
            [
                sys.executable,
                "-m",
                "mempalace",
                "--palace",
                str(palace),
                "read",
                "2024-11-08:L3-L7 chat.md",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert read.returncode == 0, f"read failed: {read.stderr}"
        # Surgical slice contract: ONLY lines [3] through [7].
        assert "[3] line 3 alpha" in read.stdout
        assert "[7] line 7 epsilon" in read.stdout
        # Hard negative: leaked lines outside the range would prove the
        # whole-chunk-leak regression class is back.
        assert "[1]" not in read.stdout, (
            f"unexpected leak of line 1; whole-chunk regression?\nstdout:\n{read.stdout}"
        )
        assert "[2]" not in read.stdout
        assert "[8]" not in read.stdout
        assert "[10]" not in read.stdout
        assert "[12]" not in read.stdout

    def test_read_garbage_pointer_exits_with_clear_error(self, tmp_path):
        """A malformed pointer must produce a clear stderr message and
        a non-zero exit code (not a traceback)."""
        import subprocess
        import sys

        palace = tmp_path / "palace"
        palace.mkdir()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "mempalace",
                "--palace",
                str(palace),
                "read",
                "totally not a pointer",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode != 0, (
            f"garbage pointer should exit non-zero; got {result.returncode}"
        )
        # Either the parser or the palace-open path should produce a
        # clear human-readable error, not a traceback.
        assert "Traceback" not in result.stderr
        assert (
            "could not parse" in result.stderr.lower()
            or "no drawers found" in result.stderr.lower()
            or "palace" in result.stderr.lower()
        )

    def test_read_help_shows_mutually_exclusive_drawer_all(self):
        """argparse must surface the --drawer/--all mutex group in
        ``read --help`` (Igor's optional-polish item)."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "mempalace", "read", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0
        # Both flags must appear in --help output. The exact layout of
        # mutex groups varies by terminal width; the load-bearing
        # contract is enforced via the rejected-combo check below.
        assert "--drawer" in result.stdout
        assert "--all" in result.stdout
        # Verify mutex enforcement: passing both must fail at parse time.
        rejected = subprocess.run(
            [
                sys.executable,
                "-m",
                "mempalace",
                "read",
                "--drawer",
                "1",
                "--all",
                "dummy_pointer",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert rejected.returncode != 0, "argparse should reject --drawer + --all combo"
        assert (
            "not allowed with" in rejected.stderr
            or "argument --all" in rejected.stderr
            or "mutually exclusive" in rejected.stderr.lower()
        ), f"argparse must surface the mutex constraint; got stderr={rejected.stderr!r}"
