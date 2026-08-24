"""Tests for write-path predicate normalisation (2026-08-24).

Background: the live KG accumulated ~236 distinct predicates because the LLM
extractor invents synonyms freely. Downstream enumerations (boot people
snapshot) then miss facts silently. These tests pin the normalise-at-write
behaviour: synonyms collapse, unknowns are stored verbatim but logged.
"""

import os
import sqlite3

import pytest

from mempalace.knowledge_graph import (
    KnowledgeGraph,
    normalize_predicate,
    PREDICATE_SYNONYMS,
    UNKNOWN_PREDICATE_LOG_ENV,
    _KNOWN_PREDICATES,
)


@pytest.fixture()
def kg(tmp_path, monkeypatch):
    log_path = tmp_path / "unknown_predicates.log"
    monkeypatch.setenv(UNKNOWN_PREDICATE_LOG_ENV, str(log_path))
    graph = KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3"))
    yield graph, log_path
    graph.close()


class TestNormalizePredicate:
    def test_basic_shape_normalisation(self):
        assert normalize_predicate("Married To") == "married_to"
        assert normalize_predicate("  WORKS_AT ") == "works_at"

    def test_synonym_collapses_to_canonical(self):
        for synonym, canonical in [
            ("dob", "date_of_birth"),
            ("born_on", "date_of_birth"),
            ("lives_in", "place_of_residence"),
            ("is_wife_of", "married_to"),
            ("mother_is", "has_mother"),
        ]:
            assert normalize_predicate(synonym) == canonical, synonym

    def test_unknown_predicate_passes_through(self):
        assert normalize_predicate("quantum_entangled_with") == "quantum_entangled_with"

    def test_every_synonym_target_is_known(self):
        assert set(PREDICATE_SYNONYMS.values()) <= _KNOWN_PREDICATES


class TestAddTripleNormalisation:
    def test_synonym_stored_as_canonical(self, kg):
        graph, _ = kg
        graph.add_triple("Max", "dob", "2015-04-01")
        row = graph.query_entity("Max")[0]
        assert row["predicate"] == "date_of_birth"

    def test_invalidate_matches_normalised_write(self, kg):
        """A fact written via a synonym must be invalidatable by either form."""
        graph, _ = kg
        graph.add_triple("Max", "lives_in", "Panipat")
        # invalidate with the SYNONYM — must find the canonically-stored triple
        graph.invalidate("Max", "resides_in", "Panipat")
        facts = [f for f in graph.query_entity("Max") if f.get("current") is not False]
        assert not any(
            f["predicate"] == "place_of_residence" and f["object"] == "panipat"
            for f in facts
        )

    def test_query_relationship_uses_normalised_form(self, kg):
        graph, _ = kg
        graph.add_triple("Alice", "works_at", "Acme")
        results = graph.query_relationship("employed_by")
        assert len(results) == 1
        assert results[0]["object"] == "Acme"

    def test_unknown_predicate_stored_verbatim_and_logged(self, kg):
        graph, log_path = kg
        graph.add_triple("Bob", "quantum_entangled_with", "Eve")
        facts = graph.query_entity("Bob")
        assert any(f["predicate"] == "quantum_entangled_with" for f in facts)
        assert log_path.exists(), "unknown predicate must be logged"
        assert "quantum_entangled_with" in log_path.read_text(encoding="utf-8")

    def test_unknown_predicate_logged_once_only(self, kg):
        graph, log_path = kg
        graph.add_triple("Bob", "quantum_entangled_with", "Eve")
        graph.add_triple("Carol", "quantum_entangled_with", "Dave")
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1, "same unknown predicate must not repeat in log"

    def test_known_vocabulary_not_logged(self, kg):
        graph, log_path = kg
        graph.add_triple("X", "married_to", "Y")
        graph.add_triple("X2", "decided", "something")
        assert not log_path.exists() or log_path.read_text() == ""

    def test_type_error_guard_rejects_date_object_for_person_predicate(self, kg):
        """The live DB contains 'hunnys_brother --married--> 2021-04-01'. A date
        value on a relationship predicate is a type error; add_triple should
        reject it rather than store a corrupted edge."""
        graph, _ = kg
        with pytest.raises(ValueError):
            graph.add_triple("hunnys_brother", "married", "2021-04-01")


class TestBackwardCompat:
    def test_existing_exact_triple_dedup_still_works(self, kg):
        graph, _ = kg
        id1 = graph.add_triple("Max", "child_of", "Alice")
        id2 = graph.add_triple("Max", "child_of", "Alice")
        assert id1 == id2

    def test_temporal_fields_preserved(self, kg):
        graph, _ = kg
        graph.add_triple("Max", "does", "swimming", valid_from="2025-01-01")
        facts = graph.query_entity("Max", as_of="2025-06-01")
        assert any(f["predicate"] == "does" for f in facts)
