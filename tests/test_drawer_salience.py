"""Drawer salience tests for issue #1921.

These tests pin the additive drawer-facing behavior before implementation:
lazy read exposure must not mutate stored metadata, while opt-in search
potentiation must update stored drawer salience safely.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mempalace.dynamics import (
    DEFAULT_STABILITY,
    DEFAULT_STRENGTH,
    POTENTIATION_INCREMENT,
    STABILITY_INCREMENT,
)


T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _patch_mcp_server(monkeypatch, config, kg):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda *a, **kw: kg)


def _stored_meta(collection, drawer_id: str) -> dict:
    result = collection.get(ids=[drawer_id], include=["metadatas"])
    ids = result["ids"] if isinstance(result, dict) else result.ids
    assert ids == [drawer_id]
    metas = result["metadatas"] if isinstance(result, dict) else result.metadatas
    return dict(metas[0])


def _add_drawer(collection, drawer_id: str, text: str, **meta):
    base_meta = {
        "wing": "salience",
        "room": "lab",
        "source_file": f"{drawer_id}.md",
        "chunk_index": 0,
        "added_by": "test",
        "filed_at": T0.isoformat(),
    }
    base_meta.update(meta)
    collection.add(ids=[drawer_id], documents=[text], metadatas=[base_meta])


def _add_chunked_drawer(collection, parent_id: str):
    filed_at = T0.isoformat()
    collection.add(
        ids=[f"{parent_id}_chunk_000000", f"{parent_id}_chunk_000001"],
        documents=[
            "chunked logical drawer first half with nebula keyword",
            "chunked logical drawer second half with nebula keyword",
        ],
        metadatas=[
            {
                "wing": "salience",
                "room": "chunks",
                "source_file": "chunked.md",
                "added_by": "test",
                "filed_at": filed_at,
                "parent_drawer_id": parent_id,
                "chunk_index": 0,
            },
            {
                "wing": "salience",
                "room": "chunks",
                "source_file": "chunked.md",
                "added_by": "test",
                "filed_at": filed_at,
                "parent_drawer_id": parent_id,
                "chunk_index": 1,
            },
        ],
    )


class TestDrawerDynamicsAdapter:
    def test_initializes_last_activated_from_filed_at(self):
        from mempalace.dynamics import initialize_drawer_dynamics_fields

        meta = {"filed_at": T0.isoformat()}
        initialize_drawer_dynamics_fields(meta, now=T0 + timedelta(days=3))

        assert meta["strength"] == DEFAULT_STRENGTH
        assert meta["stability"] == DEFAULT_STABILITY
        assert meta["last_activated"] == T0.isoformat()
        assert meta["access_count"] == 0

    def test_missing_and_unparseable_filed_at_are_default_safe(self):
        from mempalace.dynamics import drawer_salience

        missing = drawer_salience({}, now=T0)
        invalid = drawer_salience({"filed_at": "not a timestamp"}, now=T0)

        assert missing == {
            "strength": DEFAULT_STRENGTH,
            "stability": DEFAULT_STABILITY,
            "last_activated": T0.isoformat(),
            "access_count": 0,
        }
        assert invalid["strength"] == DEFAULT_STRENGTH
        assert invalid["stability"] == DEFAULT_STABILITY
        assert invalid["last_activated"] == "not a timestamp"
        assert invalid["access_count"] == 0


class TestDrawerSalienceReadExposure:
    def test_search_includes_lazy_salience_without_mutating_store(
        self, monkeypatch, config, collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _add_drawer(collection, "drawer_salience_search", "rare heliotrope search target")
        before = _stored_meta(collection, "drawer_salience_search")

        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_now", lambda: T0 + timedelta(days=1))
        result = mcp_server.tool_search(query="heliotrope", limit=1, max_distance=0)

        assert result["results"]
        salience = result["results"][0]["salience"]
        assert set(salience) == {"strength", "stability", "last_activated", "access_count"}
        assert salience["strength"] < DEFAULT_STRENGTH
        assert salience["last_activated"] == T0.isoformat()
        assert _stored_meta(collection, "drawer_salience_search") == before

    def test_get_drawer_includes_lazy_salience_without_mutating_store(
        self, monkeypatch, config, collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _add_drawer(collection, "drawer_salience_get", "drawer body")
        before = _stored_meta(collection, "drawer_salience_get")

        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_now", lambda: T0 + timedelta(days=1))
        result = mcp_server.tool_get_drawer("drawer_salience_get")

        assert result["salience"]["strength"] < DEFAULT_STRENGTH
        assert result["salience"]["last_activated"] == T0.isoformat()
        assert _stored_meta(collection, "drawer_salience_get") == before


class TestDrawerSalienceTool:
    def test_orders_by_strength_access_count_and_last_activated(
        self, monkeypatch, config, collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _add_drawer(
            collection,
            "drawer_strength_hot",
            "hot drawer",
            strength=2.0,
            access_count=1,
            last_activated=(T0 + timedelta(hours=1)).isoformat(),
        )
        _add_drawer(
            collection,
            "drawer_count_hot",
            "count drawer",
            strength=1.5,
            access_count=9,
            last_activated=(T0 + timedelta(hours=2)).isoformat(),
        )
        _add_drawer(
            collection,
            "drawer_recent_hot",
            "recent drawer",
            strength=1.0,
            access_count=3,
            last_activated=(T0 + timedelta(hours=3)).isoformat(),
        )

        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_now", lambda: T0 + timedelta(hours=3))

        by_strength = mcp_server.tool_drawer_salience(order_by="strength", limit=3)["drawers"]
        by_count = mcp_server.tool_drawer_salience(order_by="access_count", limit=3)["drawers"]
        by_recent = mcp_server.tool_drawer_salience(order_by="last_activated", limit=3)["drawers"]

        assert by_strength[0]["id"] == "drawer_strength_hot"
        assert by_count[0]["id"] == "drawer_count_hot"
        assert by_recent[0]["id"] == "drawer_recent_hot"

    def test_dedupes_chunked_drawers_by_parent(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _add_chunked_drawer(collection, "drawer_parent_salience")

        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_now", lambda: T0)
        result = mcp_server.tool_drawer_salience(wing="salience", room="chunks")

        assert result["drawers"] == [
            {
                "id": "drawer_parent_salience",
                "wing": "salience",
                "room": "chunks",
                "strength": DEFAULT_STRENGTH,
                "stability": DEFAULT_STABILITY,
                "last_activated": T0.isoformat(),
                "access_count": 0,
            }
        ]


class TestPotentiateOnSearch:
    def test_flag_off_search_writes_nothing(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _add_drawer(collection, "drawer_no_potentiate", "flag off aquamarine target")

        from mempalace import mcp_server

        monkeypatch.delenv("MEMPALACE_SALIENCE_POTENTIATE", raising=False)
        result = mcp_server.tool_search(query="aquamarine", limit=1, max_distance=0)

        assert result["results"]
        stored = _stored_meta(collection, "drawer_no_potentiate")
        assert stored.get("access_count") is None

    def test_flag_on_potentiates_store_once(self, monkeypatch, config, collection, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _add_drawer(collection, "drawer_potentiate", "flag on vermillion target")

        from mempalace import mcp_server

        now = T0 + timedelta(hours=2)
        monkeypatch.setenv("MEMPALACE_SALIENCE_POTENTIATE", "true")
        monkeypatch.setattr(mcp_server, "_READ_ONLY", False)
        monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", object())
        monkeypatch.setattr(mcp_server, "_now", lambda: now)

        result = mcp_server.tool_search(query="vermillion", limit=1, max_distance=0)

        assert result["results"]
        stored = _stored_meta(collection, "drawer_potentiate")
        assert stored["access_count"] == 1
        assert stored["last_activated"] == now.isoformat()
        assert stored["strength"] > DEFAULT_STRENGTH * 0.9
        assert stored["strength"] < DEFAULT_STRENGTH + POTENTIATION_INCREMENT
        assert stored["stability"] == pytest.approx(DEFAULT_STABILITY + STABILITY_INCREMENT)

    def test_chunked_drawer_potentiates_parent_once_and_updates_all_chunks(
        self, monkeypatch, config, collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _add_chunked_drawer(collection, "drawer_parent_once")

        from mempalace import mcp_server

        now = T0 + timedelta(hours=2)
        monkeypatch.setenv("MEMPALACE_SALIENCE_POTENTIATE", "true")
        monkeypatch.setattr(mcp_server, "_READ_ONLY", False)
        monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", object())
        monkeypatch.setattr(mcp_server, "_now", lambda: now)

        result = mcp_server.tool_search(query="nebula", limit=2, max_distance=0)

        assert result["results"]
        rows = collection.get(
            ids=["drawer_parent_once_chunk_000000", "drawer_parent_once_chunk_000001"],
            include=["metadatas"],
        )
        for meta in rows["metadatas"]:
            assert meta["access_count"] == 1
            assert meta["last_activated"] == now.isoformat()

    def test_read_only_mode_with_flag_on_still_writes_nothing(
        self, monkeypatch, config, collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _add_drawer(collection, "drawer_read_only", "read only chartreuse target")
        before = _stored_meta(collection, "drawer_read_only")

        from mempalace import mcp_server

        monkeypatch.setenv("MEMPALACE_SALIENCE_POTENTIATE", "true")
        monkeypatch.setattr(mcp_server, "_READ_ONLY", True)
        monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", object())
        result = mcp_server.tool_search(query="chartreuse", limit=1, max_distance=0)

        assert result["results"]
        assert _stored_meta(collection, "drawer_read_only") == before
