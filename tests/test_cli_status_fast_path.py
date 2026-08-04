"""Tests for miner.status() get_all_metadata() fast path (#2153)."""

from unittest import mock

import pytest


def _make_collection_with_get_all(metadata_list):
    """Mock collection exposing get_all_metadata() and tracking get() calls."""

    class _Collection:
        def __init__(self):
            self._meta = metadata_list
            self.get_calls = []

        def get_all_metadata(self, where=None):
            return list(self._meta)

        def get(self, *, ids=None, where=None, limit=None, offset=None, include=None):
            self.get_calls.append({"limit": limit, "offset": offset})
            offset = offset or 0
            limit = limit if limit is not None else len(self._meta)
            return {"ids": [], "documents": [], "metadatas": self._meta[offset : offset + limit]}

        def count(self):
            return len(self._meta)

    return _Collection()


class _LegacyCollection:
    """Collection without get_all_metadata() — triggers fallback loop."""

    def __init__(self, metadata_list):
        self._meta = metadata_list

    def get(self, *, ids=None, where=None, limit=None, offset=None, include=None):
        offset = offset or 0
        limit = limit if limit is not None else len(self._meta)
        return {"ids": [], "documents": [], "metadatas": self._meta[offset : offset + limit]}

    def count(self):
        return len(self._meta)


@pytest.fixture
def _stub_deps(monkeypatch):
    monkeypatch.setattr(
        "mempalace.backends.chroma._sqlite_wing_room_counts",
        lambda palace_path, collection_name: None,
    )
    monkeypatch.setattr(
        "mempalace.backends.chroma.hnsw_capacity_status",
        lambda palace_path, collection_name: {"diverged": False, "status": "unknown"},
    )
    yield


class TestFastPath:
    def test_uses_get_all_metadata(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _make_collection_with_get_all([
            {"wing": "sessions", "room": "technical"},
            {"wing": "sessions", "room": "planning"},
            {"wing": "knowledge", "room": "decisions"},
        ])
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert "3 drawers" in out
        assert col.get_calls == []

    def test_does_not_call_count(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _make_collection_with_get_all([{"wing": "a", "room": "b"}])
        col.count = mock.MagicMock(side_effect=AssertionError("count() should not be called"))
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        col.count.assert_not_called()

    def test_handles_none_metadata(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _make_collection_with_get_all([
            {"wing": "sessions", "room": "technical"},
            None,
        ])
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert "2 drawers" in out
        assert "?" in out

    def test_large_collection_single_pass(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _make_collection_with_get_all([{"wing": "a", "room": "b"}] * 10000)
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        assert col.get_calls == []


class TestFallback:
    def test_offset_loop_when_no_get_all(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _LegacyCollection([{"wing": "a", "room": "x"}, {"wing": "b", "room": "y"}])
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert "2 drawers" in out

    def test_empty_collection(self, _stub_deps, monkeypatch, capsys):
        from mempalace import miner

        col = _LegacyCollection([])
        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)

        miner.status("/fake/palace")
        out = capsys.readouterr().out

        assert "0 drawers" in out
