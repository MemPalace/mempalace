"""The metadata-only listing guard must not confuse source or extraction modes."""

import pytest

from mempalace.convo_visibility import visible_conversation_rows


def _meta(src="session.txt", mode="exchange", filed="A", index=0, total=3, history=False):
    return {
        "ingest_mode": "convos",
        "source_file": src,
        "extract_mode": mode,
        "source_mtime": 1.0,
        "filed_at": filed,
        "chunk_index": index,
        "chunk_total": total,
        "historical_version": history,
    }


def _visible(rows):
    ids, metadata = zip(*rows)
    return {row_id for row_id, _ in visible_conversation_rows(ids, metadata)}


def test_interrupted_replacement_exposes_only_complete_history():
    rows = [(f"a_history_{i}", _meta(index=i, history=True)) for i in range(3)]
    rows += [("b_partial", _meta(filed="B", total=4))]
    rows += [(f"a_old_{i}", _meta(index=i)) for i in (1, 2)]
    assert _visible(rows) == {f"a_history_{i}" for i in range(3)}


def test_completed_replacement_and_unrelated_source_and_mode_remain_visible():
    rows = [(f"a_history_{i}", _meta(index=i, history=True)) for i in range(3)]
    rows += [(f"b_{i}", _meta(filed="B", index=i)) for i in range(3)]
    rows += [(f"g_{i}", _meta(mode="general", index=i)) for i in range(3)]
    rows += [("other", _meta(src="other.txt", total=1))]
    assert _visible(rows) == {row_id for row_id, _ in rows}


def test_finalization_of_only_some_rows_is_not_publication():
    rows = [(f"b_{i}", _meta(filed="B", index=i, total=3 if i < 2 else 4)) for i in range(3)]
    assert not _visible(rows)


def test_missing_chunk_or_duplicate_index_cannot_fake_completion():
    rows = [(f"a_{i}", _meta(index=index)) for i, index in enumerate((0, 0, 2))]
    assert not _visible(rows)


def test_incomplete_historical_backup_does_not_hide_complete_current():
    rows = [(f"a_{i}", _meta(index=i)) for i in range(3)]
    rows += [("one_history", _meta(index=0, history=True))]
    assert _visible(rows) == {f"a_{i}" for i in range(3)}


def test_unstamped_legacy_and_non_conversation_rows_keep_previous_visibility():
    legacy = _meta()
    del legacy["chunk_total"]
    rows = [("legacy", legacy), ("project", {"ingest_mode": "project"})]
    assert _visible(rows) == {"legacy", "project"}


def test_public_neighbor_context_uses_only_the_matched_complete_generation():
    """Enrichment must not reintroduce B text after the vector hit was gated."""
    from types import SimpleNamespace

    from mempalace.searcher import _enrich_closet_hits

    a0 = _meta(filed="A", index=0, total=2, history=True)
    a1 = _meta(filed="A", index=1, total=2, history=True)
    b0 = _meta(filed="B", index=0, total=3)

    class FakeCollection:
        def get(self, **_kwargs):
            return SimpleNamespace(
                ids=["a0", "a1", "b0"],
                documents=["A chunk zero", "A chunk one", "UNPUBLISHED B neighbor"],
                metadatas=[a0, a1, b0],
            )

    hits = [
        {
            "matched_via": "drawer+closet",
            "text": "A chunk zero",
            "_source_file_full": "session.txt",
            "_chunk_index": 0,
            "_parent_drawer_id": None,
            "_convo_generation": (1.0, "A", "exchange", True),
        }
    ]
    result = _enrich_closet_hits(
        hits,
        FakeCollection(),
        visibility_filter=lambda _rows: {"a0", "a1"},
    )
    assert result[0]["context"] == "A chunk one"
    assert "UNPUBLISHED B" not in result[0]["context"]


@pytest.mark.parametrize("scope_field", ["wing", "room"])
def test_vector_candidate_gate_rejects_same_id_scope_change(monkeypatch, scope_field):
    """A validated ID is not enough if its search scope changed meanwhile."""
    from contextlib import nullcontext

    from mempalace.backends import chroma_sqlite
    from mempalace.convo_visibility import visible_conversation_candidate_ids

    committed = {
        **_meta(total=1),
        "wing": "current-wing",
        "room": "current-room",
    }
    stale_hit = {**committed, scope_field: "stale-scope"}
    connection = object()

    monkeypatch.setattr(
        chroma_sqlite,
        "sqlite_metadata_snapshot",
        lambda _palace_path, _collection_name: nullcontext(connection),
    )

    def source_snapshot(_palace_path, _collection_name, *, where, _connection):
        assert where == {"source_file": {"$in": ["session.txt"]}}
        assert _connection is connection
        return ["shared-physical-id"], [committed]

    monkeypatch.setattr(chroma_sqlite, "sqlite_list_id_metadata", source_snapshot)

    def check(meta):
        return visible_conversation_candidate_ids(
            "/unused/palace", "drawers", [("shared-physical-id", meta)]
        )

    assert check(stale_hit) == set(), "stale wing/room must not inherit current visibility"
    assert check(committed) == {"shared-physical-id"}


def test_vector_candidate_gate_rejects_same_id_torn_document(monkeypatch):
    """The query text must match the body in the validated SQLite snapshot."""
    from contextlib import nullcontext

    from mempalace.backends import chroma_sqlite
    from mempalace.convo_visibility import visible_conversation_candidate_ids

    committed = _meta(total=1)
    connection = object()
    monkeypatch.setattr(
        chroma_sqlite,
        "sqlite_metadata_snapshot",
        lambda _palace_path, _collection_name: nullcontext(connection),
    )

    def source_snapshot(_palace_path, _collection_name, *, where, _connection):
        assert where == {"source_file": {"$in": ["session.txt"]}}
        assert _connection is connection
        return ["same-id"], [committed]

    monkeypatch.setattr(chroma_sqlite, "sqlite_list_id_metadata", source_snapshot)

    def documents(_palace_path, _collection_name, ids, *, _connection):
        assert ids == ["same-id"]
        assert _connection is connection
        return {"same-id": "published A"}

    monkeypatch.setattr(chroma_sqlite, "sqlite_documents_for_ids", documents)

    def check(candidate):
        return visible_conversation_candidate_ids("/unused", "drawers", [candidate])

    assert check(("same-id", committed, "unfinished B")) == set()
    assert check(("same-id", committed, "published A")) == {"same-id"}
    # Metadata-only callers preserve their existing contract.
    assert check(("same-id", committed)) == {"same-id"}

    # Failure to read an exact body from SQLite must fail closed.
    monkeypatch.setattr(chroma_sqlite, "sqlite_documents_for_ids", lambda *_args, **_kw: None)
    assert check(("same-id", committed, "published A")) == set()


def test_public_vector_search_passes_candidate_text_to_visibility_guard(monkeypatch):
    """Ensure PUBLIC vector search actually invokes the body-aware gate."""
    from unittest.mock import MagicMock

    import mempalace.convo_visibility as visibility
    import mempalace.searcher as searcher

    meta = {**_meta(total=1), "wing": "sessions", "room": "notes"}
    collection = MagicMock()
    collection.distance_metric = "cosine"
    collection.query.return_value = {
        "ids": [["same-id"]],
        "documents": [["unpublished B"]],
        "metadatas": [[meta]],
        "distances": [[0.1]],
    }
    monkeypatch.setattr(searcher, "get_collection", lambda *_args, **_kw: collection)
    monkeypatch.setattr(searcher, "get_closets_collection", lambda *_args, **_kw: None)
    monkeypatch.setattr(searcher, "resolve_backend_name", lambda *_args: "chroma")
    seen = []

    def refuse(_palace_path, _collection_name, candidates):
        seen.extend(candidates)
        return set()

    monkeypatch.setattr(visibility, "visible_conversation_candidate_ids", refuse)
    result = searcher.search_memories("needle", "/unused", n_results=1)
    assert seen == [("same-id", meta, "unpublished B")]
    assert result["results"] == []
