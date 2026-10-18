"""AAAK summaries live in their own collection, not in the closets search reads."""

import argparse
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from _mcp_server_helpers import _patch_mcp_server

from mempalace.backends import CollectionNotInitializedError
from mempalace.cli import cmd_compress
from mempalace.palace import (
    AAAK_COLLECTION_NAME,
    get_aaak_collection,
    get_closets_collection,
    get_collection,
    is_aaak_row,
    move_aaak_out_of_closets,
    write_file_drawers,
)
from mempalace.searcher import _closet_boosts

_REAL_CLOSET = {"wing": "w", "room": "r", "source_file": "real.md", "drawer_count": 1}
# What ``mempalace compress`` wrote into the closets before this change: the
# drawer's own id and metadata plus its compression stats.
_LEGACY_AAAK = {
    "wing": "w",
    "room": "r",
    "source_file": "aaak.md",
    "compression_ratio": 4.2,
    "original_tokens": 120,
}


def _seed_closets(palace_path):
    closets = get_closets_collection(palace_path, create=True)
    closets.upsert(
        ids=["closet_w_r_abc_01", "drawer_w_r_aaak"],
        documents=[
            "kubernetes rollout notes →drawer_w_r_real",
            "KUBERNETES.ROLLOUT|notes.deploy|★★",
        ],
        metadatas=[_REAL_CLOSET, _LEGACY_AAAK],
    )
    return closets


def _compress(palace_path):
    args = argparse.Namespace(palace=palace_path, wing=None, dry_run=False, config=None)
    with patch("mempalace.cli.MempalaceConfig") as config:
        config.return_value.palace_path = palace_path
        cmd_compress(args)


def test_compress_writes_kind_aaak_rows_to_their_own_collection(tmp_path, capsys):
    palace = str(tmp_path / "palace")
    get_collection(palace, "mempalace_drawers", create=True).upsert(
        ids=["drawer-1"],
        documents=["We decided to roll out kubernetes on Friday after the review."],
        metadatas=[{"wing": "w", "room": "r", "source_file": "notes.md"}],
    )

    _compress(palace)

    assert f"'{AAAK_COLLECTION_NAME}'" in capsys.readouterr().out
    got = get_aaak_collection(palace, create=False).get(ids=["drawer-1"], include=["metadatas"])
    assert got["metadatas"][0]["kind"] == "aaak"
    with pytest.raises(CollectionNotInitializedError):
        get_closets_collection(palace, create=False)


def test_compress_moves_older_aaak_rows_out_of_the_closets(tmp_path):
    palace = str(tmp_path / "palace")
    get_collection(palace, "mempalace_drawers", create=True).upsert(
        ids=["drawer-1"],
        documents=["A drawer to compress."],
        metadatas=[{"wing": "w", "room": "r", "source_file": "notes.md"}],
    )
    closets = _seed_closets(palace)

    _compress(palace)

    assert closets.get(include=[])["ids"] == ["closet_w_r_abc_01"]
    moved = get_aaak_collection(palace, create=False).get(
        ids=["drawer_w_r_aaak"], include=["documents", "metadatas"]
    )
    assert moved["documents"] == ["KUBERNETES.ROLLOUT|notes.deploy|★★"]
    assert moved["metadatas"][0] == {**_LEGACY_AAAK, "kind": "aaak"}


def test_an_interrupted_move_keeps_the_closet_copy_until_the_aaak_copy_is_stored(
    tmp_path, monkeypatch
):
    palace = str(tmp_path / "palace")
    closets = _seed_closets(palace)
    aaak = get_aaak_collection(palace, create=True)

    def fail(*args, **kwargs):
        raise RuntimeError("killed before the copy was stored")

    monkeypatch.setattr(type(aaak), "upsert", fail)
    with pytest.raises(RuntimeError):
        move_aaak_out_of_closets(palace)
    assert "drawer_w_r_aaak" in closets.get(include=[])["ids"]

    monkeypatch.undo()
    assert move_aaak_out_of_closets(palace) == 1
    assert move_aaak_out_of_closets(palace) == 0
    assert get_aaak_collection(palace, create=False).get(ids=["drawer_w_r_aaak"])["ids"]


def test_an_older_aaak_row_no_longer_boosts_vector_closet_search(tmp_path):
    palace = str(tmp_path / "palace")
    closets = _seed_closets(palace)

    boosts = _closet_boosts(closets, query="kubernetes rollout", n_results=5, where={})

    assert set(boosts) == {"real.md"}


def test_an_older_aaak_row_no_longer_boosts_lexical_closet_search():
    hits = [
        SimpleNamespace(metadata=_LEGACY_AAAK, document="KUBERNETES.ROLLOUT|★★"),
        SimpleNamespace(metadata=_REAL_CLOSET, document="kubernetes rollout notes"),
    ]
    closets = SimpleNamespace(
        _backend=SimpleNamespace(capabilities=frozenset({"supports_lexical_search"})),
        lexical_search=lambda **kwargs: SimpleNamespace(hits=hits),
    )

    boosts = _closet_boosts(closets, query="kubernetes rollout", n_results=5, where={})

    # The AAAK row takes no rank: the real closet is first.
    assert boosts == {"real.md": (0, 0.0, "kubernetes rollout notes")}


def test_older_aaak_rows_crowding_the_window_do_not_hide_a_real_closet(tmp_path):
    palace = str(tmp_path / "palace")
    closets = get_closets_collection(palace, create=True)
    summaries = 12
    closets.upsert(
        ids=[f"drawer_w_r_{i}" for i in range(summaries)] + ["closet_w_r_real_01"],
        documents=["kubernetes rollout notes"] * summaries + ["kubernetes rollout plan"],
        metadatas=[{**_LEGACY_AAAK, "source_file": f"aaak{i}.md"} for i in range(summaries)]
        + [_REAL_CLOSET],
    )

    boosts = _closet_boosts(closets, query="kubernetes rollout notes", n_results=2, where={})

    assert set(boosts) == {"real.md"}
    assert boosts["real.md"][0] == 0


def test_closet_rows_are_never_mistaken_for_aaak():
    assert not is_aaak_row(_REAL_CLOSET)
    assert not is_aaak_row(None)
    assert is_aaak_row(_LEGACY_AAAK)
    assert is_aaak_row({"kind": "aaak"})


def test_repair_and_migrate_rebuild_the_aaak_collection(tmp_path):
    from mempalace import repair

    source = tmp_path / "source"
    get_collection(str(source), "mempalace_drawers", create=True).upsert(
        ids=["drawer-1"],
        documents=["verbatim drawer text"],
        metadatas=[{"wing": "w", "room": "r", "source_file": "notes.md"}],
    )
    get_aaak_collection(str(source), create=True).upsert(
        ids=["drawer-1"],
        documents=["VERBATIM.DRAWER|★"],
        metadatas=[{"wing": "w", "room": "r", "source_file": "notes.md", "kind": "aaak"}],
    )

    assert AAAK_COLLECTION_NAME in repair._recoverable_collections(str(source))
    dest = tmp_path / "dest"
    counts = repair.rebuild_from_sqlite(str(source), str(dest))

    assert counts[AAAK_COLLECTION_NAME] == 1
    got = get_aaak_collection(str(dest), create=False).get(
        ids=["drawer-1"], include=["documents", "metadatas"]
    )
    assert got["documents"] == ["VERBATIM.DRAWER|★"]
    assert got["metadatas"][0]["kind"] == "aaak"


def test_a_palace_without_aaak_rows_keeps_the_historical_collection_list(tmp_path):
    from mempalace import repair

    palace = tmp_path / "palace"
    get_collection(str(palace), "mempalace_drawers", create=True).upsert(
        ids=["drawer-1"], documents=["text"], metadatas=[{"wing": "w", "room": "r"}]
    )

    assert AAAK_COLLECTION_NAME not in repair._recoverable_collections(str(palace))


def _summarize(palace, rows):
    """File AAAK summaries the way ``compress`` does: ``(drawer_id, source_file)``."""
    get_aaak_collection(palace, create=True).upsert(
        ids=[drawer_id for drawer_id, _ in rows],
        documents=[f"SUMMARY|{drawer_id}" for drawer_id, _ in rows],
        metadatas=[{"kind": "aaak", "source_file": source} for _, source in rows],
    )


def _summary_ids(palace):
    return sorted(get_aaak_collection(palace, create=False).get(include=[])["ids"])


def test_deleting_a_drawer_deletes_its_summary(
    monkeypatch, config, palace_path, seeded_collection, kg
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_delete_drawer

    _summarize(
        palace_path,
        [("drawer_proj_backend_aaa", "auth.py"), ("drawer_proj_backend_bbb", "db.py")],
    )

    assert tool_delete_drawer("drawer_proj_backend_aaa")["success"] is True

    assert _summary_ids(palace_path) == ["drawer_proj_backend_bbb"]


def test_deleting_drawers_in_bulk_deletes_their_summaries(
    monkeypatch, config, palace_path, seeded_collection, kg
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_delete_drawers

    _summarize(
        palace_path,
        [
            ("drawer_proj_backend_aaa", "auth.py"),
            ("drawer_proj_backend_bbb", "db.py"),
            ("drawer_notes_planning_ddd", "sprint.md"),
        ],
    )

    tool_delete_drawers(["drawer_proj_backend_aaa", "drawer_proj_backend_bbb"])

    assert _summary_ids(palace_path) == ["drawer_notes_planning_ddd"]


def test_deleting_a_source_deletes_every_summary_from_it(
    monkeypatch, config, palace_path, seeded_collection, kg
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_delete_by_source

    _summarize(
        palace_path,
        [
            ("drawer_proj_backend_aaa", "auth.py"),
            ("drawer_from_an_older_mine", "auth.py"),
            ("drawer_proj_backend_bbb", "db.py"),
        ],
    )

    assert tool_delete_by_source("auth.py", dry_run=False)["success"] is True

    assert _summary_ids(palace_path) == ["drawer_proj_backend_bbb"]


def test_rewriting_a_drawers_text_deletes_its_summary(
    monkeypatch, config, palace_path, seeded_collection, kg
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_update_drawer

    _summarize(palace_path, [("drawer_proj_backend_aaa", "auth.py")])

    tool_update_drawer("drawer_proj_backend_aaa", wing="project_moved")
    assert _summary_ids(palace_path) == ["drawer_proj_backend_aaa"]

    tool_update_drawer("drawer_proj_backend_aaa", content="Sessions now use opaque tokens.")
    assert _summary_ids(palace_path) == []


def test_a_re_mine_deletes_the_files_summaries(tmp_path):
    palace = str(tmp_path / "palace")
    drawers = get_collection(palace, create=True)
    meta = {"wing": "w", "room": "r", "source_file": "notes.md", "chunk_index": 0}
    write_file_drawers(drawers, "notes.md", [("drawer_notes_0", "old text", meta)])
    _summarize(palace, [("drawer_notes_0", "notes.md"), ("drawer_other_0", "other.md")])
    aaak = get_aaak_collection(palace, create=False)

    write_file_drawers(drawers, "notes.md", [("drawer_notes_0", "new", meta)], prune=False)
    assert _summary_ids(palace) == ["drawer_notes_0", "drawer_other_0"]

    write_file_drawers(drawers, "notes.md", [("drawer_notes_0", "new text", meta)], aaak_col=aaak)
    assert _summary_ids(palace) == ["drawer_other_0"]


def test_migrate_moves_older_summaries_out_of_the_closets(tmp_path):
    from mempalace.backends.chroma import ChromaBackend
    from mempalace.migrate import migrate

    palace = str(tmp_path / "palace")
    # Seed through a backend that is closed again: Windows won't move a
    # palace aside while a handle inside it is open.
    backend = ChromaBackend()
    try:
        backend.get_or_create_collection(palace, "mempalace_drawers").add(
            ids=["drawer_w_r_aaak"],
            documents=["verbatim drawer text"],
            metadatas=[{"wing": "w", "room": "r", "source_file": "aaak.md"}],
        )
        backend.get_or_create_collection(palace, "mempalace_closets").add(
            ids=["closet_w_r_abc_01", "drawer_w_r_aaak"],
            documents=["kubernetes rollout notes", "KUBERNETES.ROLLOUT|★★"],
            metadatas=[_REAL_CLOSET, _LEGACY_AAAK],
        )
    finally:
        backend.close()

    with patch("mempalace.migrate.collection_write_roundtrip_works", return_value=False):
        assert migrate(palace, confirm=True) is True

    assert get_closets_collection(palace, create=False).get(include=[])["ids"] == [
        "closet_w_r_abc_01"
    ]
    assert _summary_ids(palace) == ["drawer_w_r_aaak"]


def test_repair_from_sqlite_moves_older_summaries_out_of_the_closets(tmp_path):
    from mempalace import repair

    source = str(tmp_path / "source")
    get_collection(source, "mempalace_drawers", create=True).upsert(
        ids=["drawer_w_r_aaak"], documents=["text"], metadatas=[{"wing": "w", "room": "r"}]
    )
    _seed_closets(source)
    dest = str(tmp_path / "dest")
    repair.rebuild_from_sqlite(source, dest)

    repair.move_aaak_after_rebuild(dest)

    assert get_closets_collection(dest, create=False).get(include=[])["ids"] == [
        "closet_w_r_abc_01"
    ]
    assert _summary_ids(dest) == ["drawer_w_r_aaak"]
