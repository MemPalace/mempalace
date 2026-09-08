"""Status must count each drawer once without retaining document payloads."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mempalace import miner
from mempalace.backends import qdrant
from mempalace.backends.base import (
    BackendClosedError,
    BaseCollection,
    CollectionNotInitializedError,
    GetResult,
    PalaceRef,
)
from mempalace.backends.embedding_wrapper import EmbeddingCollection


def _collection(monkeypatch, metadata, page_size=2):
    """Exercise real REST request construction; replace only the HTTP transport."""
    config = qdrant._QdrantConfig(url="http://localhost:6333")
    client = qdrant._QdrantRESTClient(config)
    requests = []

    def request(method, path, *, body=None, **kwargs):
        assert method == "POST"
        assert path.endswith("/points/scroll")
        # Reject full payloads before manufacturing a response: this fake cannot
        # conceal accidental transfer of documents or vectors.
        assert body["with_payload"] == ["metadata"]
        assert body["with_vector"] is False
        assert body["limit"] == page_size
        requests.append(dict(body))
        cursor = body.get("offset")
        start = 0 if cursor is None else int(cursor.removeprefix("cursor-"))
        batch = metadata[start : start + page_size]
        end = start + len(batch)
        return {
            "result": {
                "points": [
                    {"id": str(start + i), "payload": {"metadata": value}}
                    for i, value in enumerate(batch)
                ],
                "next_page_offset": f"cursor-{end}" if end < len(metadata) else None,
            }
        }

    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(client, "collection_exists", lambda _: True)
    monkeypatch.setattr(client, "count_points", lambda _: len(metadata))
    monkeypatch.setattr(qdrant, "_SCROLL_PAGE_SIZE", page_size)
    backend = SimpleNamespace(_closed=False, _marker_exists=lambda _: True)
    col = qdrant.QdrantCollection(
        backend=backend,
        client=client,
        config=config,
        palace=PalaceRef(id="test", local_path="/unused"),
        collection_name="mempalace_drawers",
        remote_collection="test_drawers",
    )
    return col, requests


def test_qdrant_metadata_cursor_is_lazy_projected_and_visits_each_page_once(monkeypatch):
    metadata = [{"wing": str(i)} for i in range(5)]
    col, requests = _collection(monkeypatch, metadata)
    iterator = col.iter_metadata()
    assert requests == []
    assert next(iterator) == metadata[0]
    assert len(requests) == 1
    assert next(iterator) == metadata[1]
    assert len(requests) == 1
    assert list(iterator) == metadata[2:]
    assert [body.get("offset") for body in requests] == [None, "cursor-2", "cursor-4"]
    assert len(requests) == 3  # ceil(5 / 2); never restart from the first page


@pytest.mark.parametrize("metadata", [[], [None, {}, {"wing": "w"}, {"room": "r"}]])
def test_qdrant_metadata_empty_and_partial_rows(monkeypatch, metadata):
    col, requests = _collection(monkeypatch, metadata)
    assert list(col.iter_metadata()) == [value or {} for value in metadata]
    assert len(requests) == max(1, (len(metadata) + 1) // 2)


def test_qdrant_metadata_failure_is_not_a_partial_success(monkeypatch):
    col, requests = _collection(monkeypatch, [{"wing": "a"}] * 3)
    iterator = col.iter_metadata()
    assert next(iterator) == {"wing": "a"}
    assert next(iterator) == {"wing": "a"}
    monkeypatch.setattr(col._client, "request", Mock(side_effect=RuntimeError("connection lost")))
    with pytest.raises(RuntimeError, match="connection lost"):
        next(iterator)
    assert len(requests) == 1


def test_qdrant_metadata_preserves_collection_lifecycle(monkeypatch):
    col, requests = _collection(monkeypatch, [])
    monkeypatch.setattr(col._client, "collection_exists", lambda _: False)
    with pytest.raises(CollectionNotInitializedError):
        list(col.iter_metadata())
    col._backend._marker_exists = lambda _: False
    assert list(col.iter_metadata()) == []
    col._closed = True
    with pytest.raises(BackendClosedError):
        list(col.iter_metadata())
    assert requests == []


def test_base_metadata_iterator_pages_without_prefetching():
    metadata = [{"wing": str(i)} for i in range(2001)]
    calls = []

    def get(*, limit, offset, include):
        assert include == ["metadatas"]
        assert limit == 1000
        calls.append(offset)
        return GetResult(ids=[], documents=[], metadatas=metadata[offset : offset + limit])

    iterator = BaseCollection.iter_metadata(SimpleNamespace(get=get))
    assert calls == []
    assert next(iterator) == metadata[0]
    assert calls == [0]
    assert list(iterator) == metadata[1:]
    assert calls == [0, 1000, 2000]


def test_embedding_wrapper_forwards_native_iterator():
    iterator = iter([{"wing": "a"}])
    inner = SimpleNamespace(iter_metadata=Mock(return_value=iterator))
    assert EmbeddingCollection(inner).iter_metadata() is iterator
    inner.iter_metadata.assert_called_once_with()


def test_actual_status_streams_exact_histogram_without_facet_cardinality_limit(monkeypatch):
    metadata = [None, {}, {"wing": "a"}, {"room": "r"}]
    metadata += [{"wing": "a", "room": f"r{i}"} for i in range(1001)]
    metadata += [{"wing": "a", "room": "r0"}]
    col, requests = _collection(monkeypatch, metadata, page_size=128)
    monkeypatch.setattr("mempalace.backends.chroma._sqlite_wing_room_counts", lambda *args: None)
    monkeypatch.setattr("mempalace.backends.chroma.hnsw_capacity_status", lambda *args: {})
    monkeypatch.setattr(miner, "_open_collection_or_explain", lambda _: EmbeddingCollection(col))
    rendered = Mock()
    monkeypatch.setattr(miner, "_print_status", rendered)

    miner.status("/unused")

    rendered.assert_called_once()
    total, histogram = rendered.call_args.args
    expected_rooms = {f"r{i}": 1 for i in range(1001)}
    expected_rooms.update({"?": 1, "r0": 2})
    assert total == 1006
    assert dict(histogram) == {"?": {"?": 2, "r": 1}, "a": expected_rooms}
    assert sum(sum(rooms.values()) for rooms in histogram.values()) == total
    assert len(requests) == 8  # ceil(1006 / 128)


def test_status_does_not_print_partial_histogram_after_read_error(monkeypatch):
    def metadata():
        yield {"wing": "a"}
        raise RuntimeError("page failed")

    col = SimpleNamespace(count=lambda: 2, iter_metadata=metadata)
    monkeypatch.setattr("mempalace.backends.chroma._sqlite_wing_room_counts", lambda *args: None)
    monkeypatch.setattr("mempalace.backends.chroma.hnsw_capacity_status", lambda *args: {})
    monkeypatch.setattr(miner, "_open_collection_or_explain", lambda _: col)
    rendered = Mock()
    monkeypatch.setattr(miner, "_print_status", rendered)
    with pytest.raises(RuntimeError, match="page failed"):
        miner.status("/unused")
    rendered.assert_not_called()


def _mcp_status(monkeypatch, col, facets):
    from mempalace import mcp_server as mcp

    monkeypatch.setattr(mcp, "_ensure_sqlite_integrity_status", lambda: None)
    monkeypatch.setattr(mcp, "_sqlite_integrity_errors", [])
    monkeypatch.setattr(mcp, "_backend_db_exists", lambda: True)
    monkeypatch.setattr(mcp, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp, "_vector_disabled", False)
    monkeypatch.setattr(mcp, "_sqlite_taxonomy", lambda: None)
    monkeypatch.setattr(mcp, "_get_collection", lambda create=False: col)
    monkeypatch.setattr(mcp, "_selected_backend_name", lambda: "qdrant")
    monkeypatch.setattr(mcp, "_supports_metadata_facets", lambda _: facets)
    monkeypatch.setattr(
        mcp, "_get_cached_metadata", Mock(side_effect=AssertionError("unbounded cache used"))
    )
    return mcp


@pytest.mark.parametrize("facets", [False, True])
def test_mcp_status_fallback_streams_metadata_without_cache(monkeypatch, facets):
    metadata = [None, {}, {"wing": "a"}, {"room": "r"}, {"wing": "a", "room": "r"}]
    col, requests = _collection(monkeypatch, metadata)
    # A successful first facet followed by a failed second must not leave
    # stale counts mixed into the cursor result.
    monkeypatch.setattr(
        col, "facet_counts", Mock(side_effect=[{"stale": 5}, RuntimeError("no index")])
    )
    mcp = _mcp_status(monkeypatch, EmbeddingCollection(col), facets)

    result = mcp.tool_status()

    assert result["total_drawers"] == 5
    assert result["wings"] == {"unknown": 3, "a": 2}
    assert result["rooms"] == {"unknown": 3, "r": 2}
    assert "partial" not in result
    assert "error" not in result
    assert len(requests) == 3


def test_mcp_status_stream_error_remains_explicitly_partial(monkeypatch):
    def metadata():
        yield {"wing": "a", "room": "r"}
        raise RuntimeError("page failed")

    col = SimpleNamespace(count=lambda: 2, iter_metadata=metadata)
    mcp = _mcp_status(monkeypatch, col, False)
    result = mcp.tool_status()
    assert result["partial"] is True
    assert result["error"] == "page failed"
    assert result["total_drawers"] == 2
