"""Tests for mempalace.layers — Layer0, Layer1, Layer2, Layer3, MemoryStack."""

import os
from unittest.mock import MagicMock, patch

from mempalace.layers import Layer0, Layer1, Layer2, Layer3, MemoryStack


# ── Layer0 — with identity file ─────────────────────────────────────────


def test_layer0_reads_identity_file(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas, a personal AI assistant for Alice.")
    layer = Layer0(identity_path=str(identity_file))
    text = layer.render()
    assert "Atlas" in text
    assert "Alice" in text


def test_layer0_caches_text(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("Hello world")
    layer = Layer0(identity_path=str(identity_file))
    first = layer.render()
    identity_file.write_text("Changed content")
    second = layer.render()
    assert first == second
    assert second == "Hello world"


def test_layer0_missing_file_returns_default(tmp_path):
    missing = str(tmp_path / "nonexistent.txt")
    layer = Layer0(identity_path=missing)
    text = layer.render()
    assert "No identity configured" in text
    assert "identity.txt" in text


def test_layer0_token_estimate(tmp_path):
    identity_file = tmp_path / "identity.txt"
    content = "A" * 400
    identity_file.write_text(content)
    layer = Layer0(identity_path=str(identity_file))
    estimate = layer.token_estimate()
    assert estimate == 100


def test_layer0_token_estimate_empty(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("")
    layer = Layer0(identity_path=str(identity_file))
    assert layer.token_estimate() == 0


def test_layer0_strips_whitespace(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("  Hello world  \n\n")
    layer = Layer0(identity_path=str(identity_file))
    text = layer.render()
    assert text == "Hello world"


def test_layer0_default_path():
    layer = Layer0()
    expected = os.path.expanduser("~/.mempalace/identity.txt")
    assert layer.path == expected


# ── Layer1 — mocked chromadb ────────────────────────────────────────────


def _mock_chromadb_for_layer(docs, metas, monkeypatch=None):
    """Return a mock collection whose get() returns docs/metas."""
    mock_col = MagicMock()
    # First batch returns data, second batch returns empty (end of pagination)
    mock_col.get.side_effect = [
        {"documents": docs, "metadatas": metas},
        {"documents": [], "metadatas": []},
    ]
    return mock_col


def test_layer1_no_palace():
    """Layer1 returns helpful message when no palace exists."""
    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent/palace"
        layer = Layer1(palace_path="/nonexistent/palace")
    result = layer.generate()
    assert "No palace found" in result or "No memories" in result


def test_layer1_generates_essential_story():
    docs = [
        "Important memory about project decisions",
        "Key architectural choice for the backend",
    ]
    metas = [
        {"room": "decisions", "source_file": "meeting.txt", "importance": 5},
        {"room": "architecture", "source_file": "design.txt", "importance": 4},
    ]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert "ESSENTIAL STORY" in result
    assert "project decisions" in result


def test_layer1_empty_palace():
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": [], "metadatas": []}
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert "No memories" in result


def test_layer1_with_wing_filter():
    docs = ["Memory about project X"]
    metas = [{"room": "general", "source_file": "x.txt", "importance": 3}]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake", wing="project_x")
        result = layer.generate()

    assert "ESSENTIAL STORY" in result
    # Verify wing filter was passed
    call_kwargs = mock_col.get.call_args_list[0][1]
    assert call_kwargs.get("where") == {"wing": "project_x"}


def test_layer1_truncates_long_snippets():
    docs = ["A" * 300]
    metas = [{"room": "general", "source_file": "long.txt"}]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert "..." in result


def test_layer1_respects_max_chars():
    """L1 stops adding entries once MAX_CHARS is reached."""
    docs = [f"Memory number {i} with substantial content padding here" for i in range(30)]
    metas = [{"room": "general", "source_file": f"f{i}.txt", "importance": 5} for i in range(30)]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        layer.MAX_CHARS = 200  # Very low cap to trigger truncation
        result = layer.generate()

    assert "more in L3 search" in result


def test_layer1_importance_from_various_keys():
    """Layer1 tries importance, emotional_weight, weight keys."""
    docs = ["mem1", "mem2", "mem3"]
    metas = [
        {"room": "r", "emotional_weight": 5},
        {"room": "r", "weight": 1},
        {"room": "r"},  # no weight key, defaults to 3
    ]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert "ESSENTIAL STORY" in result


def test_layer1_batch_exception_breaks():
    """If col.get raises on a batch, loop breaks gracefully."""
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        {"documents": ["doc1"], "metadatas": [{"room": "r"}]},
        RuntimeError("batch error"),
    ]
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert "ESSENTIAL STORY" in result


# ── Layer2 — mocked chromadb ────────────────────────────────────────────


def test_layer2_no_palace():
    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent/palace"
        layer = Layer2(palace_path="/nonexistent/palace")
    result = layer.retrieve(wing="test")
    assert "No palace found" in result


def test_layer2_retrieve_with_wing():
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "documents": ["Some memory about the project"],
        "metadatas": [{"room": "backend", "source_file": "notes.txt"}],
    }
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(wing="project")

    assert "ON-DEMAND" in result
    assert "memory about the project" in result


def test_layer2_retrieve_with_room():
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "documents": ["Backend architecture notes"],
        "metadatas": [{"room": "architecture", "source_file": "arch.txt"}],
    }
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(room="architecture")

    assert "ON-DEMAND" in result


def test_layer2_retrieve_wing_and_room():
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "documents": ["Filtered result"],
        "metadatas": [{"room": "backend", "source_file": "x.txt"}],
    }
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(wing="proj", room="backend")

    assert "ON-DEMAND" in result
    call_kwargs = mock_col.get.call_args[1]
    assert "$and" in call_kwargs.get("where", {})


def test_layer2_retrieve_empty():
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": [], "metadatas": []}
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(wing="missing")

    assert "No drawers found" in result


def test_layer2_retrieve_no_filter():
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": [], "metadatas": []}
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        layer.retrieve()

    # No where filter should be passed
    call_kwargs = mock_col.get.call_args[1]
    assert "where" not in call_kwargs


def test_layer2_retrieve_error():
    mock_col = MagicMock()
    mock_col.get.side_effect = RuntimeError("db error")
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(wing="test")

    assert "Retrieval error" in result


def test_layer2_truncates_long_snippets():
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "documents": ["B" * 400],
        "metadatas": [{"room": "r", "source_file": "s.txt"}],
    }
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(wing="test")

    assert "..." in result


# ── Layer3 — mocked chromadb ────────────────────────────────────────────


def _mock_query_results(docs, metas, dists):
    return {
        "documents": [docs],
        "metadatas": [metas],
        "distances": [dists],
    }


def test_layer3_no_palace():
    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent/palace"
        layer = Layer3(palace_path="/nonexistent/palace")
    result = layer.search("test query")
    assert "No palace found" in result


def test_layer3_search_raw_no_palace():
    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent/palace"
        layer = Layer3(palace_path="/nonexistent/palace")
    result = layer.search_raw("test query")
    assert result == []


def test_layer3_search_with_results():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["Found this important memory"],
        [{"wing": "project", "room": "backend", "source_file": "notes.txt"}],
        [0.2],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        result = layer.search("important")

    assert "SEARCH RESULTS" in result
    assert "important memory" in result
    assert "sim=0.8" in result


def test_layer3_search_no_results():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results([], [], [])
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        result = layer.search("nothing")

    assert "No results found" in result


def test_layer3_search_with_wing_filter():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["result"],
        [{"wing": "proj", "room": "r"}],
        [0.1],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        layer.search("q", wing="proj")

    call_kwargs = mock_col.query.call_args[1]
    assert call_kwargs["where"] == {"wing": "proj"}


def test_layer3_search_with_room_filter():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["result"],
        [{"wing": "w", "room": "backend"}],
        [0.1],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        layer.search("q", room="backend")

    call_kwargs = mock_col.query.call_args[1]
    assert call_kwargs["where"] == {"room": "backend"}


def test_layer3_search_with_wing_and_room():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["result"],
        [{"wing": "proj", "room": "backend"}],
        [0.1],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        layer.search("q", wing="proj", room="backend")

    call_kwargs = mock_col.query.call_args[1]
    assert "$and" in call_kwargs["where"]


def test_layer3_search_error():
    mock_col = MagicMock()
    mock_col.query.side_effect = RuntimeError("search failed")
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        result = layer.search("q")

    assert "Search error" in result


def test_layer3_search_truncates_long_docs():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["C" * 400],
        [{"wing": "w", "room": "r", "source_file": "s.txt"}],
        [0.1],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        result = layer.search("q")

    assert "..." in result


def test_layer3_search_raw_returns_dicts():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["doc text"],
        [{"wing": "proj", "room": "backend", "source_file": "f.txt"}],
        [0.3],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        hits = layer.search_raw("q")

    assert len(hits) == 1
    assert hits[0]["text"] == "doc text"
    assert hits[0]["wing"] == "proj"
    assert hits[0]["similarity"] == 0.7
    assert "metadata" in hits[0]


def test_layer3_search_raw_with_filters():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["doc"],
        [{"wing": "w", "room": "r"}],
        [0.1],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        layer.search_raw("q", wing="w", room="r")

    call_kwargs = mock_col.query.call_args[1]
    assert "$and" in call_kwargs["where"]


def test_layer3_search_raw_error():
    mock_col = MagicMock()
    mock_col.query.side_effect = RuntimeError("fail")
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        result = layer.search_raw("q")

    assert result == []


# ── MemoryStack ─────────────────────────────────────────────────────────


def test_memory_stack_wake_up(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent"
        stack = MemoryStack(
            palace_path="/nonexistent",
            identity_path=str(identity_file),
        )
        result = stack.wake_up()

    assert "Atlas" in result
    # L1 will say no palace found
    assert "No palace" in result or "No memories" in result


def test_memory_stack_wake_up_with_wing(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent"
        stack = MemoryStack(
            palace_path="/nonexistent",
            identity_path=str(identity_file),
        )
        result = stack.wake_up(wing="my_project")

    assert stack.l1.wing == "my_project"
    assert "Atlas" in result


def test_memory_stack_recall(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent"
        stack = MemoryStack(
            palace_path="/nonexistent",
            identity_path=str(identity_file),
        )
        result = stack.recall(wing="test")

    assert "No palace found" in result


def test_memory_stack_search(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent"
        stack = MemoryStack(
            palace_path="/nonexistent",
            identity_path=str(identity_file),
        )
        result = stack.search("test query")

    assert "No palace found" in result


def test_memory_stack_status(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent"
        stack = MemoryStack(
            palace_path="/nonexistent",
            identity_path=str(identity_file),
        )
        result = stack.status()

    assert result["palace_path"] == "/nonexistent"
    assert result["total_drawers"] == 0
    assert "L0_identity" in result
    assert "L1_essential" in result
    assert "L2_on_demand" in result
    assert "L3_deep_search" in result


def test_memory_stack_status_with_palace(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    mock_col = MagicMock()
    mock_col.count.return_value = 42
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        stack = MemoryStack(
            palace_path="/fake",
            identity_path=str(identity_file),
        )
        result = stack.status()

    assert result["total_drawers"] == 42
    assert result["L0_identity"]["exists"] is True


# ── Layer1 / Layer2 None-metadata guards ───────────────────────────────
#
# Chroma 1.5.x can return ``None`` inside the ``metadatas`` / ``documents``
# lists for partially-flushed rows. The Layer1.generate() and
# Layer2.retrieve() loops previously called ``meta.get(...)`` without
# coercing, raising ``AttributeError: 'NoneType' object has no attribute
# 'get'`` and blowing up the whole wake-up render. These tests guard that
# the loops tolerate the None entries and render the rest of the result.


def test_layer1_handles_none_metadata():
    """Layer1.generate tolerates None entries in the metadatas list."""
    docs = ["important memory", "another memory"]
    metas = [{"room": "decisions", "source_file": "a.txt"}, None]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        # Should not raise AttributeError on the None entry.
        result = layer.generate()

    assert "ESSENTIAL STORY" in result
    assert "important memory" in result


def test_layer1_handles_none_document():
    """Layer1.generate tolerates None entries in the documents list."""
    docs = ["first doc", None]
    metas = [
        {"room": "r", "source_file": "a.txt"},
        {"room": "r", "source_file": "b.txt"},
    ]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert result  # Render succeeded despite the None document.


def test_layer2_handles_none_metadata():
    """Layer2.retrieve tolerates None entries in the metadatas list."""
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "documents": ["first doc", "second doc"],
        "metadatas": [{"room": "r", "source_file": "a.txt"}, None],
    }

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        # Should not raise AttributeError on the None entry.
        result = layer.retrieve()

    assert "L2 — ON-DEMAND" in result
