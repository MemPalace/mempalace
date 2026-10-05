"""SQLite diary reads preserve the legacy response without opening Chroma."""

from unittest.mock import Mock

import chromadb
import pytest

from _mcp_server_helpers import _patch_mcp_server


def _unexpected_collection_read(*args, **kwargs):
    raise AssertionError("a successful SQLite diary read must not open or read a collection")


@pytest.fixture
def diary_palace(monkeypatch, config, collection, kg):
    from mempalace import mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
    monkeypatch.delenv("MEMPALACE_BACKEND_EXPLICIT", raising=False)
    return mcp_server, collection


def _legacy_read(monkeypatch, server, **kwargs):
    with monkeypatch.context() as patch:
        patch.setattr(server, "_sqlite_diary_read", lambda *args, **kw: None)
        return server.tool_diary_read(**kwargs)


def _fast_read(monkeypatch, server, collection, **kwargs):
    with monkeypatch.context() as patch:
        opener = Mock(side_effect=_unexpected_collection_read)
        getter = Mock(side_effect=_unexpected_collection_read)
        patch.setattr(server, "_get_collection", opener)
        patch.setattr(type(collection), "get", getter)
        result = server.tool_diary_read(**kwargs)
        opener.assert_not_called()
        getter.assert_not_called()
        return result


def _seed(collection, rows):
    collection.add(
        ids=[row[0] for row in rows],
        documents=[row[1] for row in rows],
        metadatas=[row[2] for row in rows],
    )


def _diary_metadata(timestamp, *, agent="testagent", wing="wing_alpha", **extra):
    return {
        "room": "diary",
        "agent": agent,
        "wing": wing,
        "filed_at": timestamp,
        "date": "2026-10-02",
        "topic": "general",
        **extra,
    }


@pytest.mark.parametrize("wing", ["", "wing_alpha", "wing_beta"])
def test_diary_sqlite_filters_match_legacy_across_agents_wings_and_case(
    diary_palace, monkeypatch, wing
):
    server, collection = diary_palace
    rows = [
        (
            f"entry-{i}",
            f"Verbatim entry {i}: 日本語 🧠\n  keep spacing.",
            _diary_metadata(f"2026-10-02T12:00:0{i}", wing="wing_alpha" if i % 2 else "wing_beta"),
        )
        for i in range(6)
    ]
    rows.extend(
        [
            ("other-agent", "exclude another agent", _diary_metadata("z", agent="other")),
            ("other-room", "exclude a non-diary room", _diary_metadata("z", room="notes")),
            # Existing pre-normalization records remain excluded, as on the legacy path.
            (
                "legacy-case",
                "exclude mixed-case stored agent",
                _diary_metadata("z", agent="TestAgent"),
            ),
        ]
    )
    _seed(collection, rows)

    kwargs = {"agent_name": "TeStAgEnT", "last_n": 5, "wing": wing}
    expected = _legacy_read(monkeypatch, server, **kwargs)
    actual = _fast_read(monkeypatch, server, collection, **kwargs)

    assert actual == expected
    assert actual["agent"] == "testagent"
    assert actual["total"] == (6 if not wing else 3)
    assert actual["showing"] == min(actual["total"], 5)
    assert all(entry["content"].startswith("Verbatim entry") for entry in actual["entries"])
    assert [entry["timestamp"] for entry in actual["entries"]] == sorted(
        [entry["timestamp"] for entry in actual["entries"]], reverse=True
    )


@pytest.mark.parametrize("last_n,showing", [(None, 10), (-5, 1), (0, 1), (5, 5), (1000, 100)])
def test_diary_sqlite_default_limit_and_clamps_match_legacy(
    diary_palace, monkeypatch, last_n, showing
):
    server, collection = diary_palace
    _seed(
        collection,
        [
            (
                f"entry-{i:03}",
                f"Entry {i}",
                _diary_metadata(f"2026-10-02T12:00:00.{i:06}"),
            )
            for i in range(105)
        ],
    )
    kwargs = {"agent_name": "TestAgent"}
    if last_n is not None:
        kwargs["last_n"] = last_n
    expected = _legacy_read(monkeypatch, server, **kwargs)
    actual = _fast_read(monkeypatch, server, collection, **kwargs)

    assert actual == expected
    assert actual["total"] == 105
    assert actual["showing"] == showing
    assert [entry["content"] for entry in actual["entries"]] == [
        f"Entry {i}" for i in range(104, 104 - showing, -1)
    ]


def test_diary_sqlite_ties_missing_timestamps_and_document_preserve_legacy(
    diary_palace, monkeypatch
):
    server, collection = diary_palace
    minimal = {"room": "diary", "agent": "testagent", "wing": "wing_alpha"}
    _seed(
        collection,
        [
            ("z-first", "First tied entry", _diary_metadata("2026-10-02T12:00:00")),
            ("a-second", "Second tied entry", _diary_metadata("2026-10-02T12:00:00")),
            ("missing-time", "No timestamp or topic", minimal),
        ],
    )
    # Chroma permits an embedding-only row whose document is None.
    collection.add(
        ids=["embedding-only"],
        embeddings=[[1.0] + [0.0] * 383],
        metadatas=[minimal],
    )
    monkeypatch.setattr(server, "_DIARY_READ_PAGE_SIZE", 1)
    expected = _legacy_read(monkeypatch, server, agent_name="testagent")
    actual = _fast_read(monkeypatch, server, collection, agent_name="testagent")

    assert actual == expected
    assert actual["total"] == 4
    assert [entry["content"] for entry in actual["entries"][:2]] == [
        "First tied entry",
        "Second tied entry",
    ]
    assert actual["entries"][-1] == {"date": "", "timestamp": "", "topic": "", "content": None}


def test_diary_sqlite_custom_collection_does_not_include_default_collection(
    diary_palace, monkeypatch, config, palace_path
):
    server, default_collection = diary_palace
    _seed(default_collection, [("decoy", "Wrong collection", _diary_metadata("z"))])
    monkeypatch.setitem(config._file_config, "collection_name", "custom_diary_drawers")
    client = chromadb.PersistentClient(path=palace_path)
    try:
        custom = client.get_or_create_collection(
            config.collection_name, metadata={"hnsw:space": "cosine"}
        )
        _seed(custom, [("correct", "Custom collection entry", _diary_metadata("2026-10-02"))])
        expected = _legacy_read(monkeypatch, server, agent_name="testagent")
        actual = _fast_read(monkeypatch, server, custom, agent_name="testagent")
        assert actual == expected
        assert actual["total"] == 1
        assert actual["entries"][0]["content"] == "Custom collection entry"
    finally:
        client.close()


def test_diary_sqlite_empty_match_returns_message_without_collection_open(
    diary_palace, monkeypatch
):
    server, collection = diary_palace
    _seed(collection, [("other", "Another agent's entry", _diary_metadata("z", agent="other"))])
    assert _fast_read(monkeypatch, server, collection, agent_name="TestAgent") == {
        "agent": "testagent",
        "entries": [],
        "message": "No diary entries yet.",
    }


def test_diary_sqlite_chunked_entries_remain_physical_rows(diary_palace, monkeypatch, config):
    server, collection = diary_palace
    monkeypatch.setitem(config._file_config, "chunk_size", 80)
    written = server.tool_diary_write("TestAgent", "0123456789" * 25, topic="chunked")
    assert written["success"] is True
    assert written["chunks"] == 4

    expected = _legacy_read(monkeypatch, server, agent_name="testagent", last_n=2)
    actual = _fast_read(monkeypatch, server, collection, agent_name="testagent", last_n=2)
    assert actual == expected
    assert actual["total"] == 4
    assert actual["showing"] == 2
    assert [entry["content"] for entry in actual["entries"]] == ["0123456789" * 8] * 2


def test_diary_sqlite_reads_fresh_writes_updates_and_deletes_without_sdk_reads(
    diary_palace, monkeypatch
):
    server, collection = diary_palace
    written = server.tool_diary_write(
        "TestAgent", "Original 日本語 entry", topic="fresh", wing="wing_alpha"
    )
    assert written["success"] is True
    expected = {
        "agent": "testagent",
        "entries": [
            {
                "date": written["timestamp"][:10],
                "timestamp": written["timestamp"],
                "topic": "fresh",
                "content": "Original 日本語 entry",
            }
        ],
        "total": 1,
        "showing": 1,
    }
    # Read immediately: an intervening SDK get/count/query could apply pending logs.
    assert _fast_read(monkeypatch, server, collection, agent_name="testagent") == expected

    updated = server.tool_update_drawer(
        written["entry_id"], content="Updated 🧠 entry", wing="wing_beta"
    )
    assert updated["success"] is True
    expected["entries"][0]["content"] = "Updated 🧠 entry"
    assert (
        _fast_read(monkeypatch, server, collection, agent_name="testagent", wing="wing_beta")
        == expected
    )
    assert (
        _fast_read(monkeypatch, server, collection, agent_name="testagent", wing="wing_alpha")[
            "entries"
        ]
        == []
    )

    deleted = server.tool_delete_drawer(written["entry_id"])
    assert deleted["success"] is True
    assert _fast_read(monkeypatch, server, collection, agent_name="testagent") == {
        "agent": "testagent",
        "entries": [],
        "message": "No diary entries yet.",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"agent_name": ""},
        {"agent_name": None},
        {"agent_name": "../agent"},
        {"agent_name": "testagent", "wing": "../wing"},
    ],
)
def test_diary_sqlite_invalid_names_do_not_probe_sql_or_open_collection(
    diary_palace, monkeypatch, kwargs
):
    server, _ = diary_palace
    probe = Mock(side_effect=AssertionError("invalid input reached SQLite"))
    opener = Mock(side_effect=_unexpected_collection_read)
    monkeypatch.setattr(server, "_sqlite_diary_read", probe)
    monkeypatch.setattr(server, "_get_collection", opener)
    result = server.tool_diary_read(**kwargs)
    assert "error" in result
    probe.assert_not_called()
    opener.assert_not_called()


@pytest.mark.parametrize("backend", ["sqlite_exact", "custom_backend"])
def test_diary_sqlite_unsupported_backend_keeps_legacy_paging(diary_palace, monkeypatch, backend):
    from mempalace.backends import chroma as chroma_backend

    server, collection = diary_palace
    _seed(collection, [(f"entry-{i}", f"Entry {i}", _diary_metadata(str(i))) for i in range(3)])
    expected = _legacy_read(monkeypatch, server, agent_name="testagent", last_n=2)
    sqlite_reader = Mock(side_effect=AssertionError("non-Chroma backend reached Chroma SQLite"))
    legacy_collection = Mock(wraps=collection)
    monkeypatch.setattr(server, "_selected_backend_name", lambda: backend)
    monkeypatch.setattr(chroma_backend, "sqlite_diary_rows", sqlite_reader)
    monkeypatch.setattr(server, "_get_collection", lambda: legacy_collection)
    monkeypatch.setattr(server, "_DIARY_READ_PAGE_SIZE", 2)

    assert server.tool_diary_read("testagent", last_n=2) == expected
    sqlite_reader.assert_not_called()
    assert legacy_collection.get.call_count == 2
    assert [call.kwargs["offset"] for call in legacy_collection.get.call_args_list] == [0, 2]


def test_diary_sqlite_unavailable_schema_keeps_legacy_paging(diary_palace, monkeypatch, config):
    from mempalace.backends import chroma as chroma_backend

    server, collection = diary_palace
    _seed(collection, [(f"entry-{i}", f"Entry {i}", _diary_metadata(str(i))) for i in range(3)])
    expected = _legacy_read(monkeypatch, server, agent_name="testagent", last_n=2)
    # The backend reader returns None for unavailable or unsupported schema.
    sqlite_reader = Mock(return_value=None)
    legacy_collection = Mock(wraps=collection)
    monkeypatch.setattr(chroma_backend, "sqlite_diary_rows", sqlite_reader)
    monkeypatch.setattr(server, "_get_collection", lambda: legacy_collection)
    monkeypatch.setattr(server, "_DIARY_READ_PAGE_SIZE", 2)

    assert server.tool_diary_read("testagent", last_n=2) == expected
    sqlite_reader.assert_called_once_with(
        config.palace_path, config.collection_name, agent_name="testagent", wing="", limit=2
    )
    assert legacy_collection.get.call_count == 2
    assert [call.kwargs["offset"] for call in legacy_collection.get.call_args_list] == [0, 2]
