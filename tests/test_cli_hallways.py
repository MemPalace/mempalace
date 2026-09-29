"""Tests for the `hallways` CLI command."""

import pytest
from argparse import Namespace

import mempalace.hallways as hallways_mod
from mempalace.cli import cmd_hallways


def test_lists_sorted_by_count(monkeypatch, capsys):
    rows = [
        {
            "entity_a": "C",
            "entity_b": "D",
            "co_occurrence_count": 1,
            "wing": "w",
            "label": "C <-> D (x1)",
        },
        {
            "entity_a": "A",
            "entity_b": "B",
            "co_occurrence_count": 3,
            "wing": "w",
            "label": "A <-> B (x3)",
        },
    ]
    monkeypatch.setattr(hallways_mod, "list_hallways", lambda wing=None, config=None: list(rows))
    cmd_hallways(Namespace(wing=None, limit=50))
    out = capsys.readouterr().out
    assert "2 hallway(s)" in out
    assert "A <-> B (x3)" in out
    # Highest co-occurrence first.
    assert out.index("A <-> B") < out.index("C <-> D")


def test_respects_limit(monkeypatch, capsys):
    rows = [
        {"entity_a": f"E{i}", "entity_b": "X", "co_occurrence_count": i, "label": f"E{i} <-> X"}
        for i in range(5)
    ]
    monkeypatch.setattr(hallways_mod, "list_hallways", lambda wing=None, config=None: list(rows))
    cmd_hallways(Namespace(wing=None, limit=2))
    assert capsys.readouterr().out.count("<->") == 2


def test_negative_limit_shows_nothing_not_tail(monkeypatch, capsys):
    rows = [
        {"entity_a": f"E{i}", "entity_b": "X", "co_occurrence_count": i, "label": f"E{i} <-> X"}
        for i in range(5)
    ]
    monkeypatch.setattr(hallways_mod, "list_hallways", lambda wing=None, config=None: list(rows))
    cmd_hallways(Namespace(wing=None, limit=-2))
    # A negative limit must not slice from the end (which would print all-but-2).
    assert capsys.readouterr().out.count("<->") == 0


def test_empty_message(monkeypatch, capsys):
    monkeypatch.setattr(hallways_mod, "list_hallways", lambda wing=None, config=None: [])
    cmd_hallways(Namespace(wing="x", limit=50))
    assert "No hallways yet" in capsys.readouterr().out


def test_explicit_palace_scopes_hallway_listing(monkeypatch, tmp_path):
    calls = []

    def fake_list(wing=None, config=None):
        calls.append((wing, config.palace_path))
        return []

    selected = tmp_path / "selected" / "palace"
    monkeypatch.setattr(hallways_mod, "list_hallways", fake_list)

    cmd_hallways(Namespace(wing="wing_aya", limit=50, palace=str(selected)))

    assert calls == [("wing_aya", str(selected))]


def test_prune_spellings_dry_run_then_apply(monkeypatch, capsys):
    calls = []

    def fake_prune(config=None, apply=False):
        calls.append(apply)
        return {
            "total": 10,
            "self_links": 2,
            "duplicates": 1,
            "by_wing": {"w": 3},
            "sample": ["main.zig ↔ src/main.zig"],
            "removed": 3 if apply else 0,
        }

    monkeypatch.setattr(hallways_mod, "prune_spelling_hallways", fake_prune)
    cmd_hallways(Namespace(wing=None, limit=50, prune_spellings=True, yes=False))
    out = capsys.readouterr().out
    assert "3 of 10 hallways are spelling artifacts" in out
    assert "Dry run" in out
    cmd_hallways(Namespace(wing=None, limit=50, prune_spellings=True, yes=True))
    assert "Removed 3." in capsys.readouterr().out
    assert calls == [False, True]


def test_rebuild_recomputes_every_wing(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: object())
    monkeypatch.setattr(
        "mempalace.palace_graph.sqlite_grouped_counts_reader",
        lambda config: lambda path, name: [("r", "b", "", 1), ("r", "a", "", 1)],
    )
    monkeypatch.setattr(
        hallways_mod,
        "compute_hallways_for_wing",
        lambda wing, col=None, config=None: (
            calls.append(wing) or [{"id": 1}] * (2 if wing == "a" else 1)
        ),
    )
    import contextlib

    locked = []

    @contextlib.contextmanager
    def fake_lock(path):
        locked.append(path)
        calls.append("<lock>")
        yield

    monkeypatch.setattr("mempalace.palace.mine_palace_lock", fake_lock)
    cmd_hallways(Namespace(wing=None, limit=50, rebuild=True, palace="/p"))
    out = capsys.readouterr().out
    # Scan and replace run under the palace writer lock, serialized with mines.
    assert locked == ["/p"] and calls[0] == "<lock>"
    calls.remove("<lock>")
    assert calls == ["a", "b"]
    assert "Rebuilt 3 hallways across 2 wing(s)." in out
    cmd_hallways(Namespace(wing="a", limit=50, rebuild=True, palace="/p"))
    assert calls[-1] == "a"


def test_rebuild_refuses_cleanly_when_the_palace_is_held(monkeypatch, capsys):
    import contextlib

    from mempalace.palace import MineAlreadyRunning

    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: object())

    @contextlib.contextmanager
    def held(path):
        raise MineAlreadyRunning(f"palace {path} is held by PID 42 (hub)")
        yield  # pragma: no cover

    monkeypatch.setattr("mempalace.palace.mine_palace_lock", held)
    with pytest.raises(SystemExit) as exc:
        cmd_hallways(Namespace(wing="a", limit=50, rebuild=True, palace="/p"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "held by PID 42" in err and "Traceback" not in err
