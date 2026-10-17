"""Preserve tunnel spelling context through proposal selection and application."""

import pytest

from mempalace import palace_graph as pg, palace_audit as audit
from mempalace.config import MempalaceConfig
from mempalace.tunnels_tool import (
    apply_proposal,
    propose_tunnels,
    prune_tunnels,
    save_proposal,
    load_proposal,
)
from test_tunnels_tool_aliases import halls, row


@pytest.mark.parametrize("reason", ["cap", "filtered_wings"])
def test_approved_tunnel_survives_propose_apply_context_loss(tmp_path, reason):
    cfg = MempalaceConfig(palace_path=str(tmp_path / "palace"))
    pg.create_tunnel(
        "alpha", "entity:CodeRouter.py", "beta", "entity:CodeRouter.py", kind="entity", config=cfg
    )
    candidates = halls(["src/a/CodeRouter.py", "tests/b/CodeRouter.py"])
    if reason == "filtered_wings":
        for h in candidates:
            if h["entity_a"] == "tests/b/CodeRouter.py" and h["wing"] == "beta":
                h["wing"] = "gamma"
    plan = propose_tunnels(
        candidates, {"alpha", "beta"}, max_tunnels=1, existing_tunnels=pg._load_tunnels(cfg)
    )
    assert [r["entity"] for r in plan["tunnels"]] == ["src/a/CodeRouter.py"]
    save_proposal(cfg, plan)
    count = apply_proposal(load_proposal(cfg), cfg)
    assert count == 1, {
        "approved": plan["tunnels"],
        "created": count,
        "persisted": pg._load_tunnels(cfg),
    }


def test_existing_unambiguous_tunnel_still_skipped(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path / "palace"))
    pg.create_tunnel(
        "alpha",
        "entity:src/a/CodeRouter.py",
        "beta",
        "entity:src/a/CodeRouter.py",
        kind="entity",
        config=cfg,
    )
    assert (
        apply_proposal(
            {"tunnels": [{"entity": "CodeRouter.py", "wing_a": "alpha", "wing_b": "beta"}]}, cfg
        )
        == 0
    )


def test_audit_prune_agree_with_incomplete_wing_context():
    broken = row("tests/b/CodeRouter.py", 1)
    broken["source"]["wing"] = ""
    records = [row("src/a/CodeRouter.py", 20), row("CodeRouter.py", 10), broken]
    _, pruned = prune_tunnels(records, {"alpha", "beta"})
    audited = audit._analyze_tunnels(records, wings={"alpha", "beta"})
    assert audited["duplicates"] == pruned["duplicates"], {"audit": audited, "prune": pruned}


def test_new_bare_tunnel_cannot_erase_approved_host(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path / "palace"))
    plan = propose_tunnels(
        halls(["src/a/CodeRouter.py", "tests/b/CodeRouter.py"]), {"alpha", "beta"}, max_tunnels=1
    )
    save_proposal(cfg, plan)
    pg.create_tunnel("alpha", "entity:CodeRouter.py", "beta", "entity:CodeRouter.py", config=cfg)
    loaded = load_proposal(cfg)
    assert apply_proposal(loaded, cfg) == 1
    assert apply_proposal(loaded, cfg) == 0


@pytest.mark.parametrize("context", [None, "CodeRouter.py", [3]])
def test_invalid_new_proposal_context_refuses_before_writes(tmp_path, context):
    cfg = MempalaceConfig(palace_path=str(tmp_path / "palace"))
    plan = propose_tunnels(halls(["CodeRouter.py"]), {"alpha", "beta"})
    plan["entity_spellings"] = context
    with pytest.raises(ValueError, match="entity_spellings"):
        apply_proposal(plan, cfg)
    assert pg._load_tunnels(cfg) == []
