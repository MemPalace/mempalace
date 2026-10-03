"""Drawer salience tests for issue #1921.

These tests pin the additive drawer-facing behavior before implementation:
lazy read exposure must not mutate stored metadata, while opt-in search
potentiation must update stored drawer salience safely.
"""

from __future__ import annotations

import logging
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
        assert invalid["last_activated"] == T0.isoformat()
        assert invalid["access_count"] == 0

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("strength", "unknown", 1.0),
            ("strength", None, 1.0),
            ("strength", float("nan"), 1.0),
            ("strength", float("inf"), 1.0),
            ("strength", float("-inf"), 1.0),
            ("strength", -1, 1.0),
            ("strength", True, 1.0),
            ("stability", "unknown", 1.0),
            ("stability", None, 1.0),
            ("stability", float("nan"), 1.0),
            ("stability", float("inf"), 1.0),
            ("stability", float("-inf"), 1.0),
            ("stability", 0, 1.0),
            ("stability", -1, 1.0),
            ("stability", False, 1.0),
            ("access_count", "unknown", 0),
            ("access_count", None, 0),
            ("access_count", float("nan"), 0),
            ("access_count", float("inf"), 0),
            ("access_count", float("-inf"), 0),
            ("access_count", -1, 0),
            ("access_count", 2.5, 0),
            ("access_count", True, 0),
            ("last_activated", "unknown", T0.isoformat()),
            ("last_activated", None, T0.isoformat()),
            ("last_activated", float("nan"), T0.isoformat()),
            ("last_activated", float("inf"), T0.isoformat()),
            ("last_activated", 37, T0.isoformat()),
        ],
    )
    def test_invalid_optional_fields_default_without_mutating_metadata(
        self, field, value, expected
    ):
        from mempalace.dynamics import drawer_salience

        meta = {"filed_at": T0.isoformat(), field: value, "custom": "keep verbatim"}
        before = dict(meta)

        salience = drawer_salience(meta, now=T0)

        assert salience[field] == expected
        assert meta == before

    def test_normalization_warns_without_disclosing_metadata_values(self, caplog):
        from mempalace.dynamics import drawer_salience

        private_value = "private drawer metadata must not be logged"
        with caplog.at_level(logging.WARNING):
            salience = drawer_salience({"strength": private_value}, now=T0)

        assert salience["strength"] == 1.0
        assert any("strength" in record.message for record in caplog.records)
        assert private_value not in caplog.text

    def test_valid_numeric_strings_remain_usable(self):
        from mempalace.dynamics import drawer_salience

        salience = drawer_salience(
            {
                "strength": "2.5",
                "stability": "1.25",
                "access_count": "7",
                "last_activated": T0.isoformat(),
            },
            now=T0,
        )

        assert salience == {
            "strength": 2.5,
            "stability": 1.25,
            "last_activated": T0.isoformat(),
            "access_count": 7,
        }

    def test_lazy_decay_is_precise_and_does_not_compound_on_reads(self):
        from mempalace.dynamics import drawer_salience

        meta = {"filed_at": T0.isoformat(), "strength": 1.0, "stability": 1.0}
        before = dict(meta)

        first = drawer_salience(meta, now=T0 + timedelta(days=1))
        second = drawer_salience(meta, now=T0 + timedelta(days=2))
        repeated = drawer_salience(meta, now=T0 + timedelta(days=2))

        assert first["strength"] == pytest.approx(0.36787944117144233, rel=1e-12)
        assert second["strength"] == pytest.approx(0.1353352832366127, rel=1e-12)
        assert repeated == second
        assert meta == before

    @pytest.mark.parametrize(
        ("milliseconds", "expected"),
        [(40, 0.9999995370371442), (50, 0.9999994212964637)],
    )
    def test_subsecond_decay_retains_precision(self, milliseconds, expected):
        from mempalace.dynamics import drawer_salience

        salience = drawer_salience(
            {"filed_at": T0.isoformat()},
            now=T0 + timedelta(milliseconds=milliseconds),
        )

        assert salience["strength"] == pytest.approx(expected, rel=0, abs=1e-15)

    def test_valid_stability_is_preserved_exactly(self):
        from mempalace.dynamics import drawer_salience

        meta = {"filed_at": T0.isoformat(), "stability": 1.123456789012345}

        salience = drawer_salience(meta, now=T0 + timedelta(days=1))

        assert salience["stability"] == 1.123456789012345
        assert meta == {"filed_at": T0.isoformat(), "stability": 1.123456789012345}


class TestDrawerSalienceReadExposure:
    @pytest.mark.parametrize("field", ["strength", "stability", "access_count"])
    def test_getters_preserve_verbatim_drawers_with_arbitrary_metadata(
        self, monkeypatch, config, collection, kg, field
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        content = "Verbatim body\n  preserved spacing — and punctuation!"
        _add_drawer(collection, "drawer_arbitrary", content, **{field: "unknown"})
        before = _stored_meta(collection, "drawer_arbitrary")

        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_now", lambda: T0)
        single = mcp_server.tool_get_drawer("drawer_arbitrary")
        bulk = mcp_server.tool_get_drawers(["drawer_arbitrary"])

        assert single["content"] == content
        assert bulk["errors"] == 0
        assert bulk["results"][0]["content"] == content
        assert _stored_meta(collection, "drawer_arbitrary") == before

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
    def test_flag_on_normalizes_optional_fields_before_potentiating(
        self, monkeypatch, config, collection, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        content = "Verbatim heliodor body\n  with preserved spacing."
        _add_drawer(
            collection,
            "drawer_invalid_potentiate",
            content,
            strength="unknown",
            stability="unknown",
            access_count="unknown",
            last_activated="unknown",
            custom="untouched",
        )

        from mempalace import mcp_server

        now = T0 + timedelta(days=1)
        monkeypatch.setenv("MEMPALACE_SALIENCE_POTENTIATE", "true")
        monkeypatch.setattr(mcp_server, "_READ_ONLY", False)
        monkeypatch.setattr(mcp_server, "_MCP_WRITER_LOCK_CM", object())
        monkeypatch.setattr(mcp_server, "_now", lambda: now)

        result = mcp_server.tool_search(query="heliodor", limit=1, max_distance=0)

        assert result["results"]
        stored = _stored_meta(collection, "drawer_invalid_potentiate")
        assert stored["strength"] == pytest.approx(0.4178794411714423, rel=1e-12)
        assert stored["stability"] == 1.1
        assert stored["access_count"] == 1
        assert stored["last_activated"] == now.isoformat()
        assert stored["filed_at"] == T0.isoformat()
        assert stored["custom"] == "untouched"
        assert mcp_server.tool_get_drawer("drawer_invalid_potentiate")["content"] == content

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
