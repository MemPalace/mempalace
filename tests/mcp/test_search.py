"""MCP server tests — search tool and date-window filters."""

import json
from types import SimpleNamespace
import sys
import warnings
from unittest.mock import MagicMock

import pytest

from _mcp_server_helpers import (
    _patch_mcp_server,
)


class TestSearchTool:
    def test_search_basic(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="JWT authentication tokens")
        assert "results" in result
        assert len(result["results"]) > 0
        # Top result should be the auth drawer
        top = result["results"][0]
        assert "JWT" in top["text"] or "authentication" in top["text"].lower()

    def test_search_with_wing_filter(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="planning", wing="notes")
        assert all(r["wing"] == "notes" for r in result["results"])

    def test_search_with_room_filter(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="database", room="backend")
        assert all(r["room"] == "backend" for r in result["results"])

    def test_search_with_source_file_filter(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="authentication module", source_file="auth.py")
        assert result["results"]
        assert all(r["source_file"] == "auth.py" for r in result["results"])
        assert result["filters"]["source_file"] == "auth.py"

    def test_search_source_file_allows_path_separators(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        # Unlike wing/room, a source_file is a path — '/' must NOT be rejected
        # as a path-traversal attempt the way sanitize_name() would.
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="authentication", source_file="/abs/path/to/auth.py")
        assert "error" not in result

    def test_search_blank_source_file_ignored(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="JWT authentication", source_file="   ")
        assert "results" in result
        assert result["filters"]["source_file"] is None

    def test_search_rejects_null_byte_source_file(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        # A null byte in a metadata where-value can crash chromadb add/upsert
        # (#1235 lineage); reject it cleanly the way sanitize_name does.
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="JWT", source_file="bad\x00null")
        assert "error" in result

    def test_search_rejects_overlong_source_file(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="JWT", source_file="x" * 5000)
        assert "error" in result

    def test_search_rejects_non_string_source_file(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        # A non-string source_file (e.g. a JSON number, which the schema's
        # string type does not coerce) must yield a clean validation error,
        # not an unhandled AttributeError from .strip().
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="JWT", source_file=42)
        assert "error" in result

    def test_search_rejects_lone_surrogate_source_file(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        # A lone UTF-16 surrogate can crash chromadb (#1235); reject it for
        # parity with sanitize_name rather than letting it reach the backend.
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(query="JWT", source_file="bad\udc80surrogate")
        assert "error" in result

    def test_search_accepts_source_file_at_length_boundary(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        # Exactly _MAX_SOURCE_FILE_LENGTH is allowed (the cap is a strict '>').
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import _MAX_SOURCE_FILE_LENGTH, tool_search

        result = tool_search(query="JWT", source_file="x" * _MAX_SOURCE_FILE_LENGTH)
        assert "error" not in result

    def test_search_min_similarity_backwards_compat(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        """Old min_similarity param still works via backwards-compat shim."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        # Old name should work
        result = tool_search(query="JWT", min_similarity=1.5)
        assert "results" in result

        # Old name takes precedence when both provided
        result_strict = tool_search(query="JWT", max_distance=999.0, min_similarity=0.01)
        result_loose = tool_search(query="JWT", max_distance=0.01, min_similarity=999.0)
        assert len(result_strict["results"]) <= len(result_loose["results"])

    def test_search_passes_candidate_strategy(self, monkeypatch, config, kg):
        """MCP callers can opt into backend BM25/vector union candidate gathering."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        seen = {}

        def fake_search(*args, **kwargs):
            seen["candidate_strategy"] = kwargs.get("candidate_strategy")
            return {"results": []}

        monkeypatch.setattr(mcp_server, "search_memories", fake_search)

        result = mcp_server.tool_search(query="rareterm", candidate_strategy="union")

        assert "error" not in result
        assert seen["candidate_strategy"] == "union"

    def test_search_preserves_default_max_distance(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        seen = {}

        def fake_search(*args, **kwargs):
            seen["max_distance"] = kwargs.get("max_distance")
            return {"results": []}

        monkeypatch.setattr(mcp_server, "search_memories", fake_search)

        result = mcp_server.tool_search(query="needle")

        assert "error" not in result
        assert seen["max_distance"] == 1.5

    def test_search_cli_compatible_reuses_hub_collection(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        collection = object()
        seen = {}
        monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
        monkeypatch.setattr(mcp_server, "_vector_disabled", False)
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: collection)

        def fake_cli_search(**kwargs):
            seen.update(kwargs)

        monkeypatch.setattr(mcp_server, "cli_search", fake_cli_search)
        monkeypatch.setattr(mcp_server, "_capture_fd_stdout", lambda fn: (fn(), "exact CLI\n"))

        result = mcp_server.tool_search(query="needle", limit=7, cli_compatible=True)

        assert result == {"query": "needle", "cli_output": "exact CLI\n"}
        assert seen["collection"] is collection
        assert seen["n_results"] == 7

    def test_search_cli_compatible_rejects_embedder_identity_mismatch(
        self, monkeypatch, config, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import embedding, mcp_server, palace
        from mempalace.backends.base import EmbedderIdentity

        collection = MagicMock()
        collection.effective_embedder_identity.return_value = None
        collection.get_stored_embedder_identity.return_value = EmbedderIdentity("minilm", 384)
        monkeypatch.setattr(embedding, "current_model_name", lambda: "embeddinggemma")
        monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
        monkeypatch.setattr(mcp_server, "_vector_disabled", False)
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: collection)
        monkeypatch.setattr(mcp_server, "cli_search", lambda **_kwargs: pytest.fail())
        palace._VALIDATED_IDENTITY.clear()

        result = mcp_server.tool_search(query="needle", cli_compatible=True)

        assert result["error"] == "Embedder identity mismatch"
        assert "minilm" in result["details"]
        assert "embeddinggemma" in result["details"]

    def test_search_cli_compatible_repeats_unknown_embedder_warning(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import embedding, mcp_server, palace

        collection = MagicMock()
        collection.effective_embedder_identity.return_value = None
        collection.get_stored_embedder_identity.return_value = None
        collection.count.return_value = 1
        monkeypatch.setattr(embedding, "current_model_name", lambda: "minilm")
        monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
        monkeypatch.setattr(mcp_server, "_vector_disabled", False)
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: collection)
        monkeypatch.setattr(mcp_server, "cli_search", lambda **_kwargs: None)
        monkeypatch.setattr(mcp_server, "_capture_fd_stdout", lambda fn: (fn(), "output\n"))
        palace._VALIDATED_IDENTITY.clear()

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("always")
                warnings.showwarning = lambda message, *_args, **_kwargs: print(
                    message, file=sys.stderr
                )
                results = [
                    mcp_server.tool_search(query="needle", cli_compatible=True) for _ in range(2)
                ]
        finally:
            palace._VALIDATED_IDENTITY.clear()

        for result in results:
            assert result["cli_output"] == "output\n"
            assert "no recorded embedder identity" in result["cli_error_output"]
            assert "mempalace palace set-embedder" in result["cli_error_output"]

    def test_search_cli_compatible_serializes_output_capture(self, monkeypatch, config, kg):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server, palace

        monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
        monkeypatch.setattr(mcp_server, "_vector_disabled", False)
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: object())
        monkeypatch.setattr(palace, "_enforce_embedder_identity", lambda *_a, **_kw: None)
        monkeypatch.setattr(mcp_server, "cli_search", lambda **_kwargs: None)

        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def capture(fn):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.1)
                return fn(), "output\n"
            finally:
                with state_lock:
                    active -= 1

        monkeypatch.setattr(mcp_server, "_capture_fd_stdout", capture)
        start = threading.Barrier(3)

        def invoke():
            start.wait()
            return mcp_server.tool_search(query="needle", cli_compatible=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(invoke) for _ in range(2)]
            start.wait()
            results = [future.result(timeout=5) for future in futures]

        assert [result["cli_output"] for result in results] == ["output\n", "output\n"]
        assert max_active == 1

    def test_search_cli_compatible_rejects_source_file(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_get_collection", lambda: pytest.fail())
        result = mcp_server.tool_search(query="needle", source_file="notes.md", cli_compatible=True)

        assert "source_file" in result["error"]

    @pytest.mark.parametrize(
        ("arguments", "control"),
        [
            ({"candidate_strategy": "union"}, "candidate_strategy"),
            ({"min_similarity": 0.5}, "min_similarity"),
            ({"max_distance": 0.5}, "max_distance"),
            ({"context": "background"}, "context"),
        ],
    )
    def test_search_cli_compatible_rejects_ignored_controls(
        self, monkeypatch, config, kg, arguments, control
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_get_collection", lambda: pytest.fail())
        result = mcp_server.tool_search(query="needle", cli_compatible=True, **arguments)

        assert control in result["error"]

    def test_search_cli_compatible_returns_stderr(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
        monkeypatch.setattr(mcp_server, "_vector_disabled", False)
        monkeypatch.setattr(mcp_server, "_get_collection", lambda: object())

        def fake_cli_search(**_kwargs):
            print("legacy metric warning", file=sys.stderr)

        monkeypatch.setattr(mcp_server, "cli_search", fake_cli_search)
        monkeypatch.setattr(mcp_server, "_capture_fd_stdout", lambda fn: (fn(), "CLI output\n"))

        result = mcp_server.tool_search(query="needle", cli_compatible=True)

        assert result == {
            "query": "needle",
            "cli_output": "CLI output\n",
            "cli_error_output": "legacy metric warning\n",
        }

    def test_search_rejects_invalid_candidate_strategy(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "search_memories", lambda *a, **kw: pytest.fail())

        result = mcp_server.tool_search(query="rareterm", candidate_strategy="bm25")

        assert "error" in result
        assert "candidate_strategy" in result["error"]

    def test_list_rooms_rejects_invalid_wing(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_get_collection", lambda: pytest.fail())

        result = mcp_server.tool_list_rooms(wing="../etc/passwd")
        assert "error" in result

    def test_search_rejects_invalid_room(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "search_memories", lambda: pytest.fail())

        result = mcp_server.tool_search(query="JWT", room="../backend")
        assert "error" in result

    def test_search_retries_once_on_hnsw_flush_transient(self, monkeypatch, config, kg):
        """Issue #1315: post-bulk-mine 'Error finding id' is retried once."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        calls = {"n": 0}
        reset_calls = {"n": 0}

        def fake_search(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "error": "Search error: Error executing plan: Internal error: Error finding id"
                }
            return {"results": [{"text": "ok", "wing": "w", "room": "r"}]}

        def fake_reset():
            reset_calls["n"] += 1

        monkeypatch.setattr(mcp_server, "search_memories", fake_search)
        monkeypatch.setattr(mcp_server, "_force_chroma_cache_reset", fake_reset)
        monkeypatch.setattr(mcp_server.time, "sleep", lambda _: None)

        result = mcp_server.tool_search(query="anything")

        assert calls["n"] == 2
        assert reset_calls["n"] == 1
        assert "results" in result
        assert result.get("index_recovered") is True

    def test_search_retry_preserves_collection_name(self, monkeypatch, config, kg):
        """Retry path must query the same configured collection both times."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(
            mcp_server,
            "_config",
            SimpleNamespace(
                palace_path=config.palace_path,
                collection_name="custom_drawers",
            ),
        )
        seen_calls = []

        def fake_search(*args, **kwargs):
            seen_calls.append((kwargs.get("collection_name"), kwargs.get("candidate_strategy")))
            if len(seen_calls) == 1:
                return {
                    "error": "Search error: Error executing plan: Internal error: Error finding id"
                }
            return {"results": [{"text": "ok", "wing": "w", "room": "r"}]}

        monkeypatch.setattr(mcp_server, "search_memories", fake_search)
        monkeypatch.setattr(mcp_server, "_force_chroma_cache_reset", lambda: None)
        monkeypatch.setattr(mcp_server.time, "sleep", lambda _: None)

        result = mcp_server.tool_search(
            query="anything", wing="wing_api", candidate_strategy="union"
        )

        assert "results" in result
        assert seen_calls == [("custom_drawers", "union"), ("custom_drawers", "union")]

    def test_search_does_not_retry_on_non_transient_error(self, monkeypatch, config, kg):
        """Validation / unrelated errors must not trigger the retry path."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        calls = {"n": 0}

        def fake_search(*args, **kwargs):
            calls["n"] += 1
            return {"error": "Search error: invalid query syntax"}

        monkeypatch.setattr(mcp_server, "search_memories", fake_search)

        result = mcp_server.tool_search(query="anything")

        assert calls["n"] == 1
        assert "error" in result
        assert "index_recovered" not in result

    def test_search_returns_second_error_if_retry_also_fails(self, monkeypatch, config, kg):
        """If the transient persists past the retry, surface the second error."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        calls = {"n": 0}

        def fake_search(*args, **kwargs):
            calls["n"] += 1
            return {"error": "Search error: Error executing plan: Internal error: Error finding id"}

        monkeypatch.setattr(mcp_server, "search_memories", fake_search)
        monkeypatch.setattr(mcp_server, "_force_chroma_cache_reset", lambda: None)
        monkeypatch.setattr(mcp_server.time, "sleep", lambda _: None)

        result = mcp_server.tool_search(query="anything")

        assert calls["n"] == 2
        assert "error" in result
        assert "index_recovered" not in result

    def test_search_retries_once_on_stale_index_error(self, monkeypatch, config, kg):
        """Stale-index errors should trigger one cache-reset retry."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        calls = {"n": 0}
        reset_calls = {"n": 0}

        def fake_search(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"error": "Search error: stale-index detected; retry recommended"}
            return {"results": [{"text": "ok", "wing": "w", "room": "r"}]}

        def fake_reset():
            reset_calls["n"] += 1

        monkeypatch.setattr(mcp_server, "search_memories", fake_search)
        monkeypatch.setattr(mcp_server, "_force_chroma_cache_reset", fake_reset)
        monkeypatch.setattr(mcp_server.time, "sleep", lambda _: None)

        result = mcp_server.tool_search(query="anything")

        assert calls["n"] == 2
        assert reset_calls["n"] == 1
        assert "results" in result
        assert result.get("index_recovered") is True

    def test_list_drawers_rejects_invalid_wing(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_get_collection", lambda: pytest.fail())

        result = mcp_server.tool_list_drawers(wing="../notes")
        assert "error" in result

    def test_find_tunnels_rejects_invalid_wing(self, monkeypatch, config, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_get_collection", lambda: pytest.fail())

        result = mcp_server.tool_find_tunnels(wing_a="../project")
        assert "error" in result

    def test_wal_redacts_sensitive_fields(self, monkeypatch, config, kg, tmp_path):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import wal

        wal_file = tmp_path / "write_log.jsonl"
        monkeypatch.setattr(wal, "_WAL_FILE", wal_file)

        wal._wal_log(
            "test",
            {"content": "secret note", "query": "private search", "safe": "ok"},
        )

        entry = json.loads(wal_file.read_text().strip())
        assert entry["params"]["content"].startswith("[REDACTED")
        assert entry["params"]["query"].startswith("[REDACTED")
        assert entry["params"]["safe"] == "ok"


# ── Write Tools ─────────────────────────────────────────────────────────


class TestSearchDateFilters:
    """tool_search since/before window (#463) — MCP surface.

    Window semantics and helpers are shared with list_drawers (#1128) via
    mempalace.date_window; seeded filed_at values are 2026-01-01..01-04.
    """

    BROAD = "authentication database frontend sprint planning"

    def test_search_since_inclusive(self, monkeypatch, config, palace_path, seeded_collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(self.BROAD, limit=10, since="2026-01-03")
        assert "error" not in result
        got = sorted(r["created_at"][:10] for r in result["results"])
        assert got == ["2026-01-03", "2026-01-04"]

    def test_search_window_composes_with_wing(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(self.BROAD, limit=10, wing="project", before="2026-01-03")
        got = {(r["wing"], r["created_at"][:10]) for r in result["results"]}
        assert got == {("project", "2026-01-01"), ("project", "2026-01-02")}

    def test_search_invalid_since_is_clean_error(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search("anything", since="next tuesday")
        assert set(result) == {"error"}
        assert "since" in result["error"]

    def test_search_inverted_window_is_clean_error(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search("anything", since="2026-01-04", before="2026-01-01")
        assert set(result) == {"error"}
        assert "must be earlier than" in result["error"]

    def test_search_filters_envelope_includes_window(
        self, monkeypatch, config, palace_path, seeded_collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_search

        result = tool_search(self.BROAD, since="2026-01-02", before="2026-01-04")
        assert result["filters"]["since"] == "2026-01-02"
        assert result["filters"]["before"] == "2026-01-04"

    def test_search_schema_declares_window_properties(self):
        from mempalace.mcp_server import TOOLS

        schema = TOOLS["mempalace_search"]["input_schema"]
        assert "since" in schema["properties"]
        assert "before" in schema["properties"]
        assert schema["properties"]["since"]["type"] == "string"
        assert schema["properties"]["before"]["type"] == "string"


def test_2288_grouped_graph_stats_count_distinct_room_instances(monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "_load_graph_tunnels",
        lambda config=None: [{"id": "t1"}, {"id": "t2"}],
    )
    rows = [
        ("fact", "desercion", "facts", 1, "2026-01-01"),
        ("general", "desercion-pascual", "misc", 1, "2026-01-01"),
        ("general", "desertion", "misc", 2, "2026-01-02"),
        # Same placement, different hall: this must not add a room instance.
        ("general", "desertion", "other", 3, "2026-01-03"),
        ("heatstgnn-model-selection", "desertion", "models", 1, "2026-01-01"),
        ("diary", "desertion", "journal", 1, "2026-01-01"),
        ("general", "matlab-drive", "misc", 1, "2026-01-01"),
        ("documentation", "octopus", "docs", 1, "2026-01-01"),
        ("plans", "octopus", "plans", 1, "2026-01-01"),
        ("controller", "octopus", "control", 1, "2026-01-01"),
    ]
    stats = mcp_server._graph_stats_from_grouped_rows(rows)

    assert stats["total_rooms"] == 7
    assert stats["total_room_instances"] == 9
    assert stats["tunnel_rooms"] == stats["passive_tunnel_rooms"] == 1
    assert stats["explicit_tunnels"] == 2
    assert stats["total_connections"] == stats["total_edges"] + 2
    assert set(stats["rooms_per_wing"]) == {
        "desercion",
        "desercion-pascual",
        "desertion",
        "matlab-drive",
        "octopus",
    }
