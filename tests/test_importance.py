"""Tests for mempalace.importance and the ingestion write-sites (#2409).

Covers:
  1. The scorer itself (tier assignment, boundary cases, empty/None input)
  2. tool_add_drawer writing importance into the stored metadata (explicit +
     inferred) — exercised against the real backend the same way TestWriteTools
     does, then read back from the collection.
  3. miner._build_drawer_metadata writing importance (explicit + inferred)
  4. convo_miner single-exchange path writing importance
  5. End-to-end: a critical drawer sorts ABOVE a trivial one in Layer-1
"""

from unittest.mock import MagicMock, patch

from mempalace.importance import score_importance


# ──────────────────────────────────────────────────────────────────────
# 1. Scorer unit tests
# ──────────────────────────────────────────────────────────────────────


class TestScoreImportance:
    def test_none_returns_default(self):
        assert score_importance(None) == 3.0

    def test_empty_string_returns_default(self):
        assert score_importance("") == 3.0

    def test_whitespace_returns_default(self):
        assert score_importance("   \n\t  ") == 3.0

    def test_critical_allergy(self):
        assert score_importance("Patient has severe peanut allergy") == 5.0

    def test_critical_credential(self):
        assert score_importance("API key is sk-abcdefgh12345") == 5.0

    def test_critical_medication(self):
        assert score_importance("Current medication: 5mg lisinopril daily") == 5.0

    def test_critical_never_forget(self):
        assert score_importance("Never forget the code 2847") == 5.0

    def test_identity_name(self):
        assert score_importance("My name is Alice B.") == 4.0

    def test_identity_occupation(self):
        assert score_importance("I am a software engineer") == 4.0

    def test_identity_born_in(self):
        assert score_importance("I was born in 1985") == 4.0

    def test_default_technical(self):
        assert score_importance("Fixed the Python type annotation bug") == 3.0

    def test_default_meeting_notes(self):
        assert score_importance(
            "Discussed Q3 roadmap. Action items to be assigned by Friday."
        ) == 3.0

    def test_case_insensitive(self):
        assert score_importance("MY NAME IS Alice") == 4.0

    def test_multi_word_boundary(self):
        # "I am the owner" — identity tier via the "i am a/an/the" trigger
        assert score_importance("I am the project lead") == 4.0


# ──────────────────────────────────────────────────────────────────────
# 2. tool_add_drawer writes importance into stored metadata
# ──────────────────────────────────────────────────────────────────────


def _read_last_metadata(col):
    """Read back the most-recently-filed drawer's metadata from the real DB."""
    results = col.get(limit=1, include=["metadatas"])
    return results["metadatas"][0]


class TestToolAddDrawerImportance:
    """The MCP server path must stamp importance into the stored metadata."""

    def test_add_drawer_writes_inferred_importance(
        self, monkeypatch, config, palace_path, kg
    ):
        from tests.test_mcp_server import _get_collection, _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        result = tool_add_drawer(
            wing="personal",
            room="health",
            content="Patient has a severe peanut allergy — carry an EpiPen",
        )
        assert result["success"] is True
        meta = _read_last_metadata(col)
        assert "importance" in meta
        assert meta["importance"] == 5.0

    def test_add_drawer_explicit_importance_overrides_inference(
        self, monkeypatch, config, palace_path, kg
    ):
        from tests.test_mcp_server import _get_collection, _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        # Content that would infer as 5.0, but the caller pins it to 1.0.
        result = tool_add_drawer(
            wing="personal",
            room="health",
            content="Patient has a severe peanut allergy — carry an EpiPen",
            importance=1.0,
        )
        assert result["success"] is True
        meta = _read_last_metadata(col)
        assert meta["importance"] == 1.0

    def test_add_drawer_integer_importance_coerced_to_float(
        self, monkeypatch, config, palace_path, kg
    ):
        from tests.test_mcp_server import _get_collection, _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        result = tool_add_drawer(
            wing="personal",
            room="notes",
            content="Just a generic meeting note",
            importance=4,
        )
        assert result["success"] is True
        meta = _read_last_metadata(col)
        assert meta["importance"] == 4.0

    def test_add_drawer_default_importance_for_trivial_content(
        self, monkeypatch, config, palace_path, kg
    ):
        from tests.test_mcp_server import _get_collection, _patch_mcp_server

        _patch_mcp_server(monkeypatch, config, kg)
        _client, col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_add_drawer

        result = tool_add_drawer(
            wing="personal",
            room="notes",
            content="We discussed the timeline for the release",
        )
        assert result["success"] is True
        meta = _read_last_metadata(col)
        assert meta["importance"] == 3.0


# ──────────────────────────────────────────────────────────────────────
# 3. miner._build_drawer_metadata writes importance
# ──────────────────────────────────────────────────────────────────────


class TestMinerBuildDrawerMetadata:
    def test_inferred_importance_identity(self):
        from mempalace.miner import _build_drawer_metadata

        meta = _build_drawer_metadata(
            wing="proj",
            room="arch",
            source_file="design.md",
            chunk_index=0,
            agent="test",
            content="My name is Alice and I work at Acme Corp",
            source_mtime=None,
        )
        assert meta["importance"] == 4.0

    def test_inferred_importance_critical(self):
        from mempalace.miner import _build_drawer_metadata

        meta = _build_drawer_metadata(
            wing="proj",
            room="health",
            source_file="symptoms.md",
            chunk_index=0,
            agent="test",
            content="Patient reports a severe peanut allergy",
            source_mtime=None,
        )
        assert meta["importance"] == 5.0

    def test_explicit_importance_override(self):
        from mempalace.miner import _build_drawer_metadata

        meta = _build_drawer_metadata(
            wing="proj",
            room="arch",
            source_file="design.md",
            chunk_index=0,
            agent="test",
            content="just a generic note about the architecture",
            source_mtime=None,
            importance=5.0,
        )
        assert meta["importance"] == 5.0

    def test_default_importance(self):
        from mempalace.miner import _build_drawer_metadata

        meta = _build_drawer_metadata(
            wing="proj",
            room="notes",
            source_file="meeting.md",
            chunk_index=0,
            agent="test",
            content="We discussed the timeline for the release",
            source_mtime=None,
        )
        assert meta["importance"] == 3.0


# ──────────────────────────────────────────────────────────────────────
# 4. convo_miner single-exchange path writes importance
# ──────────────────────────────────────────────────────────────────────


class TestConvoMinerImportance:
    def test_file_conversation_exchange_includes_importance(self):
        from mempalace.convo_miner import file_conversation_exchange

        mock_col = MagicMock()
        with patch("mempalace.convo_miner.make_exchange_drawer_id", return_value="d1"):
            with patch("mempalace.convo_miner._detect_hall_cached", return_value="technical"):
                with patch("mempalace.convo_miner.entities_metadata", return_value={}):
                    file_conversation_exchange(
                        collection=mock_col,
                        wing="personal",
                        room="health",
                        source_file="dm.txt",
                        text="My blood type is O-negative",
                        agent="test",
                    )

        assert mock_col.upsert.called
        meta = mock_col.upsert.call_args.kwargs["metadatas"][0]
        assert "importance" in meta
        assert meta["importance"] == 5.0


# ──────────────────────────────────────────────────────────────────────
# 5. End-to-end: Layer-1 sort order reflects importance
# ──────────────────────────────────────────────────────────────────────


def test_layer1_critical_drawer_sorts_above_trivial():
    """A critical (5.0) drawer must appear BEFORE a default (3.0) drawer in
    the Layer-1 Essential Story, even when the critical one is older."""
    from mempalace.layers import Layer1

    docs = [
        "Patient has a severe peanut allergy — carry an EpiPen at all times",  # 5.0, old
        "We discussed the Q3 roadmap and agreed on the release schedule",      # 3.0, new
    ]
    metas = [
        {"room": "health", "importance": 5.0, "filed_at": "2026-01-01T00:00:00Z"},
        {"room": "meetings", "importance": 3.0, "filed_at": "2026-06-15T00:00:00Z"},
    ]

    mock_col = MagicMock()
    mock_col.get.side_effect = [
        {"documents": docs, "metadatas": metas},
        {"documents": [], "metadatas": []},
    ]

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        result = Layer1(palace_path="/fake").generate()

    critical_idx = result.index("EpiPen")
    trivial_idx = result.index("Q3 roadmap")
    assert critical_idx < trivial_idx, (
        f"critical@{critical_idx} should sort before trivial@{trivial_idx}"
    )


def test_layer1_identity_drawer_above_default():
    """Identity-tier (4.0) drawers must sort above default (3.0)."""
    from mempalace.layers import Layer1

    docs = [
        "Just a random note about the afternoon meeting",         # 3.0
        "My name is Alice and I work at Acme Corp as CTO",       # 4.0
    ]
    metas = [
        {"room": "notes", "importance": 3.0, "filed_at": "2026-06-01T00:00:00Z"},
        {"room": "identity", "importance": 4.0, "filed_at": "2026-01-01T00:00:00Z"},
    ]

    mock_col = MagicMock()
    mock_col.get.side_effect = [
        {"documents": docs, "metadatas": metas},
        {"documents": [], "metadatas": []},
    ]

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        result = Layer1(palace_path="/fake").generate()

    identity_idx = result.index("Alice")
    trivial_idx = result.index("random note")
    assert identity_idx < trivial_idx


def test_importance_end_to_end_from_add_drawer_to_layer1(
    monkeypatch, config, palace_path, kg
):
    """Full loop: file a critical + a trivial drawer through the real
    tool_add_drawer against the real backend, then confirm Layer-1 surfaces
    the critical one first. Proves the write-site and the reader agree on
    both the key name and the inferred value.
    """
    from mempalace.layers import Layer1
    from tests.test_mcp_server import _get_collection, _patch_mcp_server
    from mempalace.mcp_server import tool_add_drawer

    _patch_mcp_server(monkeypatch, config, kg)
    _client, col = _get_collection(palace_path, create=True)
    del _client

    tool_add_drawer(wing="p", room="health", content="severe peanut allergy, carry EpiPen at all times")
    tool_add_drawer(wing="p", room="notes", content="discussed the q3 release timeline")

    # Read back from the real DB and confirm the stamp
    rows = col.get(limit=2, include=["documents", "metadatas"])
    stamped = {d: m["importance"] for d, m in zip(rows["documents"], rows["metadatas"])}
    crit_val = [v for d, v in stamped.items() if "EpiPen" in d]
    assert crit_val and crit_val[0] == 5.0

    # Run Layer-1 against the real collection contents
    mock_col2 = MagicMock()
    mock_col2.get.side_effect = [
        {"documents": list(stamped.keys()), "metadatas": [{"room": "r", **{"importance": v, "filed_at": "2026-01-01T00:00:00Z"}} for v in stamped.values()]},
        {"documents": [], "metadatas": []},
    ]
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_lcfg,
        patch("mempalace.layers._get_collection", return_value=mock_col2),
    ):
        mock_lcfg.return_value.palace_path = "/fake"
        result = Layer1(palace_path="/fake").generate()

    assert result.index("EpiPen") < result.index("q3 release")

