"""Search salience stays read-only on the damaged-vector fallback path."""

from datetime import datetime, timedelta, timezone
from functools import partial

import chromadb
import pytest

from mempalace import searcher
from mempalace.dynamics import drawer_salience


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=60)
DOCUMENT = "Verbatim heliotrope research notes.\nKeep this second line intact."
METADATA = {
    "wing": "salience",
    "room": "notes",
    "source_file": "/research/heliotrope.md",
    "filed_at": T0.isoformat(),
    "authored_at": "2025-12-30",
    "content_date": "2025-12-29",
    "content_date_source": "frontmatter",
    "strength": 2.75,
    "stability": 2.5,
    "last_activated": T0.isoformat(),
    "access_count": 7,
}


def _forbidden_storage_access(*args, **kwargs):
    pytest.fail("vector-disabled search must not open or write vector storage")


@pytest.mark.parametrize("owns_writer_lock", [True, False])
def test_vector_disabled_search_never_opens_storage_to_potentiate(
    monkeypatch, config, collection, owns_writer_lock
):
    from mempalace import mcp_server

    collection.add(ids=["heliotrope"], documents=[DOCUMENT], metadatas=[dict(METADATA)])
    before = collection.get(ids=["heliotrope"], include=["documents", "metadatas"])
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_READ_ONLY", False)
    monkeypatch.setenv("MEMPALACE_SALIENCE_POTENTIATE", "true")
    if owns_writer_lock:
        assert mcp_server._acquire_mcp_writer_lock() == (True, "")

    # Simulate the safe capacity probe's decision, not a corrupt native index.
    monkeypatch.setattr(
        mcp_server,
        "hnsw_capacity_status",
        lambda *_args: {"diverged": True, "message": "capacity divergence"},
    )
    monkeypatch.setattr(mcp_server, "_get_client", _forbidden_storage_access)
    monkeypatch.setattr(mcp_server, "_get_collection", _forbidden_storage_access)
    monkeypatch.setattr(mcp_server, "_acquire_mcp_writer_lock", _forbidden_storage_access)
    monkeypatch.setattr(searcher, "get_collection", _forbidden_storage_access)
    monkeypatch.setattr(searcher, "get_closets_collection", _forbidden_storage_access)
    monkeypatch.setattr(chromadb, "PersistentClient", _forbidden_storage_access)
    monkeypatch.setattr(collection, "update", _forbidden_storage_access)

    result = mcp_server.tool_search("heliotrope", max_distance=0)

    assert result["fallback"] == "bm25_only_via_sqlite"
    assert result["vector_disabled"] is True
    assert result["vector_disabled_reason"] == "capacity divergence"
    assert len(result["results"]) == 1
    assert result["results"][0]["drawer_id"] == "heliotrope"
    assert result["results"][0]["text"] == DOCUMENT
    assert collection.get(ids=["heliotrope"], include=["documents", "metadatas"]) == before


def test_sqlite_fallback_uses_float_metadata_for_lazy_salience_without_writes(
    monkeypatch, palace_path, collection
):
    collection.add(ids=["heliotrope"], documents=[DOCUMENT], metadatas=[dict(METADATA)])
    before = collection.get(ids=["heliotrope"], include=["documents", "metadatas"])
    monkeypatch.setattr(searcher, "drawer_salience", partial(drawer_salience, now=NOW))
    monkeypatch.setattr(searcher, "get_collection", _forbidden_storage_access)
    monkeypatch.setattr(searcher, "get_closets_collection", _forbidden_storage_access)
    monkeypatch.setattr(chromadb, "PersistentClient", _forbidden_storage_access)
    monkeypatch.setattr(collection, "update", _forbidden_storage_access)

    result = searcher.search_memories("heliotrope", palace_path, vector_disabled=True)

    assert result["fallback"] == "bm25_only_via_sqlite"
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["salience"] == {
        "strength": pytest.approx(1.0116684632214663),
        "stability": 2.5,
        "last_activated": T0.isoformat(),
        "access_count": 7,
    }
    assert hit["drawer_id"] == "heliotrope"
    assert hit["text"] == DOCUMENT
    assert hit["source_file"] == "heliotrope.md"
    assert hit["source_path"] == "/research/heliotrope.md"
    assert hit["filed_at"] == hit["created_at"] == T0.isoformat()
    assert hit["authored_at"] == "2025-12-30"
    assert hit["authored_at_source"] == "authored_at"
    assert hit["content_date"] == "2025-12-29"
    assert hit["content_date_source"] == "frontmatter"
    assert hit["matched_via"] == "bm25_sqlite"
    assert hit["distance"] is None
    assert collection.get(ids=["heliotrope"], include=["documents", "metadatas"]) == before


@pytest.mark.parametrize("backend_name", ["chroma", "sqlite_exact"])
@pytest.mark.parametrize("candidate_strategy", ["vector", "union"])
def test_vector_and_lexical_union_hits_expose_same_pure_salience(
    monkeypatch, tmp_path, backend_name, candidate_strategy
):
    from mempalace.backends import get_backend
    from mempalace.embedding import get_embedding_function
    from mempalace.palace import get_collection

    monkeypatch.setenv("MEMPALACE_BACKEND_EXPLICIT", backend_name)
    monkeypatch.setattr(searcher, "drawer_salience", partial(drawer_salience, now=NOW))
    palace_path = str(tmp_path / "palace")
    col = get_collection(palace_path, create=True)
    # The shared fixture supplies an in-process deterministic embedder. Explicit
    # stored vectors ensure lexical-only candidates miss the retained vector pool.
    query_vector = get_embedding_function()(["heliotrope"])[0]
    lexical_vector = [-value for value in query_vector]
    col.add(
        ids=["vector", "filler", "lexical"],
        documents=[
            "heliotrope vector notes",
            "ordinary unrelated filler",
            "heliotrope heliotrope heliotrope lexical notes",
        ],
        metadatas=[
            {**METADATA, "source_file": f"/research/{drawer_id}.md"}
            for drawer_id in ("vector", "filler", "lexical")
        ],
        embeddings=[query_vector, [0.0] * len(query_vector), lexical_vector],
    )
    before = col.get(include=["documents", "metadatas"])
    try:
        result = searcher.search_memories(
            "heliotrope",
            palace_path,
            n_results=2,
            candidate_strategy=candidate_strategy,
        )

        by_id = {hit["drawer_id"]: hit for hit in result["results"]}
        assert set(by_id) == {"vector", "lexical"}
        assert by_id["vector"]["matched_via"] == "drawer"
        assert by_id["lexical"]["matched_via"] == (
            "bm25_backend" if candidate_strategy == "union" else "drawer"
        )
        for hit in by_id.values():
            assert hit["salience"] == {
                "strength": pytest.approx(1.0116684632214663),
                "stability": 2.5,
                "last_activated": T0.isoformat(),
                "access_count": 7,
            }
            assert hit["source_path"] == f"/research/{hit['drawer_id']}.md"
            assert hit["authored_at"] == "2025-12-30"
            assert hit["content_date_source"] == "frontmatter"
        assert col.get(include=["documents", "metadatas"]) == before
    finally:
        get_backend(backend_name).close_palace(palace_path)
