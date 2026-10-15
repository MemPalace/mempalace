"""Registry sentinels are bookkeeping, not memories (#2635).

A path rename writes one ``room=_registry`` / ``ingest_mode=registry`` row
per conversation file. Those documents are the new path, so a query that
shares a path token used to fill the page with them.
"""

from mempalace.backends import PalaceRef
from mempalace.backends.base import (
    is_registry_sentinel,
    where_without_registry_exclusion,
)
from mempalace.backends.sqlite_exact import SQLiteExactBackend
from mempalace.searcher import (
    _bm25_only_via_sqlite,
    drawer_search_where,
    search_memories,
)

_REAL_TEXT = (
    "Remember the newdir lantern protocol: guests stay on the third floor "
    "and the west hall key lives with the night clerk."
)


def _registry(meta_kind: str, i: int) -> tuple:
    path = f"/tmp/newdir/palace/conversations/file-{meta_kind}-{i}.md"
    if meta_kind == "both":
        meta = {"room": "_registry", "ingest_mode": "registry"}
    elif meta_kind == "room":
        meta = {"room": "_registry"}
    else:
        meta = {"room": "conversations", "ingest_mode": "registry"}
    meta.update({"wing": "conversations", "source_file": path, "added_by": "convo_miner"})
    return path, meta


def test_drawer_search_where_excludes_registry_without_dropping_caller_scope():
    bare = drawer_search_where()
    assert bare == {
        "$and": [
            {"room": {"$nin": ["_registry"]}},
            {"ingest_mode": {"$nin": ["registry"]}},
        ]
    }
    scoped = drawer_search_where(wing="project", room="backend")
    assert scoped["$and"][0] == {"wing": "project"}
    assert scoped["$and"][1] == {"room": "backend"}
    assert where_without_registry_exclusion(bare) == {}
    assert where_without_registry_exclusion(scoped) == {
        "$and": [{"wing": "project"}, {"room": "backend"}]
    }
    assert is_registry_sentinel({"room": "_registry"})
    assert is_registry_sentinel({"ingest_mode": "registry", "room": "conversations"})
    assert not is_registry_sentinel({"room": "backend"})
    assert not is_registry_sentinel({"ingest_mode": "sweep"})
    assert not is_registry_sentinel({})
    assert not is_registry_sentinel(None)


def _seed_palace(collection) -> None:
    ids = ["real-project", "real-sweep", "real-extract"]
    docs = [
        _REAL_TEXT,
        "Sweeper note: the newdir import finished after the home directory moved.",
        "Format extract kept a newdir paragraph that normalized to text.",
    ]
    metas = [
        {"wing": "project", "room": "backend", "source_file": "auth.py"},
        {"wing": "conversations", "ingest_mode": "sweep", "source_file": "session.jsonl"},
        {
            "wing": "documents",
            "room": "documents",
            "ingest_mode": "extract",
            "is_sentinel": True,
            "source_file": "empty.rtf",
        },
    ]
    for kind, count in (("both", 24), ("room", 4), ("mode", 4)):
        for i in range(count):
            path, meta = _registry(kind, i)
            ids.append(f"reg-{kind}-{i}")
            docs.append(f"[registry] {path}")
            metas.append(meta)
    collection.add(ids=ids, documents=docs, metadatas=metas)


def _assert_real_memories(result: dict) -> None:
    hits = result["results"]
    found = {hit["drawer_id"] for hit in hits}
    assert "real-project" in found
    assert "real-sweep" in found
    assert "real-extract" in found
    assert all(hit.get("room") != "_registry" for hit in hits)
    assert all(not str(hit["drawer_id"]).startswith("reg-") for hit in hits)
    assert all("[registry]" not in (hit.get("text") or "") for hit in hits)


def test_search_memories_skips_registry_sentinels_on_vector_and_union(palace_path, collection):
    _seed_palace(collection)
    for strategy in ("vector", "union"):
        result = search_memories(
            "newdir",
            palace_path,
            n_results=5,
            candidate_strategy=strategy,
        )
        assert "error" not in result, result
        _assert_real_memories(result)

    scoped = search_memories("newdir", palace_path, wing="project", n_results=5)
    ids = [hit["drawer_id"] for hit in scoped["results"]]
    assert ids == ["real-project"]


def test_bm25_fallback_excludes_registry_before_the_candidate_cap(palace_path, collection):
    _seed_palace(collection)
    disabled = search_memories("newdir", palace_path, n_results=5, vector_disabled=True)
    assert disabled.get("fallback") == "bm25_only_via_sqlite"
    _assert_real_memories(disabled)

    # Fewer slots than sentinel rows. Without a SQL exclusion the cap fills
    # with ``[registry]`` paths and the real drawer never reaches BM25.
    capped = _bm25_only_via_sqlite("newdir", palace_path, n_results=3, max_candidates=3)
    assert "error" not in capped, capped
    _assert_real_memories(capped)

    scoped = _bm25_only_via_sqlite(
        "newdir", palace_path, wing="project", n_results=5, max_candidates=10
    )
    assert [hit["drawer_id"] for hit in scoped["results"]] == ["real-project"]


def test_sqlite_exact_query_and_lexical_cap_drop_registry_sentinels(tmp_path):
    backend = SQLiteExactBackend()
    palace = PalaceRef(id=str(tmp_path), local_path=str(tmp_path))
    col = backend.get_collection(palace=palace, collection_name="mempalace_drawers", create=True)
    ids = ["real", "sweep"]
    docs = [_REAL_TEXT, "sweep newdir"]
    metas = [
        {"wing": "project", "room": "backend"},
        {"ingest_mode": "sweep", "source_file": "session.jsonl"},
    ]
    embeddings = [[0.0, 1.0], [0.0, 1.0]]
    for i in range(12):
        ids.append(f"reg-{i}")
        docs.append("newdir")
        metas.append(
            {
                "room": "_registry",
                "ingest_mode": "registry",
                "source_file": f"/tmp/newdir/file-{i}.md",
            }
        )
        embeddings.append([1.0, 0.0])
    col.add(ids=ids, documents=docs, metadatas=metas, embeddings=embeddings)

    # Query vector sits on the sentinel embedding. Unfiltered search still
    # has to return the memory, not the bookkeeping row.
    ranked = col.query(query_embeddings=[[1.0, 0.0]], n_results=3)
    assert set(ranked.ids[0]) == {"real", "sweep"}

    lexical = col.lexical_search(query="newdir", n_results=1).hits
    assert lexical and lexical[0].id in {"real", "sweep"}
    assert all(not hit.id.startswith("reg-") for hit in lexical)
