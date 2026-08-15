from pathlib import Path

import numpy as np
import pytest

from mempalace.miner import process_file
from mempalace.palace import get_closets_collection, get_collection
from mempalace.subject_refile import SubjectRefileError, subject_refile
from mempalace.subject_router import SubjectRoute


class FakeRouter:
    fingerprint = "sha256:" + "b" * 64
    version = "private-subjects-v1"

    def route_many(self, texts):
        routes = {
            "exact music evidence": SubjectRoute("music", "semantic", 0.81),
            "exact health evidence": SubjectRoute("health", "keyword", 2.0),
        }
        return [routes[text] for text in texts]


def _seed(palace: Path, source: Path):
    source.mkdir()
    first = source / "first.md"
    second = source / "second.md"
    first.write_text("exact music evidence")
    second.write_text("exact health evidence")
    collection = get_collection(str(palace))
    collection.upsert(
        ids=["drawer-one", "drawer-two", "manual-drawer"],
        documents=["exact music evidence", "exact health evidence", "manual evidence"],
        metadatas=[
            {
                "wing": "wing_daphne",
                "room": "continuity",
                "source_file": str(first.resolve()),
                "chunk_index": 0,
                "added_by": "daphne",
                "filed_at": "2026-07-31T00:00:00Z",
                "normalize_version": 2,
                "id_recipe": "v1",
                "source_mtime": first.stat().st_mtime,
            },
            {
                "wing": "wing_daphne",
                "room": "work",
                "source_file": str(second.resolve()),
                "chunk_index": 0,
                "added_by": "daphne",
                "filed_at": "2026-07-31T00:00:00Z",
                "normalize_version": 2,
                "id_recipe": "v1",
                "source_mtime": second.stat().st_mtime,
            },
            {
                "wing": "wing_daphne",
                "room": "presence",
                "source_file": str(first.resolve()),
                "chunk_index": 0,
                "added_by": "daphne",
                "filed_at": "2026-07-31T00:00:00Z",
                "normalize_version": 2,
                "id_recipe": "v1",
            },
        ],
    )
    return collection


def _dry_plan(palace: Path, source: Path) -> str:
    return subject_refile(
        str(palace),
        str(source),
        "wing_daphne",
        router=FakeRouter(),
        dry_run=True,
        allowed_source_roots=[str(source)],
    )["plan_sha256"]


def test_dry_run_reports_content_free_room_transitions(tmp_path):
    palace = tmp_path / "palace"
    source = tmp_path / "source"
    _seed(palace, source)

    report = subject_refile(
        str(palace),
        str(source),
        "wing_daphne",
        router=FakeRouter(),
        dry_run=True,
        allowed_source_roots=[str(source)],
    )

    assert report["eligible_drawers"] == 2
    assert report["moved_drawers"] == 2
    assert report["destinations"] == {"health": 1, "music": 1}
    assert report["plan_sha256"].startswith("sha256:")
    assert report["transitions"] == [
        {"from": "continuity", "to": "music", "count": 1},
        {"from": "work", "to": "health", "count": 1},
    ]
    assert "document" not in str(report)


def test_plan_fingerprint_binds_exact_drawer_content_and_metadata(tmp_path):
    palace = tmp_path / "palace"
    source = tmp_path / "source"
    collection = _seed(palace, source)

    first = subject_refile(
        str(palace),
        str(source),
        "wing_daphne",
        router=FakeRouter(),
        dry_run=True,
        allowed_source_roots=[str(source)],
    )
    collection.update(
        ids=["drawer-one"],
        documents=["changed but still exact music evidence"],
    )

    class ChangedRouter(FakeRouter):
        def route_many(self, texts):
            return [
                SubjectRoute(
                    "music" if "music" in text else "health",
                    "semantic" if "music" in text else "keyword",
                    0.81 if "music" in text else 2.0,
                )
                for text in texts
            ]

    second = subject_refile(
        str(palace),
        str(source),
        "wing_daphne",
        router=ChangedRouter(),
        dry_run=True,
        allowed_source_roots=[str(source)],
    )

    assert first["transitions"] == second["transitions"]
    assert first["plan_sha256"] != second["plan_sha256"]


def test_apply_changes_only_room_metadata_and_preserves_drawer_vectors(tmp_path):
    palace = tmp_path / "palace"
    source = tmp_path / "source"
    collection = _seed(palace, source)
    closets = get_closets_collection(str(palace))
    closets.upsert(
        ids=["amber-closet"],
        documents=["other wing pointer"],
        metadatas=[
            {
                "wing": "wing_amber",
                "room": "continuity",
                "source_file": str((source / "first.md").resolve()),
            }
        ],
    )
    before = collection.get(
        ids=["drawer-one", "drawer-two", "manual-drawer"],
        include=["documents", "embeddings", "metadatas"],
    )

    report = subject_refile(
        str(palace),
        str(source),
        "wing_daphne",
        router=FakeRouter(),
        dry_run=False,
        expected_plan_sha256=_dry_plan(palace, source),
        allowed_source_roots=[str(source)],
    )
    after = collection.get(
        ids=["drawer-one", "drawer-two", "manual-drawer"],
        include=["documents", "embeddings", "metadatas"],
    )

    assert report["dry_run"] is False
    assert after["ids"] == before["ids"]
    assert after["documents"] == before["documents"]
    assert np.allclose(np.asarray(after["embeddings"]), np.asarray(before["embeddings"]))
    by_id = dict(zip(after["ids"], after["metadatas"]))
    assert by_id["drawer-one"]["room"] == "music"
    assert by_id["drawer-two"]["room"] == "health"
    assert by_id["drawer-one"]["subject_policy"] == FakeRouter.fingerprint
    assert by_id["manual-drawer"]["room"] == "presence"
    assert "subject_policy" not in by_id["manual-drawer"]
    closet_rows = closets.get(include=["metadatas"])
    daphne_rooms = {
        metadata["room"]
        for metadata in closet_rows["metadatas"]
        if metadata["wing"] == "wing_daphne"
    }
    assert daphne_rooms == {
        "music",
        "health",
    }
    assert "amber-closet" in closet_rows["ids"]


def test_refile_rejects_a_parent_of_the_allowed_source(tmp_path):
    palace = tmp_path / "palace"
    source = tmp_path / "source"
    _seed(palace, source)

    with pytest.raises(SubjectRefileError, match="exactly match"):
        subject_refile(
            str(palace),
            str(tmp_path),
            "wing_daphne",
            router=FakeRouter(),
            allowed_source_roots=[str(source)],
        )


def test_failed_apply_rolls_back_drawer_metadata_and_clears_journal(tmp_path, monkeypatch):
    import mempalace.subject_refile as refile_module

    palace = tmp_path / "palace"
    source = tmp_path / "source"
    collection = _seed(palace, source)
    before = collection.get(ids=["drawer-one", "drawer-two"], include=["metadatas"])
    expected_plan_sha256 = _dry_plan(palace, source)
    real_rebuild = refile_module._rebuild_closets
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected closet failure")
        return real_rebuild(*args, **kwargs)

    monkeypatch.setattr(refile_module, "_rebuild_closets", fail_once)

    with pytest.raises(RuntimeError, match="injected closet failure"):
        subject_refile(
            str(palace),
            str(source),
            "wing_daphne",
            router=FakeRouter(),
            dry_run=False,
            expected_plan_sha256=expected_plan_sha256,
            allowed_source_roots=[str(source)],
        )

    after = collection.get(ids=["drawer-one", "drawer-two"], include=["metadatas"])
    assert dict(zip(after["ids"], after["metadatas"])) == dict(
        zip(before["ids"], before["metadatas"])
    )
    assert not (palace / ".subject-refile-journal.json").exists()


def test_apply_recovers_an_interrupted_journal_before_refiling(tmp_path):
    import mempalace.subject_refile as refile_module

    palace = tmp_path / "palace"
    source = tmp_path / "source"
    collection = _seed(palace, source)
    expected_plan_sha256 = _dry_plan(palace, source)
    rows = refile_module._eligible_rows(collection, source.resolve(), "wing_daphne")
    refile_module._route_rows(rows, FakeRouter())
    refile_module._write_journal(
        palace / ".subject-refile-journal.json",
        source.resolve(),
        "wing_daphne",
        rows,
    )
    refile_module._apply_rows(collection, rows, FakeRouter())

    report = subject_refile(
        str(palace),
        str(source),
        "wing_daphne",
        router=FakeRouter(),
        dry_run=False,
        expected_plan_sha256=expected_plan_sha256,
        allowed_source_roots=[str(source)],
    )

    assert report["recovered_pending_refile"] is True
    assert not (palace / ".subject-refile-journal.json").exists()
    after = collection.get(ids=["drawer-one", "drawer-two"], include=["metadatas"])
    assert {metadata["subject_policy"] for metadata in after["metadatas"]} == {
        FakeRouter.fingerprint
    }


def test_apply_rejects_missing_or_changed_expected_plan(tmp_path):
    palace = tmp_path / "palace"
    source = tmp_path / "source"
    _seed(palace, source)

    with pytest.raises(SubjectRefileError, match="requires an expected plan"):
        subject_refile(
            str(palace),
            str(source),
            "wing_daphne",
            router=FakeRouter(),
            dry_run=False,
            allowed_source_roots=[str(source)],
        )
    with pytest.raises(SubjectRefileError, match="plan changed"):
        subject_refile(
            str(palace),
            str(source),
            "wing_daphne",
            router=FakeRouter(),
            dry_run=False,
            expected_plan_sha256="sha256:" + "0" * 64,
            allowed_source_roots=[str(source)],
        )


def test_project_miner_routes_drawer_chunks_and_builds_room_scoped_closets(tmp_path):
    palace = tmp_path / "palace"
    project = tmp_path / "project"
    project.mkdir()
    source = project / "journal.md"
    source.write_text(("music evidence " * 20) + "\n\n" + ("health evidence " * 20))
    collection = get_collection(str(palace))
    closets = get_closets_collection(str(palace))

    class SequenceRouter(FakeRouter):
        def route_many(self, texts):
            assert len(texts) >= 2
            return [
                SubjectRoute("music" if index % 2 == 0 else "health", "semantic", 0.8)
                for index, _text in enumerate(texts)
            ]

    drawers, _room, skip_reason = process_file(
        source,
        project,
        collection,
        "wing_daphne",
        [{"name": "continuity", "description": "legacy", "keywords": []}],
        "daphne",
        False,
        closets_col=closets,
        chunk_size=120,
        chunk_overlap=0,
        min_chunk_size=1,
        subject_router=SequenceRouter(),
    )

    rows = collection.get(include=["metadatas"])
    assert skip_reason is None
    assert drawers == len(rows["ids"])
    assert {metadata["room"] for metadata in rows["metadatas"]} == {"music", "health"}
    assert {metadata["subject_policy"] for metadata in rows["metadatas"]} == {
        SequenceRouter.fingerprint
    }
    closet_rows = closets.get(include=["metadatas"])
    assert {metadata["room"] for metadata in closet_rows["metadatas"]} == {
        "music",
        "health",
    }


def test_operator_refile_tool_is_registered_as_dry_run_by_default():
    from mempalace import mcp_server

    tool = mcp_server.TOOLS["mempalace_subject_refile"]

    assert tool["handler"] is mcp_server.tool_subject_refile
    assert tool["input_schema"]["required"] == ["source", "wing"]
    assert "expected_plan_sha256" in tool["input_schema"]["properties"]
    assert "mempalace_subject_refile" in mcp_server._MUTATING_TOOLS
