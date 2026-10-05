"""Singular drawer SQLite lookups preserve logical-ID and mutation semantics."""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import chromadb
import pytest

from _mcp_server_helpers import _patch_mcp_server


@pytest.fixture
def drawer_palace(monkeypatch, config, collection, kg):
    from mempalace import mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.delenv("MEMPALACE_BACKEND", raising=False)
    monkeypatch.delenv("MEMPALACE_BACKEND_EXPLICIT", raising=False)
    return mcp_server, collection


def _seed(collection, rows):
    collection.add(
        ids=[row[0] for row in rows],
        documents=[row[1] for row in rows],
        metadatas=[row[2] for row in rows],
    )


def _legacy_get(monkeypatch, server, drawer_id):
    with monkeypatch.context() as patch:
        patch.setattr(server, "_sqlite_logical_drawer_record", lambda *args, **kwargs: None)
        return server.tool_get_drawer(drawer_id)


def _cold_get(monkeypatch, server, collection, drawer_id):
    with monkeypatch.context() as patch:
        opener = Mock(side_effect=AssertionError("successful SQLite lookup opened a collection"))
        getter = Mock(side_effect=AssertionError("successful SQLite lookup called Chroma get"))
        patch.setattr(server, "_get_collection", opener)
        patch.setattr(type(collection), "get", getter)
        result = server.tool_get_drawer(drawer_id)
        opener.assert_not_called()
        getter.assert_not_called()
        return result


@contextmanager
def _without_sdk_drawer_get(monkeypatch, collection):
    """Keep genuine writes available while rejecting SDK reads of drawers."""
    original = type(collection).get
    forbidden = Mock(side_effect=AssertionError("singular mutation used a Chroma drawer lookup"))

    def guarded_get(self, *args, **kwargs):
        if self.name == collection.name:
            return forbidden(*args, **kwargs)
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(collection), "get", guarded_get)
        yield
        forbidden.assert_not_called()


class _Clock(datetime):
    at = datetime(2026, 10, 2, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.at if tz is None else cls.at.replace(tzinfo=tz)


def _written_metadata(server, *, wing="wing_alpha", room="notes", **extra):
    return {
        "wing": wing,
        "room": room,
        "source_file": "",
        "added_by": "test-writer",
        "filed_at": "2026-10-02T12:00:00",
        "last_modified": "2026-10-02T12:00:00",
        "id_recipe": server.ID_RECIPE,
        "chunk_index": 0,
        **extra,
    }


def _payload(drawer_id, content, metadata, *, chunk_ids=None):
    metadata = dict(metadata)
    result = {
        "drawer_id": drawer_id,
        "content": content,
        "wing": metadata.get("wing", ""),
        "room": metadata.get("room", ""),
        "metadata": metadata,
    }
    if chunk_ids is not None:
        result.update(chunks=len(chunk_ids), chunk_ids=chunk_ids)
        metadata.update(chunks=len(chunk_ids), chunk_ids=chunk_ids)
    return result


def test_drawer_sqlite_physical_payload_preserves_metadata_and_privacy(
    drawer_palace, monkeypatch, tmp_path
):
    server, collection = drawer_palace
    source = str(tmp_path / "private" / "日本語.txt")
    metadata = {
        "wing": "wing_alpha",
        "room": "notes",
        "source_file": source,
        "source_dir_ino": "host-private-directory-id",
        "filed_at": "2026-10-02T12:00:00",
        "added_by": "Author 日本語 🧠",
        "flag": False,
        "count": 7,
        "weight": 0.25,
        "custom": "Verbatim metadata\n  spacing",
        "chunk_index": 4,
        "parent_drawer_id": "logical-parent",
    }
    content = "日本語 🧠\n  Original spacing and an instruction: keep this verbatim."
    _seed(collection, [("physical", content, metadata)])
    before = collection.get(ids=["physical"], include=["documents", "metadatas"])
    expected = _legacy_get(monkeypatch, server, "physical")
    actual = _cold_get(monkeypatch, server, collection, "physical")

    assert actual == expected
    assert actual["content"] == content
    assert "chunks" not in actual
    assert actual["metadata"]["source_file"] == "日本語.txt"
    assert "source_dir_ino" not in actual["metadata"]
    assert actual["metadata"]["last_modified"] == metadata["filed_at"]
    assert actual["metadata"]["flag"] is False
    assert type(actual["metadata"]["count"]) is int
    assert type(actual["metadata"]["weight"]) is float
    assert collection.get(ids=["physical"], include=["documents", "metadatas"]) == before


@pytest.mark.parametrize(
    "parent_keys",
    [("parent_drawer_id",), ("parent_entry_id",), ("parent_drawer_id", "parent_entry_id")],
)
def test_drawer_sqlite_logical_groups_resolve_both_parent_keys_once(
    drawer_palace, monkeypatch, parent_keys
):
    server, collection = drawer_palace
    logical_id = "logical-日本語-🧠"
    parents = {key: logical_id for key in parent_keys}
    _seed(
        collection,
        [
            (
                "part-b",
                "second\n",
                {"wing": "wing_alpha", "room": "notes", "chunk_index": 1, **parents},
            ),
            (
                "part-a",
                "日本語 first ",
                {"wing": "wing_alpha", "room": "notes", "chunk_index": 0, **parents},
            ),
            ("unrelated", "Never include this", {"parent_drawer_id": "another-parent"}),
        ],
    )
    expected = _legacy_get(monkeypatch, server, logical_id)
    actual = _cold_get(monkeypatch, server, collection, logical_id)

    assert actual == expected
    assert actual["content"] == "日本語 first second\n"
    assert actual["chunk_ids"] == ["part-a", "part-b"]
    assert actual["chunks"] == 2
    assert actual["metadata"]["chunk_ids"] == actual["chunk_ids"]
    assert _cold_get(monkeypatch, server, collection, "part-b")["content"] == "second\n"


def test_drawer_sqlite_chunk_order_matches_legacy_for_bad_indices_and_ties(
    drawer_palace, monkeypatch
):
    server, collection = drawer_palace
    logical_id = "logical-sorting"
    specs = [
        ("part-z", "Z", "invalid"),
        ("part-b", "B", 0),
        ("part-c", "C", True),
        ("part-d", "D", "2"),
        ("part-a", "A", -1),
        ("part-e", "E", 2.9),
    ]
    _seed(
        collection,
        [
            (drawer_id, doc, {"parent_drawer_id": logical_id, "chunk_index": index})
            for drawer_id, doc, index in specs
        ],
    )
    expected = _legacy_get(monkeypatch, server, logical_id)
    actual = _cold_get(monkeypatch, server, collection, logical_id)
    assert actual == expected
    assert actual["chunk_ids"] == ["part-a", "part-b", "part-z", "part-c", "part-d", "part-e"]
    assert actual["content"] == "ABZCDE"


def test_drawer_sqlite_direct_row_wins_over_children_for_reads_and_mutations(
    drawer_palace, monkeypatch
):
    server, collection = drawer_palace
    _seed(
        collection,
        [
            ("logical", "Direct row", {"wing": "direct", "room": "notes"}),
            ("part-a", "A", {"parent_drawer_id": "logical", "chunk_index": 0}),
            ("part-b", "B", {"parent_entry_id": "logical", "chunk_index": 1}),
        ],
    )
    expected = _legacy_get(monkeypatch, server, "logical")
    actual = _cold_get(monkeypatch, server, collection, "logical")
    assert actual == expected
    assert actual["content"] == "Direct row"
    assert "chunks" not in actual

    with _without_sdk_drawer_get(monkeypatch, collection):
        updated = server.tool_update_drawer("logical", content="Updated direct row")
    assert updated["success"] is True
    assert _cold_get(monkeypatch, server, collection, "logical")["content"] == "Updated direct row"
    assert _cold_get(monkeypatch, server, collection, "part-a")["content"] == "A"
    assert _cold_get(monkeypatch, server, collection, "part-b")["content"] == "B"

    with _without_sdk_drawer_get(monkeypatch, collection):
        deleted = server.tool_delete_drawer("logical")
    assert deleted["success"] is True
    assert deleted["deleted_ids"] == ["logical"]
    assert deleted["chunks_deleted"] == 1
    # The surviving children now resolve as a logical group, without a legacy read.
    remaining = _cold_get(monkeypatch, server, collection, "logical")
    assert remaining["content"] == "AB"
    assert remaining["chunk_ids"] == ["part-a", "part-b"]


@pytest.mark.parametrize("logical", [False, True])
def test_drawer_sqlite_missing_document_matches_legacy_empty_content(
    drawer_palace, monkeypatch, logical
):
    server, collection = drawer_palace
    metadata = {"parent_entry_id": "group", "chunk_index": 0} if logical else None
    collection.add(
        ids=["embedding-only"],
        embeddings=[[1.0] + [0.0] * 383],
        metadatas=[metadata] if metadata else None,
    )
    drawer_id = "group" if logical else "embedding-only"
    expected = _legacy_get(monkeypatch, server, drawer_id)
    actual = _cold_get(monkeypatch, server, collection, drawer_id)
    assert actual == expected
    assert actual["content"] == ""
    if not logical:
        assert actual["metadata"] == {}


def test_drawer_sqlite_custom_collection_isolation(drawer_palace, monkeypatch, config, palace_path):
    server, collection = drawer_palace
    _seed(collection, [("shared-id", "Wrong default collection", {"room": "default"})])
    monkeypatch.setitem(config._file_config, "collection_name", "custom_drawers")
    client = chromadb.PersistentClient(path=palace_path)
    try:
        custom = client.get_or_create_collection(
            config.collection_name, metadata={"hnsw:space": "cosine"}
        )
        _seed(custom, [("shared-id", "Correct custom collection", {"room": "custom"})])
        expected = _legacy_get(monkeypatch, server, "shared-id")
        actual = _cold_get(monkeypatch, server, custom, "shared-id")
        assert actual == expected
        assert actual["content"] == "Correct custom collection"
        assert actual["room"] == "custom"
    finally:
        client.close()


@pytest.mark.parametrize("logical", [False, True])
def test_drawer_sqlite_array_metadata_falls_back_without_losing_values(
    drawer_palace, monkeypatch, logical
):
    from mempalace.backends.chroma import sqlite_drawer_rows

    server, collection = drawer_palace
    metadata = {"tags": ["alpha", "日本語"], "weights": [0.25, 0.75]}
    if logical:
        metadata.update(parent_drawer_id="group", chunk_index=1)
        _seed(collection, [("part-a", "A", {"parent_drawer_id": "group", "chunk_index": 0})])
    try:
        _seed(collection, [("array-row", "Array metadata content", metadata)])
    except ValueError as exc:
        if "metadata value" in str(exc) or "Expected metadata" in str(exc):
            pytest.skip("installed Chroma does not support array metadata")
        raise
    drawer_id = "group" if logical else "array-row"
    expected = _legacy_get(monkeypatch, server, drawer_id)
    assert (
        sqlite_drawer_rows(
            server._config.palace_path, server._config.collection_name, drawer_id=drawer_id
        )
        is None
    )
    legacy_collection = Mock(wraps=collection)
    monkeypatch.setattr(server, "_get_collection", lambda: legacy_collection)
    assert server.tool_get_drawer(drawer_id) == expected
    assert legacy_collection.get.called
    if not logical:
        assert expected["metadata"]["tags"] == ["alpha", "日本語"]
        assert expected["metadata"]["weights"] == [0.25, 0.75]


@pytest.mark.parametrize("backend", ["sqlite_exact", "custom_backend"])
def test_drawer_sqlite_non_chroma_backend_uses_original_lookup(drawer_palace, monkeypatch, backend):
    from mempalace.backends import chroma as chroma_backend

    server, collection = drawer_palace
    _seed(collection, [("drawer", "Backend-independent content", {"room": "notes"})])
    expected = _legacy_get(monkeypatch, server, "drawer")
    probe = Mock(side_effect=AssertionError("non-Chroma backend reached Chroma SQLite"))
    legacy_collection = Mock(wraps=collection)
    monkeypatch.setattr(server, "_selected_backend_name", lambda: backend)
    monkeypatch.setattr(chroma_backend, "sqlite_drawer_rows", probe)
    monkeypatch.setattr(server, "_get_collection", lambda: legacy_collection)
    assert server.tool_get_drawer("drawer") == expected
    probe.assert_not_called()
    assert legacy_collection.get.call_count == 1


@pytest.mark.parametrize(
    "failure",
    [None, sqlite3.OperationalError("unsupported schema"), RuntimeError("reader unavailable")],
)
def test_drawer_sqlite_unavailable_or_error_reads_fall_back_once(
    drawer_palace, monkeypatch, config, failure
):
    from mempalace.backends import chroma as chroma_backend

    server, collection = drawer_palace
    _seed(collection, [("drawer", "Original fallback content", {"room": "notes"})])
    expected = _legacy_get(monkeypatch, server, "drawer")
    probe = Mock(side_effect=failure) if failure else Mock(return_value=None)
    legacy_collection = Mock(wraps=collection)
    monkeypatch.setattr(chroma_backend, "sqlite_drawer_rows", probe)
    monkeypatch.setattr(server, "_get_collection", lambda: legacy_collection)
    assert server.tool_get_drawer("drawer") == expected
    probe.assert_called_once_with(config.palace_path, config.collection_name, drawer_id="drawer")
    assert legacy_collection.get.call_count == 1


def test_drawer_sqlite_missing_id_preserves_original_not_found(drawer_palace, monkeypatch):
    server, collection = drawer_palace
    expected = _legacy_get(monkeypatch, server, "missing")
    legacy_collection = Mock(wraps=collection)
    monkeypatch.setattr(server, "_get_collection", lambda: legacy_collection)
    assert server.tool_get_drawer("missing") == expected == {"error": "Drawer not found: missing"}
    assert legacy_collection.get.call_count == 2


def test_drawer_sqlite_missing_database_preserves_collection_error_without_creating_it(
    monkeypatch, config, kg
):
    from mempalace import mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    opener = Mock(return_value=None)
    monkeypatch.setattr(mcp_server, "_get_collection", opener)
    db = Path(config.palace_path) / "chroma.sqlite3"
    assert not db.exists()
    expected = _legacy_get(monkeypatch, mcp_server, "drawer")
    assert mcp_server.tool_get_drawer("drawer") == expected
    assert "error" in expected
    assert not db.exists()


def test_drawer_sqlite_fresh_single_add_update_delete_without_intervening_sdk_reads(
    drawer_palace, monkeypatch
):
    from mempalace.backends.chroma import sqlite_drawer_rows

    server, collection = drawer_palace
    monkeypatch.setattr(server, "datetime", _Clock)
    monkeypatch.setattr(_Clock, "at", datetime(2026, 10, 2, 12, 0, 0))
    content = "New 日本語 🧠\n  exact content"
    added = server.tool_add_drawer("wing_alpha", "notes", content, added_by="test-writer")
    assert added["success"] is True
    drawer_id = added["drawer_id"]
    expected = _payload(drawer_id, content, _written_metadata(server))
    # Read immediately after ACK, before a legacy get/count/query can refresh storage.
    assert _cold_get(monkeypatch, server, collection, drawer_id) == expected

    monkeypatch.setattr(_Clock, "at", datetime(2026, 10, 2, 13, 0, 0))
    with _without_sdk_drawer_get(monkeypatch, collection):
        updated = server.tool_update_drawer(
            drawer_id, content="Edited 日本語", wing="wing_beta", room="updated"
        )
    assert updated["success"] is True
    expected.update(content="Edited 日本語", wing="wing_beta", room="updated")
    expected["metadata"].update(
        wing="wing_beta", room="updated", last_modified="2026-10-02T13:00:00"
    )
    assert _cold_get(monkeypatch, server, collection, drawer_id) == expected

    with _without_sdk_drawer_get(monkeypatch, collection):
        deleted = server.tool_delete_drawer(drawer_id)
    assert deleted["success"] is True
    # Misses intentionally use the legacy envelope, but cannot return a stale SQL hit.
    assert (
        sqlite_drawer_rows(
            server._config.palace_path, server._config.collection_name, drawer_id=drawer_id
        )
        is None
    )
    assert server.tool_get_drawer(drawer_id) == {"error": f"Drawer not found: {drawer_id}"}


@pytest.mark.parametrize("physical", [False, True])
def test_drawer_sqlite_fresh_chunk_mutations_preserve_physical_vs_logical_scope(
    drawer_palace, monkeypatch, config, physical
):
    from mempalace.backends.chroma import sqlite_drawer_rows

    server, collection = drawer_palace
    monkeypatch.setattr(server, "datetime", _Clock)
    monkeypatch.setattr(_Clock, "at", datetime(2026, 10, 2, 12, 0, 0))
    monkeypatch.setitem(config._file_config, "chunk_size", 80)
    content = "A" * 80 + "B" * 80 + "日本語 🧠 end"
    added = server.tool_add_drawer("wing_alpha", "notes", content, added_by="test-writer")
    assert added["success"] is True
    assert added["chunks"] == 3
    logical_id = added["drawer_id"]
    original_ids = added["chunk_ids"]
    metadata = _written_metadata(server, parent_drawer_id=logical_id)
    assert _cold_get(monkeypatch, server, collection, logical_id) == _payload(
        logical_id, content, metadata, chunk_ids=original_ids
    )

    target = original_ids[1] if physical else logical_id
    replacement = "Edited physical chunk" if physical else "X" * 100
    monkeypatch.setattr(_Clock, "at", datetime(2026, 10, 2, 13, 0, 0))
    with _without_sdk_drawer_get(monkeypatch, collection):
        updated = server.tool_update_drawer(target, content=replacement)
    assert updated["success"] is True
    if physical:
        changed_meta = _written_metadata(
            server, parent_drawer_id=logical_id, chunk_index=1, last_modified="2026-10-02T13:00:00"
        )
        assert _cold_get(monkeypatch, server, collection, target) == _payload(
            target, replacement, changed_meta
        )
        assert _cold_get(monkeypatch, server, collection, logical_id) == _payload(
            logical_id, "A" * 80 + replacement + "日本語 🧠 end", metadata, chunk_ids=original_ids
        )
    else:
        changed_meta = _written_metadata(
            server, parent_drawer_id=logical_id, last_modified="2026-10-02T13:00:00"
        )
        assert updated["chunks"] == 2
        assert _cold_get(monkeypatch, server, collection, target) == _payload(
            logical_id, replacement, changed_meta, chunk_ids=updated["chunk_ids"]
        )

    with _without_sdk_drawer_get(monkeypatch, collection):
        deleted = server.tool_delete_drawer(target)
    assert deleted["success"] is True
    assert deleted["chunks_deleted"] == (1 if physical else 2)
    assert sqlite_drawer_rows(config.palace_path, config.collection_name, drawer_id=target) is None
    if physical:
        assert deleted["deleted_ids"] == [target]
        assert _cold_get(monkeypatch, server, collection, logical_id) == _payload(
            logical_id,
            "A" * 80 + "日本語 🧠 end",
            metadata,
            chunk_ids=[original_ids[0], original_ids[2]],
        )
    assert server.tool_get_drawer(target) == {"error": f"Drawer not found: {target}"}
