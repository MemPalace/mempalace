"""
test_kg_extractor.py — Tests for auto-KG extraction from palace drawers.

Covers: pattern extraction from text, full pipeline with ChromaDB,
dry-run mode, wing/room filtering, deduplication, and edge cases.
"""

import os

from mempalace.kg_extractor import extract_from_text, extract_kg, ExtractionResult


# ── Pattern extraction from text ─────────────────────────────────────


class TestEmploymentExtraction:
    def test_works_at(self):
        triples = extract_from_text("Alice works at Acme Corp on the backend team.")
        assert any(
            t["subject"] == "Alice" and t["predicate"] == "works_at" and "Acme" in t["object"]
            for t in triples
        )

    def test_joined(self):
        triples = extract_from_text("Bob joined Google recently.")
        assert any(
            t["subject"] == "Bob" and t["predicate"] == "works_at" and "Google" in t["object"]
            for t in triples
        )

    def test_employed_by(self):
        triples = extract_from_text("Carol is employed by Microsoft.")
        assert any(
            t["subject"] == "Carol" and t["predicate"] == "works_at"
            for t in triples
        )


class TestRoleExtraction:
    def test_role_with_keyword(self):
        triples = extract_from_text("Alice is the lead engineer at Acme.")
        assert any(
            t["subject"] == "Alice" and t["predicate"] == "has_role" and "engineer" in t["object"]
            for t in triples
        )

    def test_ignores_non_role(self):
        """'Alice is a good friend' should NOT extract a role."""
        triples = extract_from_text("Alice is a good friend to everyone.")
        role_triples = [t for t in triples if t["predicate"] == "has_role"]
        assert len(role_triples) == 0

    def test_senior_developer(self):
        triples = extract_from_text("Bob is a senior developer for the platform team.")
        assert any(
            t["subject"] == "Bob" and t["predicate"] == "has_role"
            for t in triples
        )


class TestFamilyExtraction:
    def test_possessive_pattern(self):
        """'Alice's daughter Riley' → Alice parent_of Riley."""
        triples = extract_from_text("I spoke with Alice's daughter Riley today.")
        assert any(
            t["subject"] == "Alice" and t["predicate"] == "parent_of" and t["object"] == "Riley"
            for t in triples
        )

    def test_is_possessive_pattern(self):
        """'Riley is Alice's daughter' → Alice parent_of Riley."""
        triples = extract_from_text("Riley is Alice's daughter and she loves chess.")
        assert any(
            t["subject"] == "Alice" and t["predicate"] == "parent_of" and t["object"] == "Riley"
            for t in triples
        )

    def test_marriage(self):
        triples = extract_from_text("Dan is Carol's husband and works remotely.")
        assert any(
            t["predicate"] == "married_to"
            for t in triples
        )

    def test_pet(self):
        triples = extract_from_text("Alice's dog Buddy is very playful.")
        assert any(
            t["predicate"] == "is_pet_of"
            for t in triples
        )


class TestToolExtraction:
    def test_we_use(self):
        triples = extract_from_text("We use PostgreSQL for the main database.")
        assert any(
            t["predicate"] == "uses" and t["object"] == "PostgreSQL"
            for t in triples
        )

    def test_switched_to(self):
        triples = extract_from_text("We switched to GraphQL for the API layer.")
        assert any(
            t["predicate"] == "uses" and t["object"] == "GraphQL"
            for t in triples
        )

    def test_decided_to_use(self):
        triples = extract_from_text("The team decided to use Redis for caching.")
        assert any(
            t["predicate"] == "uses" and t["object"] == "Redis"
            for t in triples
        )


class TestCreationExtraction:
    def test_created(self):
        triples = extract_from_text("Alice created the authentication module.")
        assert any(
            t["subject"] == "Alice" and t["predicate"] == "created"
            and "authentication" in t["object"]
            for t in triples
        )

    def test_built(self):
        triples = extract_from_text("Bob built the deployment pipeline last month.")
        assert any(
            t["subject"] == "Bob" and t["predicate"] == "created"
            for t in triples
        )


class TestInterestExtraction:
    def test_loves(self):
        triples = extract_from_text("Max loves chess and plays every weekend.")
        assert any(
            t["subject"] == "Max" and t["predicate"] == "loves"
            for t in triples
        )

    def test_enjoys(self):
        triples = extract_from_text("Riley enjoys swimming at the local pool.")
        assert any(
            t["subject"] == "Riley" and t["predicate"] == "loves"
            for t in triples
        )


# ── Edge cases ───────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_text(self):
        triples = extract_from_text("")
        assert triples == []

    def test_no_entities(self):
        triples = extract_from_text("the quick brown fox jumps over the lazy dog")
        assert triples == []

    def test_skip_common_words(self):
        """Common words like 'The' should not be extracted as entity names."""
        triples = extract_from_text("The system is very important for everyone.")
        assert all(t["subject"] != "The" for t in triples)

    def test_multiple_patterns_same_text(self):
        """Text with multiple patterns should extract all."""
        text = (
            "Alice works at Acme Corp. She is the lead engineer. "
            "Alice's daughter Riley loves swimming. We use PostgreSQL."
        )
        triples = extract_from_text(text)
        predicates = {t["predicate"] for t in triples}
        assert "works_at" in predicates
        assert "has_role" in predicates
        assert "parent_of" in predicates
        assert "uses" in predicates

    def test_source_file_preserved(self):
        triples = extract_from_text("Alice works at Acme.", source_file="notes.md")
        assert triples[0]["source"] == "notes.md"

    def test_long_unpunctuated_line(self):
        """Creation pattern on a long line without punctuation should truncate cleanly."""
        long_obj = "the " + "very " * 20 + "important authentication module"
        text = f"Alice created {long_obj}"
        triples = extract_from_text(text)
        created = [t for t in triples if t["predicate"] == "created"]
        if created:
            assert len(created[0]["object"]) <= 60

    def test_entity_types_inferred(self):
        """Employment triples should have subject_type and object_type."""
        triples = extract_from_text("Alice works at Acme Corp.")
        emp = [t for t in triples if t["predicate"] == "works_at"]
        assert emp[0]["subject_type"] == "person"
        assert emp[0]["object_type"] == "company"


# ── Full pipeline with ChromaDB ──────────────────────────────────────


class TestFullPipeline:
    def test_extract_from_seeded_palace(self, palace_path, seeded_collection, kg):
        """Extract KG from the seeded test palace."""
        result = extract_kg(palace_path=palace_path, kg=kg)
        assert isinstance(result, ExtractionResult)
        assert result.drawers_scanned == 4  # seeded_collection has 4 drawers
        assert result.errors == []

    def test_dry_run_does_not_write(self, palace_path, seeded_collection, kg):
        """Dry run should not add triples to the KG."""
        before = kg.stats()
        result = extract_kg(palace_path=palace_path, kg=kg, dry_run=True)
        after = kg.stats()
        assert before["triples"] == after["triples"]
        # But should still report what it found
        assert result.drawers_scanned == 4

    def test_wing_filter(self, palace_path, seeded_collection, kg):
        """Filtering by wing should only scan matching drawers."""
        result = extract_kg(palace_path=palace_path, kg=kg, wing="notes")
        assert result.drawers_scanned == 1  # only "notes" wing has 1 drawer

    def test_room_filter(self, palace_path, seeded_collection, kg):
        """Filtering by room should only scan matching drawers."""
        result = extract_kg(palace_path=palace_path, kg=kg, room="backend")
        assert result.drawers_scanned == 2  # 2 drawers in "backend" room

    def test_idempotent(self, palace_path, seeded_collection, kg):
        """Running extraction twice should not duplicate triples."""
        result1 = extract_kg(palace_path=palace_path, kg=kg)
        result2 = extract_kg(palace_path=palace_path, kg=kg)
        assert result2.triples_skipped >= result1.triples_added or result2.triples_added == 0

    def test_no_palace_returns_error(self, tmp_dir, kg):
        """Missing palace should return error in result."""
        result = extract_kg(palace_path=os.path.join(tmp_dir, "missing"), kg=kg)
        assert len(result.errors) > 0
        assert result.drawers_scanned == 0


class TestExtractionResultSerialization:
    def test_to_dict(self):
        result = ExtractionResult(
            drawers_scanned=10,
            entities_found=5,
            triples_added=3,
            triples_skipped=1,
            patterns_matched=4,
        )
        d = result.to_dict()
        assert d["drawers_scanned"] == 10
        assert d["entities_found"] == 5
        assert d["triples_added"] == 3
        assert isinstance(d["sample_triples"], list)
        assert isinstance(d["errors"], list)
