"""Extra metadata and connection-lifecycle tests for mempalace.knowledge_graph."""

import json

import pytest


class TestEntityMetadata:
    def test_add_entity_persists_properties_json(self, kg):
        kg.add_entity(
            "Alice",
            entity_type="person",
            properties={"role": "engineer", "team": "platform"},
        )

        row = (
            kg._conn()
            .execute(
                "SELECT name, type, properties FROM entities WHERE id = ?",
                ("alice",),
            )
            .fetchone()
        )

        assert row["name"] == "Alice"
        assert row["type"] == "person"
        assert json.loads(row["properties"]) == {
            "role": "engineer",
            "team": "platform",
        }

    def test_add_entity_upsert_replaces_properties(self, kg):
        kg.add_entity("Alice", entity_type="person", properties={"role": "engineer"})
        kg.add_entity("Alice", entity_type="person", properties={"role": "manager"})

        row = (
            kg._conn()
            .execute(
                "SELECT properties FROM entities WHERE id = ?",
                ("alice",),
            )
            .fetchone()
        )

        assert json.loads(row["properties"]) == {"role": "manager"}


class TestTripleMetadata:
    def test_add_triple_persists_confidence_and_sources(self, kg):
        tid = kg.add_triple(
            "Alice",
            "works_at",
            "NewCo",
            confidence=0.65,
            source_closet="career",
            source_file="jobs.md",
        )

        row = (
            kg._conn()
            .execute(
                "SELECT confidence, source_closet, source_file FROM triples WHERE id = ?",
                (tid,),
            )
            .fetchone()
        )

        assert row["confidence"] == pytest.approx(0.65)
        assert row["source_closet"] == "career"
        assert row["source_file"] == "jobs.md"

    def test_query_entity_exposes_confidence_and_source_closet(self, kg):
        kg.add_triple(
            "Alice",
            "works_at",
            "NewCo",
            confidence=0.42,
            source_closet="career",
            source_file="jobs.md",
        )

        results = kg.query_entity("Alice", direction="outgoing")

        assert len(results) == 1
        assert results[0]["predicate"] == "works_at"
        assert results[0]["object"] == "NewCo"
        assert results[0]["confidence"] == pytest.approx(0.42)
        assert results[0]["source_closet"] == "career"
        assert results[0]["current"] is True


class TestConnectionLifecycle:
    def test_close_allows_lazy_reconnect_for_reads(self, kg):
        kg.add_triple("Alice", "knows", "Bob")
        old_conn = kg._conn()

        kg.close()

        assert kg._connection is None

        results = kg.query_entity("Alice", direction="outgoing")

        assert any(r["predicate"] == "knows" and r["object"] == "Bob" for r in results)
        assert kg._connection is not None
        assert kg._connection is not old_conn

    def test_close_allows_lazy_reconnect_for_writes(self, kg):
        kg.close()

        tid = kg.add_triple("Alice", "works_at", "NewCo")

        assert tid.startswith("t_alice_works_at_newco_")

        results = kg.query_entity("Alice", direction="outgoing")

        assert len(results) == 1
        assert results[0]["object"] == "NewCo"


class TestStatsMetadata:
    def test_stats_reports_sorted_distinct_relationship_types(self, kg):
        kg.add_triple("Alice", "works at", "NewCo")
        kg.add_triple("Alice", "loves", "Chess")
        kg.add_triple("Bob", "works_at", "OldCo")

        stats = kg.stats()

        assert stats["relationship_types"] == ["loves", "works_at"]
