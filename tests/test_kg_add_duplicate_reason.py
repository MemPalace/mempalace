"""Regression coverage for issue #2268: truthful duplicate KG responses."""

import pytest

from mempalace import mcp_server
from mempalace.knowledge_graph import KnowledgeGraph


@pytest.fixture
def isolated_kg(monkeypatch, tmp_path):
    db_path = tmp_path / "knowledge_graph.sqlite3"
    kg = KnowledgeGraph(db_path=str(db_path))
    monkeypatch.setattr(mcp_server, "_resolve_kg_path", lambda: str(db_path))
    monkeypatch.setattr(mcp_server, "_get_kg", lambda *args, **kwargs: kg)
    monkeypatch.setattr(mcp_server, "_wal_log", lambda *args, **kwargs: None)
    try:
        yield kg
    finally:
        kg.close()


def test_kg_add_duplicate_reports_already_exists_and_preserves_original_provenance(isolated_kg):
    first = mcp_server.tool_kg_add(
        subject="Dax",
        predicate="likes",
        object="tea",
        source_closet="cap_1",
    )
    second = mcp_server.tool_kg_add(
        subject="Dax",
        predicate="likes",
        object="tea",
        source_closet="cap_2",
    )

    assert first["success"] is True
    assert "reason" not in first
    assert second["success"] is True
    assert second["reason"] == "already_exists"
    assert second["triple_id"] == first["triple_id"]

    facts = isolated_kg.query_entity("Dax", direction="outgoing")
    current = [
        fact
        for fact in facts
        if fact["current"] and fact["predicate"] == "likes" and fact["object"] == "tea"
    ]
    assert len(current) == 1
    assert current[0]["source_closet"] == "cap_1"


def test_kg_add_duplicate_reason_respects_entity_identity_normalization(isolated_kg):
    first = mcp_server.tool_kg_add(
        subject="Dax Rider",
        predicate="visits",
        object="Tea Shop",
    )
    second = mcp_server.tool_kg_add(
        subject="dax rider",
        predicate="visits",
        object="tea shop",
    )

    assert second["reason"] == "already_exists"
    assert second["triple_id"] == first["triple_id"]


def test_kg_add_after_invalidation_is_a_fresh_insert(isolated_kg):
    first = mcp_server.tool_kg_add(
        subject="Dax",
        predicate="likes",
        object="tea",
    )
    isolated_kg.invalidate("Dax", "likes", "tea", ended="2026-08-15")

    second = mcp_server.tool_kg_add(
        subject="Dax",
        predicate="likes",
        object="tea",
        valid_from="2026-08-16",
    )

    assert second["success"] is True
    assert "reason" not in second
    assert second["triple_id"] != first["triple_id"]
