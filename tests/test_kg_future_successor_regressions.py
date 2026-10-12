"""KG rewrite regressions for PR #2654 review finding F4."""

import json
import subprocess
import sys

import pytest

from mempalace.knowledge_graph import KnowledgeGraph
from mempalace.kg_normalize import apply_normalize


def _fresh_json(code, *args):
    return json.loads(
        subprocess.check_output([sys.executable, "-B", "-c", code, *map(str, args)], text=True)
    )


@pytest.mark.parametrize("same_provenance", [False, True])
def test_normalize_future_successor_preserves_current_interval_and_provenance(
    tmp_path, same_provenance
):
    db = tmp_path / "knowledge_graph.sqlite3"
    with KnowledgeGraph(db_path=str(db)) as kg:
        old = kg.add_triple(
            "project",
            "planned_status",
            "release ready",
            valid_from="2026-01-01",
            source_drawer_id="original-evidence",
            source_file="original.md",
            confidence=0.6,
        )
        future = kg.add_triple(
            "project",
            "status",
            "release ready",
            valid_from="2027-01-01",
            source_drawer_id="original-evidence" if same_provenance else "future-evidence",
            source_file="original.md" if same_provenance else "future.md",
            confidence=0.6 if same_provenance else 0.9,
        )
        plan = {
            "facts": [
                {
                    "id": old,
                    "old_predicate": "planned_status",
                    "old_object": "release ready",
                    "predicate": "status",
                    "object": "release ready",
                }
            ]
        }
        assert apply_normalize(kg, plan, at="2026-10-02T00:00:00Z")["applied"] == 1
        assert apply_normalize(kg, plan, at="2026-10-02T00:00:00Z")["stale"] == 1
    result = _fresh_json(
        """
import json, sys
from mempalace.knowledge_graph import KnowledgeGraph
with KnowledgeGraph(db_path=sys.argv[1]) as kg:
    current = kg.query_entity("project", as_of="2026-10-03T00:00:00Z")
    before = kg.query_entity("project", as_of="2026-10-01T00:00:00Z")
    rows = [dict(r) for r in kg._conn().execute("SELECT * FROM triples")]
print(json.dumps({"current": current, "before": before, "rows": rows}))
""",
        db,
    )
    assert result["current"], "normalization left a validity gap before the future successor"
    assert result["current"][0]["predicate"] == "status"
    assert result["before"][0]["predicate"] == "planned_status"
    rows = {r["id"]: r for r in result["rows"]}
    successor = next(r for i, r in rows.items() if i not in {old, future})
    assert successor["valid_from"] == rows[old]["valid_to"] == "2026-10-02T00:00:00Z"
    assert successor["source_drawer_id"] == "original-evidence"
    assert successor["source_file"] == "original.md"
    assert successor["confidence"] == 0.6
    assert rows[future]["valid_from"] == "2027-01-01"
    assert rows[future]["source_drawer_id"] == (
        "original-evidence" if same_provenance else "future-evidence"
    )


@pytest.mark.parametrize("starts", [None, "2026-10-02", "2026-01-01"])
def test_rewrite_reuses_successor_covering_boundary_with_same_provenance(tmp_path, starts):
    with KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")) as kg:
        old = kg.add_triple(
            "project",
            "planned_status",
            "ready",
            valid_from="2026-01-01",
            source_drawer_id="shared-evidence",
        )
        existing = kg.add_triple(
            "project", "status", "ready", valid_from=starts, source_drawer_id="shared-evidence"
        )
        assert kg.rewrite(old, "status", "ready", at="2026-10-02") == existing
        assert kg._conn().execute("SELECT COUNT(*) FROM triples").fetchone()[0] == 2
        assert len(kg.query_entity("project", as_of="2026-10-02T00:00:00Z")) == 1


def test_rewrite_does_not_replace_original_provenance_with_another_witness(tmp_path):
    with KnowledgeGraph(db_path=str(tmp_path / "kg.sqlite3")) as kg:
        old = kg.add_triple(
            "project",
            "planned_status",
            "ready",
            valid_from="2026-01-01",
            source_drawer_id="original-evidence",
            confidence=0.7,
        )
        other = kg.add_triple(
            "project", "status", "ready", valid_from="2026-01-01", source_drawer_id="other-evidence"
        )
        new = kg.rewrite(old, "status", "ready", at="2026-10-02")
        assert new != other
        row = kg._conn().execute("SELECT * FROM triples WHERE id=?", (new,)).fetchone()
        assert row["source_drawer_id"] == "original-evidence"
        assert row["confidence"] == 0.7
        assert (
            kg._conn()
            .execute("SELECT source_drawer_id FROM triples WHERE id=?", (other,))
            .fetchone()[0]
            == "other-evidence"
        )
