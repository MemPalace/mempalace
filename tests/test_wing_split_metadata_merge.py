"""`wings split` rewrites only ``wing``/``last_modified``; every other key survives.

``_rekey_collection`` passes a partial ``{"wing", "last_modified"}`` dict to
``collection.update``. That is correct only because every backend's
``update`` merges metadata (chromadb's own update merges; SQLiteExact and the
remote backends merge explicitly; ``BaseCollection.update`` documents
get+merge). These tests pin that contract through the real CLI split on the
real backends a user gets, so a backend or chromadb change that turned update
into replace would fail here instead of silently wiping ``source_file``,
``room``, ``hall`` and the rest during a split.
"""

from argparse import Namespace

import pytest

from mempalace.backends import PalaceRef
from mempalace.config import MempalaceConfig
from mempalace.wing_split import plan_split, save_split_plan

_SRC = "/Users/test/.claude/projects/-Users-test-acme-portal/session.jsonl"
_RICH = {
    "wing": "convos",
    "room": "technical",
    "hall": "technical",
    "source_file": _SRC,
    "chunk_index": 3,
    "chunk_total": 9,
    "added_by": "mempalace",
    "ingest_mode": "convos",
    "entities": "build.sh",
    "filed_at": "2026-10-01T00:00:00",
    "source_mtime": 1791408530.5,
}


def _backend(name):
    if name == "chroma":
        from mempalace.backends.chroma import ChromaBackend

        return ChromaBackend()
    from mempalace.backends.sqlite_exact import SQLiteExactBackend

    return SQLiteExactBackend()


def _collections(backend, path):
    ref = PalaceRef(id=path, local_path=path)
    drawers = backend.get_collection(palace=ref, collection_name="mempalace_drawers", create=True)
    closets = backend.get_collection(palace=ref, collection_name="mempalace_closets", create=True)
    return drawers, closets


def _by_id(col):
    got = col.get(include=["metadatas"])
    return dict(zip(got.ids, got.metadatas))


@pytest.mark.parametrize("backend_name", ["chroma", "sqlite_exact"])
def test_backend_update_merges_partial_metadata(tmp_path, backend_name):
    """The contract `wings split` and `rooms apply` rely on."""
    backend = _backend(backend_name)
    try:
        drawers, _ = _collections(backend, str(tmp_path))
        drawers.add(
            ids=["d1"],
            documents=["exact original"],
            metadatas=[dict(_RICH)],
            embeddings=[[1.0, 0.0]],
        )
        drawers.update(ids=["d1"], metadatas=[{"wing": "portal", "last_modified": "now"}])
        assert _by_id(drawers)["d1"] == {**_RICH, "wing": "portal", "last_modified": "now"}
    finally:
        backend.close()


@pytest.mark.parametrize("backend_name", ["chroma", "sqlite_exact"])
def test_cli_wing_split_preserves_all_other_metadata(tmp_path, monkeypatch, backend_name):
    """501 drawers (two update batches) plus a closet, split through `cmd_wings`,
    then re-read through a freshly opened backend."""
    import mempalace.cli as cli

    path = str(tmp_path)
    cfg = MempalaceConfig(palace_path=path)
    backend = _backend(backend_name)
    drawers, closets = _collections(backend, path)
    ids = [f"d-{i:04d}" for i in range(501)]
    metas = [{**_RICH, "chunk_index": i} for i in range(501)]
    drawers.add(
        ids=ids, documents=["exact original"] * 501, metadatas=metas, embeddings=[[1.0, 0.0]] * 501
    )
    closet_meta = {**_RICH, "closet_kind": "probe"}
    closets.add(
        ids=["k-1"], documents=["index text"], metadatas=[closet_meta], embeddings=[[1.0, 0.0]]
    )
    before = {**_by_id(drawers), **{f"closet:{k}": v for k, v in _by_id(closets).items()}}
    plan = plan_split(drawers, "convos", ["portal"])
    assert len(plan["projects"]) == 1
    save_split_plan(cfg, plan)
    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: drawers)
    monkeypatch.setattr("mempalace.palace.get_closets_collection", lambda *a, **k: closets)
    cli.cmd_wings(Namespace(wings_action="split", palace=path, wing="convos", yes=True))
    backend.close()

    backend = _backend(backend_name)
    try:
        drawers, closets = _collections(backend, path)
        after = {**_by_id(drawers), **{f"closet:{k}": v for k, v in _by_id(closets).items()}}
    finally:
        backend.close()
    assert set(after) == set(before)
    for row_id, old in before.items():
        new = after[row_id]
        assert new["wing"] == "portal", row_id
        assert new["last_modified"], row_id
        untouched = {k: v for k, v in new.items() if k not in {"wing", "last_modified"}}
        expected = {k: v for k, v in old.items() if k not in {"wing", "last_modified"}}
        assert untouched == expected, (row_id, sorted(set(old) - set(new)))
