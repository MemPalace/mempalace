"""What goes when drawers go: closets, AAAK summaries, and saved revisions.

Every path that removes drawers on purpose also removes what was derived from
them, after the drawer change commits and without ever undoing it.
"""

from unittest.mock import patch

from _mcp_server_helpers import _patch_mcp_server

from mempalace.palace import (
    get_aaak_collection,
    get_closets_collection,
    get_collection,
    write_file_drawers,
)
from mempalace.revisions import append_drawer_revision, load_drawer_revisions


def _closets(palace, sources):
    get_closets_collection(palace, create=True).upsert(
        ids=[f"closet_{source}" for source in sources],
        documents=[f"topic →drawer_{source}" for source in sources],
        metadatas=[{"wing": "w", "room": "r", "source_file": source} for source in sources],
    )


def _closet_sources(palace):
    rows = get_closets_collection(palace, create=False).get(include=["metadatas"])
    return sorted(meta["source_file"] for meta in rows["metadatas"])


def _summarize(palace, rows):
    get_aaak_collection(palace, create=True).upsert(
        ids=[drawer_id for drawer_id, _ in rows],
        documents=[f"SUMMARY|{drawer_id}" for drawer_id, _ in rows],
        metadatas=[{"kind": "aaak", "source_file": source} for _, source in rows],
    )


def _summary_ids(palace):
    return sorted(get_aaak_collection(palace, create=False).get(include=[])["ids"])


def _revise(palace, rows):
    for drawer_id, source in rows:
        append_drawer_revision(palace, drawer_id, f"old {drawer_id}", {"source_file": source})


def _revised_ids(palace):
    return sorted({row["drawer_id"] for row in load_drawer_revisions(palace)})


def _seed_derived(palace):
    _closets(palace, ["auth.py", "db.py"])
    _summarize(
        palace,
        [
            ("drawer_proj_backend_aaa", "auth.py"),
            ("drawer_auth_older_mine", "auth.py"),
            ("drawer_proj_backend_bbb", "db.py"),
        ],
    )
    _revise(palace, [("drawer_proj_backend_aaa", "auth.py"), ("drawer_proj_backend_bbb", "db.py")])


def test_deleting_a_drawer_drops_its_source_closets_its_summary_and_its_revisions(
    monkeypatch, config, palace_path, seeded_collection, kg
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_delete_drawer

    _seed_derived(palace_path)

    result = tool_delete_drawer("drawer_proj_backend_aaa")

    assert result["success"] is True
    assert result["closets_deleted"] == 1
    assert result["revisions_deleted"] == 1
    assert _closet_sources(palace_path) == ["db.py"]
    # By id: the other summary of the same file stays with its drawer.
    assert _summary_ids(palace_path) == ["drawer_auth_older_mine", "drawer_proj_backend_bbb"]
    assert _revised_ids(palace_path) == ["drawer_proj_backend_bbb"]


def test_deleting_drawers_in_bulk_drops_what_each_one_had(
    monkeypatch, config, palace_path, seeded_collection, kg
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_delete_drawers

    _seed_derived(palace_path)

    result = tool_delete_drawers(["drawer_proj_backend_aaa", "drawer_proj_backend_bbb"])

    assert result["deleted"] == 2
    assert result["revisions_deleted"] == 2
    assert [row["closets_deleted"] for row in result["results"]] == [1, 1]
    assert _closet_sources(palace_path) == []
    assert _summary_ids(palace_path) == ["drawer_auth_older_mine"]
    assert _revised_ids(palace_path) == []


def test_deleting_a_source_drops_everything_derived_from_it(
    monkeypatch, config, palace_path, seeded_collection, kg
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_delete_by_source

    _seed_derived(palace_path)

    dry = tool_delete_by_source("auth.py", dry_run=True)
    assert dry["closet_match_count"] == 1
    assert _closet_sources(palace_path) == ["auth.py", "db.py"]

    result = tool_delete_by_source("auth.py", dry_run=False)

    assert result["success"] is True
    assert result["closets_deleted"] == 1
    assert result["revisions_deleted"] == 1
    assert _closet_sources(palace_path) == ["db.py"]
    assert _summary_ids(palace_path) == ["drawer_proj_backend_bbb"]
    assert _revised_ids(palace_path) == ["drawer_proj_backend_bbb"]


def test_rewriting_a_drawer_drops_its_closets_and_summary_but_keeps_revisions(
    monkeypatch, config, palace_path, seeded_collection, kg
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_update_drawer

    _seed_derived(palace_path)

    moved = tool_update_drawer("drawer_proj_backend_aaa", wing="project_moved")
    assert moved["closets_deleted"] == 0
    assert _closet_sources(palace_path) == ["auth.py", "db.py"]
    assert len(_summary_ids(palace_path)) == 3

    rewritten = tool_update_drawer("drawer_proj_backend_aaa", content="Sessions use opaque tokens.")

    assert rewritten["closets_deleted"] == 1
    assert _closet_sources(palace_path) == ["db.py"]
    assert _summary_ids(palace_path) == ["drawer_auth_older_mine", "drawer_proj_backend_bbb"]
    revisions = load_drawer_revisions(palace_path, "drawer_proj_backend_aaa")
    assert len(revisions) == 2


def test_a_failed_purge_never_undoes_the_delete(
    monkeypatch, config, palace_path, seeded_collection, kg
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_delete_drawer

    _seed_derived(palace_path)

    with (
        patch("mempalace.revisions.purge_drawer_revisions", side_effect=OSError("disk")),
        patch("mempalace.palace._delete_aaak_rows", side_effect=RuntimeError("aaak")),
    ):
        result = tool_delete_drawer("drawer_proj_backend_aaa")

    assert result["success"] is True
    assert result["revisions_deleted"] is None
    assert result["revisions_error"] == "revision purge failed: OSError"
    assert get_collection(palace_path).get(ids=["drawer_proj_backend_aaa"])["ids"] == []


def test_a_palace_without_derived_collections_deletes_cleanly(
    monkeypatch, config, palace_path, seeded_collection, kg
):
    _patch_mcp_server(monkeypatch, config, kg)
    from mempalace.mcp_server import tool_delete_drawer

    result = tool_delete_drawer("drawer_proj_backend_aaa")

    assert result["success"] is True
    assert result["closets_deleted"] == 0
    assert result["revisions_deleted"] == 0


def test_a_re_mine_drops_the_files_summaries(tmp_path):
    palace = str(tmp_path / "palace")
    drawers = get_collection(palace, create=True)
    meta = {"wing": "w", "room": "r", "source_file": "notes.md", "chunk_index": 0}
    write_file_drawers(drawers, "notes.md", [("drawer_notes_0", "old text", meta)])
    _summarize(palace, [("drawer_notes_0", "notes.md"), ("drawer_other_0", "other.md")])
    aaak = get_aaak_collection(palace, create=False)

    write_file_drawers(drawers, "notes.md", [("drawer_notes_0", "new text", meta)], aaak_col=aaak)

    assert _summary_ids(palace) == ["drawer_other_0"]


def test_prune_corrupt_drops_the_summaries_of_the_rows_it_removes(tmp_path, capsys):
    from mempalace.repair import prune_corrupt

    palace = str(tmp_path / "palace")
    col = get_collection(palace, create=True)
    col.upsert(
        ids=["drawer_good", "drawer_bad"],
        documents=["fine text", "corrupt text"],
        metadatas=[{"wing": "w", "room": "r", "source_file": "notes.md"}] * 2,
    )
    _summarize(palace, [("drawer_good", "notes.md"), ("drawer_bad", "notes.md")])
    (tmp_path / "palace" / "corrupt_ids.txt").write_text("drawer_bad\n")

    prune_corrupt(palace_path=palace, confirm=True)

    assert col.get(ids=["drawer_bad"])["ids"] == []
    assert _summary_ids(palace) == ["drawer_good"]
