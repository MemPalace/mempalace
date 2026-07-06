"""Tests for read-only near-duplicate cluster discovery."""

from __future__ import annotations

from unittest.mock import MagicMock


class FakeDuplicateCollection:
    """Small backend-like fake with filtered get/query and deterministic distances."""

    def __init__(self, rows, neighbors=None):
        self.rows = {row["id"]: row for row in rows}
        self.neighbors = neighbors or {}
        self.query_calls = []
        self.get_calls = []
        self.delete = MagicMock()

    def count(self):
        return len(self.rows)

    def get(
        self, *, ids=None, where=None, include=None, limit=None, offset=None, where_document=None
    ):
        self.get_calls.append(
            {
                "ids": ids,
                "where": where,
                "include": include,
                "limit": limit,
                "offset": offset,
                "where_document": where_document,
            }
        )
        rows = [self.rows[row_id] for row_id in ids] if ids else list(self.rows.values())
        rows = [row for row in rows if self._matches(row, where)]
        if offset is not None:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return {
            "ids": [row["id"] for row in rows],
            "documents": [row.get("document") for row in rows],
            "metadatas": [row.get("metadata", {}) for row in rows],
        }

    def query(self, *, query_texts=None, n_results=10, where=None, include=None, **_kwargs):
        query_doc = query_texts[0]
        query_id = next(row["id"] for row in self.rows.values() if row.get("document") == query_doc)
        self.query_calls.append(
            {"query_id": query_id, "n_results": n_results, "where": where, "include": include}
        )
        scoped_ids = {row_id for row_id, row in self.rows.items() if self._matches(row, where)}
        ids = []
        distances = []
        metadatas = []
        for neighbor_id, distance in self.neighbors.get(query_id, []):
            if neighbor_id not in scoped_ids:
                continue
            ids.append(neighbor_id)
            distances.append(distance)
            metadatas.append(self.rows[neighbor_id].get("metadata", {}))
        return {
            "ids": [ids[:n_results]],
            "distances": [distances[:n_results]],
            "metadatas": [metadatas[:n_results]],
        }

    @staticmethod
    def _matches(row, where):
        if not where:
            return True
        metadata = row.get("metadata", {}) or {}
        return all(metadata.get(key) == value for key, value in where.items())


def row(row_id, document=None, wing="w", room="r", parent=None, chunk_index=0):
    metadata = {"wing": wing, "room": room, "chunk_index": chunk_index}
    if parent is not None:
        metadata["parent_drawer_id"] = parent
    return {"id": row_id, "document": document or f"document for {row_id}", "metadata": metadata}


def assert_cluster_ids(result, expected):
    actual = [set(cluster["drawer_ids"]) for cluster in result["clusters"]]
    assert set(map(frozenset, actual)) == set(map(frozenset, expected))


def test_find_duplicate_clusters_groups_near_identical_drawers():
    from mempalace.dedup import find_duplicate_clusters

    col = FakeDuplicateCollection(
        [row("a"), row("b")],
        {"a": [("a", 0.0), ("b", 0.01)], "b": [("b", 0.0), ("a", 0.01)]},
    )

    result = find_duplicate_clusters(col, threshold=0.15)

    assert_cluster_ids(result, [{"a", "b"}])
    assert result["clusters"][0]["pairs"] == [{"a": "a", "b": "b", "distance": 0.01}]
    col.delete.assert_not_called()


def test_find_duplicate_clusters_ignores_dissimilar_drawers():
    from mempalace.dedup import find_duplicate_clusters

    col = FakeDuplicateCollection(
        [row("a"), row("b")],
        {"a": [("a", 0.0), ("b", 0.6)], "b": [("b", 0.0), ("a", 0.6)]},
    )

    result = find_duplicate_clusters(col, threshold=0.15)

    assert result["clusters"] == []


def test_find_duplicate_clusters_uses_strict_threshold_boundary():
    from mempalace.dedup import find_duplicate_clusters

    col = FakeDuplicateCollection(
        [row("a"), row("b"), row("c")],
        {
            "a": [("a", 0.0), ("b", 0.149), ("c", 0.15)],
            "b": [("b", 0.0), ("a", 0.149)],
            "c": [("c", 0.0), ("a", 0.15)],
        },
    )

    result = find_duplicate_clusters(col, threshold=0.15)

    assert_cluster_ids(result, [{"a", "b"}])
    assert all("c" not in cluster["drawer_ids"] for cluster in result["clusters"])


def test_find_duplicate_clusters_applies_wing_and_room_scope_to_get_and_query():
    from mempalace.dedup import find_duplicate_clusters

    col = FakeDuplicateCollection(
        [
            row("a", wing="w", room="r"),
            row("b", wing="w", room="r"),
            row("other", wing="w", room="x"),
        ],
        {"a": [("a", 0.0), ("b", 0.01), ("other", 0.01)], "b": [("b", 0.0), ("a", 0.01)]},
    )

    result = find_duplicate_clusters(col, wing="w", room="r", threshold=0.15)

    assert_cluster_ids(result, [{"a", "b"}])
    assert col.get_calls[0]["where"] == {"wing": "w", "room": "r"}
    assert all(call["where"] == {"wing": "w", "room": "r"} for call in col.query_calls)


def test_find_duplicate_clusters_excludes_self_matches():
    from mempalace.dedup import find_duplicate_clusters

    col = FakeDuplicateCollection([row("a")], {"a": [("a", 0.0)]})

    result = find_duplicate_clusters(col, threshold=0.15)

    assert result["clusters"] == []


def test_find_duplicate_clusters_connects_bridge_components_without_inventing_pairs():
    from mempalace.dedup import find_duplicate_clusters

    col = FakeDuplicateCollection(
        [row("a"), row("b"), row("c")],
        {
            "a": [("a", 0.0), ("b", 0.01), ("c", 0.4)],
            "b": [("b", 0.0), ("a", 0.01), ("c", 0.02)],
            "c": [("c", 0.0), ("b", 0.02), ("a", 0.4)],
        },
    )

    result = find_duplicate_clusters(col, threshold=0.15)

    assert_cluster_ids(result, [{"a", "b", "c"}])
    assert {tuple(pair[key] for key in ("a", "b")) for pair in result["clusters"][0]["pairs"]} == {
        ("a", "b"),
        ("b", "c"),
    }


def test_find_duplicate_clusters_does_not_self_cluster_chunks_of_one_logical_drawer():
    from mempalace.dedup import find_duplicate_clusters

    col = FakeDuplicateCollection(
        [
            row("chunk-0", parent="drawer-parent", chunk_index=0),
            row("chunk-1", parent="drawer-parent", chunk_index=1),
        ],
        {
            "chunk-0": [("chunk-0", 0.0), ("chunk-1", 0.01)],
            "chunk-1": [("chunk-1", 0.0), ("chunk-0", 0.01)],
        },
    )

    result = find_duplicate_clusters(col, threshold=0.15)

    assert result["clusters"] == []


def test_find_duplicate_clusters_returns_logical_parent_ids_for_chunked_drawers():
    from mempalace.dedup import find_duplicate_clusters

    col = FakeDuplicateCollection(
        [row("a-chunk", parent="drawer-a"), row("b-chunk", parent="drawer-b")],
        {
            "a-chunk": [("a-chunk", 0.0), ("b-chunk", 0.01)],
            "b-chunk": [("b-chunk", 0.0), ("a-chunk", 0.01)],
        },
    )

    result = find_duplicate_clusters(col, threshold=0.15)

    assert_cluster_ids(result, [{"drawer-a", "drawer-b"}])
    assert result["clusters"][0]["pairs"] == [{"a": "drawer-a", "b": "drawer-b", "distance": 0.01}]


def test_find_duplicate_clusters_respects_max_clusters():
    from mempalace.dedup import find_duplicate_clusters

    col = FakeDuplicateCollection(
        [row("a"), row("b"), row("c"), row("d")],
        {
            "a": [("a", 0.0), ("b", 0.01)],
            "b": [("b", 0.0), ("a", 0.01)],
            "c": [("c", 0.0), ("d", 0.02)],
            "d": [("d", 0.0), ("c", 0.02)],
        },
    )

    result = find_duplicate_clusters(col, threshold=0.15, max_clusters=1)

    assert len(result["clusters"]) == 1
    assert result["truncated"] is True


def test_find_duplicate_clusters_handles_empty_and_single_drawer():
    from mempalace.dedup import find_duplicate_clusters

    assert find_duplicate_clusters(FakeDuplicateCollection([]), threshold=0.15)["clusters"] == []
    assert (
        find_duplicate_clusters(FakeDuplicateCollection([row("only")]), threshold=0.15)["clusters"]
        == []
    )


def test_find_duplicate_clusters_adapts_k_while_boundary_neighbor_is_below_threshold():
    from mempalace.dedup import find_duplicate_clusters

    rows = [row("a"), row("b"), row("c")]
    col = FakeDuplicateCollection(rows, {"a": [("a", 0.0), ("b", 0.01), ("c", 0.02)]})

    result = find_duplicate_clusters(col, threshold=0.15, initial_k=2, max_neighbors=3)

    assert_cluster_ids(result, [{"a", "b", "c"}])
    assert [call["n_results"] for call in col.query_calls if call["query_id"] == "a"] == [2, 3]
    assert result["params"]["neighbor_bound"] == 3


def test_tool_find_duplicates_returns_vector_disabled_response(monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "hnsw_capacity_status",
        lambda *_args, **_kwargs: {"diverged": True, "message": "capacity mismatch"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_get_collection",
        lambda: (_ for _ in ()).throw(AssertionError("collection should not be opened")),
    )

    result = mcp_server.tool_find_duplicates()

    assert result["clusters"] == []
    assert result["vector_disabled"] is True
    assert result["vector_disabled_reason"] == "capacity mismatch"


def test_tool_find_duplicates_sanitizes_scope_and_shapes_response(monkeypatch):
    from mempalace import mcp_server

    col = FakeDuplicateCollection(
        [row("a", wing="project", room="backend"), row("b", wing="project", room="backend")],
        {"a": [("a", 0.0), ("b", 0.01)], "b": [("b", 0.0), ("a", 0.01)]},
    )
    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp_server, "_vector_disabled", False)
    monkeypatch.setattr(mcp_server, "_get_collection", lambda: col)

    result = mcp_server.tool_find_duplicates(
        wing="project", room="backend", threshold=0.15, max_clusters=5
    )

    assert_cluster_ids(result, [{"a", "b"}])
    assert result["params"]["wing"] == "project"
    assert result["params"]["room"] == "backend"
    assert "documents" not in result
    assert "embeddings" not in result


def test_tool_find_duplicates_rejects_invalid_scope(monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
    result = mcp_server.tool_find_duplicates(wing="../bad")

    assert "error" in result


def test_find_duplicates_is_registered_as_read_tool():
    from mempalace import mcp_server, service

    assert "mempalace_find_duplicates" in mcp_server.TOOLS
    assert service.classify_tool("mempalace_find_duplicates") == "read"
    assert "mempalace_find_duplicates" not in mcp_server._MUTATING_TOOLS
    assert "mempalace_find_duplicates" not in mcp_server._SQLITE_INTEGRITY_ALLOWED_TOOLS
