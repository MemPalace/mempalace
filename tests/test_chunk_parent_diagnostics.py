"""Chunk-parent diagnostics for #2752 (Option C: diagnostics + docs only).

A chunked drawer is stored as physical ``<drawer_id>_chunk_NNNNNN`` rows
only — no writer ever upserts a row under the logical drawer id — so its
parent embedding row never exists. These tests prove, through the real
``tool_add_drawer`` chunking path (never fabricated sqlite rows), that the
pure-sqlite probe reports that intentional structure and that
``tool_status`` surfaces it.
"""

from _mcp_server_helpers import (
    _get_collection,
    _patch_mcp_server,
)

from mempalace.backends.chroma_sqlite import get_parentless_chunk_group_stats


def _add_drawers(monkeypatch, config, kg, contents):
    """File each content via the real write path; return the outcomes."""
    from mempalace.mcp_server import tool_add_drawer

    _patch_mcp_server(monkeypatch, config, kg)
    results = [tool_add_drawer(wing="w", room="r", content=content) for content in contents]
    assert all(result["success"] is True for result in results), results
    return results


class TestParentlessChunkGroupStats:
    def test_probe_empty_collection_returns_zeros(self, palace_path):
        _client, _col = _get_collection(palace_path, create=True)
        del _client

        stats = get_parentless_chunk_group_stats(palace_path, "mempalace_drawers")

        assert stats == {
            "drawers": 0,
            "chunks": 0,
            "rows": 0,
            "parentless_chunk_groups": 0,
        }

    def test_probe_unchunked_drawer_is_not_parentless(self, monkeypatch, config, palace_path, kg):
        _add_drawers(monkeypatch, config, kg, ["A short drawer."])

        stats = get_parentless_chunk_group_stats(palace_path, "mempalace_drawers")

        assert stats == {
            "drawers": 1,
            "chunks": 0,
            "rows": 1,
            "parentless_chunk_groups": 0,
        }

    def test_probe_counts_one_chunk_group_once(self, monkeypatch, config, palace_path, kg):
        """1 unchunked + 1 chunked drawer (3 chunks): logical 2, physical 4.

        The 3 chunks share one logical id, so they count as one parentless
        group, not three missing rows.
        """
        results = _add_drawers(monkeypatch, config, kg, ["A short drawer.", "Q" * 2000])
        assert results[1]["chunks"] == 3

        stats = get_parentless_chunk_group_stats(palace_path, "mempalace_drawers")

        assert stats == {
            "drawers": 2,
            "chunks": 3,
            "rows": 4,
            "parentless_chunk_groups": 1,
        }

    def test_probe_counts_multiple_parentless_groups(self, monkeypatch, config, palace_path, kg):
        """2 chunked drawers (3 + 2 chunks): logical 2, physical 5, parentless 2."""
        results = _add_drawers(monkeypatch, config, kg, ["Q" * 2000, "Z" * 1600])
        assert [result["chunks"] for result in results] == [3, 2]

        stats = get_parentless_chunk_group_stats(palace_path, "mempalace_drawers")

        assert stats == {
            "drawers": 2,
            "chunks": 5,
            "rows": 5,
            "parentless_chunk_groups": 2,
        }

    def test_probe_missing_collection_returns_none(self, monkeypatch, config, palace_path):
        """No database at all, and a database with no collection, both read as unknown."""
        import chromadb

        assert get_parentless_chunk_group_stats(palace_path, "mempalace_drawers") is None

        client = chromadb.PersistentClient(path=palace_path)
        del client
        assert get_parentless_chunk_group_stats(palace_path, "mempalace_drawers") is None


class TestStatusStorageStructure:
    def test_status_reports_storage_structure(self, monkeypatch, config, palace_path, kg):
        _add_drawers(monkeypatch, config, kg, ["A short drawer.", "Q" * 2000])
        from mempalace.mcp_server import tool_status

        result = tool_status()

        assert result["total_drawers"] == 4
        assert result["storage_structure"] == {
            "drawers": 2,
            "chunks": 3,
            "rows": 4,
            "parentless_chunk_groups": 1,
        }

    def test_status_empty_palace_reports_zero_structure(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace.mcp_server import tool_status

        result = tool_status()

        assert result["total_drawers"] == 0
        assert result["storage_structure"] == {
            "drawers": 0,
            "chunks": 0,
            "rows": 0,
            "parentless_chunk_groups": 0,
        }

    def test_status_cold_start_reports_zero_structure(self, monkeypatch, config, palace_path, kg):
        """No collection yet: the client path bootstraps the empty collection
        first, so the probe can report an empty palace instead of omitting."""
        import chromadb

        _patch_mcp_server(monkeypatch, config, kg)
        client = chromadb.PersistentClient(path=palace_path)
        del client
        from mempalace.mcp_server import tool_status

        result = tool_status()

        assert result["total_drawers"] == 0
        assert result["storage_structure"] == {
            "drawers": 0,
            "chunks": 0,
            "rows": 0,
            "parentless_chunk_groups": 0,
        }

    def test_storage_structure_omitted_without_collection(
        self, monkeypatch, config, palace_path, kg
    ):
        """The probe reads ``None`` for an unbootstrapped collection, so the
        helper omits the key instead of reporting a wrong one."""
        import chromadb

        from mempalace.mcp_server import _storage_structure

        _patch_mcp_server(monkeypatch, config, kg)
        client = chromadb.PersistentClient(path=palace_path)
        del client

        assert _storage_structure() is None
