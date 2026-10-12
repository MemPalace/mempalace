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

On Chroma the wing is read in one pass over ``chroma.sqlite3`` instead,
since Chroma pages with SQL ``OFFSET`` (#2684); the tests at the bottom
check that it builds the same hallways as paging.
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


# ── #2684: on Chroma, one sqlite pass instead of OFFSET paging ─────────────


def _chroma_fixture_palace(tmp_path):
    """A real Chroma palace with two wings and every drawer shape the
    hallway walk distinguishes."""
    from mempalace.palace import get_collection

    col = get_collection(str(tmp_path / "palace"), create=True)
    rows = [
        {"wing": "w", "room": "diary", "entities": "Alice;Bob"},
        {"wing": "w", "room": "work", "entities": "Alice;Bob;Carol"},
        {"wing": "w", "room": "work", "entities": "Bob;Carol"},
        {"wing": "w", "room": "  ", "entities": "Alice;Carol"},
        {"wing": "w", "entities": "Alice;Carol;Alice"},
        # One file under two spellings, and a file paired with itself.
        {"wing": "w", "room": "code", "entities": "ChatStore.swift;RootView.swift"},
        {"wing": "w", "room": "code", "entities": "ChatStore;RootView"},
        {"wing": "w", "room": "code", "entities": "src/main.zig;main.zig"},
        # Sentinels and entity-less drawers contribute nothing.
        {"wing": "w", "room": "diary", "entities": "Alice;Mallory", "is_sentinel": True},
        {"wing": "w", "room": "diary"},
        {"wing": "w", "room": "diary", "entities": ""},
        # Another wing's pairs stay out of this one.
        {"wing": "other", "room": "diary", "entities": "Alice;Bob"},
        {"wing": "other", "room": "diary", "entities": "Alice;Bob"},
        # A wing whose drawers hold no entities at all.
        {"wing": "bare", "room": "diary"},
    ]
    col.add(
        ids=[f"d{i}" for i in range(len(rows))],
        documents=[f"doc {i}" for i in range(len(rows))],
        embeddings=[[0.1 * (i % 7), 0.2] for i in range(len(rows))],
        metadatas=rows,
    )
    return col


def _records(hallways):
    return sorted(
        (h["id"], h["entity_a"], h["entity_b"], tuple(h["rooms"]), h.get("co_occurrence_count"))
        for h in hallways
    )


def _compute_both_ways(tmp_path, monkeypatch, col, wing, *, seed=()):
    """compute_hallways_for_wing through the sqlite pass and through paging,
    each against its own hallway file seeded with ``seed``."""
    import json

    import mempalace.palace as palace_pkg

    results = {}
    for path in ("sqlite", "paged"):
        _use_tmp_hallway_file(monkeypatch, tmp_path / path)
        (tmp_path / path).mkdir(parents=True, exist_ok=True)
        hallways_mod._save_hallways(list(seed))
        with monkeypatch.context() as m:
            if path == "paged":
                m.setattr(palace_pkg, "_fast_collection_metadata", lambda *_a, **_k: None)
            created = hallways_mod.compute_hallways_for_wing(wing, col=col, min_count=1)
        stored = json.loads((tmp_path / path / "hallways.json").read_text(encoding="utf-8"))
        results[path] = (_records(created), _records(stored["hallways"]))
    return results


class TestComputeHallwaysSqlitePass:
    def test_sqlite_pass_matches_paging(self, tmp_path, monkeypatch):
        col = _chroma_fixture_palace(tmp_path)
        results = _compute_both_ways(tmp_path, monkeypatch, col, "w")

        assert results["sqlite"] == results["paged"]
        created, _ = results["sqlite"]
        pairs = {(a, b): (rooms, n) for _id, a, b, rooms, n in created}
        assert pairs[("Alice", "Bob")] == (("diary", "work"), 2)
        assert pairs[("Alice", "Carol")] == (("work",), 3)
        # ChatStore.swift and ChatStore are one file, so one pair counted twice.
        chat = [v for pair, v in pairs.items() if any(e.startswith("ChatStore") for e in pair)]
        assert chat == [(("code",), 2)]
        assert not any("Mallory" in pair for pair in pairs)
        assert not any("main.zig" in a or "main.zig" in b for a, b in pairs)

    def test_sqlite_pass_does_not_page(self, tmp_path, monkeypatch):
        from mempalace.backends.chroma import ChromaCollection

        col = _chroma_fixture_palace(tmp_path)
        _use_tmp_hallway_file(monkeypatch, tmp_path)

        def _no_paging(*_a, **_k):
            raise AssertionError("paged through Chroma instead of reading chroma.sqlite3")

        monkeypatch.setattr(ChromaCollection, "get", _no_paging)
        created = hallways_mod.compute_hallways_for_wing("w", col=col, min_count=1)
        assert created
        # A wing whose drawers hold no entities is told apart from an empty
        # wing through sqlite too, not through a get that loads the index.
        assert hallways_mod.compute_hallways_for_wing("bare", col=col, min_count=1) == []

    def test_sqlite_pass_reads_only_the_wing(self, tmp_path, monkeypatch):
        """The scan is scoped to the wing in sqlite, so a small wing in a
        large palace does not read every other wing's drawers."""
        from mempalace.backends.chroma import ChromaCollection

        col = _chroma_fixture_palace(tmp_path)
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        real = ChromaCollection.iter_metadata
        scopes = []
        yielded = []

        def recording(self, keys=None, *, require_key=None, equals=None):
            scopes.append(equals)
            for meta in real(self, keys, require_key=require_key, equals=equals):
                yielded.append(meta)
                yield meta

        monkeypatch.setattr(ChromaCollection, "iter_metadata", recording)
        hallways_mod.compute_hallways_for_wing("w", col=col, min_count=1)
        hallways_mod.compute_hallways_for_wing("bare", col=col, min_count=1)

        # "bare" holds no entities, so its scan comes back empty and the
        # empty-wing probe runs, still scoped.
        assert scopes == [{"wing": "w"}, {"wing": "bare"}, {"wing": "bare"}]
        assert yielded and all(meta["wing"] in ("w", "bare") for meta in yielded)

    def test_wing_without_entities_still_replaces_its_old_hallways(self, tmp_path, monkeypatch):
        """The sqlite pass reads only drawers with entities; a wing whose
        drawers hold none must still clear its stale records, as paging
        does, while a wing with no drawers at all leaves them alone."""
        col = _chroma_fixture_palace(tmp_path)
        stale = [
            {"id": f"hallway_{w}_A_B", "wing": w, "entity_a": "A", "entity_b": "B", "rooms": []}
            for w in ("bare", "gone")
        ]

        bare = _compute_both_ways(tmp_path / "bare", monkeypatch, col, "bare", seed=stale)
        assert bare["sqlite"] == bare["paged"]
        assert bare["sqlite"][1] == [("hallway_gone_A_B", "A", "B", (), None)]

        gone = _compute_both_ways(tmp_path / "gone", monkeypatch, col, "gone", seed=stale)
        assert gone["sqlite"] == gone["paged"]
        assert len(gone["sqlite"][1]) == 2

    def test_failed_sqlite_pass_falls_back_to_paging(self, tmp_path, monkeypatch):
        import sqlite3

        from mempalace.backends.chroma import ChromaCollection

        col = _chroma_fixture_palace(tmp_path)
        expected = _compute_both_ways(tmp_path / "ref", monkeypatch, col, "w")["paged"]

        def breaks_after_one_row(self, keys=None, *, require_key=None, equals=None):
            def gen():
                yield {"wing": "w", "entities": "Alice;Bob"}
                raise sqlite3.OperationalError("injected failure")

            return gen()

        monkeypatch.setattr(ChromaCollection, "iter_metadata", breaks_after_one_row)
        _use_tmp_hallway_file(monkeypatch, tmp_path)
        created = hallways_mod.compute_hallways_for_wing("w", col=col, min_count=1)
        assert _records(created) == expected[0]
