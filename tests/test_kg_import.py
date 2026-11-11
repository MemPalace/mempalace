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


def _legacy_fact(path, subject, predicate, obj):
    """Rows written with the v3.11 ID recipe, before punctuation was preserved."""
    from mempalace.knowledge_graph import legacy_entity_id

    with KnowledgeGraph(str(path)) as graph:
        conn = graph._conn()
        for name in (subject, obj):
            conn.execute(
                "INSERT INTO entities (id, name) VALUES (?, ?)", (legacy_entity_id(name), name)
            )
        conn.execute(
            "INSERT INTO triples (id, subject, predicate, object, valid_from) "
            "VALUES ('legacy-fact', ?, ?, ?, '2024-01-01')",
            (legacy_entity_id(subject), predicate, legacy_entity_id(obj)),
        )
        conn.commit()


@pytest.mark.parametrize(
    "legacy_name, other_name", [("O'Brien", "OBrien"), ("my_project", "my project")]
)
@pytest.mark.parametrize("endpoint", ["subject", "object"])
def test_legacy_import_distinguishes_names_with_the_same_stored_id(
    tmp_path, legacy_name, other_name, endpoint
):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    subject, obj = (legacy_name, "Paris") if endpoint == "subject" else ("Paris", legacy_name)
    _legacy_fact(source, subject, "knows", obj)
    with KnowledgeGraph(str(target)) as graph:
        graph.add_triple(other_name, "likes", "Coffee")
    before = _files(tmp_path)

    preview = import_graph_rows(str(source), str(target))
    assert _files(tmp_path) == before
    assert import_graph_rows(str(source), str(target), apply=True) == preview

    with KnowledgeGraph(str(target)) as graph:
        imported = graph.query_entity(legacy_name, direction="both")
        other = graph.query_entity(other_name, direction="both")
    assert [(f["subject"], f["predicate"], f["object"]) for f in imported] == [
        (subject, "knows", obj)
    ]
    assert [(f["subject"], f["predicate"], f["object"]) for f in other] == [
        (other_name, "likes", "Coffee")
    ]
    replay = import_graph_rows(str(source), str(target), apply=True)
    assert replay["triples"]["added"] == 0
    assert replay["triples"]["present"] == 1


@pytest.mark.parametrize("name", ["O'Brien", "my_project"])
@pytest.mark.parametrize("endpoint", ["subject", "object"])
def test_legacy_import_reuses_the_existing_canonical_name_across_id_recipes(
    tmp_path, name, endpoint
):
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    subject, obj = (name, "Paris") if endpoint == "subject" else ("Paris", name)
    _legacy_fact(source, subject, "knows", obj)
    with KnowledgeGraph(str(target)) as graph:
        entity_id = graph.add_entity(name)
        graph.add_triple(name, "likes", "Coffee")
    preview = import_graph_rows(str(source), str(target))

    assert import_graph_rows(str(source), str(target), apply=True) == preview

    with KnowledgeGraph(str(target)) as graph:
        facts = graph.query_entity(name, direction="both")
        assert {f["predicate"] for f in facts} == {"knows", "likes"}
        rows = graph._conn().execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchall()
        assert [r["id"] for r in rows] == [entity_id]
    replay = import_graph_rows(str(source), str(target), apply=True)
    assert replay["triples"]["added"] == 0
    assert replay["triples"]["present"] == 1


@pytest.mark.parametrize("endpoint", ["subject", "object"])
@pytest.mark.parametrize("apply", [False, True])
def test_legacy_import_reports_unresolvable_entities_and_their_dependent_facts(
    tmp_path, capsys, endpoint, apply
):
    from mempalace.cli import cmd_kg

    source = tmp_path / "source.sqlite3"
    target = tmp_path / "palace" / "knowledge_graph.sqlite3"
    subject, obj = ("OBrien", "Paris") if endpoint == "subject" else ("Paris", "OBrien")
    _legacy_fact(source, subject, "knows", obj)
    # An existing legacy row owns the modern ID for the distinct name OBrien.
    _legacy_fact(target, "O'Brien", "likes", "Coffee")
    cmd_kg(
        argparse.Namespace(
            kg_action="import-legacy", palace=str(target.parent), source=str(source), yes=apply
        )
    )
    output = capsys.readouterr().out
    assert "1 entity conflict(s)" in output
    assert "O'Brien" in output and "OBrien" in output
    assert "1 fact(s) not copied because their entities could not be resolved" in output
    assert "legacy-fact" in output
    with KnowledgeGraph(str(target)) as graph:
        assert [f["predicate"] for f in graph.query_entity("O'Brien")] == ["likes"]
