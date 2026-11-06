"""Offline acceptance tests for examples/parallel_search_mcp.py (issue #2720).

Acceptance gate per the issue discussion:
  1.  Cancellation — a non-confirmation ("n", empty, EOF) files nothing.
  2.  Text preservation  — the extracted text is stored byte-for-byte, and the
      provenance (source URL) is recorded without touching the text.
  3.  Drawer write → recall round-trip — the exact kwargs the example builds
      feed ``mempalace_add_drawer`` and come back unchanged through
      ``mempalace_get_drawer``.

Everything runs offline: no live Parallel Search server, no network calls, and
Chroma's local default embedder (no external embedding service).  The Parallel
Search network layer is stubbed at the ``_mcp_tool_first_match`` seam.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))

import parallel_search_mcp as psm
from _mcp_server_helpers import _patch_mcp_server

# ── fixtures ───────────────────────────────────────────────────────────


def _make_input(value):
    """Build a drop-in ``builtins.input`` replacement that returns ``value``.

    Hoisted to module scope so the fake does not capture a loop variable
    (ruff B023).  ``confirm()`` calls ``input(prompt)`` — the prompt is a
    positional arg — so the fake ignores it and returns the canned answer.
    """

    def _input(*_a, **_k):
        return value

    return _input


@pytest.fixture
def fake_parallel(monkeypatch):
    """Stub the Parallel Search MCP transport seam (offline network layer)."""
    fake = {
        "results": [
            {"title": "T1", "url": "https://example.com/a", "snippet": "s1"},
            {"title": "T2", "url": "https://example.com/b", "snippet": "s2"},
        ],
        "fetched": {
            "https://example.com/a": "\ufeffBOM \u2014 exact text \u00e9\u00e8\u00ea\nline2",
            "https://example.com/b": "b content",
        },
    }

    class _Block:
        def __init__(self, type_: str, text: str):
            self.type = type_
            self.text = text

    class _Result:
        content = [_Block("text", "")]

    async def fake_mcp_tool_first_match(tool_names, arguments, block_type="text"):
        if "query" in arguments:
            args = ("web_search", "search", "web_search_v2")
            assert tool_names == args or "search" in tool_names
            fake["search_calls"].append({"tools": list(tool_names), "args": dict(arguments)})
            _Result.content[0].text = json.dumps(
                {
                    "results": [
                        {"title": r["title"], "url": r["url"], "description": r["snippet"]}
                        for r in fake["results"]
                    ]
                }
            )
        else:
            assert "url" in arguments
            fake["fetch_calls"].append({"tools": list(tool_names), "args": dict(arguments)})
            _Result.content[0].text = json.dumps({"text": fake["fetched"][arguments["url"]]})
        return _Result()

    fake["search_calls"] = []
    fake["fetch_calls"] = []
    monkeypatch.setattr(psm, "HAS_MCP_CLIENT", True)
    monkeypatch.setattr(psm, "_mcp_tool_first_match", fake_mcp_tool_first_match)
    fake["result_cls"] = _Result
    return fake


# ── 1. Cancellation — nothing is filed on non-confirmation ────────────


class TestCancellation:
    def test_yes_variants_confirm(self, monkeypatch):
        for value, expected in [
            ("y", True),
            ("Y", True),
            ("yes", True),
            (" YES ", True),
            ("n", False),
            ("", False),
            ("no", False),
            ("x", False),
        ]:
            monkeypatch.setattr("builtins.input", _make_input(value))
            assert psm.confirm() is expected, value

    def test_eof_cancels(self, monkeypatch):
        def eof_input(*a, **k):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof_input)
        assert psm.confirm() is False

    def test_dry_run_files_nothing(self, fake_parallel, monkeypatch):
        """A cancelled (dry-run) flow returns the payload but must NOT hit the
        MCP HTTP path — assert the transport is never invoked."""
        calls = []
        monkeypatch.setattr(
            psm,
            "_post_jsonrpc",
            lambda *a, **k: calls.append((a, k)) or {"success": True, "drawer_id": "x"},
        )
        item = psm.build_captured("w", "r", "content", "https://example.com/a")
        result = psm.file_captured("http://mcp", "", item, dry_run=True)
        assert result["success"] is True
        assert result["dry_run"] is True
        assert calls == [], "dry-run must not call the MCP HTTP transport"

    def test_confirmed_flow_files(self, fake_parallel, monkeypatch):
        calls = []
        monkeypatch.setattr(
            psm,
            "_post_jsonrpc",
            lambda *a, **k: calls.append((a, k)) or {"success": True, "drawer_id": "d1"},
        )
        item = psm.build_captured("w", "r", "content", "https://example.com/a")
        result = psm.file_captured("http://mcp", "", item, dry_run=False)
        assert result == {"success": True, "drawer_id": "d1"}
        assert len(calls) == 1
        url, name, kwargs = calls[0][0][:3]
        assert name == "mempalace_add_drawer"


# ── 2. Text preservation — byte-for-byte + provenance ─────────────────


class TestTextPreservation:
    @pytest.mark.parametrize(
        "text",
        [
            "plain",
            "\ufeffBOM \u2014 exact text \u00e9\u00e8\u00ea\nline2\n",
            "a" * 50000 + "\n" * 200,
            "trailing spaces\t\t\n",
        ],
    )
    def test_content_stored_unchanged(self, text):
        item = psm.build_captured("w", "r", text, "https://example.com/a")
        kwargs = item.to_add_drawer_kwargs()
        assert kwargs["content"] == text, "stored text must be byte-for-byte identical"
        assert kwargs["wing"] == "w"
        assert kwargs["room"] == "r"

    def test_provenance_records_source_unmodified(self):
        item = psm.build_captured("w", "r", "text", "https://example.com/a?q=x")
        kwargs = item.to_add_drawer_kwargs()
        assert kwargs["content"] == "text"
        assert kwargs["source_file"].startswith(psm.PROVENANCE_SOURCE)
        assert "https://example.com/a?q=x" in kwargs["source_file"]
        assert kwargs["added_by"] == psm.ADDED_BY_LABEL
        assert psm.ADDED_BY_LABEL.startswith("parallel_search")

    def test_preview_is_display_only(self):
        long = "x" * 100000
        shown = psm.preview(long, limit=1000)
        assert len(shown) < len(long)
        assert "100000" in shown or "99" in shown
        full = psm.preview("short", limit=1000)
        assert full == "short"


# ── 3. Write → recall round-trip through the real MCP server path ─────


class TestRoundTrip:
    def test_add_drawer_then_get_drawer_returns_exact_text(
        self, monkeypatch, config, collection, kg
    ):
        """The exact kwargs the example produces flow through
        ``mempalace_add_drawer`` and ``mempalace_get_drawer`` byte-for-byte."""
        from mempalace import mcp_server

        _patch_mcp_server(monkeypatch, config, kg)

        exact = (
            (
                "\ufeffBOM \u2014 exact text \u00e9\u00e8\u00ea\n"
                "line2 with tab\tand emoji \U0001f600\n"
            )
            + ("x" * 4000)
            + "\n"
            + "final line"
        )
        assert len(exact) < 100_000, len(exact)  # must fit comfortably within the 100k cap
        item = psm.build_captured(
            "research", "captured_web", exact, "https://example.com/doc?section=2"
        )
        kwargs = item.to_add_drawer_kwargs()

        added = mcp_server.tool_add_drawer(**kwargs)
        assert added["success"], added
        drawer_id = added["drawer_id"]

        got = mcp_server.tool_get_drawer(drawer_id)
        assert "error" not in got, got
        assert got["content"] == exact, "recall must return the stored text byte-for-byte"
        # get_drawer strips source_file to Path(source_file).name
        src = (
            got["metadata"].get("source_file", "")
            if "metadata" in got
            else got.get("source_file", "")
        )
        assert "doc?section=2" in src, f"URL tail missing from source_file: {src!r}"

    def test_duplicate_file_is_idempotent(self, monkeypatch, config, collection, kg):
        from mempalace import mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        kwargs = psm.build_captured(
            "research", "captured_web", "idempotent body", "https://example.com/dup"
        ).to_add_drawer_kwargs()
        first = mcp_server.tool_add_drawer(**kwargs)
        second = mcp_server.tool_add_drawer(**kwargs)
        assert first["success"] and second["success"]
        assert first["drawer_id"] == second["drawer_id"]

    def test_roundtrip_of_parallel_stubbed_text(
        self, fake_parallel, monkeypatch, config, collection, kg
    ):
        """End-to-end offline: stubbed Parallel fetch text → confirm → add → get."""
        from mempalace import mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        fetched_text = fake_parallel["fetched"]["https://example.com/a"]

        import asyncio

        results = asyncio.new_event_loop().run_until_complete(
            psm.parallel_search("rust async runtime comparison 2025")
        )
        assert results[0].url == "https://example.com/a"

        raw = asyncio.new_event_loop().run_until_complete(
            psm.parallel_fetch("https://example.com/a")
        )
        assert raw == fetched_text, "stub fetch text must arrive unchanged"

        item = psm.build_captured("research", "captures", raw, results[0].url)
        added = mcp_server.tool_add_drawer(**item.to_add_drawer_kwargs())
        assert added["success"]

        got = mcp_server.tool_get_drawer(added["drawer_id"])
        assert got["content"] == fetched_text


# ── module contract ───────────────────────────────────────────────────


def test_constants_and_public_api():
    assert psm.PARALLEL_SEARCH_MCP_URL == "https://search.parallel.ai/mcp"
    assert psm.DEFAULT_MEMPALACE_URL.startswith("http://")
    for name in (
        "CapturedItem",
        "SearchResult",
        "build_captured",
        "preview",
        "confirm",
        "file_captured",
        "parallel_search",
        "parallel_fetch",
        "parse_args",
        "main",
    ):
        assert hasattr(psm, name), name
    assert not psm.ADDED_BY_LABEL or psm.ADDED_BY_LABEL.startswith("parallel_search")


def test_arg_defaults():
    args = psm.parse_args([])
    assert args.wing == "research"
    assert args.room == "captured_web"
    assert args.auto is False
    assert args.dry_run is False
