from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mempalace.miner import process_file
from mempalace.palace import (
    get_closets_collection,
    get_collection,
)
from mempalace.searcher import (
    _candidate_pool_limits,
    _dedupe_rendered_hits,
    _enrich_closet_hits,
    search_memories,
)


def test_rendered_dedup_preserves_first_ranked_closet_hit_and_plain_repeats():
    hits = [
        {
            "drawer_id": "closet-first",
            "source_file": "same.md",
            "source_path": "/one/same.md",
            "text": "same rendered passage",
            "matched_via": "drawer+closet",
        },
        {
            "drawer_id": "closet-later",
            "source_file": "same.md",
            "source_path": "/one/same.md",
            "text": "same rendered passage",
            "matched_via": "drawer+closet",
        },
        {
            "drawer_id": "other-path",
            "source_file": "same.md",
            "source_path": "/two/same.md",
            "text": "same rendered passage",
            "matched_via": "drawer+closet",
        },
        {
            "drawer_id": "plain-repeat-1",
            "source_file": "repeat.md",
            "source_path": "/one/repeat.md",
            "text": ("legitimate repeated source text"),
            "matched_via": "drawer",
        },
        {
            "drawer_id": "plain-repeat-2",
            "source_file": "repeat.md",
            "source_path": "/one/repeat.md",
            "text": ("legitimate repeated source text"),
            "matched_via": "drawer",
        },
    ]

    result = _dedupe_rendered_hits(hits)

    assert [hit["drawer_id"] for hit in result] == [
        "closet-first",
        "other-path",
        "plain-repeat-1",
        "plain-repeat-2",
    ]


def test_closet_enrichment_memoises_by_source_and_parent_group():
    drawers_col = MagicMock()

    def get_group(
        *,
        where,
        include,
    ):
        assert include == [
            "documents",
            "metadatas",
        ]

        parent = where["$and"][1]["parent_drawer_id"]

        return SimpleNamespace(
            documents=[
                (f"{parent} token best chunk"),
                (f"{parent} neighboring context"),
            ],
            metadatas=[
                {
                    "chunk_index": 0,
                },
                {
                    "chunk_index": 1,
                },
            ],
        )

    drawers_col.get.side_effect = get_group

    hits = [
        {
            "drawer_id": "a-0",
            "text": "raw a0",
            "matched_via": ("drawer+closet"),
            "_source_file_full": ("/shared/log.md"),
            "_parent_drawer_id": ("parent-a"),
        },
        {
            "drawer_id": "a-1",
            "text": "raw a1",
            "matched_via": ("drawer+closet"),
            "_source_file_full": ("/shared/log.md"),
            "_parent_drawer_id": ("parent-a"),
        },
        {
            "drawer_id": "b-0",
            "text": "raw b0",
            "matched_via": ("drawer+closet"),
            "_source_file_full": ("/shared/log.md"),
            "_parent_drawer_id": ("parent-b"),
        },
    ]

    _enrich_closet_hits(
        hits,
        drawers_col,
        "token",
    )

    assert drawers_col.get.call_count == 2
    assert hits[0]["text"] == hits[1]["text"]
    assert "parent-a" in hits[0]["text"]
    assert "parent-b" in hits[2]["text"]
    assert hits[0]["text"] != hits[2]["text"]


def test_search_promotes_distinct_results_from_wider_pool_and_fetches_source_once():
    repeated_source = "/project/large.md"
    query = "quasar authentication token rotation"

    repeated_documents = [(f"raw repeated-source drawer {index}") for index in range(5)]
    fallback_documents = [(f"independent fallback passage {index}") for index in range(4)]
    documents = repeated_documents + fallback_documents

    repeated_metadatas = [
        {
            "source_file": (repeated_source),
            "wing": "code",
            "room": "docs",
            "chunk_index": index,
            "filed_at": (f"2026-08-04T00:00:0{index}"),
        }
        for index in range(5)
    ]
    fallback_metadatas = [
        {
            "source_file": (f"/project/fallback-{index}.md"),
            "wing": "code",
            "room": "docs",
            "chunk_index": 0,
            "filed_at": (f"2026-08-04T00:01:0{index}"),
        }
        for index in range(4)
    ]

    drawers_col = MagicMock()
    drawers_col.distance_metric = "cosine"
    drawers_col.query.return_value = {
        "ids": [
            [
                *[f"same-{index}" for index in range(5)],
                *[f"fallback-{index}" for index in range(4)],
            ]
        ],
        "documents": [documents],
        "metadatas": [(repeated_metadatas + fallback_metadatas)],
        "distances": [
            [
                0.05,
                0.06,
                0.07,
                0.08,
                0.09,
                0.45,
                0.46,
                0.47,
                0.48,
            ]
        ],
    }
    drawers_col.get.return_value = SimpleNamespace(
        documents=[
            ("context before the target"),
            ("quasar authentication token rotation exact target"),
            ("context after the target"),
        ],
        metadatas=[
            {
                "chunk_index": 0,
            },
            {
                "chunk_index": 1,
            },
            {
                "chunk_index": 2,
            },
        ],
    )

    closets_col = MagicMock()
    closets_col.query.return_value = {
        "ids": [
            [
                "closet-1",
            ]
        ],
        "documents": [
            [
                query,
            ]
        ],
        "metadatas": [
            [
                {
                    "source_file": (repeated_source),
                }
            ]
        ],
        "distances": [
            [
                0.1,
            ]
        ],
    }

    with patch(
        "mempalace.searcher.get_collection",
        return_value=drawers_col,
    ):
        with patch(
            "mempalace.searcher.get_closets_collection",
            return_value=closets_col,
        ):
            result = search_memories(
                query,
                "/fake/palace",
                n_results=5,
            )

    hits = result["results"]
    rendered_keys = {
        (
            hit["source_path"],
            hit["text"],
        )
        for hit in hits
    }

    assert len(hits) == 5
    assert len(rendered_keys) == 5
    assert hits[0]["drawer_id"] == "same-0"
    assert drawers_col.query.call_args.kwargs["n_results"] == 20
    assert drawers_col.get.call_count == 1
    assert sum(hit["source_path"] == repeated_source for hit in hits) == 1


def test_process_file_preserves_legitimate_exact_repeats_at_different_positions(
    tmp_path,
):
    repeated = (
        "REPEATED LEGAL NOTICE: "
        + ("This clause must appear in both the introduction and the appendix. " * 2)
    ).strip()

    unique = (
        "UNIQUE MIDDLE SECTION: "
        + ("This paragraph explains a different implementation detail for the reader. " * 2)
    ).strip()

    source = tmp_path / "legitimate-repeat.md"
    source.write_text(
        (repeated + "\n\n" + unique + "\n\n" + repeated),
        encoding="utf-8",
    )

    (
        drawer_count,
        room,
        skip_reason,
    ) = process_file(
        source,
        project_path=tmp_path,
        collection=None,
        wing="test",
        rooms=[
            {
                "name": "general",
                "description": ("General"),
                "keywords": [],
            }
        ],
        agent="test",
        dry_run=True,
        chunk_size=220,
        chunk_overlap=0,
        min_chunk_size=1,
    )

    assert drawer_count == 3
    assert room == "general"
    assert skip_reason is None


def test_real_chromadb_source_scoped_search_returns_no_duplicate_rendered_passages(
    palace_path,
):
    source = "/project/drift-reminder.py"
    query = "quasar authentication token rotation"

    documents = [
        (
            f"SECTION_{index:02d} "
            f"unique marker {index:02d}. "
            + (query if index == 3 else (f"independent topic {index:02d}"))
            + (f". Distinct diagnostic prose for chunk {index:02d}.")
        )
        for index in range(8)
    ]

    drawers = get_collection(palace_path)
    drawers.upsert(
        ids=[f"drawer_{index}" for index in range(8)],
        documents=documents,
        metadatas=[
            {
                "wing": "code",
                "room": "docs",
                "source_file": source,
                "chunk_index": index,
                "filed_at": (f"2026-08-04T12:00:0{index}"),
            }
            for index in range(8)
        ],
    )

    closets = get_closets_collection(palace_path)
    closets.upsert(
        ids=[
            "closet_drift_reminder",
        ],
        documents=[(f"{query} {query}|;|→drawer_3")],
        metadatas=[
            {
                "wing": "code",
                "room": "docs",
                "source_file": source,
            }
        ],
    )

    raw = drawers.get(
        where={
            "source_file": source,
        },
        include=[
            "documents",
            "metadatas",
        ],
    )

    assert len(raw.ids) == 8
    assert len(set(raw.ids)) == 8
    assert len(set(raw.documents)) == 8

    result = search_memories(
        query,
        palace_path,
        wing="code",
        room="docs",
        source_file=source,
        n_results=5,
    )

    assert "error" not in result, result

    hits = result["results"]

    assert hits
    assert any(hit["matched_via"] == "drawer+closet" for hit in hits)
    assert len(hits) == len(
        {
            (
                hit["source_path"],
                hit["text"],
            )
            for hit in hits
        }
    )


def test_candidate_pool_limits_widen_only_default_vector_strategy():
    assert _candidate_pool_limits(
        "vector",
        5,
    ) == (
        20,
        20,
    )

    assert _candidate_pool_limits(
        "union",
        5,
    ) == (
        15,
        5,
    )
