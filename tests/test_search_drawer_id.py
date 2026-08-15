"""Regression tests for round-trippable search result drawer IDs (#2080)."""

from unittest.mock import MagicMock, patch

from mempalace.backends import LexicalHit, LexicalResult
from mempalace.searcher import (
    _aligned_query_ids,
    _finalize_candidate_hits,
    _query_drawers_with_filter_fallback,
    search_memories,
)


def _results_by_source(result: dict) -> dict:
    return {hit["source_file"]: hit for hit in result["results"]}


def test_vector_results_use_parent_for_chunks_and_stored_id_for_singles():
    drawers_col = MagicMock()
    drawers_col.distance_metric = "cosine"
    drawers_col.query.return_value = {
        "ids": [
            [
                "logical-parent_chunk_000001",
                "single-drawer",
            ]
        ],
        "documents": [
            [
                "chunk text",
                "single text",
            ]
        ],
        "metadatas": [
            [
                {
                    "wing": "work",
                    "room": "notes",
                    "source_file": "/palace/chunked.md",
                    "parent_drawer_id": "logical-parent",
                    "chunk_index": 1,
                },
                {
                    "wing": "work",
                    "room": "notes",
                    "source_file": "/palace/single.md",
                },
            ]
        ],
        "distances": [[0.1, 0.2]],
    }

    with patch(
        "mempalace.searcher.get_collection",
        return_value=drawers_col,
    ):
        with patch(
            "mempalace.searcher.get_closets_collection",
            side_effect=RuntimeError("no closets"),
        ):
            result = search_memories(
                "drawer identity",
                "/unused",
                n_results=5,
            )

    by_source = _results_by_source(result)

    assert by_source["chunked.md"]["drawer_id"] == "logical-parent"
    assert by_source["single.md"]["drawer_id"] == "single-drawer"

    assert all("_parent_drawer_id" not in hit for hit in result["results"])


def test_aligned_query_ids_pads_legacy_mock_results():
    assert _aligned_query_ids({}, 2) == [None, None]

    result_without_ids = {
        "documents": [["first", "second"]],
        "metadatas": [[{}, {}]],
        "distances": [[0.1, 0.2]],
    }

    assert _aligned_query_ids(result_without_ids, 2) == [None, None]


def test_filtered_query_fallback_keeps_ids_aligned_with_filtered_hits():
    drawers_col = MagicMock()
    drawers_col.query.side_effect = [
        RuntimeError("Error finding id"),
        {
            "ids": [["drop-id", "keep-id"]],
            "documents": [
                [
                    "drop document",
                    "keep document",
                ]
            ],
            "metadatas": [
                [
                    {
                        "wing": "drop",
                        "room": "notes",
                        "source_file": "drop.md",
                    },
                    {
                        "wing": "keep",
                        "room": "notes",
                        "source_file": "keep.md",
                    },
                ]
            ],
            "distances": [[0.2, 0.1]],
        },
    ]

    query_kwargs = {
        "query_texts": ["needle"],
        "n_results": 6,
        "include": [
            "documents",
            "metadatas",
            "distances",
        ],
        "where": {"wing": "keep"},
    }

    result = _query_drawers_with_filter_fallback(
        drawers_col,
        query_kwargs,
        "needle",
        2,
        "keep",
        None,
    )

    assert drawers_col.query.call_count == 2
    assert result["ids"] == [["keep-id"]]
    assert result["documents"] == [["keep document"]]
    assert result["metadatas"][0][0]["source_file"] == "keep.md"
    assert result["distances"] == [[0.1]]


def test_vector_drawer_id_round_trips_to_complete_chunked_drawer(
    seeded_collection,
):
    parent_id = "logical-roundtrip-2080"
    chunk_ids = [
        f"{parent_id}_chunk_000000",
        f"{parent_id}_chunk_000001",
    ]
    chunk_documents = [
        "roundtrip2080 first half ",
        "roundtrip2080 second half",
    ]

    seeded_collection.add(
        ids=chunk_ids,
        documents=chunk_documents,
        metadatas=[
            {
                "wing": "work",
                "room": "notes",
                "source_file": "/palace/roundtrip-2080.md",
                "parent_drawer_id": parent_id,
                "chunk_index": 0,
            },
            {
                "wing": "work",
                "room": "notes",
                "source_file": "/palace/roundtrip-2080.md",
                "parent_drawer_id": parent_id,
                "chunk_index": 1,
            },
        ],
    )

    search_col = MagicMock()
    search_col.distance_metric = "cosine"
    search_col.query.return_value = {
        "ids": [[chunk_ids[1]]],
        "documents": [[chunk_documents[1]]],
        "metadatas": [
            [
                {
                    "wing": "work",
                    "room": "notes",
                    "source_file": "/palace/roundtrip-2080.md",
                    "parent_drawer_id": parent_id,
                    "chunk_index": 1,
                }
            ]
        ],
        "distances": [[0.1]],
    }

    with patch(
        "mempalace.searcher.get_collection",
        return_value=search_col,
    ):
        with patch(
            "mempalace.searcher.get_closets_collection",
            side_effect=RuntimeError("no closets"),
        ):
            result = search_memories(
                "roundtrip2080",
                "/unused",
                n_results=5,
            )

    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["drawer_id"] == parent_id

    from mempalace import mcp_server

    with patch.object(
        mcp_server,
        "_get_collection",
        return_value=seeded_collection,
    ):
        fetched = mcp_server.tool_get_drawer(hit["drawer_id"])

    assert fetched["drawer_id"] == parent_id
    assert fetched["content"] == "".join(chunk_documents)
    assert fetched["chunks"] == 2
    assert fetched["chunk_ids"] == chunk_ids


def test_bm25_sqlite_results_use_parent_or_stored_id(
    palace_path,
    seeded_collection,
):
    seeded_collection.add(
        ids=[
            "bm25-parent_chunk_000000",
            "bm25-single-2080",
        ],
        documents=[
            "needle2080 appears in a chunked drawer",
            "needle2080 appears in an ordinary drawer",
        ],
        metadatas=[
            {
                "wing": "work",
                "room": "notes",
                "source_file": "/palace/chunked-2080.md",
                "parent_drawer_id": "bm25-parent",
                "chunk_index": 0,
            },
            {
                "wing": "work",
                "room": "notes",
                "source_file": "/palace/single-2080.md",
            },
        ],
    )

    result = search_memories(
        "needle2080",
        palace_path,
        n_results=10,
        vector_disabled=True,
        collection_name="mempalace_drawers",
    )

    assert "error" not in result

    by_source = _results_by_source(result)

    assert by_source["chunked-2080.md"]["drawer_id"] == "bm25-parent"
    assert by_source["single-2080.md"]["drawer_id"] == "bm25-single-2080"


def test_union_results_use_parent_or_lexical_hit_id():
    drawers_col = MagicMock()
    drawers_col.distance_metric = "cosine"
    drawers_col.lexical_search.return_value = LexicalResult(
        hits=[
            LexicalHit(
                id="union-parent_chunk_000001",
                document="chunk drawer identity text",
                metadata={
                    "wing": "work",
                    "room": "notes",
                    "source_file": "/palace/chunked.md",
                    "parent_drawer_id": "union-parent",
                    "chunk_index": 1,
                },
                score=2.0,
            ),
            LexicalHit(
                id="union-single",
                document="single drawer identity text",
                metadata={
                    "wing": "work",
                    "room": "notes",
                    "source_file": "/palace/single.md",
                    "chunk_index": 0,
                },
                score=1.0,
            ),
        ]
    )

    hits, error = _finalize_candidate_hits(
        candidate_strategy="union",
        hits=[],
        drawers_col=drawers_col,
        query="drawer identity",
        wing=None,
        room=None,
        n_results=5,
        max_distance=0.0,
    )

    assert error is None
    drawers_col.lexical_search.assert_called_once()

    by_source = {hit["source_file"]: hit for hit in hits}

    assert by_source["chunked.md"]["drawer_id"] == "union-parent"
    assert by_source["single.md"]["drawer_id"] == "union-single"

    assert all(
        "_source_file_full" not in hit
        and "_chunk_index" not in hit
        and "_parent_drawer_id" not in hit
        for hit in hits
    )
