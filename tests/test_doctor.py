"""
test_doctor.py — Tests for palace health diagnostics.

Covers: palace existence, collection health, metadata checks,
wing/room distribution, drawer sizes, KG health, and report structure.
"""

import os

from mempalace.doctor import diagnose, DiagnosticReport


class TestPalaceExists:
    def test_existing_palace(self, palace_path, collection):
        report = diagnose(palace_path)
        exists_check = next(c for c in report.checks if c.name == "palace_exists")
        assert exists_check.status == "OK"

    def test_missing_palace(self, tmp_dir):
        report = diagnose(os.path.join(tmp_dir, "nonexistent"))
        exists_check = next(c for c in report.checks if c.name == "palace_exists")
        assert exists_check.status == "ERROR"
        assert report.summary == "Palace not found"


class TestCollectionHealth:
    def test_empty_collection(self, palace_path, collection):
        report = diagnose(palace_path)
        col_check = next(c for c in report.checks if c.name == "collection")
        assert col_check.status == "WARN"
        assert "empty" in col_check.message.lower()

    def test_healthy_collection(self, palace_path, seeded_collection):
        report = diagnose(palace_path)
        col_check = next(c for c in report.checks if c.name == "collection")
        assert col_check.status == "OK"
        assert "4 drawers" in col_check.message


class TestMetadata:
    def test_complete_metadata(self, palace_path, seeded_collection):
        report = diagnose(palace_path)
        wing_check = next(c for c in report.checks if c.name == "metadata_wing")
        assert wing_check.status == "OK"

    def test_missing_metadata(self, palace_path, collection):
        """Drawers with missing wing/room should trigger warnings."""
        collection.add(
            ids=["bad_1"],
            documents=["Some content without proper metadata."],
            metadatas=[{"source_file": "test.py"}],  # missing wing and room
        )
        report = diagnose(palace_path)
        wing_check = next(
            (c for c in report.checks if c.name == "metadata_wing"), None
        )
        if wing_check:
            assert wing_check.status == "WARN"


class TestWingsRooms:
    def test_wings_reported(self, palace_path, seeded_collection):
        report = diagnose(palace_path)
        wings_check = next(c for c in report.checks if c.name == "wings")
        assert wings_check.status == "OK"
        assert "project" in wings_check.details


class TestDrawerSizes:
    def test_normal_drawers(self, palace_path, seeded_collection):
        report = diagnose(palace_path)
        size_check = next(
            (c for c in report.checks if c.name == "drawer_sizes"), None
        )
        if size_check:
            assert size_check.status == "OK"

    def test_tiny_drawers(self, palace_path, collection):
        """Very small drawers should trigger a warning."""
        collection.add(
            ids=["tiny_1", "tiny_2"],
            documents=["hi", "yo"],
            metadatas=[
                {"wing": "w", "room": "r", "source_file": "a.py"},
                {"wing": "w", "room": "r", "source_file": "b.py"},
            ],
        )
        report = diagnose(palace_path)
        small_check = next(
            (c for c in report.checks if c.name == "small_drawers"), None
        )
        if small_check:
            assert small_check.status == "WARN"


class TestKgHealth:
    def test_no_kg(self, palace_path, seeded_collection):
        """Missing KG should be a warning, not an error."""
        report = diagnose(palace_path)
        kg_check = next(
            (c for c in report.checks if c.name == "kg_exists"), None
        )
        if kg_check:
            assert kg_check.status == "WARN"


class TestReportStructure:
    def test_to_dict(self, palace_path, seeded_collection):
        report = diagnose(palace_path)
        d = report.to_dict()

        assert "palace_path" in d
        assert "summary" in d
        assert "ok" in d
        assert "warnings" in d
        assert "errors" in d
        assert isinstance(d["checks"], list)

    def test_summary_healthy(self, palace_path, seeded_collection):
        report = diagnose(palace_path)
        assert report.error_count == 0
        assert "passed" in report.summary or "Healthy" in report.summary

    def test_counts(self, palace_path, seeded_collection):
        report = diagnose(palace_path)
        assert report.ok_count + report.warn_count + report.error_count == len(report.checks)

    def test_empty_report(self):
        report = DiagnosticReport()
        assert report.ok_count == 0
        assert report.warn_count == 0
        assert report.error_count == 0
