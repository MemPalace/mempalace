"""Tests for the `hallways` CLI command."""

import json
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


def test_malformed_stored_count_is_skipped_before_cli_sort(monkeypatch, tmp_path, capsys):
    hallway_file = tmp_path / "hallways.json"
    monkeypatch.setattr(
        hallways_mod,
        "_get_hallway_file",
        lambda *args, **kwargs: str(hallway_file),
    )
    monkeypatch.setattr(
        hallways_mod,
        "_legacy_hallway_file",
        lambda: str(tmp_path / "legacy-hallways.json"),
    )
    valid = {
        "id": "valid",
        "wing": "wing_a",
        "entity_a": "A",
        "entity_b": "B",
        "co_occurrence_count": 2,
        "label": "A <-> B",
    }
    malformed = {
        **valid,
        "id": "malformed",
        "co_occurrence_count": "SECRET_BAD_COUNT",
    }
    hallway_file.write_text(
        json.dumps({"schema_version": 1, "hallways": [valid, malformed]}),
        encoding="utf-8",
    )

    cmd_hallways(Namespace(wing=None, limit=50, palace=None))

    captured = capsys.readouterr()
    assert "1 hallway(s)" in captured.out
    assert "A <-> B" in captured.out
    assert "SECRET_BAD_COUNT" not in captured.out
    assert "SECRET_BAD_COUNT" not in captured.err
