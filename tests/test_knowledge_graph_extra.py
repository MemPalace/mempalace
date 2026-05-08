"""Extra knowledge graph tests for seed_from_entity_facts and query_relationship."""

import pytest

from mempalace.knowledge_graph import KnowledgeGraph


@pytest.fixture
def kg(tmp_path):
    return KnowledgeGraph(db_path=str(tmp_path / "kg.db"))


class TestSeedFromEntityFacts:
    def test_seed_person_with_partner(self, kg):
        facts = {
            "alice": {
                "full_name": "Alice Smith",
                "type": "person",
                "gender": "female",
                "partner": "bob",
                "relationship": "husband",
            }
        }
        kg.seed_from_entity_facts(facts)
        stats = kg.stats()
        assert stats["entities"] >= 1
        results = kg.query_entity("Alice Smith", direction="outgoing")
        predicates = {r["predicate"] for r in results}
        assert "married_to" in predicates
        assert "is_partner_of" in predicates

    def test_seed_child(self, kg):
        facts = {
            "max": {
                "full_name": "Max",
                "type": "person",
                "birthday": "2015-04-01",
                "parent": "alice",
                "relationship": "daughter",
            }
        }
        kg.seed_from_entity_facts(facts)
        results = kg.query_entity("Max", direction="outgoing")
        predicates = {r["predicate"] for r in results}
        assert "child_of" in predicates
        assert "is_child_of" in predicates

    def test_seed_sibling(self, kg):
        facts = {
            "emma": {
                "full_name": "Emma",
                "type": "person",
                "relationship": "brother",
                "sibling": "max",
            }
        }
        kg.seed_from_entity_facts(facts)
        results = kg.query_entity("Emma", direction="outgoing")
        predicates = {r["predicate"] for r in results}
        assert "is_sibling_of" in predicates

    def test_seed_dog(self, kg):
        facts = {
            "rex": {
                "full_name": "Rex",
                "type": "animal",
                "relationship": "dog",
                "owner": "alice",
            }
        }
        kg.seed_from_entity_facts(facts)
        results = kg.query_entity("Rex", direction="outgoing")
        predicates = {r["predicate"] for r in results}
        assert "is_pet_of" in predicates

    def test_seed_with_interests(self, kg):
        facts = {
            "max": {
                "full_name": "Max",
                "type": "person",
                "interests": ["swimming", "chess"],
            }
        }
        kg.seed_from_entity_facts(facts)
        results = kg.query_entity("Max", direction="outgoing")
        objects = {r["object"] for r in results if r["predicate"] == "loves"}
        assert "Swimming" in objects
        assert "Chess" in objects

    def test_seed_minimal_facts(self, kg):
        """Facts with no relationships just create entities."""
        facts = {"bob": {"full_name": "Bob"}}
        kg.seed_from_entity_facts(facts)
        stats = kg.stats()
        assert stats["entities"] >= 1


class TestQueryRelationshipWithTime:
    def test_query_relationship_with_as_of(self, kg):
        kg.add_triple("Alice", "works_at", "Acme", valid_from="2020-01-01", valid_to="2024-12-31")
        kg.add_triple("Alice", "works_at", "NewCo", valid_from="2025-01-01")
        results = kg.query_relationship("works_at", as_of="2023-06-01")
        objects = [r["object"] for r in results]
        assert "Acme" in objects
        assert "NewCo" not in objects
