"""Tests for mempalace.dream — non-destructive consolidation.

Focus on the dream *orchestration* contract (drawer dedup is covered by
test_dedup): the input palace is never mutated, KG contradictions are detected,
and opt-in retirement resolves them on the candidate only.
"""

import os

import chromadb

from mempalace.dream import detect_kg_conflicts, dream
from mempalace.knowledge_graph import KnowledgeGraph

KG_FILE = "knowledge_graph.sqlite3"


def _seed_palace(palace_path):
    """A tiny real palace: a few drawers plus a KG holding one genuine
    contradiction (Roy lives_in Denver→Austin) and one legitimately
    single-valued fact (Roy knows Alice) that must NOT be flagged.
    """
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
    col.add(
        ids=["d1", "d2", "d3"],
        documents=["alpha beta gamma", "alpha beta gamma", "wholly distinct content"],
        metadatas=[
            {"wing": "wing_x", "source_file": "a.txt"},
            {"wing": "wing_x", "source_file": "a.txt"},
            {"wing": "wing_x", "source_file": "b.txt"},
        ],
    )
    client.close()

    kg = KnowledgeGraph(db_path=os.path.join(palace_path, KG_FILE))
    kg.add_triple("Roy", "lives_in", "Denver", valid_from="2020-01-01")
    kg.add_triple("Roy", "lives_in", "Austin", valid_from="2025-01-01")
    kg.add_triple("Roy", "knows", "Alice", valid_from="2019-01-01")
    kg.close()


def _drawer_count(palace_path):
    client = chromadb.PersistentClient(path=palace_path)
    n = client.get_or_create_collection("mempalace_drawers").count()
    client.close()
    return n


def _conflict_keys(kg_db_path):
    return {(c["subject"], c["predicate"]) for c in detect_kg_conflicts(kg_db_path)}


def test_dream_is_nondestructive(palace_path):
    _seed_palace(palace_path)
    before = _drawer_count(palace_path)

    report = dream(palace_path, wing="wing_x")

    assert os.path.isdir(report["candidate"])
    assert report["candidate"] != report["palace"]
    assert report["palace"] == os.path.abspath(palace_path)
    # original drawer count and KG conflicts are untouched
    assert _drawer_count(palace_path) == before
    assert ("Roy", "lives_in") in _conflict_keys(os.path.join(palace_path, KG_FILE))


def test_dream_detects_kg_contradiction(palace_path):
    _seed_palace(palace_path)
    report = dream(palace_path, wing="wing_x")
    keys = {(c["subject"], c["predicate"]) for c in report["kg_conflicts"]}
    assert ("Roy", "lives_in") in keys  # divergent objects -> flagged
    assert ("Roy", "knows") not in keys  # single object -> not flagged


def test_dream_retire_conflicts_only_on_candidate(palace_path):
    _seed_palace(palace_path)
    report = dream(palace_path, wing="wing_x", retire_conflicts=True)

    assert report["kg_facts_retired"] >= 1
    # candidate contradiction resolved...
    assert ("Roy", "lives_in") not in _conflict_keys(os.path.join(report["candidate"], KG_FILE))
    # ...but the original palace still holds it (never mutated)
    assert ("Roy", "lives_in") in _conflict_keys(os.path.join(palace_path, KG_FILE))


def test_dream_default_review_only_keeps_conflicts(palace_path):
    _seed_palace(palace_path)
    report = dream(palace_path, wing="wing_x")  # retire_conflicts defaults False
    assert report["kg_facts_retired"] == 0
    assert ("Roy", "lives_in") in _conflict_keys(os.path.join(report["candidate"], KG_FILE))


def test_dream_missing_palace_raises(tmp_dir):
    import pytest

    with pytest.raises(FileNotFoundError):
        dream(os.path.join(tmp_dir, "nope"))


def test_cow_copytree_is_independent_on_write(tmp_dir):
    """The candidate must be independent of the source: whether the clone is
    copy-on-write or a deep copy, writing the clone must not change the source.
    This is what makes the dream non-destructive regardless of copy mechanism.
    """
    from mempalace.dream import _cow_copytree

    src = os.path.join(tmp_dir, "src")
    os.makedirs(os.path.join(src, "sub"))
    with open(os.path.join(src, "sub", "f.txt"), "w") as f:
        f.write("original")

    dst = os.path.join(tmp_dir, "dst")
    _cow_copytree(src, dst)
    assert open(os.path.join(dst, "sub", "f.txt")).read() == "original"

    with open(os.path.join(dst, "sub", "f.txt"), "w") as f:
        f.write("changed")
    assert open(os.path.join(src, "sub", "f.txt")).read() == "original"
    assert open(os.path.join(dst, "sub", "f.txt")).read() == "changed"
