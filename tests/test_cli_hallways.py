"""Tests for the `hallways` CLI command."""

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
    monkeypatch.setattr(hallways_mod, "list_hallways", lambda wing=None: list(rows))
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
    monkeypatch.setattr(hallways_mod, "list_hallways", lambda wing=None: list(rows))
    cmd_hallways(Namespace(wing=None, limit=2))
    assert capsys.readouterr().out.count("<->") == 2


def test_negative_limit_shows_nothing_not_tail(monkeypatch, capsys):
    rows = [
        {"entity_a": f"E{i}", "entity_b": "X", "co_occurrence_count": i, "label": f"E{i} <-> X"}
        for i in range(5)
    ]
    monkeypatch.setattr(hallways_mod, "list_hallways", lambda wing=None: list(rows))
    cmd_hallways(Namespace(wing=None, limit=-2))
    # A negative limit must not slice from the end (which would print all-but-2).
    assert capsys.readouterr().out.count("<->") == 0


def test_empty_message(monkeypatch, capsys):
    monkeypatch.setattr(hallways_mod, "list_hallways", lambda wing=None: [])
    cmd_hallways(Namespace(wing="x", limit=50))
    assert "No hallways yet" in capsys.readouterr().out
