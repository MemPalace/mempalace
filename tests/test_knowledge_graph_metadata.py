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


class TestQueryEntityTripleIdentity:
    """query_entity returns the triple id and source_file that add_triple stored."""

    def test_outgoing_fact_carries_triple_id_and_source_file(self, kg):
        tid = kg.add_triple("Alice", "works_at", "NewCo", source_file="jobs.md")

        (fact,) = kg.query_entity("Alice", direction="outgoing")

        assert fact["id"] == tid
        assert fact["source_file"] == "jobs.md"

    def test_incoming_fact_carries_triple_id_and_source_file(self, kg):
        tid = kg.add_triple("Alice", "works_at", "NewCo", source_file="jobs.md")

        (fact,) = kg.query_entity("NewCo", direction="incoming")

        assert fact["id"] == tid
        assert fact["source_file"] == "jobs.md"

    def test_both_direction_query_carries_an_id_on_each_fact(self, kg):
        out_id = kg.add_triple("Alice", "knows", "Bob", source_file="people.md")
        in_id = kg.add_triple("Carol", "knows", "Alice", source_file="org.md")

        facts = kg.query_entity("Alice", direction="both")

        assert {(f["direction"], f["id"], f["source_file"]) for f in facts} == {
            ("outgoing", out_id, "people.md"),
            ("incoming", in_id, "org.md"),
        }

    def test_source_closet_and_source_file_are_independent(self, kg):
        kg.add_triple("Alice", "works_at", "NewCo", source_closet="career")
        kg.add_triple("Bob", "works_at", "NewCo", source_file="jobs.md")

        (alice,) = kg.query_entity("Alice", direction="outgoing")
        (bob,) = kg.query_entity("Bob", direction="outgoing")

        assert (alice["source_closet"], alice["source_file"]) == ("career", None)
        assert (bob["source_closet"], bob["source_file"]) == (None, "jobs.md")

    def test_facts_sharing_a_tuple_keep_distinct_ids(self, kg):
        first = kg.add_triple("Alice", "works_at", "NewCo", valid_from="2020-01-01")
        kg.invalidate("Alice", "works_at", "NewCo", ended="2022-01-01")
        second = kg.add_triple("Alice", "works_at", "NewCo", valid_from="2024-01-01")

        facts = kg.query_entity("Alice", direction="outgoing")

        assert first != second
        assert {f["valid_from"]: f["id"] for f in facts} == {
            "2020-01-01": first,
            "2024-01-01": second,
        }

    def test_rows_with_identical_dates_keep_distinct_ids(self, kg):
        first = kg.add_triple(
            "Alice", "works_at", "NewCo", valid_from="2020-01-01", valid_to="2021-01-01"
        )
        second = kg.add_triple(
            "Alice", "works_at", "NewCo", valid_from="2020-01-01", valid_to="2021-01-01"
        )

        facts = kg.query_entity("Alice", direction="outgoing")

        assert first != second
        assert sorted(f["id"] for f in facts) == sorted([first, second])

    def test_candidate_fallback_carries_triple_id_and_source_file(self, kg):
        outgoing = kg.add_triple("Alice Smith", "works_at", "Acme", source_file="jobs.md")
        incoming = kg.add_triple("Carol", "reports_to", "Alice Smith", source_file="org.md")

        out_facts = kg.query_entity("Smith", direction="outgoing")
        in_facts = kg.query_entity("Smith", direction="incoming")

        assert [(f["id"], f["source_file"]) for f in out_facts] == [(outgoing, "jobs.md")]
        assert [(f["id"], f["source_file"]) for f in in_facts] == [(incoming, "org.md")]

    def test_source_file_is_none_when_not_supplied(self, kg):
        tid = kg.add_triple("Alice", "works_at", "NewCo")

        (fact,) = kg.query_entity("Alice", direction="outgoing")

        assert fact["id"] == tid
        assert fact["source_file"] is None

    def test_fact_keeps_its_existing_keys_and_adds_id_and_source_file(self, kg):
        tid = kg.add_triple("Alice", "works_at", "NewCo", valid_from="2020-01-01", confidence=0.5)
        expected = {
            "id": tid,
            "direction": "outgoing",
            "subject": "Alice",
            "predicate": "works_at",
            "object": "NewCo",
            "valid_from": "2020-01-01",
            "valid_to": None,
            "confidence": 0.5,
            "source_closet": None,
            "source_file": None,
            "current": True,
        }

        (fact,) = kg.query_entity("Alice", direction="outgoing")

        assert fact.items() >= expected.items()


class TestTimelineAndRelationshipTripleIdentity:
    """timeline and query_relationship return the triple id add_triple returned."""

    def test_timeline_fact_carries_the_triple_id(self, kg):
        tid = kg.add_triple("Alice", "works_at", "NewCo", valid_from="2020-01-01")

        all_facts = kg.timeline()
        (fact,) = kg.timeline("Alice")

        assert [f["id"] for f in all_facts] == [tid]
        assert fact["id"] == tid

    def test_timeline_keeps_facts_sharing_a_tuple_distinct(self, kg):
        first = kg.add_triple("Alice", "works_at", "NewCo", valid_from="2020-01-01")
        kg.invalidate("Alice", "works_at", "NewCo", ended="2022-01-01")
        second = kg.add_triple("Alice", "works_at", "NewCo", valid_from="2024-01-01")

        facts = kg.timeline("Alice")

        assert [f["id"] for f in facts] == [first, second]

    def test_query_relationship_fact_carries_the_triple_id(self, kg):
        tid = kg.add_triple("Alice", "works_at", "NewCo", valid_from="2020-01-01")

        (fact,) = kg.query_relationship("works_at")

        assert fact["id"] == tid

    def test_query_relationship_keeps_facts_sharing_a_tuple_distinct(self, kg):
        first = kg.add_triple("Alice", "works_at", "NewCo", valid_from="2020-01-01")
        kg.invalidate("Alice", "works_at", "NewCo", ended="2022-01-01")
        second = kg.add_triple("Alice", "works_at", "NewCo", valid_from="2024-01-01")

        facts = kg.query_relationship("works_at")

        assert {f["valid_from"]: f["id"] for f in facts} == {
            "2020-01-01": first,
            "2024-01-01": second,
        }

    def test_timeline_and_relationship_facts_keep_their_keys_and_add_id(self, kg):
        tid = kg.add_triple("Alice", "works_at", "NewCo", valid_from="2020-01-01")
        expected = {
            "id": tid,
            "subject": "Alice",
            "predicate": "works_at",
            "object": "NewCo",
            "valid_from": "2020-01-01",
            "valid_to": None,
            "current": True,
        }

        (timeline_fact,) = kg.timeline()
        (relationship_fact,) = kg.query_relationship("works_at")

        assert timeline_fact.items() >= expected.items()
        assert relationship_fact.items() >= expected.items()


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
