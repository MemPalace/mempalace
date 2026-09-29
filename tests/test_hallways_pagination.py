"""Regression tests for #1619 and #2466.

``compute_hallways_for_wing`` must fetch drawers with a wing-scoped,
paginated ``get(where={"wing": wing}, limit=, offset=)``:

- NOT a single unbounded ``get(where={"wing": wing})`` — that binds one SQL
  variable per matched id and overflows SQLite's ``SQLITE_MAX_VARIABLE_NUMBER``
  (32766) on wings larger than ~32k drawers, silently leaving the hallway
  graph unbuilt on exactly the large wings that benefit most (#1619);
- NOT an unscoped walk of the whole collection filtered client-side — that
  costs O(total palace drawers) on every mine, so filing one small session
  into an 800k-drawer palace pegged the CPU for minutes (#2466).
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


def _collection_that_counts_fetched_rows(drawers):
    """Records every get() call and how many rows each one served, honouring
    a ``where={"wing": ...}`` filter the way ChromaDB does."""
    col = MagicMock()
    col.count.return_value = len(drawers)
    calls: list[dict] = []

    def _get(limit=None, offset=0, include=None, where=None, ids=None, **kw):
        filtered_drawers = drawers
        if where and "wing" in where:
            filtered_drawers = [d for d in drawers if d.get("wing") == where["wing"]]
        page = filtered_drawers[offset : offset + limit] if limit is not None else filtered_drawers
        calls.append({"where": where, "limit": limit, "offset": offset, "served": len(page)})
        return {
            "ids": [f"d{i}" for i in range(offset, offset + len(page))],
            "metadatas": page,
        }

    col.get.side_effect = _get
    return col, calls


class TestComputeHallwaysWingScoping:
    def test_fetch_is_scoped_to_the_wing_not_the_whole_palace(self, tmp_path, monkeypatch):
        """#2466: 3 drawers in the target wing next to 12k drawers elsewhere —
        the fetch must page through the 3, not the 12k."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        drawers = [{"wing": "wing_other", "room": "r", "entities": "X;Y"}] * 12_000
        drawers += [{"wing": "wing_alpha", "room": "diary", "entities": "Alice;Bob"}] * 3
        col, calls = _collection_that_counts_fetched_rows(drawers)

        result = hallways_mod.compute_hallways_for_wing("wing_alpha", col=col)

        assert any({h["entity_a"], h["entity_b"]} == {"Alice", "Bob"} for h in result)
        assert calls, "no fetch happened"
        assert all(c["where"] == {"wing": "wing_alpha"} for c in calls), calls
        assert all(c["limit"] is not None for c in calls), (
            "an unbounded where-get overflows (#1619)"
        )
        assert sum(c["served"] for c in calls) == 3, calls

    def test_large_wing_is_paged_in_bounded_batches(self, tmp_path, monkeypatch):
        """A wing above the batch size is walked page by page until a short
        page, every page bounded and wing-scoped."""
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        drawers = [{"wing": "wing_alpha", "room": "diary", "entities": "Alice;Bob"}] * 12_001
        drawers += [{"wing": "wing_other", "room": "r", "entities": "X;Y"}] * 5
        col, calls = _collection_that_counts_fetched_rows(drawers)

        result = hallways_mod.compute_hallways_for_wing("wing_alpha", col=col)

        assert any({h["entity_a"], h["entity_b"]} == {"Alice", "Bob"} for h in result)
        assert [c["offset"] for c in calls] == [0, 5000, 10000]
        assert [c["served"] for c in calls] == [5000, 5000, 2001]
        assert all(c["where"] == {"wing": "wing_alpha"} and c["limit"] == 5000 for c in calls)
