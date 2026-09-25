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
        "wing": "acme_app",
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
        "wing": "meshkit",
        "entity_a": "swim.zig",
        "entity_b": "codec.zig",
        "co_occurrence_count": 195,
    },
    {"wing": "ringdb", "entity_a": "swim.zig", "entity_b": "MeshKit", "co_occurrence_count": 40},
    {
        "wing": "acme_app",
        "entity_a": "content",
        "entity_b": "RootView",
        "co_occurrence_count": 90,
    },
    {"wing": "ringdb", "entity_a": "content", "entity_b": "MeshKit", "co_occurrence_count": 80},
    {"wing": "gone", "entity_a": "swim.zig", "entity_b": "X", "co_occurrence_count": 999},
]
WINGS = {"acme_app", "mempalace-ts", "meshkit", "ringdb"}


def test_propose_ranks_by_weaker_side_and_drops_generic_and_missing_wings():
    plan = propose_tunnels(HALLWAYS, WINGS)
    rows = [(r["entity"], r["wing_a"], r["wing_b"], r["strength"]) for r in plan["tunnels"]]
    assert rows == [
        ("swim.zig", "meshkit", "ringdb", 40),
        ("ChatStore", "acme_app", "mempalace-ts", 12),
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
    assert apply_proposal(loaded) == 0 and len(pg.list_tunnels()) == 2  # idempotent
    path = tmp_path / "tunnels" / "proposal.json"
    path.write_text(json.dumps({"tunnels": [{"entity": "", "wing_a": "a", "wing_b": "b"}]}))
    with pytest.raises(ValueError):
        load_proposal(cfg)


def test_prune_removes_generic_dangling_and_duplicate_spellings():
    tunnels = [
        {
            "access_count": 3,
            "source": {"wing": "acme_app", "room": "entity:ChatStore"},
            "target": {"wing": "mempalace-ts", "room": "entity:ChatStore"},
        },
        {
            "access_count": 0,
            "source": {"wing": "mempalace-ts", "room": "entity:ChatStore.swift"},
            "target": {"wing": "acme_app", "room": "entity:ChatStore.swift"},
        },
        {
            "access_count": 0,
            "source": {"wing": "acme_app", "room": "entity:content"},
            "target": {"wing": "ringdb", "room": "entity:content"},
        },
        {
            "access_count": 0,
            "source": {"wing": "meshkit", "room": "entity:swim.zig"},
            "target": {"wing": "gone", "room": "entity:swim.zig"},
        },
        {
            "access_count": 0,
            "source": {"wing": "meshkit", "room": "decisions"},
            "target": {"wing": "ringdb", "room": "decisions"},
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
    assert "Created 2 tunnels" in capsys.readouterr().out
    pg.create_tunnel("acme_app", "entity:content", "ringdb", "entity:content", kind="entity")
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


def test_propose_skips_existing_links_and_covers_unlinked_wings_first():
    existing = [
        {
            "source": {"wing": "meshkit", "room": "entity:swim.zig"},
            "target": {"wing": "ringdb", "room": "entity:swim.zig"},
        }
    ]
    plan = propose_tunnels(HALLWAYS, WINGS, existing_tunnels=existing)
    rows = [(r["entity"], r["wing_a"], r["wing_b"]) for r in plan["tunnels"]]
    assert rows == [("ChatStore", "acme_app", "mempalace-ts")]
    assert plan["candidates"] == 1

    # Coverage first: with one slot, a wing pair nobody reaches beats a
    # stronger link between wings that already have a tunnel.
    hallways = HALLWAYS + [
        {"wing": "meshkit", "entity_a": "Rope", "entity_b": "Q", "co_occurrence_count": 500},
        {"wing": "ringdb", "entity_a": "Rope", "entity_b": "Q", "co_occurrence_count": 500},
    ]
    plan = propose_tunnels(hallways, WINGS, max_tunnels=1, existing_tunnels=existing)
    assert [(r["entity"], r["strength"]) for r in plan["tunnels"]] == [("ChatStore", 12)]
    plan = propose_tunnels(hallways, WINGS, max_tunnels=2, existing_tunnels=existing)
    assert [r["entity"] for r in plan["tunnels"]] == ["Q", "ChatStore"]


def test_prune_keeps_two_links_that_swap_rooms_between_the_same_wings():
    tunnels = [
        {"source": {"wing": "a", "room": "x"}, "target": {"wing": "b", "room": "y"}},
        {"source": {"wing": "a", "room": "y"}, "target": {"wing": "b", "room": "x"}},
        {"source": {"wing": "b", "room": "y"}, "target": {"wing": "a", "room": "x"}},  # dup of 1st
    ]
    kept, report = prune_tunnels(tunnels, {"a", "b"})
    assert report["duplicates"] == 1 and len(kept) == 2


def test_prune_and_propose_normalize_wing_spellings():
    tunnels = [
        {
            "source": {"wing": "acme-app", "room": "entity:X"},
            "target": {"wing": "b", "room": "entity:X"},
        }
    ]
    kept, report = prune_tunnels(tunnels, {"acme_app", "b"})
    assert report["dangling"] == 0 and len(kept) == 1
    hallways = [
        {"wing": "acme-app", "entity_a": "Rope", "entity_b": "Q", "co_occurrence_count": 9},
        {"wing": "b", "entity_a": "Rope", "entity_b": "Q", "co_occurrence_count": 9},
    ]
    plan = propose_tunnels(hallways, {"acme_app", "b"})
    assert sorted(r["entity"] for r in plan["tunnels"]) == ["Q", "Rope"]


def test_load_proposal_rejects_non_object_rows(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    save_proposal(cfg, {"tunnels": ["not a row"]})
    with pytest.raises(ValueError, match="not an object"):
        load_proposal(cfg)


def test_cmd_tunnels_apply_skips_rows_naming_a_wing_that_is_gone(tmp_path, monkeypatch, capsys):
    """A plan sits under review; a wing may be split or renamed meanwhile."""
    import mempalace.cli as cli

    cfg = MempalaceConfig(palace_path=str(tmp_path))
    save_proposal(
        cfg,
        {
            "tunnels": [
                {"entity": "X", "wing_a": "a", "wing_b": "gone", "strength": 9},
                {"entity": "Y", "wing_a": "a", "wing_b": "b", "strength": 8},
            ]
        },
    )
    monkeypatch.setattr(
        "mempalace.palace_graph.sqlite_grouped_counts_reader",
        lambda config: lambda path, name: [("r", w, "", 1) for w in ("a", "b")],
    )
    created = []
    monkeypatch.setattr(
        "mempalace.palace_graph.create_tunnel", lambda **kw: created.append(kw["label"])
    )
    cli.cmd_tunnels(Namespace(tunnels_action="propose", palace=str(tmp_path), yes=True, max=60))
    out = capsys.readouterr().out
    assert created == ["shared entity: Y"]
    assert "Skipped 1 row" in out


def test_prune_keeps_two_files_that_only_share_a_basename():
    tunnels = [
        {
            "access_count": 1,
            "source": {"wing": "a", "room": "entity:src/models/user.py"},
            "target": {"wing": "b", "room": "entity:src/models/user.py"},
        },
        {
            "access_count": 0,
            "source": {"wing": "a", "room": "entity:tests/fixtures/user.py"},
            "target": {"wing": "b", "room": "entity:tests/fixtures/user.py"},
        },
        # A second spelling of the first file: a real duplicate.
        {
            "access_count": 0,
            "source": {"wing": "b", "room": "entity:models/user.py"},
            "target": {"wing": "a", "room": "entity:models/user.py"},
        },
    ]
    kept, report = prune_tunnels(tunnels, {"a", "b"})
    assert report["duplicates"] == 1
    assert [t["source"]["room"] for t in kept] == [
        "entity:src/models/user.py",
        "entity:tests/fixtures/user.py",
    ]


def test_propose_skips_an_existing_link_under_another_spelling_only():
    existing = [
        {
            "source": {"wing": "meshkit", "room": "entity:src/swim.zig"},
            "target": {"wing": "ringdb", "room": "entity:src/swim.zig"},
        }
    ]
    # swim.zig is the same file as src/swim.zig: already linked, not proposed.
    plan = propose_tunnels(HALLWAYS, WINGS, existing_tunnels=existing)
    assert "swim.zig" not in {r["entity"] for r in plan["tunnels"]}


def test_apply_proposal_skips_a_link_created_meanwhile_under_another_spelling(
    tmp_path, monkeypatch
):
    import mempalace.palace_graph as pg

    tunnel_file = tmp_path / "tunnels.json"
    monkeypatch.setattr(pg, "_get_tunnel_file", lambda *a, **k: str(tunnel_file))
    monkeypatch.setattr(pg, "_legacy_tunnel_file", lambda: str(tmp_path / "legacy.json"))
    plan = {
        "tunnels": [
            {"entity": "src/main.py", "wing_a": "a", "wing_b": "b"},
            {"entity": "Router", "wing_a": "a", "wing_b": "b"},
            {"entity": "Router", "wing_a": "b", "wing_b": "a"},  # repeats the row above
        ]
    }
    # While the plan waited for review, someone linked the same file.
    pg.create_tunnel("a", "entity:main.py", "b", "entity:main.py", label="x", kind="entity")
    assert apply_proposal(plan) == 1
    rooms = sorted(t["source"]["room"] for t in pg.list_tunnels())
    assert rooms == ["entity:Router", "entity:main.py"]
