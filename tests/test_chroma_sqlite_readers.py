"""Every raw-SQLite reader over chroma.sqlite3, pinned on one real palace.

The palace holds text needing the stored-text escape (NUL, a lone surrogate),
typed metadata, three spellings of one project wing, a diary, a chunked
drawer, a registry row and a sentinel, so a change to how these readers open
the database, build their SQL or decode stored text shows up here.
"""

import pytest

from mempalace.backends import PalaceRef, chroma, chroma_sqlite

_NAME = "mempalace_drawers"

_DRAWERS = [
    (
        "drawer_a",
        "alpha whole word vector notes",
        {
            "wing": "my_app",
            "room": "notes",
            "filed_at": "2026-01-03",
            "n": 3,
            "f": 1.5,
            "flag": True,
            "date": "2026-01-03",
            "hall": "h1",
            "source_file": "/home/u/.claude/projects/p/s1.jsonl",
        },
    ),
    (
        "drawer_b",
        "nul\x00byte vector text",
        {
            "wing": "my-app",
            "room": "notes",
            "filed_at": "2026-01-02",
            "hall": "h2",
            "source_file": "/home/u/.codex/sessions/s2.jsonl",
        },
    ),
    (
        "drawer_c",
        "surrogate \udcff tail vectors",
        {"wing": "wing_my_app", "room": "ideas", "filed_at": "2026-01-01", "flag": False},
    ),
    (
        "diary_1",
        "dear diary one",
        {
            "wing": "wing_my_app",
            "room": "diary",
            "agent": "observer",
            "filed_at": "2026-01-05",
            "topic": "t1",
        },
    ),
    (
        "diary_2",
        "dear diary \x00 two",
        {"wing": "my_app", "room": "diary", "agent": "observer", "filed_at": "2026-01-06"},
    ),
    (
        "chunk_1",
        "first chunk vector",
        {"wing": "other", "room": "code", "parent_drawer_id": "logical", "chunk_index": 0},
    ),
    (
        "chunk_2",
        "second chunk vector",
        {"wing": "other", "room": "code", "parent_drawer_id": "logical", "chunk_index": 1},
    ),
    (
        "undated",
        "no date vector at all",
        {"wing": "other", "room": "code"},
    ),
    (
        "_reg_0123456789abcdef01234567",
        "[registry] /chats/empty.txt",
        {
            "wing": "my_app",
            "room": "_registry",
            "ingest_mode": "registry",
            "filed_at": "2026-02-01",
        },
    ),
    (
        "sentinel_project_0123456789abcdef",
        "[empty]",
        {"wing": "my_app", "room": "notes", "is_sentinel": True, "filed_at": "2026-02-02"},
    ),
]


@pytest.fixture
def palace(tmp_path):
    path = tmp_path / "palace"
    col = chroma.ChromaBackend().get_collection(
        palace=PalaceRef(id=str(path), local_path=str(path)),
        collection_name=_NAME,
        create=True,
    )
    col.add(
        ids=[d[0] for d in _DRAWERS],
        documents=[d[1] for d in _DRAWERS],
        metadatas=[d[2] for d in _DRAWERS],
        embeddings=[[float(i + 1), 1.0, 0.5] for i in range(len(_DRAWERS))],
    )
    return str(path), col


def _sorted_ids_meta(result):
    if result is None:
        return None
    ids, metas = result
    return sorted(zip(ids, (sorted(m.items()) for m in metas)))


def _records(result):
    return [
        (i, d, sorted((m or {}).items()))
        for i, d, m in zip(result.ids, result.documents, result.metadatas)
    ]


def _outputs(palace_path, col):
    wings = ["my_app", "my-app", "wing_my_app"]
    wing_rooms = chroma_sqlite._sqlite_wing_room_counts(palace_path, _NAME)
    recent = col.get_recent(limit=4)
    recent_wing = col.get_recent(
        limit=10, where={"$and": [{"wing": {"$in": wings}}, {"room": "notes"}]}
    )
    hits = col.lexical_search(query="vector", n_results=20)
    scoped_hits = col.lexical_search(query="vector", n_results=20, where={"wing": {"$in": wings}})
    return {
        "embedding_count": chroma_sqlite._sqlite_embedding_count(palace_path, _NAME),
        "has_rows": chroma_sqlite._sqlite_collection_has_rows(palace_path, _NAME),
        "has_rows_missing": chroma_sqlite._sqlite_collection_has_rows(palace_path, "nope"),
        "recent": _records(recent),
        "recent_wing": _records(recent_wing),
        "wing_names": chroma_sqlite.sqlite_wing_names(palace_path),
        "wing_rooms": (
            wing_rooms[0],
            sorted((w, sorted(r.items())) for w, r in wing_rooms[1].items()),
        ),
        "hall_counts": sorted(chroma_sqlite.sqlite_room_wing_hall_counts(palace_path, _NAME)),
        "source_counts": sorted(chroma_sqlite.sqlite_wing_source_counts(palace_path, _NAME)),
        "iter_metadata": [
            sorted((m or {}).items()) for m in col.iter_metadata(keys=["wing", "flag"])
        ],
        "iter_metadata_all": [sorted((m or {}).items()) for m in col.iter_metadata()],
        "list_all": _sorted_ids_meta(chroma_sqlite.sqlite_list_id_metadata(palace_path, _NAME)),
        "list_wings": _sorted_ids_meta(
            chroma_sqlite.sqlite_list_id_metadata(
                palace_path, _NAME, {"$and": [{"wing": {"$in": wings}}, {"room": "notes"}]}
            )
        ),
        "documents": chroma_sqlite.sqlite_documents_for_ids(
            palace_path, _NAME, ["drawer_b", "drawer_c", "missing"]
        ),
        "diary": chroma_sqlite.sqlite_diary_rows(
            palace_path, _NAME, agent_name="observer", wing=wings, limit=5
        ),
        "drawer_direct": chroma_sqlite.sqlite_drawer_rows(palace_path, _NAME, drawer_id="drawer_c"),
        "drawer_group": chroma_sqlite.sqlite_drawer_rows(palace_path, _NAME, drawer_id="logical"),
        "lexical": sorted((h.id, h.document) for h in hits.hits),
        "lexical_scoped": sorted((h.id, h.document) for h in scoped_hits.hits),
        "bm25": _bm25_hits(palace_path),
        "bm25_wing": _bm25_hits(palace_path, wing="my_app"),
    }


def _bm25_hits(palace_path, **filters):
    from mempalace.searcher import _bm25_only_via_sqlite

    result = _bm25_only_via_sqlite(
        "vector", palace_path, n_results=20, collection_name=_NAME, **filters
    )
    return sorted(
        (hit["drawer_id"], hit["text"], hit["wing"], hit["room"]) for hit in result["results"]
    )


def test_every_sqlite_reader_returns_what_it_did(palace):
    palace_path, col = palace
    assert _outputs(palace_path, col) == EXPECTED


def test_missing_database_reads_as_none(tmp_path):
    empty = str(tmp_path)
    assert chroma_sqlite._sqlite_embedding_count(empty, _NAME) is None
    assert chroma_sqlite._sqlite_wing_room_counts(empty, _NAME) is None
    assert chroma_sqlite.sqlite_wing_names(empty) is None
    assert chroma_sqlite.sqlite_room_wing_hall_counts(empty, _NAME) is None
    assert chroma_sqlite.sqlite_wing_source_counts(empty, _NAME) is None
    assert chroma_sqlite.sqlite_list_id_metadata(empty, _NAME) is None
    assert chroma_sqlite.sqlite_documents_for_ids(empty, _NAME, ["x"]) is None
    assert chroma_sqlite.sqlite_diary_rows(empty, _NAME, agent_name="a") is None
    assert chroma_sqlite.sqlite_drawer_rows(empty, _NAME, drawer_id="x") is None
    assert (
        chroma_sqlite._sqlite_recent_records(
            empty, _NAME, limit=1, equalities=[], order_field="filed_at"
        )
        is None
    )


EXPECTED = {
    "bm25": [
        ("drawer_a", "alpha whole word vector notes", "my_app", "notes"),
        ("drawer_b", "nul\x00byte vector text", "my-app", "notes"),
        ("drawer_c", "surrogate \udcff tail vectors", "wing_my_app", "ideas"),
        ("logical", "first chunk vector", "other", "code"),
        ("logical", "second chunk vector", "other", "code"),
        ("undated", "no date vector at all", "other", "code"),
    ],
    "bm25_wing": [
        ("drawer_a", "alpha whole word vector notes", "my_app", "notes"),
        ("drawer_b", "nul\x00byte vector text", "my-app", "notes"),
        ("drawer_c", "surrogate \udcff tail vectors", "wing_my_app", "ideas"),
    ],
    "diary": (
        2,
        [
            (
                "diary_2",
                "dear diary \x00 two",
                {
                    "agent": "observer",
                    "filed_at": "2026-01-06",
                    "last_modified": "2026-01-06",
                    "room": "diary",
                    "wing": "my_app",
                },
            ),
            (
                "diary_1",
                "dear diary one",
                {
                    "agent": "observer",
                    "filed_at": "2026-01-05",
                    "last_modified": "2026-01-05",
                    "room": "diary",
                    "topic": "t1",
                    "wing": "wing_my_app",
                },
            ),
        ],
    ),
    "documents": {
        "drawer_b": "nul\x00byte vector text",
        "drawer_c": "surrogate \udcff tail vectors",
    },
    "drawer_direct": (
        True,
        [
            (
                "drawer_c",
                "surrogate \udcff tail vectors",
                {
                    "filed_at": "2026-01-01",
                    "flag": False,
                    "last_modified": "2026-01-01",
                    "room": "ideas",
                    "wing": "wing_my_app",
                },
            )
        ],
    ),
    "drawer_group": (
        False,
        [
            (
                "chunk_1",
                "first chunk vector",
                {"chunk_index": 0, "parent_drawer_id": "logical", "room": "code", "wing": "other"},
            ),
            (
                "chunk_2",
                "second chunk vector",
                {"chunk_index": 1, "parent_drawer_id": "logical", "room": "code", "wing": "other"},
            ),
        ],
    ),
    "embedding_count": 10,
    "hall_counts": [
        ("_registry", "my_app", "", 1, ""),
        ("code", "other", "", 3, ""),
        ("diary", "my_app", "", 1, ""),
        ("diary", "wing_my_app", "", 1, ""),
        ("ideas", "wing_my_app", "", 1, ""),
        ("notes", "my-app", "h2", 1, ""),
        ("notes", "my_app", "", 1, ""),
        ("notes", "my_app", "h1", 1, "2026-01-03"),
    ],
    "has_rows": True,
    "has_rows_missing": False,
    "iter_metadata": [
        [("flag", True), ("wing", "my_app")],
        [("wing", "my-app")],
        [("flag", False), ("wing", "wing_my_app")],
        [("wing", "wing_my_app")],
        [("wing", "my_app")],
        [("wing", "other")],
        [("wing", "other")],
        [("wing", "other")],
        [("wing", "my_app")],
        [("wing", "my_app")],
    ],
    "iter_metadata_all": [
        [
            ("date", "2026-01-03"),
            ("f", 1.5),
            ("filed_at", "2026-01-03"),
            ("flag", True),
            ("hall", "h1"),
            ("last_modified", "2026-01-03"),
            ("n", 3),
            ("room", "notes"),
            ("source_file", "/home/u/.claude/projects/p/s1.jsonl"),
            ("wing", "my_app"),
        ],
        [
            ("filed_at", "2026-01-02"),
            ("hall", "h2"),
            ("last_modified", "2026-01-02"),
            ("room", "notes"),
            ("source_file", "/home/u/.codex/sessions/s2.jsonl"),
            ("wing", "my-app"),
        ],
        [
            ("filed_at", "2026-01-01"),
            ("flag", False),
            ("last_modified", "2026-01-01"),
            ("room", "ideas"),
            ("wing", "wing_my_app"),
        ],
        [
            ("agent", "observer"),
            ("filed_at", "2026-01-05"),
            ("last_modified", "2026-01-05"),
            ("room", "diary"),
            ("topic", "t1"),
            ("wing", "wing_my_app"),
        ],
        [
            ("agent", "observer"),
            ("filed_at", "2026-01-06"),
            ("last_modified", "2026-01-06"),
            ("room", "diary"),
            ("wing", "my_app"),
        ],
        [("chunk_index", 0), ("parent_drawer_id", "logical"), ("room", "code"), ("wing", "other")],
        [("chunk_index", 1), ("parent_drawer_id", "logical"), ("room", "code"), ("wing", "other")],
        [("room", "code"), ("wing", "other")],
        [
            ("filed_at", "2026-02-01"),
            ("ingest_mode", "registry"),
            ("last_modified", "2026-02-01"),
            ("room", "_registry"),
            ("wing", "my_app"),
        ],
        [
            ("filed_at", "2026-02-02"),
            ("is_sentinel", True),
            ("last_modified", "2026-02-02"),
            ("room", "notes"),
            ("wing", "my_app"),
        ],
    ],
    "lexical": [
        ("chunk_1", "first chunk vector"),
        ("chunk_2", "second chunk vector"),
        ("drawer_a", "alpha whole word vector notes"),
        ("drawer_b", "nul\x00byte vector text"),
        ("undated", "no date vector at all"),
    ],
    "lexical_scoped": [
        ("drawer_a", "alpha whole word vector notes"),
        ("drawer_b", "nul\x00byte vector text"),
    ],
    "list_all": [
        (
            "_reg_0123456789abcdef01234567",
            [
                ("filed_at", "2026-02-01"),
                ("ingest_mode", "registry"),
                ("last_modified", "2026-02-01"),
                ("room", "_registry"),
                ("wing", "my_app"),
            ],
        ),
        (
            "chunk_1",
            [
                ("chunk_index", 0),
                ("parent_drawer_id", "logical"),
                ("room", "code"),
                ("wing", "other"),
            ],
        ),
        (
            "chunk_2",
            [
                ("chunk_index", 1),
                ("parent_drawer_id", "logical"),
                ("room", "code"),
                ("wing", "other"),
            ],
        ),
        (
            "diary_1",
            [
                ("agent", "observer"),
                ("filed_at", "2026-01-05"),
                ("last_modified", "2026-01-05"),
                ("room", "diary"),
                ("topic", "t1"),
                ("wing", "wing_my_app"),
            ],
        ),
        (
            "diary_2",
            [
                ("agent", "observer"),
                ("filed_at", "2026-01-06"),
                ("last_modified", "2026-01-06"),
                ("room", "diary"),
                ("wing", "my_app"),
            ],
        ),
        (
            "drawer_a",
            [
                ("date", "2026-01-03"),
                ("f", 1.5),
                ("filed_at", "2026-01-03"),
                ("flag", True),
                ("hall", "h1"),
                ("last_modified", "2026-01-03"),
                ("n", 3),
                ("room", "notes"),
                ("source_file", "/home/u/.claude/projects/p/s1.jsonl"),
                ("wing", "my_app"),
            ],
        ),
        (
            "drawer_b",
            [
                ("filed_at", "2026-01-02"),
                ("hall", "h2"),
                ("last_modified", "2026-01-02"),
                ("room", "notes"),
                ("source_file", "/home/u/.codex/sessions/s2.jsonl"),
                ("wing", "my-app"),
            ],
        ),
        (
            "drawer_c",
            [
                ("filed_at", "2026-01-01"),
                ("flag", False),
                ("last_modified", "2026-01-01"),
                ("room", "ideas"),
                ("wing", "wing_my_app"),
            ],
        ),
        (
            "sentinel_project_0123456789abcdef",
            [
                ("filed_at", "2026-02-02"),
                ("is_sentinel", True),
                ("last_modified", "2026-02-02"),
                ("room", "notes"),
                ("wing", "my_app"),
            ],
        ),
        ("undated", [("room", "code"), ("wing", "other")]),
    ],
    "list_wings": [
        (
            "drawer_a",
            [
                ("date", "2026-01-03"),
                ("f", 1.5),
                ("filed_at", "2026-01-03"),
                ("flag", True),
                ("hall", "h1"),
                ("last_modified", "2026-01-03"),
                ("n", 3),
                ("room", "notes"),
                ("source_file", "/home/u/.claude/projects/p/s1.jsonl"),
                ("wing", "my_app"),
            ],
        ),
        (
            "drawer_b",
            [
                ("filed_at", "2026-01-02"),
                ("hall", "h2"),
                ("last_modified", "2026-01-02"),
                ("room", "notes"),
                ("source_file", "/home/u/.codex/sessions/s2.jsonl"),
                ("wing", "my-app"),
            ],
        ),
        (
            "sentinel_project_0123456789abcdef",
            [
                ("filed_at", "2026-02-02"),
                ("is_sentinel", True),
                ("last_modified", "2026-02-02"),
                ("room", "notes"),
                ("wing", "my_app"),
            ],
        ),
    ],
    "recent": [
        (
            "sentinel_project_0123456789abcdef",
            "[empty]",
            [
                ("filed_at", "2026-02-02"),
                ("is_sentinel", True),
                ("last_modified", "2026-02-02"),
                ("room", "notes"),
                ("wing", "my_app"),
            ],
        ),
        (
            "_reg_0123456789abcdef01234567",
            "[registry] /chats/empty.txt",
            [
                ("filed_at", "2026-02-01"),
                ("ingest_mode", "registry"),
                ("last_modified", "2026-02-01"),
                ("room", "_registry"),
                ("wing", "my_app"),
            ],
        ),
        (
            "diary_2",
            "dear diary \x00 two",
            [
                ("agent", "observer"),
                ("filed_at", "2026-01-06"),
                ("last_modified", "2026-01-06"),
                ("room", "diary"),
                ("wing", "my_app"),
            ],
        ),
        (
            "diary_1",
            "dear diary one",
            [
                ("agent", "observer"),
                ("filed_at", "2026-01-05"),
                ("last_modified", "2026-01-05"),
                ("room", "diary"),
                ("topic", "t1"),
                ("wing", "wing_my_app"),
            ],
        ),
    ],
    "recent_wing": [
        (
            "sentinel_project_0123456789abcdef",
            "[empty]",
            [
                ("filed_at", "2026-02-02"),
                ("is_sentinel", True),
                ("last_modified", "2026-02-02"),
                ("room", "notes"),
                ("wing", "my_app"),
            ],
        ),
        (
            "drawer_a",
            "alpha whole word vector notes",
            [
                ("date", "2026-01-03"),
                ("f", 1.5),
                ("filed_at", "2026-01-03"),
                ("flag", True),
                ("hall", "h1"),
                ("last_modified", "2026-01-03"),
                ("n", 3),
                ("room", "notes"),
                ("source_file", "/home/u/.claude/projects/p/s1.jsonl"),
                ("wing", "my_app"),
            ],
        ),
        (
            "drawer_b",
            "nul\x00byte vector text",
            [
                ("filed_at", "2026-01-02"),
                ("hall", "h2"),
                ("last_modified", "2026-01-02"),
                ("room", "notes"),
                ("source_file", "/home/u/.codex/sessions/s2.jsonl"),
                ("wing", "my-app"),
            ],
        ),
    ],
    "source_counts": [
        ("my-app", "/home/u/.codex/sessions/s2.jsonl", 1),
        ("my_app", "/home/u/.claude/projects/p/s1.jsonl", 1),
    ],
    "wing_names": ["my-app", "my_app", "other", "wing_my_app"],
    "wing_rooms": (
        8,
        [
            ("my-app", [("notes", 1)]),
            ("my_app", [("diary", 1), ("notes", 1)]),
            ("other", [("code", 3)]),
            ("wing_my_app", [("diary", 1), ("ideas", 1)]),
        ],
    ),
}
