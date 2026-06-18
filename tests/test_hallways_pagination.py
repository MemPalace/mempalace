"""Regression test for #1619.

``compute_hallways_for_wing`` must fetch drawers by paginating
(``count()`` + ``get(limit=, offset=)``) and filtering the wing client-side,
NOT with a single ``get(where={"wing": wing})`` — the latter binds one SQL
variable per matched id and overflows SQLite's ``SQLITE_MAX_VARIABLE_NUMBER``
(32766) on wings larger than ~32k drawers, silently leaving the hallway graph
unbuilt on exactly the large wings that benefit most.
"""

from unittest.mock import MagicMock, patch

with patch.dict("sys.modules", {"chromadb": MagicMock()}):
    from mempalace import hallways as hallways_mod


def _use_tmp_hallway_file(monkeypatch, tmp_path):
    hallway_file = tmp_path / "hallways.json"
    monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **kw: str(hallway_file))
    monkeypatch.setattr(
        hallways_mod,
        "_legacy_hallway_file",
        lambda: str(tmp_path / "legacy-hallways.json"),
    )


def _collection_that_rejects_where_get(drawers):
    """count() + paginated get(limit,offset) work; a where-get raises, exactly
    as ChromaDB does when the bound-variable count overflows on a big wing."""
    col = MagicMock()
    col.count.return_value = len(drawers)

    def _get(limit=None, offset=0, include=None, where=None, ids=None, **kw):
        if where is not None and limit is None:
            raise RuntimeError("Error executing plan: too many SQL variables")
        filtered_drawers = drawers
        if where and "wing" in where:
            target_wing = where["wing"]
            filtered_drawers = [
                d for d in drawers if isinstance(d, dict) and d.get("wing") == target_wing
            ]
        page = filtered_drawers[offset : offset + limit] if limit is not None else filtered_drawers
        return {
            "ids": [f"d{i}" for i in range(offset, offset + len(page))],
            "metadatas": page,
        }

    col.get.side_effect = _get
    return col


class TestComputeHallwaysPagination:
    def test_large_wing_builds_hallways_via_pagination(self, tmp_path, monkeypatch):
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        # 3 drawers all co-placing Alice+Bob → one hallway at min_count=2,
        # but ONLY if the fetch paginates instead of the variable-bound where-get.
        drawers = [{"wing": "wing_alpha", "room": "diary", "entities": "Alice;Bob"}] * 3
        col = _collection_that_rejects_where_get(drawers)
        result = hallways_mod.compute_hallways_for_wing("wing_alpha", col=col)
        assert any({h["entity_a"], h["entity_b"]} == {"Alice", "Bob"} for h in result), (
            "hallways came back empty — the where-get path crashed; the fetch must paginate (#1619)"
        )
