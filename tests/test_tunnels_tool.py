"""Tests for mempalace.tunnels_tool — reviewable tunnel proposals and artifact pruning."""

import json
from argparse import Namespace

import pytest

from mempalace.config import MempalaceConfig
from mempalace.tunnels_tool import (
    apply_proposal,
    load_proposal,
    propose_tunnels,
    prune_tunnels,
    save_proposal,
)

HALLWAYS = [
    {
        "wing": "liquid_llm",
        "entity_a": "ChatStore",
        "entity_b": "RootView",
        "co_occurrence_count": 240,
    },
    {
        "wing": "mempalace-ts",
        "entity_a": "ChatStore.swift",
        "entity_b": "Bridge",
        "co_occurrence_count": 12,
    },
    {
        "wing": "meshguard",
        "entity_a": "swim.zig",
        "entity_b": "codec.zig",
        "co_occurrence_count": 195,
    },
    {"wing": "wormdb", "entity_a": "swim.zig", "entity_b": "MeshGuard", "co_occurrence_count": 40},
    {
        "wing": "liquid_llm",
        "entity_a": "content",
        "entity_b": "RootView",
        "co_occurrence_count": 90,
    },
    {"wing": "wormdb", "entity_a": "content", "entity_b": "MeshGuard", "co_occurrence_count": 80},
    {"wing": "gone", "entity_a": "swim.zig", "entity_b": "X", "co_occurrence_count": 999},
]
WINGS = {"liquid_llm", "mempalace-ts", "meshguard", "wormdb"}


def test_propose_ranks_by_weaker_side_and_drops_generic_and_missing_wings():
    plan = propose_tunnels(HALLWAYS, WINGS)
    rows = [(r["entity"], r["wing_a"], r["wing_b"], r["strength"]) for r in plan["tunnels"]]
    assert rows == [
        ("swim.zig", "meshguard", "wormdb", 40),
        ("ChatStore", "liquid_llm", "mempalace-ts", 12),
    ]
    assert plan["candidates"] == 2
    assert propose_tunnels(HALLWAYS, WINGS, max_tunnels=1)["tunnels"][0]["entity"] == "swim.zig"


def test_proposal_round_trip_apply_and_validation(tmp_path, monkeypatch):
    import mempalace.palace_graph as pg

    tunnel_file = tmp_path / "tunnels.json"
    monkeypatch.setattr(pg, "_get_tunnel_file", lambda *a, **k: str(tunnel_file))
    monkeypatch.setattr(pg, "_legacy_tunnel_file", lambda: str(tmp_path / "legacy.json"))
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    plan = propose_tunnels(HALLWAYS, WINGS)
    save_proposal(cfg, plan)
    loaded = load_proposal(cfg)
    assert apply_proposal(loaded) == 2
    stored = pg.list_tunnels()
    assert {(t["source"]["room"], t["kind"]) for t in stored} == {
        ("entity:swim.zig", "entity"),
        ("entity:ChatStore", "entity"),
    }
    assert apply_proposal(loaded) == 2 and len(pg.list_tunnels()) == 2  # idempotent
    path = tmp_path / "tunnels" / "proposal.json"
    path.write_text(json.dumps({"tunnels": [{"entity": "", "wing_a": "a", "wing_b": "b"}]}))
    with pytest.raises(ValueError):
        load_proposal(cfg)


def test_prune_removes_generic_dangling_and_duplicate_spellings():
    tunnels = [
        {
            "access_count": 3,
            "source": {"wing": "liquid_llm", "room": "entity:ChatStore"},
            "target": {"wing": "mempalace-ts", "room": "entity:ChatStore"},
        },
        {
            "access_count": 0,
            "source": {"wing": "mempalace-ts", "room": "entity:ChatStore.swift"},
            "target": {"wing": "liquid_llm", "room": "entity:ChatStore.swift"},
        },
        {
            "access_count": 0,
            "source": {"wing": "liquid_llm", "room": "entity:content"},
            "target": {"wing": "wormdb", "room": "entity:content"},
        },
        {
            "access_count": 0,
            "source": {"wing": "meshguard", "room": "entity:swim.zig"},
            "target": {"wing": "gone", "room": "entity:swim.zig"},
        },
        {
            "access_count": 0,
            "source": {"wing": "meshguard", "room": "decisions"},
            "target": {"wing": "wormdb", "room": "decisions"},
        },
    ]
    kept, report = prune_tunnels(tunnels, WINGS)
    assert report == {"total": 5, "generic": 1, "dangling": 1, "duplicates": 1, "removed": 3}
    assert [t["source"]["room"] for t in kept] == ["entity:ChatStore", "decisions"]


def test_cmd_tunnels_propose_prune_and_dispatch(tmp_path, monkeypatch, capsys):
    import mempalace.cli as cli
    import mempalace.palace_graph as pg

    tunnel_file = tmp_path / "tunnels.json"
    monkeypatch.setattr(pg, "_get_tunnel_file", lambda *a, **k: str(tunnel_file))
    monkeypatch.setattr(pg, "_legacy_tunnel_file", lambda: str(tmp_path / "legacy.json"))
    monkeypatch.setattr(
        "mempalace.hallways.list_hallways", lambda wing=None, config=None: list(HALLWAYS)
    )
    monkeypatch.setattr(
        "mempalace.palace_graph.sqlite_grouped_counts_reader",
        lambda config: lambda path, name: [("r", w, "", 1) for w in WINGS],
    )
    ns = dict(palace=str(tmp_path))
    cli.cmd_tunnels(Namespace(tunnels_action="propose", yes=False, max=60, **ns))
    out = capsys.readouterr().out
    assert "proposing the strongest 2" in out and "Plan saved" in out
    cli.cmd_tunnels(Namespace(tunnels_action="propose", yes=True, max=60, **ns))
    assert "Created or refreshed 2 tunnels" in capsys.readouterr().out
    pg.create_tunnel("liquid_llm", "entity:content", "wormdb", "entity:content", kind="entity")
    cli.cmd_tunnels(Namespace(tunnels_action="prune", yes=False, **ns))
    assert "1 of 3 tunnels are artifacts" in capsys.readouterr().out
    cli.cmd_tunnels(Namespace(tunnels_action="prune", yes=True, **ns))
    assert "Removed 1." in capsys.readouterr().out
    assert len(pg.list_tunnels()) == 2

    seen = {}
    monkeypatch.setattr(cli, "cmd_tunnels", lambda args: seen.update(vars(args)))
    monkeypatch.setattr("sys.argv", ["mempalace", "tunnels", "prune", "--yes"])
    cli.main()
    assert seen["tunnels_action"] == "prune" and seen["yes"]
