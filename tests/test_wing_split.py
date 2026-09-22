"""Tests for mempalace.wing_split — one wing per source project."""

import json
from argparse import Namespace

import pytest

from mempalace.config import MempalaceConfig
from mempalace.wing_split import (
    apply_split,
    load_split_plan,
    plan_split,
    plan_targets,
    project_key,
    resolve_target,
    save_split_plan,
)
from tests.test_rooms import FakeCollection

WINGS = ["bentokit", "nfs_e", "liquid_llm", "mempalace", "mempalace-ts", "bebel_dashboard_ng"]


@pytest.mark.parametrize(
    "path, key",
    [
        (
            r"C:\Users\igorl\.claude\projects\p--rioblocks-bentokit\abc.jsonl",
            "p--rioblocks-bentokit",
        ),
        (
            "/Users/igorls/.claude/projects/-Users-igorls-dev-mempalace/s/subagents/agent-1.jsonl",
            "-Users-igorls-dev-mempalace",
        ),
        ("/Users/igorls/dev/bentokit/CHANGELOG.md", None),
        ("", None),
        (None, None),
    ],
)
def test_project_key_from_claude_paths(path, key):
    assert project_key(path) == key


def test_project_key_reads_codex_cwd_when_file_exists(tmp_path):
    session = tmp_path / ".codex" / "sessions" / "2026" / "05" / "27"
    session.mkdir(parents=True)
    f = session / "rollout-x.jsonl"
    f.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": "/Users/igorls/dev/simpleos"}})
        + "\n"
    )
    assert project_key(str(f)) == "simpleos"
    assert project_key(str(session / "missing.jsonl")) is None


@pytest.mark.parametrize(
    "key, target, how",
    [
        ("p--rioblocks-bentokit", "bentokit", "existing"),
        ("P--ecce-nfs-e", "nfs_e", "existing"),
        ("-Users-igorls-dev-liquid-llm", "liquid_llm", "existing"),
        (
            "-Users-igorls-dev-mempalace-ts",
            "mempalace-ts",
            "existing",
        ),  # longest match, not mempalace
        ("p--UAM-bebel-dashboard-ng", "bebel_dashboard_ng", "existing"),
        ("-Users-igorls-dev-vaulta-rfp", "vaulta_rfp", "derived"),
        (
            "c--Users-igorl-Claude-Projects-gemma-cerebras-hackathon",
            "gemma_cerebras_hackathon",
            "derived",
        ),
        ("-home-igorls-matchos", "matchos", "derived"),
        ("p--afterpic", "afterpic", "derived"),
        ("P--MemPalace-mempalace-ts--claude-worktrees-agent-ae3c", "mempalace-ts", "existing"),
        ("-Users-igorls-dev-mempalace--claude-worktrees-review-pr-1696", "mempalace", "existing"),
        ("-Users-igorls--codex-worktrees-8c22-mempalace", "mempalace", "existing"),
        ("-Users-igorls--codex-worktrees-8c22-liquid-llm", "liquid_llm", "existing"),
    ],
)
def test_resolve_target(key, target, how):
    assert resolve_target(key, WINGS) == (target, how)


def _rows():
    cw = "C:\\Users\\igorl\\.claude\\projects\\"
    return [
        {
            "id": "a1",
            "meta": {
                "wing": "convos",
                "room": "technical",
                "source_file": cw + "p--rioblocks-bentokit\\1.jsonl",
            },
        },
        {
            "id": "a2",
            "meta": {
                "wing": "convos",
                "room": "technical",
                "source_file": cw + "p--rioblocks-bentokit\\2.jsonl",
            },
        },
        {
            "id": "b1",
            "meta": {
                "wing": "convos",
                "room": "planning",
                "source_file": cw + "p--afterpic\\1.jsonl",
            },
        },
        {"id": "c1", "meta": {"wing": "convos", "room": "general", "source_file": "notes.md"}},
        {"id": "z1", "meta": {"wing": "bentokit", "room": "decisions", "source_file": "x"}},
    ]


def test_plan_split_groups_and_resolves():
    plan = plan_split(FakeCollection(_rows()), "convos", ["bentokit", "convos"])
    assert plan["wing"] == "convos"
    assert plan["projects"]["p--rioblocks-bentokit"] == {
        "target": "bentokit",
        "how": "existing",
        "drawers": 2,
    }
    assert plan["projects"]["p--afterpic"] == {"target": "afterpic", "how": "derived", "drawers": 1}
    assert plan["unresolved"] == 1
    assert plan_targets(plan) == {"bentokit": 2, "afterpic": 1}


def test_apply_split_moves_only_planned_drawers_and_drops_hallways(tmp_path, monkeypatch):
    import mempalace.hallways as hallways_mod

    hallway_file = tmp_path / "hallways.json"
    monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **k: str(hallway_file))
    monkeypatch.setattr(hallways_mod, "_legacy_hallway_file", lambda: str(tmp_path / "legacy.json"))
    hallways_mod._save_hallways(
        [
            {"id": "h1", "wing": "convos", "entity_a": "a", "entity_b": "b"},
            {"id": "h2", "wing": "bentokit", "entity_a": "a", "entity_b": "b"},
        ]
    )
    col = FakeCollection(_rows())
    closets = FakeCollection(
        [
            {
                "id": "k1",
                "meta": {
                    "wing": "convos",
                    "room": "technical",
                    "source_file": _rows()[0]["meta"]["source_file"],
                },
            },
            {
                "id": "k2",
                "meta": {"wing": "convos", "room": "technical", "source_file": "notes.md"},
            },
        ]
    )
    plan = plan_split(col, "convos", ["bentokit"])
    plan["projects"]["p--afterpic"]["target"] = "convos"  # user edited: keep afterpic where it is
    result = apply_split(col, plan, closets_col=closets)
    assert result["moved"] == 2
    assert result["closets_moved"] == 1
    assert closets.rows["k1"]["meta"]["wing"] == "bentokit"
    assert closets.rows["k2"]["meta"]["wing"] == "convos"
    assert result["per_target"] == {"bentokit": 2}
    assert result["skipped"] == 2  # afterpic (kept) + notes.md (no key)
    assert result["hallways_dropped"] == 1
    assert (
        col.rows["a1"]["meta"]["wing"] == "bentokit"
        and col.rows["a1"]["meta"]["room"] == "technical"
    )
    assert col.rows["b1"]["meta"]["wing"] == "convos"
    assert set(col.updates[0][1][0]) == {"wing", "last_modified"}
    assert [h["id"] for h in hallways_mod.list_hallways()] == ["h2"]


def test_split_plan_round_trip_and_validation(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    plan = plan_split(FakeCollection(_rows()), "convos", ["bentokit"])
    path = save_split_plan(cfg, plan)
    assert path == str(tmp_path / "wings" / "split-convos.json")
    assert load_split_plan(cfg, "convos")["projects"]["p--afterpic"]["target"] == "afterpic"
    data = json.loads((tmp_path / "wings" / "split-convos.json").read_text())
    data["projects"]["p--afterpic"]["target"] = ""
    (tmp_path / "wings" / "split-convos.json").write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_split_plan(cfg, "convos")


def test_cmd_wings_split_plan_then_apply(tmp_path, monkeypatch, capsys):
    import contextlib

    import mempalace.cli as cli

    col = FakeCollection(_rows())
    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: col)
    monkeypatch.setattr("mempalace.palace.get_closets_collection", lambda *a, **k: None)
    monkeypatch.setattr("mempalace.palace.mine_palace_lock", lambda p: contextlib.nullcontext())
    monkeypatch.setattr(
        "mempalace.palace_graph.sqlite_grouped_counts_reader",
        lambda config: (
            lambda path, name: [("decisions", "bentokit", "", 1), ("technical", "convos", "", 3)]
        ),
    )
    monkeypatch.setattr(
        "mempalace.wing_split._load_hallways", lambda config=None: [], raising=False
    )
    monkeypatch.setattr("mempalace.hallways._load_hallways", lambda config=None: [])

    cli.cmd_wings(Namespace(wings_action="split", palace=str(tmp_path), wing="convos", yes=False))
    out = capsys.readouterr().out
    assert "2 source projects" in out and "Plan saved" in out
    assert col.updates == []

    cli.cmd_wings(Namespace(wings_action="split", palace=str(tmp_path), wing="convos", yes=True))
    out = capsys.readouterr().out
    assert "Moved 3 drawers into 2 wings and 0 closets" in out
    assert col.rows["b1"]["meta"]["wing"] == "afterpic"


def test_cmd_wings_split_without_plan_exits_1(tmp_path, capsys):
    import mempalace.cli as cli

    with pytest.raises(SystemExit) as exc:
        cli.cmd_wings(
            Namespace(wings_action="split", palace=str(tmp_path), wing="convos", yes=True)
        )
    assert exc.value.code == 1
    assert "No plan" in capsys.readouterr().out


def test_main_dispatches_wings(monkeypatch):
    import mempalace.cli as cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_wings", lambda args: seen.update(vars(args)))
    monkeypatch.setattr("sys.argv", ["mempalace", "wings", "split", "--wing", "w", "--yes"])
    cli.main()
    assert seen["wings_action"] == "split" and seen["wing"] == "w" and seen["yes"]
