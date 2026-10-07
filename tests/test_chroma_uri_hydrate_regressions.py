"""C1: SQLite drawer hydrate must not publish chroma:uri."""

from _mcp_server_helpers import _patch_mcp_server


def test_singular_drawer_preserves_sdk_metadata_disclosure(monkeypatch, config, collection, kg):
    """C1: internal Chroma URIs stay hidden, while ordinary metadata survives."""
    from mempalace import mcp_server

    _patch_mcp_server(monkeypatch, config, kg)
    metadata = {"wing": "test", "room": "notes", "flag": False, "label": "日本語"}
    collection.add(
        ids=["uri-row"],
        documents=["exact content"],
        uris=["file:///private/source.png"],
        metadatas=[metadata],
        embeddings=[[1.0] + [0.0] * 383],
    )
    expected = collection.get(ids=["uri-row"], include=["documents", "metadatas"])

    def no_collection():
        raise AssertionError("A supported scalar drawer must use the SQLite fast path")

    monkeypatch.setattr(mcp_server, "_get_collection", no_collection)
    actual = mcp_server.tool_get_drawer("uri-row")
    assert actual["content"] == expected["documents"][0]
    assert actual["metadata"] == expected["metadatas"][0], (
        "SQLite drawer reads must not publish Chroma's internal URI"
    )
    assert actual["metadata"] == metadata


def test_lexical_search_hides_reserved_chroma_metadata(collection, palace_path):
    """C1 sibling: the lexical SQLite read must not publish chroma:uri either."""
    from mempalace.backends.chroma import ChromaCollection

    metadata = {"wing": "test", "room": "notes"}
    collection.add(
        ids=["uri-lex"],
        documents=["kestrel migration lexical note"],
        uris=["file:///private/source.png"],
        metadatas=[metadata],
        embeddings=[[1.0] + [0.0] * 383],
    )

    lexical = ChromaCollection(collection, palace_path=palace_path)
    hits = lexical.lexical_search(query="kestrel migration", n_results=5).hits

    assert hits, "expected a lexical hit from the SQLite path"
    assert hits[0].metadata == metadata, (
        "lexical search must not publish Chroma's reserved metadata"
    )
