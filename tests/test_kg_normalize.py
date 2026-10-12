"""Tests for mempalace.kg_normalize — closed predicate vocabulary with history kept."""

import json
from argparse import Namespace

import pytest

from mempalace.config import MempalaceConfig
from mempalace.kg_normalize import (
    DEFAULT_VOCABULARY,
    apply_normalize,
    load_normalize_plan,
    off_vocabulary_facts,
    plan_normalize,
    save_normalize_plan,
)
from tests.test_rooms import FakeProvider


def _seed(kg):
    kg.add_triple("weatherstation", "deployed_commit", "fc81c6f6", valid_from="2026-08-12")
    kg.add_triple("AcmeApp", "appearance_default", "dark", valid_from="2026-08-19")
    kg.add_triple("Igor", "works_on", "mempalace", valid_from="2026-01-01")
    kg.add_triple("old", "gone_predicate", "x", valid_from="2026-01-01", valid_to="2026-02-01")


def test_off_vocabulary_facts_lists_only_open_foreign_predicates(kg):
    _seed(kg)
    facts = off_vocabulary_facts(kg, DEFAULT_VOCABULARY)
    assert [(f["subject"], f["predicate"]) for f in facts] == [
        ("weatherstation", "deployed_commit"),
        ("AcmeApp", "appearance_default"),
    ]


def test_plan_normalize_keeps_vocabulary_rows_and_rejects_others(kg):
    _seed(kg)
    facts = off_vocabulary_facts(kg, DEFAULT_VOCABULARY)
    provider = FakeProvider(
        '{"facts": [{"index": 1, "predicate": "status", "object": "deployed_commit fc81c6f6"}, '
        '{"index": 2, "predicate": "invented", "object": "dark"}]}'
    )
    plan = plan_normalize(facts, provider)
    assert plan["rejected"] == 1
    assert plan["facts"][0]["object"] == "deployed commit fc81c6f6"  # pasted predicate humanized
    assert plan["facts"] == [
        {
            "id": facts[0]["id"],
            "subject": "weatherstation",
            "old_predicate": "deployed_commit",
            "old_object": "fc81c6f6",
            "predicate": "status",
            "object": "deployed commit fc81c6f6",
        }
    ]
    assert "ALLOWED predicates" in provider.calls[0][1]
    assert plan_normalize([], provider)["facts"] == []


def test_apply_normalize_supersedes_at_one_boundary_and_keeps_history(kg):
    _seed(kg)
    facts = off_vocabulary_facts(kg, DEFAULT_VOCABULARY)
    plan = plan_normalize(
        facts,
        FakeProvider(
            '{"facts": [{"index": 1, "predicate": "status", "object": "deployed commit fc81c6f6"}]}'
        ),
    )
    result = apply_normalize(kg, plan, at="2026-09-21T00:00:00Z")
    assert result["applied"] == 1
    now = {
        (t["predicate"], t["object"])
        for t in kg.query_entity("weatherstation", as_of="2026-09-22T00:00:00Z")
    }
    assert ("status", "deployed commit fc81c6f6") in now
    assert ("deployed_commit", "fc81c6f6") not in now
    before = {
        (t["predicate"], t["object"]) for t in kg.query_entity("weatherstation", as_of="2026-08-20")
    }
    assert ("deployed_commit", "fc81c6f6") in before
    assert off_vocabulary_facts(kg, DEFAULT_VOCABULARY)[0]["predicate"] == "appearance_default"


def test_apply_normalize_leaves_a_fact_closed_since_planning_alone(kg):
    _seed(kg)
    facts = off_vocabulary_facts(kg, DEFAULT_VOCABULARY)
    plan = plan_normalize(
        facts,
        FakeProvider(
            '{"facts": [{"index": 1, "predicate": "status", "object": "deployed commit fc81c6f6"}]}'
        ),
    )
    # Someone closed the fact while the plan sat under review.
    kg.invalidate("weatherstation", "deployed_commit", "fc81c6f6", ended="2026-09-20T00:00:00Z")
    result = apply_normalize(kg, plan, at="2026-09-21T00:00:00Z")
    assert result == {"applied": 0, "skipped": 0, "stale": 1, "boundary": "2026-09-21T00:00:00Z"}
    now = {(t["predicate"], t["object"]) for t in kg.query_entity("weatherstation")}
    assert ("status", "deployed commit fc81c6f6") not in now  # not resurrected


def test_kg_rewrite_keeps_confidence_and_provenance(kg):
    kg.add_triple(
        "portal",
        "deployed_commit",
        "abc123",
        valid_from="2026-08-01",
        confidence=0.6,
        source_file="notes.md",
        source_drawer_id="d42",
        source_closet="c7",
        adapter_name="convos",
    )
    fact = [f for f in off_vocabulary_facts(kg, DEFAULT_VOCABULARY) if f["subject"] == "portal"][0]
    new_id = kg.rewrite(fact["id"], "status", "deployed commit abc123", at="2026-09-21T00:00:00Z")
    row = kg._conn().execute("SELECT * FROM triples WHERE id=?", (new_id,)).fetchone()
    assert row["confidence"] == 0.6
    assert row["source_file"] == "notes.md"  # still points at the evidence
    assert row["source_drawer_id"] == "d42"
    assert row["source_closet"] == "c7"
    assert row["adapter_name"] == "convos"


def test_kg_rewrite_is_one_transaction(kg, monkeypatch):
    _seed(kg)
    fact = off_vocabulary_facts(kg, DEFAULT_VOCABULARY)[0]
    real_conn = kg._conn

    class Boom:
        def __init__(self, conn):
            self._c = conn

        def __enter__(self):
            return self._c.__enter__()

        def __exit__(self, *a):
            return self._c.__exit__(*a)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("INSERT INTO triples"):
                raise RuntimeError("disk full")
            return self._c.execute(sql, *args)

    monkeypatch.setattr(kg, "_conn", lambda: Boom(real_conn()))
    with pytest.raises(RuntimeError):
        kg.rewrite(fact["id"], "status", "x", at="2026-09-21T00:00:00Z")
    monkeypatch.setattr(kg, "_conn", real_conn)
    # The close was rolled back with the failed insert: the fact is still open.
    assert [f["id"] for f in off_vocabulary_facts(kg, DEFAULT_VOCABULARY)][0] == fact["id"]


def test_plan_round_trip_and_validation(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    plan = {
        "vocabulary": list(DEFAULT_VOCABULARY),
        "facts": [
            {
                "id": "t",
                "subject": "s",
                "old_predicate": "p",
                "old_object": "o",
                "predicate": "uses",
                "object": "n",
            }
        ],
    }
    save_normalize_plan(cfg, plan)
    assert load_normalize_plan(cfg)["facts"][0]["predicate"] == "uses"
    path = tmp_path / "kg" / "normalize.json"
    bad = json.loads(path.read_text())
    bad["facts"][0]["predicate"] = "invented"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        load_normalize_plan(cfg)


def test_cmd_kg_normalize_plan_then_apply(tmp_path, kg, monkeypatch, capsys):
    import mempalace.cli as cli

    _seed(kg)
    palace = tmp_path / "palace"
    palace.mkdir()
    monkeypatch.setattr(
        "mempalace.palace_audit.resolve_kg_path", lambda p, explicit=False: kg.db_path
    )
    monkeypatch.setattr("mempalace.knowledge_graph.KnowledgeGraph", lambda db_path=None: kg)
    provider = FakeProvider(
        '{"facts": [{"index": 1, "predicate": "status", "object": "deployed commit fc81c6f6"}, '
        '{"index": 2, "predicate": "uses", "object": "dark appearance by default"}]}'
    )
    monkeypatch.setattr(cli, "get_provider", lambda **kw: provider)
    ns = dict(
        kg_action="normalize",
        palace=str(palace),
        vocabulary=None,
        llm_provider="ollama",
        llm_model="m",
        llm_endpoint=None,
        llm_api_key=None,
        accept_external_llm=False,
        sample=0,
    )
    cli.cmd_kg(Namespace(yes=False, **ns))
    out = capsys.readouterr().out
    assert "2 open facts use a predicate outside" in out and "Plan saved" in out
    cli.cmd_kg(Namespace(yes=True, **ns))
    assert "Rewrote 2 fact(s)" in capsys.readouterr().out
    assert off_vocabulary_facts(kg, DEFAULT_VOCABULARY) == []


def test_main_dispatches_kg(monkeypatch):
    import mempalace.cli as cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_kg", lambda args: seen.update(vars(args)))
    monkeypatch.setattr(
        "sys.argv", ["mempalace", "kg", "normalize", "--vocabulary", "uses,owns", "--yes"]
    )
    cli.main()
    assert seen["kg_action"] == "normalize" and seen["vocabulary"] == "uses,owns" and seen["yes"]
