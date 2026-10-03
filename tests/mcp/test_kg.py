"""MCP server tests — knowledge-graph tools and tenant cache."""

import os
import subprocess
import sys


from _mcp_server_helpers import (
    _get_collection,
    _patch_mcp_server,
)


class TestKGTools:
    def test_kg_add(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(
            subject="Alice",
            predicate="likes",
            object="coffee",
            valid_from="2025-01-01",
        )
        assert result["success"] is True

    def test_kg_query(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_query

        result = tool_kg_query(entity="Max")
        assert result["count"] > 0

    def test_kg_invalidate(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_invalidate

        result = tool_kg_invalidate(
            subject="Max",
            predicate="does",
            object="chess",
            ended="2026-03-01",
        )
        assert result["success"] is True
        # Regression #1314: response must echo the actual ended date,
        # not silently drop it and return the literal string "today".
        assert result["ended"] == "2026-03-01"

    def test_kg_supersede(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_supersede

        kg.add_triple("Bot", "uses_model", "old", valid_from="2026-05-01")
        result = tool_kg_supersede(
            subject="Bot",
            predicate="uses_model",
            old_object="old",
            new_object="new",
            at="2026-06-02",
        )
        assert result["success"] is True
        assert result["superseded"] == "old"
        models = [
            f["object"]
            for f in kg.query_entity("Bot", as_of="2026-06-02", direction="outgoing")
            if f["predicate"] == "uses_model"
        ]
        assert models == ["new"]

    def test_kg_add_forwards_valid_to(self, monkeypatch, config, palace_path, kg):
        """Regression #1314 case 1: valid_to must round-trip through kg_add."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(
            subject="_test_temporal",
            predicate="had_value",
            object="probe",
            valid_from="2026-01-01",
            valid_to="2026-04-28",
        )
        assert result["success"] is True

        facts = kg.query_entity("_test_temporal")
        assert len(facts) == 1
        assert facts[0]["valid_from"] == "2026-01-01"
        assert facts[0]["valid_to"] == "2026-04-28"
        # An already-ended fact must not be reported as still current.
        assert facts[0]["current"] is False

    def test_kg_add_forwards_source_provenance(self, monkeypatch, config, palace_path, kg):
        """Regression #1314 case 3: source_file / source_drawer_id reach storage."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(
            subject="operating-verb",
            predicate="candidate",
            object="husbandry",
            valid_from="2026-04-28",
            source_closet="closet-42",
            source_file="docs/decisions.md",
            source_drawer_id="drawer_abc123",
        )
        assert result["success"] is True

        triple_id = result["triple_id"]
        # Read raw row to verify all provenance columns persisted.
        with kg._lock:
            row = (
                kg._conn()
                .execute(
                    "SELECT source_closet, source_file, source_drawer_id FROM triples WHERE id = ?",
                    (triple_id,),
                )
                .fetchone()
            )
        assert row is not None
        assert row["source_closet"] == "closet-42"
        assert row["source_file"] == "docs/decisions.md"
        assert row["source_drawer_id"] == "drawer_abc123"

    def test_kg_invalidate_returns_actual_ended_date(
        self, monkeypatch, config, palace_path, seeded_kg
    ):
        """Regression #1314 case 2: response reports the resolved date, not 'today'."""
        from datetime import date as _date

        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_invalidate

        # Caller-supplied date round-trips into the response.
        explicit = tool_kg_invalidate(
            subject="Max",
            predicate="does",
            object="swimming",
            ended="2026-04-28",
        )
        assert explicit["ended"] == "2026-04-28"

        # Caller-omitted date resolves to today's ISO date — never the
        # literal string "today" the buggy implementation used to return.
        implicit = tool_kg_invalidate(
            subject="Max",
            predicate="loves",
            object="Chess",
        )
        assert implicit["ended"] != "today"
        assert implicit["ended"] == _date.today().isoformat()

    def test_kg_timeline(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_timeline

        result = tool_kg_timeline(entity="Alice")
        assert result["count"] > 0
        assert result["total"] >= result["count"]
        assert result["offset"] == 0
        assert result["limit"] == 100

    def test_kg_timeline_paginates(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_timeline

        full = tool_kg_timeline()
        total = full["total"]
        assert total == full["count"]  # seeded KG fits in one default page

        page1 = tool_kg_timeline(limit=2, offset=0)
        page2 = tool_kg_timeline(limit=2, offset=2)
        assert page1["count"] == 2
        assert page1["total"] == total
        assert page1["offset"] == 0 and page1["limit"] == 2
        assert page2["offset"] == 2
        # pages are disjoint and in timeline order
        assert page1["timeline"] + page2["timeline"] == full["timeline"][: 2 + page2["count"]]

    def test_kg_timeline_clamps_pagination_args(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_timeline

        result = tool_kg_timeline(limit=10_000, offset=-3)
        assert result["limit"] == 100  # clamped to _MAX_RESULTS
        assert result["offset"] == 0

    def test_kg_stats(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_stats

        result = tool_kg_stats()
        assert result["entities"] >= 4

    # --- Date validation at the MCP boundary (issue #1164) ---

    def test_kg_add_rejects_invalid_valid_from(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace.mcp_server import tool_kg_add

        result = tool_kg_add(
            subject="Alice",
            predicate="likes",
            object="coffee",
            valid_from="Jan 2025",
        )
        assert result["success"] is False
        assert "valid_from" in result["error"]
        assert "ISO-8601" in result["error"]

    def test_kg_query_rejects_invalid_as_of(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_query

        result = tool_kg_query(entity="Max", as_of="March 2026")
        assert "error" in result
        assert "as_of" in result["error"]

    def test_kg_invalidate_rejects_invalid_ended(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_invalidate

        result = tool_kg_invalidate(
            subject="Max",
            predicate="does",
            object="chess",
            ended="yesterday",
        )
        assert result["success"] is False
        assert "ended" in result["error"]

    def test_kg_query_rejects_partial_iso_dates(self, monkeypatch, config, palace_path, seeded_kg):
        _patch_mcp_server(monkeypatch, config, seeded_kg)
        from mempalace.mcp_server import tool_kg_query

        # Partial ISO dates are rejected: KG queries compare TEXT dates
        # lexicographically, so "2026-01-01" <= "2026" is False, which
        # silently excludes facts. Reject at the boundary — only YYYY-MM-DD
        # produces correct results.
        for value in ("2026", "2026-03"):
            result = tool_kg_query(entity="Max", as_of=value)
            assert "error" in result, f"accepted partial date {value!r}: {result}"

        # Full ISO-8601 dates still pass.
        result = tool_kg_query(entity="Max", as_of="2026-03-15")
        assert "error" not in result, f"rejected valid date: {result}"

    def test_kg_add_accepts_datetime_valid_from(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace import mcp_server

        result = mcp_server.tool_kg_add(
            "Alice",
            "works_at",
            "Acme",
            valid_from="2026-05-06T14:23:00Z",
        )

        assert result["success"] is True

        facts = kg.query_entity("Alice", direction="outgoing")
        fact = next(r for r in facts if r["predicate"] == "works_at" and r["object"] == "Acme")

        assert fact["valid_from"] == "2026-05-06T14:23:00Z"

    def test_kg_add_accepts_datetime_valid_to(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace import mcp_server

        result = mcp_server.tool_kg_add(
            "Alice",
            "worked_at",
            "OldCo",
            valid_from="2026-05-06T14:00:00Z",
            valid_to="2026-05-06T15:00:00Z",
        )

        assert result["success"] is True

        facts = kg.query_entity("Alice", direction="outgoing")
        fact = next(r for r in facts if r["predicate"] == "worked_at" and r["object"] == "OldCo")

        assert fact["valid_from"] == "2026-05-06T14:00:00Z"
        assert fact["valid_to"] == "2026-05-06T15:00:00Z"

    def test_kg_query_accepts_datetime_as_of(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        kg.add_triple(
            "Alice",
            "works_at",
            "Acme",
            valid_from="2026-05-06T14:00:00Z",
        )

        from mempalace import mcp_server

        result = mcp_server.tool_kg_query(
            "Alice",
            as_of="2026-05-06T14:23:00Z",
            direction="outgoing",
        )

        assert "error" not in result
        assert result["as_of"] == "2026-05-06T14:23:00Z"
        assert result["count"] == 1
        assert result["facts"][0]["object"] == "Acme"

    def test_kg_query_as_of_does_not_duplicate_ended_facts(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        kg.add_triple(
            "Alice",
            "works_at",
            "Acme",
            valid_from="2026-01-01",
            valid_to="2026-06-01",
        )
        result = mcp_server.tool_kg_query("Alice", as_of="2026-04-01", direction="outgoing")
        assert result["count"] == 1
        assert len(result["active_facts"]) == 1
        assert result["historical_facts"] == []

    def test_kg_query_excludes_future_valid_from_from_active(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        kg.add_triple("Alice", "starts", "school", valid_from="2099-01-01")
        kg.add_triple("Alice", "lives_in", "Town", valid_from="2020-01-01")
        result = mcp_server.tool_kg_query("Alice", direction="outgoing")
        active_preds = {r["predicate"] for r in result["active_facts"]}
        future_preds = {r["predicate"] for r in result["future_facts"]}
        assert "lives_in" in active_preds
        assert "starts" not in active_preds
        assert "starts" in future_preds

    def test_kg_query_keeps_bounded_future_end_as_active(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        kg.add_triple(
            "Alice",
            "on_med",
            "Empagliflozin",
            valid_from="2025-01-01",
            valid_to="2099-12-31",
        )
        kg.add_triple(
            "Alice",
            "on_med",
            "Metformin",
            valid_from="2020-01-01",
            valid_to="2025-01-01",
        )
        result = mcp_server.tool_kg_query("Alice", direction="outgoing")
        active_objs = {r["object"] for r in result["active_facts"]}
        historical_objs = {r["object"] for r in result["historical_facts"]}
        assert "Empagliflozin" in active_objs
        assert "Empagliflozin" not in historical_objs
        assert "Metformin" in historical_objs

    def test_kg_invalidate_accepts_datetime_ended(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        kg.add_triple(
            "Alice",
            "works_at",
            "Acme",
            valid_from="2026-05-06T14:00:00Z",
        )

        from mempalace import mcp_server

        result = mcp_server.tool_kg_invalidate(
            "Alice",
            "works_at",
            "Acme",
            ended="2026-05-06T14:23:00Z",
        )

        assert result["success"] is True
        assert result["ended"] == "2026-05-06T14:23:00Z"

        facts = kg.query_entity("Alice", direction="outgoing")
        fact = next(r for r in facts if r["predicate"] == "works_at" and r["object"] == "Acme")

        assert fact["valid_to"] == "2026-05-06T14:23:00Z"

    def test_kg_add_rejects_non_canonical_datetimes(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace import mcp_server

        invalid_values = [
            "2026-05-06T14:23:00+02:00",
            "2026-05-06T14:23:00-05:30",
            "2026-05-06T14:23:00.123Z",
            "2026-05-06 14:23:00",
            "2026-05-06T14:23:00",
        ]

        for value in invalid_values:
            result = mcp_server.tool_kg_add(
                "Alice",
                "works_at",
                "Acme",
                valid_from=value,
            )

            assert result["success"] is False, value
            assert "valid_from" in result["error"]
            assert "YYYY-MM-DDTHH:MM:SSZ" in result["error"]

    def test_kg_query_rejects_non_canonical_datetime_as_of(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace import mcp_server

        invalid_values = [
            "2026-05-06T14:23:00+02:00",
            "2026-05-06T14:23:00-05:30",
            "2026-05-06T14:23:00.123Z",
            "2026-05-06 14:23:00",
            "2026-05-06T14:23:00",
        ]

        for value in invalid_values:
            result = mcp_server.tool_kg_query(
                "Alice",
                as_of=value,
                direction="outgoing",
            )

            assert "error" in result, value
            assert "as_of" in result["error"]
            assert "YYYY-MM-DDTHH:MM:SSZ" in result["error"]

    def test_kg_invalidate_rejects_non_canonical_ended(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        kg.add_triple(
            "Alice",
            "works_at",
            "Acme",
            valid_from="2026-05-06T14:00:00Z",
        )

        from mempalace import mcp_server

        invalid_values = [
            "2026-05-06T14:23:00+02:00",
            "2026-05-06T14:23:00-05:30",
            "2026-05-06T14:23:00.123Z",
            "2026-05-06 14:23:00",
            "2026-05-06T14:23:00",
        ]

        for value in invalid_values:
            result = mcp_server.tool_kg_invalidate(
                "Alice",
                "works_at",
                "Acme",
                ended=value,
            )

            assert result["success"] is False, value
            assert "ended" in result["error"]
            assert "YYYY-MM-DDTHH:MM:SSZ" in result["error"]

    def test_kg_add_rejects_timezone_offset_datetime(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)

        from mempalace import mcp_server

        result = mcp_server.tool_kg_add(
            "Alice",
            "works_at",
            "Acme",
            valid_from="2026-05-06T14:23:00+02:00",
        )

        assert result["success"] is False
        assert "valid_from" in result["error"]
        assert "YYYY-MM-DDTHH:MM:SSZ" in result["error"]

    def test_kg_query_recursive_predicate_filter(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        mcp_server.tool_kg_add(subject="node-c", predicate="synthesized-from", object="node-b")
        mcp_server.tool_kg_add(subject="node-b", predicate="synthesized-from", object="node-a")
        mcp_server.tool_kg_add(subject="node-b", predicate="related-to", object="noise")

        result = mcp_server.tool_kg_query(
            entity="node-c",
            direction="outgoing",
            predicate="synthesized-from",
            recurse=True,
            max_depth=5,
        )

        assert result["recurse"] is True
        edges = {(fact["subject"], fact["predicate"], fact["object"]) for fact in result["facts"]}
        assert ("node-c", "synthesized-from", "node-b") in edges
        assert ("node-b", "synthesized-from", "node-a") in edges
        assert all(fact["predicate"] == "synthesized-from" for fact in result["facts"])

    def test_resolve_canonical_follows_merged_into_chain(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        mcp_server.tool_kg_add(subject="node-b", predicate="merged-into", object="node-a")
        mcp_server.tool_kg_add(subject="node-c", predicate="merged-into", object="node-b")

        resolved = mcp_server.tool_resolve_canonical("node-c")
        assert resolved["canonical_node_id"] == "node-a"
        assert resolved["chain"] == ["node-c", "node-b", "node-a"]

    def test_get_height_on_synthesized_from_chain(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        s1 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-1")
        s2 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-2")
        s3 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-3")

        n1 = mcp_server.tool_add_drawer(wing="g", room="syn", content="node-1")
        n2 = mcp_server.tool_add_drawer(wing="g", room="syn", content="node-2")

        mcp_server.tool_kg_add(
            subject=n1["drawer_id"], predicate="synthesized-from", object=s1["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=n1["drawer_id"], predicate="synthesized-from", object=s2["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=n2["drawer_id"], predicate="synthesized-from", object=n1["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=n2["drawer_id"], predicate="synthesized-from", object=s3["drawer_id"]
        )

        height = mcp_server.tool_get_height(n2["drawer_id"])
        assert height["height"] == 2

    def test_find_merge_candidates_filters_by_topological_distance(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        s1 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-1")
        s2 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-2")
        s3 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-3")
        s4 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-4")

        node_a = mcp_server.tool_add_drawer(wing="g", room="syn", content="node-a")
        node_b = mcp_server.tool_add_drawer(wing="g", room="syn", content="node-b")
        node_c = mcp_server.tool_add_drawer(wing="g", room="syn", content="node-c")

        mcp_server.tool_kg_add(
            subject=node_a["drawer_id"], predicate="synthesized-from", object=s1["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=node_a["drawer_id"], predicate="synthesized-from", object=s2["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=node_b["drawer_id"], predicate="synthesized-from", object=s3["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=node_b["drawer_id"], predicate="synthesized-from", object=s4["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=node_c["drawer_id"], predicate="synthesized-from", object=s1["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=node_c["drawer_id"], predicate="synthesized-from", object=s3["drawer_id"]
        )

        def _fake_batch_matches(_collection, seed_records, _threshold):
            assert [r["drawer_id"] for r in seed_records] == [node_a["drawer_id"]]
            return (
                {
                    node_a["drawer_id"]: [
                        {"id": node_b["drawer_id"], "similarity": 0.96},
                        {"id": node_c["drawer_id"], "similarity": 0.94},
                        {"id": node_a["drawer_id"], "similarity": 1.0},
                    ]
                },
                None,
            )

        monkeypatch.setattr(
            mcp_server,
            "_batch_duplicate_matches_for_records",
            _fake_batch_matches,
        )

        strict = mcp_server.tool_find_merge_candidates(
            drawer_id=node_a["drawer_id"],
            require_topological_distance=True,
        )
        assert strict["count"] == 1
        assert strict["candidates"][0]["target_node_id"] == node_b["drawer_id"]
        assert strict["candidates"][0]["topologically_distant"] is True

        relaxed = mcp_server.tool_find_merge_candidates(
            drawer_id=node_a["drawer_id"],
            require_topological_distance=False,
        )
        assert relaxed["count"] == 2
        by_target = {c["target_node_id"]: c for c in relaxed["candidates"]}
        assert by_target[node_c["drawer_id"]]["topologically_distant"] is False
        assert by_target[node_c["drawer_id"]]["common_ancestor_count"] >= 1

    def test_find_closet_lineage_issues_reports_missing_sources(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        self._add_closet_row(palace_path, "closet_bad_missing")
        self._add_closet_row(palace_path, "closet_plain")

        mcp_server.tool_kg_add(
            subject="closet_bad_missing",
            predicate="synthesized-from",
            object="drawer_missing_source",
        )

        result = mcp_server.tool_find_closet_lineage_issues()

        assert result["target"] == "closets"
        assert result["total"] == 1
        orphan = result["orphans"][0]
        assert orphan["closet_id"] == "closet_bad_missing"
        assert "missing_source_nodes" in orphan["issues"]
        assert orphan["active_source_ids"] == ["drawer_missing_source"]

    def test_apply_merge_sets_canonical_and_invalidates_source_edges(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        s1 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-1")
        s2 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-2")
        s3 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-3")
        s4 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-4")

        source = mcp_server.tool_add_drawer(wing="g", room="syn", content="source")
        canonical = mcp_server.tool_add_drawer(wing="g", room="syn", content="canonical")

        mcp_server.tool_kg_add(
            subject=source["drawer_id"], predicate="synthesized-from", object=s1["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=source["drawer_id"], predicate="synthesized-from", object=s2["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=canonical["drawer_id"], predicate="synthesized-from", object=s3["drawer_id"]
        )
        mcp_server.tool_kg_add(
            subject=canonical["drawer_id"], predicate="synthesized-from", object=s4["drawer_id"]
        )

        merged = mcp_server.tool_apply_merge(
            source_node_id=source["drawer_id"],
            canonical_node_id=canonical["drawer_id"],
            ended="2026-06-01",
        )
        assert merged["success"] is True
        assert merged["merged"] is True
        assert merged["invalidated_lineage_edges"] == 2

        resolved = mcp_server.tool_resolve_canonical(source["drawer_id"])
        assert resolved["canonical_node_id"] == canonical["drawer_id"]

        outgoing = mcp_server.tool_kg_query(source["drawer_id"], direction="outgoing")
        active_synth = [
            fact
            for fact in outgoing["facts"]
            if fact.get("predicate") == "synthesized-from" and fact.get("current")
        ]
        assert active_synth == []

        repeat = mcp_server.tool_apply_merge(
            source_node_id=source["drawer_id"],
            canonical_node_id=canonical["drawer_id"],
        )
        assert repeat["success"] is True
        assert repeat["merged"] is False

    def test_apply_merge_requires_existing_nodes(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        existing = mcp_server.tool_add_drawer(wing="g", room="raw", content="exists")

        bad_source = mcp_server.tool_apply_merge(
            source_node_id="drawer_missing", canonical_node_id=existing["drawer_id"]
        )
        assert bad_source["success"] is False
        assert "source node not found" in bad_source["error"]

        bad_target = mcp_server.tool_apply_merge(
            source_node_id=existing["drawer_id"], canonical_node_id="drawer_missing_target"
        )
        assert bad_target["success"] is False
        assert "canonical node not found" in bad_target["error"]

    def test_find_merge_candidates_rejects_missing_seed(self, monkeypatch, config, palace_path, kg):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        result = mcp_server.tool_find_merge_candidates(drawer_id="drawer_missing")

        assert "error" in result
        assert "drawer_id not found" in result["error"]

    def test_find_merge_candidates_scan_queries_with_stored_vectors(
        self, monkeypatch, config, palace_path, kg
    ):
        """Without a seed the tool scans the palace, and it queries with the
        vectors already stored rather than embedding each seed's text again."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        for i in range(4):
            mcp_server.tool_add_drawer(wing="g", room="syn", content=f"merge candidate {i}")

        seen = {}
        real = mcp_server._batch_duplicate_matches_for_records

        def _spy(col, seed_records, threshold):
            seen["records"] = seed_records
            return real(col, seed_records, threshold)

        monkeypatch.setattr(mcp_server, "_batch_duplicate_matches_for_records", _spy)

        result = mcp_server.tool_find_merge_candidates(threshold=0.0, wing="g")

        assert "error" not in result, result
        assert result["scanned_nodes"] == 4
        assert result["count"] >= 1
        assert all(rec["embedding"] for rec in seen["records"])

    def test_find_closet_lineage_issues_opens_closets_once(
        self, monkeypatch, config, palace_path, kg
    ):
        """The audit reads every closet's lineage in bulk: missing, stale and
        looping sources are all reported, and the closets collection is opened
        once however many closets take part."""
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server, palace

        live = mcp_server.tool_add_drawer(wing="g", room="raw", content="live source")["drawer_id"]
        old = mcp_server.tool_add_drawer(wing="g", room="raw", content="old source")["drawer_id"]
        for cid in ("closet_ok", "closet_missing", "closet_stale", "closet_loop", "closet_plain"):
            self._add_closet_row(palace_path, cid)

        mcp_server.tool_kg_add(subject="closet_ok", predicate="synthesized-from", object=live)
        mcp_server.tool_kg_add(
            subject="closet_missing", predicate="synthesized-from", object="drawer_gone"
        )
        mcp_server.tool_kg_add(subject="closet_stale", predicate="synthesized-from", object=old)
        mcp_server.tool_kg_add(subject=old, predicate="merged-into", object=live)
        mcp_server.tool_kg_add(subject="closet_loop", predicate="synthesized-from", object="x1")
        mcp_server.tool_kg_add(subject="x1", predicate="merged-into", object="x2")
        mcp_server.tool_kg_add(subject="x2", predicate="merged-into", object="x1")

        opened = []
        real_open = palace.get_closets_collection

        def _count(*args, **kwargs):
            opened.append(args)
            return real_open(*args, **kwargs)

        monkeypatch.setattr(palace, "get_closets_collection", _count)

        result = mcp_server.tool_find_closet_lineage_issues(limit=50)

        assert len(opened) == 1
        by_id = {row["closet_id"]: row for row in result["orphans"]}
        assert set(by_id) == {"closet_missing", "closet_stale", "closet_loop"}
        assert by_id["closet_missing"]["issues"] == ["missing_source_nodes"]
        assert by_id["closet_stale"]["issues"] == ["stale_source_references"]
        assert by_id["closet_stale"]["stale_sources"] == [
            {"source_node_id": old, "canonical_node_id": live}
        ]
        assert by_id["closet_loop"]["unresolvable_source_ids"] == ["x1"]

    def test_resolve_canonical_does_not_follow_a_near_match(
        self, monkeypatch, config, palace_path, kg
    ):
        """A lineage id that does not exist resolves to itself; it is never
        swapped for a stored id that merely looks like it."""
        _patch_mcp_server(monkeypatch, config, kg)
        from mempalace import mcp_server

        mcp_server.tool_kg_add(
            subject="drawer_one_two", predicate="merged-into", object="elsewhere"
        )

        resolved = mcp_server.tool_resolve_canonical("drawer_one")

        assert resolved["canonical_node_id"] == "drawer_one"
        assert resolved["chain"] == ["drawer_one"]

    def test_apply_merge_refused_part_way_changes_nothing(
        self, monkeypatch, config, palace_path, kg
    ):
        _patch_mcp_server(monkeypatch, config, kg)
        _client, _col = _get_collection(palace_path, create=True)
        del _client
        from mempalace import mcp_server

        s1 = mcp_server.tool_add_drawer(wing="g", room="raw", content="src-1")["drawer_id"]
        source = mcp_server.tool_add_drawer(wing="g", room="syn", content="source")["drawer_id"]
        target = mcp_server.tool_add_drawer(wing="g", room="syn", content="target")["drawer_id"]
        mcp_server.tool_kg_add(
            subject=source, predicate="synthesized-from", object=s1, valid_from="2026-06-01"
        )

        refused = mcp_server.tool_apply_merge(
            source_node_id=source, canonical_node_id=target, ended="2026-01-01"
        )

        assert refused["success"] is False
        assert mcp_server.tool_resolve_canonical(source)["canonical_node_id"] == source
        current = [
            f
            for f in mcp_server.tool_kg_query(source, direction="outgoing")["facts"]
            if f["current"]
        ]
        assert [(f["predicate"], f["object"]) for f in current] == [("synthesized-from", s1)]

    @staticmethod
    def _add_closet_row(palace_path, closet_id, wing="g", room="syn", extra_meta=None):
        from mempalace.palace import get_closets_collection

        closets_col = get_closets_collection(palace_path, create=True)
        metadata = {"wing": wing, "room": room, "source_file": f"{closet_id}.txt"}
        if extra_meta:
            metadata.update(extra_meta)
        closets_col.add(
            ids=[closet_id],
            documents=[f"closet content for {closet_id}"],
            metadatas=[metadata],
        )


# ── Diary Tools ─────────────────────────────────────────────────────────


class TestKGLazyCache:
    """Lazy per-path KnowledgeGraph cache (issue #1136)."""

    def test_lazy_init_no_import_side_effect(self, tmp_path):
        """Importing mcp_server must not create knowledge_graph.sqlite3.

        Runs in a fresh subprocess with HOME pointed at tmp_path so the
        assertion targets a clean filesystem, independent of conftest's
        session-level HOME patch.
        """

        kg_file = tmp_path / ".mempalace" / "knowledge_graph.sqlite3"
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMPAL")}
        env["HOME"] = str(tmp_path)
        env["USERPROFILE"] = str(tmp_path)
        result = subprocess.run(
            [sys.executable, "-c", "import mempalace.mcp_server"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"import failed: {result.stderr}"
        assert not kg_file.exists(), f"import created sqlite file at {kg_file} as a side effect"

    def test_get_kg_returns_same_instance(self, tmp_path, monkeypatch):
        """Two calls with the same resolved path return the same KG."""
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_kg_by_path", {})
        monkeypatch.setattr(mcp_server, "_palace_flag_given", True)
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))

        kg1 = mcp_server._get_kg()
        kg2 = mcp_server._get_kg()
        assert kg1 is kg2
        assert len(mcp_server._kg_by_path) == 1

    def test_get_kg_different_paths_different_instances(self, tmp_path, monkeypatch):
        """Different palace paths map to different KG instances."""
        from mempalace import mcp_server

        tmp_a = tmp_path / "a"
        tmp_b = tmp_path / "b"
        tmp_a.mkdir()
        tmp_b.mkdir()

        monkeypatch.setattr(mcp_server, "_kg_by_path", {})
        monkeypatch.setattr(mcp_server, "_palace_flag_given", True)

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_a))
        kg_a = mcp_server._get_kg()
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_b))
        kg_b = mcp_server._get_kg()

        assert kg_a is not kg_b
        assert len(mcp_server._kg_by_path) == 2

    def test_multi_tenant_env_switch(self, tmp_path, monkeypatch):
        """The issue #1136 acceptance scenario.

        Rotating MEMPALACE_PALACE_PATH between MCP tool calls must route
        each call to the correct tenant's KG sqlite file.
        """
        from mempalace import mcp_server

        tmp_a = tmp_path / "tenant_a"
        tmp_b = tmp_path / "tenant_b"
        tmp_a.mkdir()
        tmp_b.mkdir()

        monkeypatch.setattr(mcp_server, "_kg_by_path", {})
        monkeypatch.setattr(mcp_server, "_palace_flag_given", True)

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_a))
        add_result = mcp_server.tool_kg_add(
            subject="alice_secret",
            predicate="owns",
            object="repo_a",
        )
        assert add_result.get("success") is True, add_result

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_b))
        query_b = mcp_server.tool_kg_query(entity="alice_secret")
        assert query_b.get("count", 0) == 0, f"tenant B leaked tenant A's fact: {query_b}"

        monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_a))
        query_a = mcp_server.tool_kg_query(entity="alice_secret")
        assert query_a.get("count", 0) >= 1, f"tenant A lost its own fact: {query_a}"


# ── Structured error codes + MineAlreadyRunning (#1552) ─────────────────
