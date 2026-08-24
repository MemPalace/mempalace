#!/usr/bin/env python3
"""Reproduce the duplicate hydrated-search-result regression.

Run with:

    uv run python examples/reproduce_duplicate_hydrated_results.py

The fixture has five chunks from one source and a closet that boosts that
source. Before the fix, the search returned three results from that source;
each was hydrated around the same query-best chunk, so an agent saw the same
expanded response repeatedly. The fixed behavior returns one hydrated result.
"""

from tempfile import TemporaryDirectory

from mempalace.palace import get_closets_collection, get_collection
from mempalace.searcher import search_memories


SOURCE = "/examples/duplicate-hydration.md"
QUERY = "JWT authentication"


def seed(palace_path: str) -> None:
    drawers = get_collection(palace_path)
    for index in range(5):
        drawers.upsert(
            ids=[f"drawer_example_duplicate_hydration_{index:03d}"],
            documents=[f"chunk {index}: JWT authentication flow details"],
            metadatas=[
                {
                    "wing": "example",
                    "room": "search",
                    "source_file": SOURCE,
                    "chunk_index": index,
                    "filed_at": "2026-08-11T00:00:00",
                }
            ],
        )

    closets = get_closets_collection(palace_path)
    closets.upsert(
        ids=["closet_example_duplicate_hydration"],
        documents=["JWT authentication|;|→drawer_example_duplicate_hydration_002"],
        metadatas=[{"wing": "example", "room": "search", "source_file": SOURCE}],
    )


def main() -> None:
    with TemporaryDirectory() as palace_path:
        seed(palace_path)
        result = search_memories(QUERY, palace_path, n_results=3)

    source_hits = [hit for hit in result["results"] if hit["source_path"] == SOURCE]
    print(f"Results for {SOURCE}: {len(source_hits)}")
    for index, hit in enumerate(source_hits, start=1):
        print(f"  {index}. matched_via={hit['matched_via']}; text={hit['text']!r}")

    assert len(source_hits) == 1, (
        "Expected one hydrated result per logical source. Before the fix, "
        "three matching chunks each hydrated to the same query-best response."
    )
    assert source_hits[0]["matched_via"] == "drawer+closet"
    print("PASS: source-level closet relevance produced one hydrated response.")


if __name__ == "__main__":
    main()
