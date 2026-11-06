"""Scope check: the separate list helper also returns public metadata."""

import json

from _mcp_server_helpers import _patch_mcp_server


def test_public_list_metadata_matches_sdk_reserved_key_policy(monkeypatch, config, collection, kg):
    from mempalace import mcp_server as mcp

    _patch_mcp_server(monkeypatch, config, kg)
    collection.add(
        ids=["listed-uri"],
        documents=["preserved content"],
        uris=["file:///private/source.png"],
        metadatas=[{"wing": "test", "room": "notes", "chroma:custom": "internal"}],
        embeddings=[[1.0] + [0.0] * 383],
    )
    sdk = collection.get(ids=["listed-uri"], include=["documents", "metadatas"])
    singular = mcp.tool_get_drawer("listed-uri")
    listed = mcp.tool_list_drawers()
    assert singular["metadata"] == sdk["metadatas"][0]
    assert listed["drawers"][0]["content_preview"] == sdk["documents"][0]
    print(
        json.dumps(
            {
                "sdk_metadata": sdk["metadatas"][0],
                "singular_metadata": singular["metadata"],
                "list_metadata": listed["drawers"][0]["metadata"],
            }
        )
    )
    assert listed["drawers"][0]["metadata"] == sdk["metadatas"][0], (
        "List helper leaks reserved metadata hidden by both SDK and fixed singular helper"
    )
