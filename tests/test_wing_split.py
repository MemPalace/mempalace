"""Tests for mempalace.wing_split — one wing per source project."""

import os
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

WINGS = ["portal", "invoices", "acme_app", "mempalace", "mempalace-ts", "dash_dashboard_ng"]


@pytest.mark.parametrize(
    "path, key",
    [
        (
            r"C:\Users\igorl\.claude\projects\p--acme-portal\abc.jsonl",
            "p--acme-portal",
        ),
        (
            "/Users/me/.claude/projects/-Users-me-dev-mempalace/s/subagents/agent-1.jsonl",
            "-Users-me-dev-mempalace",
        ),
        ("/Users/me/dev/portal/CHANGELOG.md", None),
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
        json.dumps({"type": "session_meta", "payload": {"cwd": "/Users/me/dev/walletapp"}}) + "\n"
    )
    assert project_key(str(f)) == "walletapp"
    assert project_key(str(session / "missing.jsonl")) is None


@pytest.mark.parametrize(
    "key, target, how",
    [
        ("p--acme-portal", "portal", "existing"),
        ("P--org-invoices", "invoices", "existing"),
        ("-Users-me-dev-acme-app", "acme_app", "existing"),
        (
            "-Users-me-dev-mempalace-ts",
            "mempalace-ts",
            "existing",
        ),  # longest match, not mempalace
        ("p--UAM-dash-dashboard-ng", "dash_dashboard_ng", "existing"),
        ("-Users-me-dev-ledger-rfp", "ledger_rfp", "derived"),
        (
            "c--Users-igorl-Claude-Projects-gemma-cerebras-hackathon",
            "gemma_cerebras_hackathon",
            "derived",
        ),
        ("-home-me-matcher", "matcher", "derived"),
        ("p--photoapp", "photoapp", "derived"),
        ("P--MemPalace-mempalace-ts--claude-worktrees-agent-ae3c", "mempalace-ts", "existing"),
        ("-Users-me-dev-mempalace--claude-worktrees-review-pr-1696", "mempalace", "existing"),
        ("-Users-me--codex-worktrees-8c22-mempalace", "mempalace", "existing"),
        ("-Users-me--codex-worktrees-8c22-acme-app", "acme_app", "existing"),
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
                "source_file": cw + "p--acme-portal\\1.jsonl",
            },
        },
        {
            "id": "a2",
            "meta": {
                "wing": "convos",
                "room": "technical",
                "source_file": cw + "p--acme-portal\\2.jsonl",
            },
        },
        {
            "id": "b1",
            "meta": {
                "wing": "convos",
                "room": "planning",
                "source_file": cw + "p--photoapp\\1.jsonl",
            },
        },
        {"id": "c1", "meta": {"wing": "convos", "room": "general", "source_file": "notes.md"}},
        {"id": "z1", "meta": {"wing": "portal", "room": "decisions", "source_file": "x"}},
    ]


def test_plan_split_groups_and_resolves():
    plan = plan_split(FakeCollection(_rows()), "convos", ["portal", "convos"])
    assert plan["wing"] == "convos"
    assert plan["projects"]["p--acme-portal"] == {
        "target": "portal",
        "how": "existing",
        "drawers": 2,
    }
    assert plan["projects"]["p--photoapp"] == {"target": "photoapp", "how": "derived", "drawers": 1}
    assert plan["unresolved"] == 1
    assert plan_targets(plan) == {"portal": 2, "photoapp": 1}


def test_apply_split_moves_only_planned_drawers_and_drops_hallways(tmp_path, monkeypatch):
    import mempalace.hallways as hallways_mod

    hallway_file = tmp_path / "hallways.json"
    monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **k: str(hallway_file))
    monkeypatch.setattr(hallways_mod, "_legacy_hallway_file", lambda: str(tmp_path / "legacy.json"))
    hallways_mod._save_hallways(
        [
            {"id": "h1", "wing": "convos", "entity_a": "a", "entity_b": "b"},
            {"id": "h2", "wing": "portal", "entity_a": "a", "entity_b": "b"},
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
    plan = plan_split(col, "convos", ["portal"])
    plan["projects"]["p--photoapp"]["target"] = "convos"  # user edited: keep photoapp where it is
    result = apply_split(col, plan, closets_col=closets)
    assert result["moved"] == 2
    assert result["closets_moved"] == 1
    assert closets.rows["k1"]["meta"]["wing"] == "portal"
    assert closets.rows["k2"]["meta"]["wing"] == "convos"
    assert result["per_target"] == {"portal": 2}
    assert result["skipped"] == 2  # photoapp (kept) + notes.md (no key)
    assert result["hallways_dropped"] == 1
    assert (
        col.rows["a1"]["meta"]["wing"] == "portal" and col.rows["a1"]["meta"]["room"] == "technical"
    )
    assert col.rows["b1"]["meta"]["wing"] == "convos"
    assert set(col.updates[0][1][0]) == {"wing", "last_modified"}
    assert [h["id"] for h in hallways_mod.list_hallways()] == ["h2"]


def test_split_plan_round_trip_and_validation(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    plan = plan_split(FakeCollection(_rows()), "convos", ["portal"])
    path = save_split_plan(cfg, plan)
    assert path == str(tmp_path / "wings" / "split-convos.json")
    assert load_split_plan(cfg, "convos")["projects"]["p--photoapp"]["target"] == "photoapp"
    data = json.loads((tmp_path / "wings" / "split-convos.json").read_text())
    data["projects"]["p--photoapp"]["target"] = ""
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
            lambda path, name: [("decisions", "portal", "", 1), ("technical", "convos", "", 3)]
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
    assert col.rows["b1"]["meta"]["wing"] == "photoapp"


def test_cmd_wings_split_dry_run_keeps_the_plan_of_an_interrupted_split(
    tmp_path, monkeypatch, capsys
):
    """Mid-split, re-planning would see only the drawers not yet moved and
    replace the plan being followed, hand-edited targets included."""
    from pathlib import Path

    import mempalace.cli as cli
    from mempalace.config import MempalaceConfig
    from mempalace.wing_split import split_pending_path, split_plan_path

    cfg = MempalaceConfig(palace_path=str(tmp_path))
    plan_file = Path(split_plan_path(cfg, "convos"))
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text('{"edited": "by hand"}')
    marker = Path(split_pending_path(cfg, "convos"))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("2026-09-24T00:00:00+00:00\n")

    def no_planning(*a, **k):
        raise AssertionError("a pending split must not be re-planned")

    monkeypatch.setattr("mempalace.palace.get_collection", no_planning)
    cli.cmd_wings(Namespace(wings_action="split", palace=str(tmp_path), wing="convos", yes=False))
    assert "was interrupted" in capsys.readouterr().out
    assert plan_file.read_text() == '{"edited": "by hand"}'


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


def test_apply_split_resumes_after_an_interrupted_batch(monkeypatch):
    """A crash mid-split leaves rows wholly in one wing; a re-run moves the rest."""
    import mempalace.wing_split as ws

    monkeypatch.setattr(ws, "_UPDATE_BATCH", 1)
    col = FakeCollection(_rows())
    plan = plan_split(col, "convos", ["portal"])
    plan["projects"]["p--photoapp"]["target"] = "convos"

    real_update = col.update
    calls = {"n": 0}

    def flaky_update(**kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_update(**kw)

    monkeypatch.setattr(col, "update", flaky_update)
    with pytest.raises(KeyboardInterrupt):
        apply_split(col, plan)
    moved = lambda: [r["id"] for r in col.rows.values() if "last_modified" in r["meta"]]  # noqa: E731
    assert len(moved()) == 1

    monkeypatch.setattr(col, "update", real_update)
    second = apply_split(col, plan)
    assert second["moved"] == 1  # only the remainder
    assert len(moved()) == 2
    assert all(col.rows[i]["meta"]["wing"] == "portal" for i in moved())
    assert apply_split(col, plan)["moved"] == 0  # completed split is a no-op


def test_cmd_wings_split_retry_drops_hallways_after_drawers_completed(
    tmp_path, monkeypatch, capsys
):
    """Killed after every drawer moved: the retry moves nothing but must still
    drop the source wing's stale hallways. A completed split stays a no-op."""
    import contextlib

    import mempalace.cli as cli
    import mempalace.hallways as hallways_mod
    from mempalace.wing_split import split_pending_path

    hallway_file = tmp_path / "hallways.json"
    monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **k: str(hallway_file))
    monkeypatch.setattr(hallways_mod, "_legacy_hallway_file", lambda: str(tmp_path / "legacy.json"))
    hallways_mod._save_hallways([{"id": "h1", "wing": "convos", "entity_a": "a", "entity_b": "b"}])
    col = FakeCollection(_rows())
    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: col)
    monkeypatch.setattr("mempalace.palace.get_closets_collection", lambda *a, **k: None)
    monkeypatch.setattr("mempalace.palace.mine_palace_lock", lambda p: contextlib.nullcontext())
    monkeypatch.setattr(
        "mempalace.palace_graph.sqlite_grouped_counts_reader",
        lambda config: (
            lambda path, name: [("decisions", "portal", "", 1), ("technical", "convos", "", 3)]
        ),
    )
    ns = dict(wings_action="split", palace=str(tmp_path), wing="convos")
    cli.cmd_wings(Namespace(yes=False, **ns))

    real_lock = hallways_mod._hallway_file_lock

    def dies(config=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(hallways_mod, "_hallway_file_lock", dies)
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_wings(Namespace(yes=True, **ns))
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    assert os.path.isfile(split_pending_path(cfg, "convos"))
    assert [h["id"] for h in hallways_mod.list_hallways()] == ["h1"]  # not dropped yet

    monkeypatch.setattr(hallways_mod, "_hallway_file_lock", real_lock)
    capsys.readouterr()
    cli.cmd_wings(Namespace(yes=True, **ns))
    out = capsys.readouterr().out
    assert "Resuming an interrupted split" in out
    assert "Moved 0 drawers" in out and "1 hallway records of convos dropped" in out
    assert hallways_mod.list_hallways() == []
    assert not os.path.exists(split_pending_path(cfg, "convos"))

    # A later rebuild of the source wing survives a no-op re-run.
    hallways_mod._save_hallways([{"id": "h2", "wing": "convos", "entity_a": "c", "entity_b": "d"}])
    cli.cmd_wings(Namespace(yes=True, **ns))
    assert [h["id"] for h in hallways_mod.list_hallways()] == ["h2"]


def test_cmd_wings_split_stops_before_moving_when_closets_fail_to_open(tmp_path, monkeypatch):
    import contextlib

    import mempalace.cli as cli

    col = FakeCollection(_rows())
    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: col)
    monkeypatch.setattr("mempalace.palace.mine_palace_lock", lambda p: contextlib.nullcontext())
    monkeypatch.setattr(
        "mempalace.palace_graph.sqlite_grouped_counts_reader",
        lambda config: (
            lambda path, name: [("decisions", "portal", "", 1), ("technical", "convos", "", 3)]
        ),
    )
    monkeypatch.setattr("mempalace.hallways._load_hallways", lambda config=None: [])
    ns = dict(wings_action="split", palace=str(tmp_path), wing="convos")
    cli.cmd_wings(Namespace(yes=False, **ns))

    def broken(*a, **k):
        raise OSError("disk I/O error")

    monkeypatch.setattr("mempalace.palace.get_closets_collection", broken)
    with pytest.raises(OSError):
        cli.cmd_wings(Namespace(yes=True, **ns))
    assert col.updates == []  # no drawer moved without its closets


def test_load_split_plan_rejects_a_plan_for_another_wing(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    plan = plan_split(FakeCollection(_rows()), "convos", ["portal"])
    save_split_plan(cfg, plan)
    # Someone copied the file into another wing's plan slot.
    import shutil

    from mempalace.wing_split import split_plan_path

    shutil.copy(split_plan_path(cfg, "convos"), split_plan_path(cfg, "other"))
    with pytest.raises(ValueError, match="not 'other'"):
        load_split_plan(cfg, "other")
    assert load_split_plan(cfg, "convos")["wing"] == "convos"
