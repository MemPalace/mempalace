"""Regression cases for tunnel aliases, proposal application and audit/prune agreement.

Contract: explicit different source extensions and incompatible qualified paths
are distinct. A short alias is insufficient evidence to suppress either host.
Existing ambiguous rows may be retained conservatively. Unique aliases and exact
reversed endpoints still deduplicate. Audit and prune must agree on duplicates.
"""

from argparse import Namespace
from itertools import permutations

import pytest

from mempalace import hallways as hw
from mempalace import palace_graph as pg
from mempalace import palace_audit as audit
from mempalace.config import MempalaceConfig
from mempalace.tunnels_tool import (
    apply_proposal,
    load_proposal,
    propose_tunnels,
    prune_tunnels,
    save_proposal,
)


CASES = [
    ("CodeRouter.py", "src/models/CodeRouter.py", "tests/fixtures/CodeRouter.py"),
    ("models/CodeRouter.py", "src/models/CodeRouter.py", "tests/models/CodeRouter.py"),
    ("CodeRouter", "src/CodeRouter.c", "src/CodeRouter.h"),
    ("src/CodeRouter", "src/CodeRouter.cpp", "src/CodeRouter.hpp"),
    ("CodeRouter", "frontend/CodeRouter.ts", "frontend/CodeRouter.tsx"),
]


def row(entity, count=10, reverse=False):
    return {
        "id": entity,
        "kind": "entity",
        "access_count": count,
        "source": {"wing": "beta" if reverse else "alpha", "room": "entity:" + entity},
        "target": {"wing": "alpha" if reverse else "beta", "room": "entity:" + entity},
    }


def halls(names):
    return [
        {
            "wing": w,
            "entity_a": n,
            "entity_b": "AlphaPeer" if w == "alpha" else "BetaPeer",
            "co_occurrence_count": 20,
        }
        for w in ("alpha", "beta")
        for n in names
    ]


def cfg_at(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    return MempalaceConfig(palace_path=str(palace))


@pytest.mark.parametrize("names", CASES)
def test_proposal_qualified_hosts_survive_existing_ambiguous_alias(names):
    bare, *qualified = names
    candidate_keys = set(pg.entity_tunnel_candidates(halls(qualified)))
    assert candidate_keys == set(qualified), candidate_keys
    plan = propose_tunnels(halls(qualified), {"alpha", "beta"}, existing_tunnels=[row(bare)])
    assert {r["entity"] for r in plan["tunnels"]} == set(qualified), plan


@pytest.mark.parametrize("names", CASES)
def test_approved_proposal_survives_ambiguous_alias_created_during_review(names, tmp_path):
    bare, *qualified = names
    cfg = cfg_at(tmp_path)
    plan = {"tunnels": [{"entity": n, "wing_a": "alpha", "wing_b": "beta"} for n in qualified]}
    save_proposal(cfg, plan)
    pg.create_tunnel("alpha", "entity:" + bare, "beta", "entity:" + bare, kind="entity", config=cfg)
    count = apply_proposal(load_proposal(cfg), cfg)
    stored = pg._load_tunnels(cfg)
    assert count == 2, {"created": count, "persisted": stored}
    assert {r["source"]["room"] for r in stored} == {"entity:" + n for n in names}
    assert apply_proposal(plan, cfg) == 0


@pytest.mark.parametrize("names", CASES)
def test_audit_agrees_with_fixed_prune(names):
    for ordering in permutations(names):
        records = [row(n, 20 - i) for i, n in enumerate(ordering)]
        kept, pruned = prune_tunnels(records, {"alpha", "beta"})
        analysis = audit._analyze_tunnels(records, wings={"alpha", "beta"})
        assert {r["id"] for r in kept} == set(names), {"order": ordering, "prune": pruned}
        assert analysis["duplicates"] == pruned["duplicates"] == 0, {
            "order": ordering,
            "audit": analysis,
            "prune": pruned,
        }


@pytest.mark.parametrize("names", CASES)
def test_prune_preserves_distinct_hosts_and_removes_exact_reversed_duplicate(names):
    for ordering in permutations(names):
        records = [row(n, 20 - i) for i, n in enumerate(ordering)]
        records.append({**row(names[1], 0, reverse=True), "id": "exact-duplicate"})
        kept, report = prune_tunnels(records, {"alpha", "beta"})
        assert {r["id"] for r in kept} == set(names), report
        assert report["duplicates"] == report["removed"] == 1
        assert prune_tunnels(kept, {"alpha", "beta"})[1]["removed"] == 0


@pytest.mark.parametrize(
    "names",
    [
        ("CodeRouter.py", "models/CodeRouter.py", "/repo/src/models/CodeRouter.py"),
        ("CodeRouter", "src/CodeRouter", "/repo/src/CodeRouter.py"),
        ("CodeRouter.py", "a/src/CodeRouter.py", "b/src/CodeRouter.py"),
    ],
)
def test_unambiguous_chain_still_deduplicates(names):
    for ordering in permutations(names):
        records = [row(n, 20 - i) for i, n in enumerate(ordering)]
        kept, report = prune_tunnels(records, {"alpha", "beta"})
        assert len(kept) == 1, {"names": ordering, "report": report}
        assert report["duplicates"] == 2
        assert audit._analyze_tunnels(records, wings={"alpha", "beta"})["duplicates"] == 2


@pytest.mark.parametrize("exts", [("c", "h"), ("cpp", "hpp"), ("ts", "tsx")])
def test_hallway_explicit_extensions_survive_all_paths(exts, tmp_path):
    cfg = cfg_at(tmp_path)
    names = [f"src/CodeRouter.{e}" for e in exts]
    assert not hw.same_file_spelling(*names)
    assert set(hw.canonical_entities([*names, "CodeRouter"])) == set(names)
    for reverse in (False, True):
        raw = list(reversed(names)) if reverse else names
        metadata = [
            {"wing": "alpha", "room": "r", "entities": ";".join(raw)},
            {"wing": "alpha", "room": "r", "entities": f"CodeRouter;{names[0]};OtherPeer"},
            {"wing": "alpha", "room": "r", "entities": f"{names[0]};OtherPeer"},
        ]

        class Collection:
            def get(self, limit, offset, **kwargs):
                return {"metadatas": metadata[offset : offset + limit]}

        rebuilt = hw.compute_hallways_for_wing("alpha", col=Collection(), min_count=1, config=cfg)
        pairs = {
            tuple(sorted((h["entity_a"], h["entity_b"]))): h["co_occurrence_count"] for h in rebuilt
        }
        assert pairs[tuple(sorted(names))] == 1, pairs
        assert pairs[tuple(sorted((names[0], "OtherPeer")))] == 2, pairs
        assert audit._analyze_hallways(rebuilt)["self_links"] == 0
        assert hw.prune_spelling_hallways(config=cfg, apply=True)["removed"] == 0
        assert hw.list_hallways(config=cfg) == rebuilt


def test_crossed_endpoints_and_normalized_wings_are_preserved():
    names = CASES[0]
    rows = [row(names[0])]
    for left, right in ((names[1], names[2]), (names[2], names[1])):
        rows.append(
            {
                "id": left,
                "source": {"wing": "alpha", "room": "entity:" + left},
                "target": {"wing": "beta", "room": "entity:" + right},
            }
        )
    kept, report = prune_tunnels(rows, {"alpha", "beta"})
    assert len(kept) == 3, report


@pytest.mark.parametrize("names", CASES)
def test_automatic_entity_tunnel_builder_preserves_qualified_hosts(names, tmp_path):
    cfg = cfg_at(tmp_path)
    records = pg.entity_tunnels_for_wing("alpha", halls(names[1:]), config=cfg)
    assert {r["source"]["room"] for r in records} == {"entity:" + n for n in names[1:]}
    assert len(pg._load_tunnels(cfg)) == 2


def test_cli_hallway_rebuild_prune_and_audit(tmp_path, monkeypatch):
    import mempalace.cli as cli
    import mempalace.palace as palace

    cfg = cfg_at(tmp_path)
    metadata = [
        {"wing": "alpha", "room": "r", "entities": "CodeRouter;src/CodeRouter.cpp;OtherPeer"},
        {"wing": "alpha", "room": "r", "entities": "src/CodeRouter.cpp;OtherPeer"},
        {"wing": "alpha", "room": "r", "entities": "src/CodeRouter.hpp;OtherPeer"},
        {"wing": "alpha", "room": "r", "entities": "src/CodeRouter.hpp;OtherPeer"},
    ]

    class Collection:
        def get(self, limit, offset, **kwargs):
            return {"metadatas": metadata[offset : offset + limit]}

    monkeypatch.setattr(palace, "get_collection", lambda *a, **kw: Collection())
    cli.cmd_hallways(Namespace(palace=cfg.palace_path, rebuild=True, wing="alpha"))
    rebuilt = hw.list_hallways(config=cfg)
    pairs = {
        tuple(sorted((r["entity_a"], r["entity_b"]))): r["co_occurrence_count"] for r in rebuilt
    }
    assert pairs == {
        ("OtherPeer", "src/CodeRouter.cpp"): 2,
        ("OtherPeer", "src/CodeRouter.hpp"): 2,
    }, pairs
    cli.cmd_hallways(Namespace(palace=cfg.palace_path, prune_spellings=True, yes=True))
    assert hw.list_hallways(config=cfg) == rebuilt
    hallway_audit = audit._analyze_hallways(rebuilt)
    assert hallway_audit["self_links"] == hallway_audit["duplicates"] == 0


def test_cli_propose_then_approved_apply_and_prune(tmp_path, monkeypatch, capsys):
    import mempalace.cli as cli

    cfg = cfg_at(tmp_path)
    bare, *qualified = CASES[0]
    monkeypatch.setattr(cli, "_wings_from_sqlite", lambda *a: {"alpha", "beta"})
    hw._save_hallways(halls(qualified), cfg)
    pg._save_tunnels([row(bare)], cfg)
    args = {"tunnels_action": "propose", "palace": cfg.palace_path, "yes": False, "max": 60}
    cli.cmd_tunnels(Namespace(**args))
    output = capsys.readouterr().out
    assert "2 candidate links" in output, output
    assert {r["entity"] for r in load_proposal(cfg)["tunnels"]} == set(qualified)
    cli.cmd_tunnels(Namespace(**{**args, "yes": True}))
    assert len(pg._load_tunnels(cfg)) == 3, capsys.readouterr().out
    cli.cmd_tunnels(Namespace(tunnels_action="prune", palace=cfg.palace_path, yes=True))
    assert len(pg._load_tunnels(cfg)) == 3


def test_cli_applies_approved_plan_after_ambiguous_alias_arrives(tmp_path, monkeypatch, capsys):
    import mempalace.cli as cli

    cfg = cfg_at(tmp_path)
    bare, *qualified = CASES[0]
    monkeypatch.setattr(cli, "_wings_from_sqlite", lambda *a: {"alpha", "beta"})
    save_proposal(
        cfg, {"tunnels": [{"entity": n, "wing_a": "alpha", "wing_b": "beta"} for n in qualified]}
    )
    pg._save_tunnels([row(bare)], cfg)
    cli.cmd_tunnels(Namespace(tunnels_action="propose", palace=cfg.palace_path, yes=True))
    output = capsys.readouterr().out
    assert len(pg._load_tunnels(cfg)) == 3, output


def test_full_audit_entrypoint_agrees_with_persisted_prune(tmp_path, monkeypatch):
    import mempalace.cli as cli

    cfg = cfg_at(tmp_path)
    records = [row(n, 20 - i) for i, n in enumerate(CASES[0])]
    pg._save_tunnels(records, cfg)
    monkeypatch.setattr(cli, "_wings_from_sqlite", lambda *a: {"alpha", "beta"})
    cli.cmd_tunnels(Namespace(tunnels_action="prune", palace=cfg.palace_path, yes=True))
    monkeypatch.setattr(
        audit, "_read_wing_room_counts", lambda *a: {"alpha": {"r": 20}, "beta": {"r": 20}}
    )
    monkeypatch.setattr(audit, "_read_wing_project_mix", lambda *a: None)
    monkeypatch.setattr(audit, "_read_kg", lambda *a: None)
    result = audit.audit_palace(config=cfg, explicit_palace=True)
    assert len(pg._load_tunnels(cfg)) == 3
    assert result["tunnels"]["duplicates"] == result["tunnels"]["artifacts"] == 0, result["tunnels"]
