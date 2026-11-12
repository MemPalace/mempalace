"""Exercise sidecar pagination against isolated real storage collections."""

import datetime as dt
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mempalace.backends.base import BaseCollection, GetResult, PalaceRef
from mempalace.backends.chroma import ChromaBackend
from mempalace.backends.sqlite_exact import SQLiteExactBackend


@pytest.fixture
def bridge():
    path = Path(__file__).resolve().parents[1] / "plugins/mempalace/sidecars/palace/bridge.py"
    spec = importlib.util.spec_from_file_location("sidecar_pagination_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["sqlite_exact", "chroma"])
def collection(request, tmp_path):
    backend = SQLiteExactBackend() if request.param == "sqlite_exact" else ChromaBackend()
    path = str(tmp_path / "palace")
    col = backend.get_collection(
        palace=PalaceRef(id=path, local_path=path),
        collection_name="pagination_drawers",
        create=True,
    )
    yield col
    backend.close()


def _populate(col, count, *, prefix="drawer", wing="work", room="notes"):
    ids = [f"{prefix}-{i:04d}" for i in range(count)]
    origin = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    documents = [f"Original memory {i}: café — exact text.\nSecond line." for i in range(count)]
    metadatas = [
        {
            "wing": wing,
            "room": room,
            "filed_at": (origin + dt.timedelta(seconds=i)).isoformat(),
        }
        for i in range(count)
    ]
    col.add(
        ids=ids,
        documents=documents,
        metadatas=metadatas,
        embeddings=[[1.0, 0.0, 0.0, 0.0]] * count,
    )
    return ids, documents, metadatas


def _wire(bridge, col, monkeypatch, count):
    monkeypatch.setattr(bridge, "_locus_total_from_cache", lambda *_: count)
    monkeypatch.setattr(bridge.mp, "_get_collection", lambda: col)
    monkeypatch.setattr(bridge.mp, "_palace_known_wings", lambda: frozenset({"work", "other"}))
    fallback = Mock(side_effect=AssertionError("unexpected slow-path fallback"))
    monkeypatch.setattr(bridge.mp, "tool_list_drawers", fallback)
    return fallback


def _pages(bridge, count, *, limit=30, sort="recent"):
    pages = [
        bridge._handle_drawers(
            {"wing": "work", "room": "notes", "sort": sort, "limit": limit, "offset": offset}
        )
        for offset in range(0, count, limit)
    ]
    assert all(page["fast_path"] for page in pages)
    for page in pages:
        assert page["total"] == count
        assert page["count"] == len(page["drawers"])
        assert page["has_more"] == (page["offset"] + limit < count)
    return [drawer for page in pages for drawer in page["drawers"]]


def test_large_recent_pages_return_every_drawer_once_in_order(bridge, collection, monkeypatch):
    """The actual 1,501-row boundary previously skipped and repeated 30 rows."""
    ids, documents, _ = _populate(collection, 1501)
    before = collection.get(ids=ids)
    _wire(bridge, collection, monkeypatch, len(ids))
    drawers = _pages(bridge, len(ids))
    assert [drawer["id"] for drawer in drawers] == ids[::-1]
    assert [drawer["content"] for drawer in drawers] == documents[::-1]
    stored = collection.get(ids=ids)
    assert dict(zip(stored.ids, stored.documents)) == dict(zip(ids, documents))
    assert dict(zip(stored.ids, stored.metadatas)) == dict(zip(before.ids, before.metadatas))


def test_storage_pages_keep_storage_order(bridge, collection, monkeypatch):
    ids, _, _ = _populate(collection, 73)
    monkeypatch.setattr(bridge, "_LARGE_LOCUS_THRESHOLD", 0)
    _wire(bridge, collection, monkeypatch, len(ids))
    monkeypatch.setattr(collection, "get_recent", Mock(side_effect=AssertionError("recent read")))
    expected = collection.get(include=[]).ids
    assert [d["id"] for d in _pages(bridge, len(ids), sort="storage")] == expected


def test_recent_pages_stay_inside_selected_room(bridge, collection, monkeypatch):
    ids, _, _ = _populate(collection, 43)
    _populate(collection, 51, prefix="other-room", room="different")
    _populate(collection, 51, prefix="other-wing", wing="other")
    monkeypatch.setattr(bridge, "_LARGE_LOCUS_THRESHOLD", 0)
    _wire(bridge, collection, monkeypatch, len(ids))
    assert [d["id"] for d in _pages(bridge, len(ids), limit=7)] == ids[::-1]


def test_ties_and_undated_drawers_keep_the_same_order_on_every_page(
    bridge, collection, monkeypatch
):
    ids, _, metadatas = _populate(collection, 42)
    for i, meta in enumerate(metadatas):
        meta["filed_at"] = "2026-01-01T00:00:00Z" if i < 35 else ""
    collection.update(ids=ids, metadatas=metadatas)
    monkeypatch.setattr(bridge, "_LARGE_LOCUS_THRESHOLD", 0)
    _wire(bridge, collection, monkeypatch, len(ids))
    # Each backend chooses its tie breaker; splitting pages must preserve it.
    expected = collection.get_recent(limit=len(ids), include=[]).ids
    assert [d["id"] for d in _pages(bridge, len(ids), limit=6)] == expected


def test_recent_page_requests_only_its_text_and_preserves_selected_order(
    bridge, collection, monkeypatch
):
    ids, documents, _ = _populate(collection, 23)
    monkeypatch.setattr(bridge, "_LARGE_LOCUS_THRESHOLD", 0)
    _wire(bridge, collection, monkeypatch, len(ids))
    original_recent = collection.get_recent
    original_get = collection.get
    recent_calls = []
    text_reads = []

    def recent(**kwargs):
        recent_calls.append(kwargs)
        return original_recent(**kwargs)

    def get(**kwargs):
        if "documents" in kwargs.get("include", []):
            text_reads.append(kwargs)
        result = original_get(**kwargs)
        # Backend get(ids=...) is not required to preserve requested ID order.
        return GetResult(
            ids=result.ids[::-1], documents=result.documents[::-1], metadatas=result.metadatas[::-1]
        )

    monkeypatch.setattr(collection, "get_recent", recent)
    monkeypatch.setattr(collection, "get", get)
    result = bridge._handle_drawers({"sort": "recent", "offset": 11, "limit": 5})
    assert [d["id"] for d in result["drawers"]] == ids[::-1][11:16]
    assert [d["content"] for d in result["drawers"]] == documents[::-1][11:16]
    assert all(call.get("include") == [] for call in recent_calls)
    assert len(text_reads) == 1
    assert text_reads[0]["ids"] == ids[::-1][11:16]


def test_recent_page_past_end_does_not_fetch_all_documents(bridge, collection, monkeypatch):
    ids, _, _ = _populate(collection, 9)
    monkeypatch.setattr(bridge, "_LARGE_LOCUS_THRESHOLD", 0)
    _wire(bridge, collection, monkeypatch, len(ids))
    get = Mock(side_effect=AssertionError("empty page must not call get(ids=[])"))
    monkeypatch.setattr(collection, "get", get)
    page = bridge._handle_drawers({"sort": "recent", "offset": 9, "limit": 5})
    assert page["drawers"] == []
    assert page["count"] == 0
    assert not page["has_more"]
    get.assert_not_called()


def test_chroma_recent_ids_do_not_decode_prefix_text(tmp_path, monkeypatch):
    """An ID-only ordering pass must not hydrate earlier pages internally."""
    from mempalace.backends import chroma_sqlite

    backend = ChromaBackend()
    col = backend.get_collection(str(tmp_path / "palace"), "pagination_drawers", create=True)
    try:
        ids, _, _ = _populate(col, 23)
        monkeypatch.setattr(
            chroma_sqlite, "_decoded_rows", Mock(side_effect=AssertionError("decoded prefix text"))
        )
        result = col.get_recent(limit=15, include=[])
        assert result.ids == ids[::-1][:15]
        assert result.documents == []
        assert result.metadatas == []
    finally:
        backend.close()


def test_backend_without_exact_recency_scans_the_complete_ordering(bridge, collection, monkeypatch):
    ids, _, _ = _populate(collection, 41)
    monkeypatch.setattr(bridge, "_LARGE_LOCUS_THRESHOLD", 0)
    _wire(bridge, collection, monkeypatch, len(ids))
    monkeypatch.setattr(collection, "_backend", SimpleNamespace(capabilities=frozenset()))
    monkeypatch.setattr(
        collection, "get_recent", lambda **kwargs: BaseCollection.get_recent(collection, **kwargs)
    )
    assert [d["id"] for d in _pages(bridge, len(ids), limit=8)] == ids[::-1]


def test_embedding_wrapper_preserves_recency_pagination(bridge, collection, monkeypatch):
    from mempalace.backends.embedding_wrapper import EmbeddingCollection

    ids, _, _ = _populate(collection, 43)
    monkeypatch.setattr(bridge, "_LARGE_LOCUS_THRESHOLD", 0)
    _wire(bridge, EmbeddingCollection(collection), monkeypatch, len(ids))
    assert [d["id"] for d in _pages(bridge, len(ids), limit=8)] == ids[::-1]


def test_selected_record_missing_at_hydration_keeps_remaining_order(
    bridge, collection, monkeypatch
):
    ids, _, _ = _populate(collection, 23)
    monkeypatch.setattr(bridge, "_LARGE_LOCUS_THRESHOLD", 0)
    _wire(bridge, collection, monkeypatch, len(ids))
    original_get = collection.get

    def get(**kwargs):
        result = original_get(**kwargs)
        # Simulate one selected record no longer being available at readback.
        retained = [i for i, doc_id in enumerate(result.ids) if doc_id != ids[10]]
        return GetResult(
            ids=[result.ids[i] for i in retained],
            documents=[result.documents[i] for i in retained],
            metadatas=[result.metadatas[i] for i in retained],
        )

    monkeypatch.setattr(collection, "get", get)
    page = bridge._handle_drawers({"sort": "recent", "offset": 11, "limit": 5})
    assert [d["id"] for d in page["drawers"]] == [ids[11], ids[9], ids[8], ids[7]]
    assert page["count"] == 4


@pytest.mark.parametrize("reason", ["small", "since", "before", "storage_error"])
def test_existing_slow_path_is_preserved(bridge, collection, monkeypatch, reason):
    ids, _, _ = _populate(collection, 9)
    _wire(bridge, collection, monkeypatch, len(ids))
    expected = {"drawers": [], "total": 0, "count": 0}
    fallback = Mock(return_value=expected)
    monkeypatch.setattr(bridge.mp, "tool_list_drawers", fallback)
    params = {"wing": "work", "room": "notes", "sort": "recent", "offset": 3, "limit": 2}
    if reason != "small":
        monkeypatch.setattr(bridge, "_LARGE_LOCUS_THRESHOLD", 0)
    if reason in ("since", "before"):
        params[reason] = "2026-01-01"
    if reason == "storage_error":
        monkeypatch.setattr(collection, "get_recent", Mock(side_effect=OSError("read failed")))
    assert bridge._handle_drawers(params) == expected
    fallback.assert_called_once_with(
        wing="work",
        room="notes",
        since=params.get("since"),
        before=params.get("before"),
        limit=2,
        offset=3,
    )
