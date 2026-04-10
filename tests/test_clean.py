"""Tests for the `mempalace clean` subcommand."""

import json
import os
import sys

import chromadb
import pytest

from mempalace import cli
from mempalace.palace import count_drawers, delete_drawers


# ── palace.count_drawers / delete_drawers ────────────────────────────────


def test_count_drawers_by_wing(seeded_collection):
    assert count_drawers(seeded_collection, "project") == 3
    assert count_drawers(seeded_collection, "notes") == 1
    assert count_drawers(seeded_collection, "missing") == 0


def test_count_drawers_by_wing_and_room(seeded_collection):
    assert count_drawers(seeded_collection, "project", "backend") == 2
    assert count_drawers(seeded_collection, "project", "frontend") == 1
    assert count_drawers(seeded_collection, "project", "missing") == 0


def test_delete_drawers_by_wing_leaves_other_wings(seeded_collection):
    removed = delete_drawers(seeded_collection, "project")
    assert removed == 3
    assert count_drawers(seeded_collection, "project") == 0
    assert count_drawers(seeded_collection, "notes") == 1


def test_delete_drawers_by_room_leaves_sibling_rooms(seeded_collection):
    removed = delete_drawers(seeded_collection, "project", "backend")
    assert removed == 2
    assert count_drawers(seeded_collection, "project", "backend") == 0
    assert count_drawers(seeded_collection, "project", "frontend") == 1
    assert count_drawers(seeded_collection, "notes") == 1


def test_delete_drawers_nothing_to_remove_returns_zero(seeded_collection):
    assert delete_drawers(seeded_collection, "missing") == 0


# ── cli.cmd_clean ─────────────────────────────────────────────────────────


class _Args:
    """Minimal args namespace for invoking cmd_clean directly."""

    def __init__(self, palace, wing, room=None, dry_run=False, yes=True):
        self.palace = palace
        self.wing = wing
        self.room = room
        self.dry_run = dry_run
        self.yes = yes


def _seed_palace(palace_path):
    """Seed a palace with drawers and a compressed collection mirror."""
    client = chromadb.PersistentClient(path=palace_path)
    drawers = client.get_or_create_collection("mempalace_drawers")
    drawers.add(
        ids=["d_a", "d_b", "d_c", "d_d"],
        documents=["alpha", "beta", "gamma", "delta"],
        metadatas=[
            {"wing": "proj", "room": "backend"},
            {"wing": "proj", "room": "backend"},
            {"wing": "proj", "room": "frontend"},
            {"wing": "notes", "room": "planning"},
        ],
    )
    compressed = client.get_or_create_collection("mempalace_compressed")
    compressed.add(
        ids=["d_a", "d_b", "d_c", "d_d"],
        documents=["a", "b", "g", "d"],
        metadatas=[
            {"wing": "proj", "room": "backend"},
            {"wing": "proj", "room": "backend"},
            {"wing": "proj", "room": "frontend"},
            {"wing": "notes", "room": "planning"},
        ],
    )
    del client


def _read_collection(palace_path, name):
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_collection(name)
    result = col.get()
    del client
    return result


def test_clean_wing_removes_drawers_and_compressed(palace_path, capsys):
    _seed_palace(palace_path)
    cli.cmd_clean(_Args(palace=palace_path, wing="proj", yes=True))

    drawers = _read_collection(palace_path, "mempalace_drawers")
    compressed = _read_collection(palace_path, "mempalace_compressed")
    assert sorted(drawers["ids"]) == ["d_d"]
    assert sorted(compressed["ids"]) == ["d_d"]

    out = capsys.readouterr().out
    assert "Removed 3 drawer(s)" in out
    assert "Removed 3 compressed drawer(s)" in out


def test_clean_room_only_removes_matching_room(palace_path, capsys):
    _seed_palace(palace_path)
    cli.cmd_clean(_Args(palace=palace_path, wing="proj", room="backend", yes=True))

    drawers = _read_collection(palace_path, "mempalace_drawers")
    assert sorted(drawers["ids"]) == ["d_c", "d_d"]

    out = capsys.readouterr().out
    assert "Removed 2 drawer(s)" in out


def test_clean_dry_run_deletes_nothing(palace_path, capsys):
    _seed_palace(palace_path)
    cli.cmd_clean(_Args(palace=palace_path, wing="proj", dry_run=True, yes=True))

    drawers = _read_collection(palace_path, "mempalace_drawers")
    assert len(drawers["ids"]) == 4

    out = capsys.readouterr().out
    assert "dry run" in out
    assert "Removed" not in out


def test_clean_no_matches_is_graceful(palace_path, capsys):
    _seed_palace(palace_path)
    cli.cmd_clean(_Args(palace=palace_path, wing="ghost", yes=True))

    drawers = _read_collection(palace_path, "mempalace_drawers")
    assert len(drawers["ids"]) == 4

    out = capsys.readouterr().out
    assert "Nothing to clean" in out


def test_clean_prompt_cancel_preserves_drawers(palace_path, monkeypatch, capsys):
    _seed_palace(palace_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    cli.cmd_clean(_Args(palace=palace_path, wing="proj", yes=False))

    drawers = _read_collection(palace_path, "mempalace_drawers")
    assert len(drawers["ids"]) == 4

    out = capsys.readouterr().out
    assert "Cancelled" in out


def test_clean_prompt_yes_proceeds(palace_path, monkeypatch, capsys):
    _seed_palace(palace_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    cli.cmd_clean(_Args(palace=palace_path, wing="proj", yes=False))

    drawers = _read_collection(palace_path, "mempalace_drawers")
    assert sorted(drawers["ids"]) == ["d_d"]


def test_clean_works_without_compressed_collection(palace_path, capsys):
    # Seed only the drawers collection — no compressed mirror
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers")
    col.add(
        ids=["d_x"],
        documents=["x"],
        metadatas=[{"wing": "proj", "room": "backend"}],
    )
    del client

    cli.cmd_clean(_Args(palace=palace_path, wing="proj", yes=True))

    drawers = _read_collection(palace_path, "mempalace_drawers")
    assert drawers["ids"] == []

    out = capsys.readouterr().out
    assert "Removed 1 drawer(s)" in out
    # No compressed summary line when the collection doesn't exist
    assert "compressed" not in out.lower() or "Removed 0 compressed" not in out


def test_clean_writes_wal_entry(palace_path, tmp_dir, monkeypatch):
    _seed_palace(palace_path)
    # Redirect HOME so the WAL lands in tmp_dir
    monkeypatch.setenv("HOME", tmp_dir)

    cli.cmd_clean(_Args(palace=palace_path, wing="proj", room="backend", yes=True))

    wal_file = os.path.join(tmp_dir, ".mempalace", "wal", "write_log.jsonl")
    assert os.path.exists(wal_file)
    with open(wal_file) as f:
        entries = [json.loads(line) for line in f if line.strip()]
    clean_entries = [e for e in entries if e["operation"] == "clean_room"]
    assert len(clean_entries) == 1
    params = clean_entries[0]["params"]
    assert params["wing"] == "proj"
    assert params["room"] == "backend"
    assert params["drawers_deleted"] == 2


def test_clean_missing_wing_flag_exits_nonzero(palace_path, monkeypatch):
    _seed_palace(palace_path)
    monkeypatch.setattr(sys, "argv", ["mempalace", "clean"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code != 0


def test_clean_no_palace_exits_nonzero(tmp_dir, capsys):
    missing = os.path.join(tmp_dir, "does_not_exist")
    with pytest.raises(SystemExit):
        cli.cmd_clean(_Args(palace=missing, wing="proj", yes=True))
    out = capsys.readouterr().out
    assert "No palace found" in out
