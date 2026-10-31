"""Legacy graph imports must preview and report the actual preserved facts."""

import argparse
import hashlib
import sqlite3
from contextlib import closing

import pytest

from mempalace.knowledge_graph import KnowledgeGraph, import_graph_rows


def _files(directory):
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        # SQLite may create empty WAL/SHM bookkeeping even on a mode=ro open.
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))
    }


@pytest.mark.parametrize("existing_target", [False, True])
def test_preview_simulates_accepted_source_facts_without_changing_stored_graphs(
    tmp_path, existing_target
):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "nested" / "target.sqlite3"
    with KnowledgeGraph(str(source)) as graph:
        graph.add_triple("Alice", "likes", "Tea", valid_from="2024-01-01")
        graph.add_triple("Alice", "likes", "Coffee", valid_from="2024-01-01")
        graph.add_triple("Alice", "likes", "Cocoa", valid_from="2020-01-01", valid_to="2021-01-01")
    if existing_target:
        with KnowledgeGraph(str(target)) as graph:
            graph.add_triple("Bob", "likes", "Milk")
    before = _files(tmp_path)

    preview = import_graph_rows(str(source), str(target))

    assert _files(tmp_path) == before
    assert target.parent.exists() is existing_target
    assert preview["triples"]["added"] == 2  # one current plus the historical fact
    assert len(preview["triples"]["conflicts"]) == 1
    applied = import_graph_rows(str(source), str(target), apply=True)
    assert applied == preview
    with KnowledgeGraph(str(target)) as graph:
        facts = graph.query_entity("Alice")
    assert len(facts) == 2
    assert sum(f["current"] for f in facts) == 1


def _divergent_clone(tmp_path):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "palace" / "knowledge_graph.sqlite3"
    target.parent.mkdir()
    with KnowledgeGraph(str(source)) as graph:
        triple_id = graph.add_triple("Alice", "lives_in", "Paris", valid_from="2024-01-01")
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    with KnowledgeGraph(str(target)) as graph:
        graph.invalidate("Alice", "lives_in", "Paris", ended="2025-01-01")
    return source, target, triple_id


def test_id_collision_is_reported_without_resurrecting_target_fact(tmp_path):
    source, target, triple_id = _divergent_clone(tmp_path)
    before = _files(tmp_path)
    preview = import_graph_rows(str(source), str(target))
    assert _files(tmp_path) == before

    applied = import_graph_rows(str(source), str(target), apply=True)

    assert applied == preview
    assert applied["triples"]["added"] == 0
    assert applied["triples"]["present"] == 0
    assert applied["triples"]["id_conflicts"] == [triple_id]
    with KnowledgeGraph(str(target)) as graph:
        [fact] = graph.query_entity("Alice")
    assert fact["valid_to"] == "2025-01-01"
    assert fact["current"] is False
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before["source.sqlite3"]


@pytest.mark.parametrize("apply", [False, True])
def test_cli_identifies_unchanged_fact_id_collision(tmp_path, capsys, apply):
    from mempalace.cli import cmd_kg

    source, target, triple_id = _divergent_clone(tmp_path)
    cmd_kg(
        argparse.Namespace(
            kg_action="import-legacy",
            palace=str(target.parent),
            source=str(source),
            yes=apply,
        )
    )
    output = capsys.readouterr().out
    assert f"{'Copied' if apply else 'Would copy'} 0 triples" in output
    assert "1 fact ID conflict(s) not copied" in output
    assert triple_id in output


def test_semantic_duplicate_with_a_different_id_is_still_present(tmp_path):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    for path in (source, target):
        with KnowledgeGraph(str(path)) as graph:
            graph.add_triple("Alice", "likes", "Tea", valid_from="2024-01-01")
    with closing(sqlite3.connect(target)) as conn:
        conn.execute("UPDATE triples SET id = 'different-id'")
        conn.commit()

    preview = import_graph_rows(str(source), str(target))
    applied = import_graph_rows(str(source), str(target), apply=True)

    assert preview == applied
    assert applied["triples"]["added"] == 0
    assert applied["triples"]["present"] == 1
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT id FROM triples").fetchall() == [("different-id",)]
