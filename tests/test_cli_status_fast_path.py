"""Tests for miner.status() get_all_metadata() fast path (#2153)."""

from types import SimpleNamespace
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
        "mempalace.backends.chroma_sqlite._sqlite_wing_room_counts",
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

        col = _make_collection_with_get_all(
            [
                {"wing": "sessions", "room": "technical"},
                {"wing": "sessions", "room": "planning"},
                {"wing": "knowledge", "room": "decisions"},
            ]
        )
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

        col = _make_collection_with_get_all(
            [
                {"wing": "sessions", "room": "technical"},
                None,
            ]
        )
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


class TestLogicalCounts:
    """Every fallback renders what the sqlite path renders: logical drawers."""

    ROWS = [
        ("root", {"wing": "w", "room": "r"}),
        ("root_c0", {"wing": "w", "room": "r", "parent_drawer_id": "root"}),
        ("root_c1", {"wing": "w", "room": "r", "parent_drawer_id": "root"}),
        ("gone_c0", {"wing": "w", "room": "r", "parent_drawer_id": "gone"}),
        ("gone_c1", {"wing": "w", "room": "r", "parent_entry_id": "gone"}),
        ("both_c0", {"wing": "w", "room": "r", "parent_drawer_id": "both", "parent_entry_id": "x"}),
        ("single", {"wing": "w", "room": "r"}),
        ("blank", {"wing": "w", "room": "r", "parent_drawer_id": ""}),
        ("reg", {"wing": "w", "room": "_registry", "ingest_mode": "registry"}),
        ("sentinel", {"wing": "w", "room": "r", "is_sentinel": True}),
    ]

    def _status(self, col, monkeypatch, capsys):
        from mempalace import miner

        monkeypatch.setattr(miner, "_open_collection_or_explain", lambda p: col)
        miner.status("/fake/palace")
        return capsys.readouterr().out

    def test_get_all_rows_collapses_a_root_and_its_chunks(self, _stub_deps, monkeypatch, capsys):
        ids = [row[0] for row in self.ROWS]
        metas = [row[1] for row in self.ROWS]

        class _Collection:
            def get_all_rows(self, where=None, include=None):
                assert include == ["metadatas"]
                return SimpleNamespace(ids=ids, metadatas=metas)

        out = self._status(_Collection(), monkeypatch, capsys)
        assert "5 drawers" in out
        assert "_registry" not in out

    def test_offset_loop_collapses_a_root_and_its_chunks(self, _stub_deps, monkeypatch, capsys):
        ids = [row[0] for row in self.ROWS]
        metas = [row[1] for row in self.ROWS]

        class _Collection:
            def count(self):
                return len(ids)

            def get(self, *, limit, offset, include):
                return {
                    "ids": ids[offset : offset + limit],
                    "metadatas": metas[offset : offset + limit],
                }

        assert "5 drawers" in self._status(_Collection(), monkeypatch, capsys)

    def test_metadata_only_counts_a_shared_parent_once(self, _stub_deps, monkeypatch, capsys):
        # No ids: the legacy root reads as its own row, so root + 2 chunks is 2.
        col = _make_collection_with_get_all([row[1] for row in self.ROWS])
        assert "6 drawers" in self._status(col, monkeypatch, capsys)
