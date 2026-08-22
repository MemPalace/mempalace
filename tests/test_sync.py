"""
test_sync.py — Tests for `mempalace.sync` (gitignore-aware drawer prune, #1252).

Builds a focused fixture: a temp project with .gitignore + on-disk files +
matching drawers, exercising every classification bucket sync produces.
"""

import os
import shutil
from pathlib import Path

import chromadb
import pytest

from _chroma_palace_helper import make_minimal_chroma_sqlite


def _seed_drawers(palace_path, repo_path, deleted_path, elsewhere_path):
    """Populate the drawers collection with 6 entries covering all buckets."""
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})

    metas = [
        {
            "wing": "demo",
            "room": "src",
            "source_file": str(repo_path / "src" / "keep.py"),
            "chunk_index": 0,
            "added_by": "miner",
            "filed_at": "2026-05-09T00:00:00",
        },
        {
            "wing": "demo",
            "room": "build",
            "source_file": str(repo_path / "build" / "ignored.py"),
            "chunk_index": 0,
            "added_by": "miner",
            "filed_at": "2026-05-09T00:00:00",
        },
        {
            "wing": "demo",
            "room": "logs",
            "source_file": str(repo_path / "app.log"),
            "chunk_index": 0,
            "added_by": "miner",
            "filed_at": "2026-05-09T00:00:00",
        },
        {
            "wing": "demo",
            "room": "stale",
            "source_file": str(deleted_path),
            "chunk_index": 0,
            "added_by": "miner",
            "filed_at": "2026-05-09T00:00:00",
        },
        {
            "wing": "demo",
            "room": "convo",
            # No source_file key — convo / explicit-add drawers.
            "chunk_index": 0,
            "added_by": "convo_miner",
            "filed_at": "2026-05-09T00:00:00",
        },
        {
            "wing": "demo",
            "room": "elsewhere",
            "source_file": str(elsewhere_path),
            "chunk_index": 0,
            "added_by": "miner",
            "filed_at": "2026-05-09T00:00:00",
        },
    ]

    col.add(
        ids=[
            "drawer_keep",
            "drawer_gitignored_dir",
            "drawer_gitignored_glob",
            "drawer_missing",
            "drawer_no_source",
            "drawer_out_of_scope",
        ],
        documents=[f"doc {i}" for i in range(6)],
        embeddings=[[float(i + 1), 0.0, 0.0] for i in range(6)],
        metadatas=metas,
    )
    del client


@pytest.fixture
def synced_world(tmp_dir, palace_path):
    """Temp project with .gitignore + on-disk files + matching drawers."""
    repo_path = Path(tmp_dir) / "repo"
    (repo_path / "src").mkdir(parents=True)
    (repo_path / "build").mkdir()

    # .gitignore: ignore build/ directory and any *.log file
    (repo_path / ".gitignore").write_text("build/\n*.log\n")

    # Files that exist on disk
    (repo_path / "src" / "keep.py").write_text("# keep\n")
    (repo_path / "build" / "ignored.py").write_text("# ignored by gitignore\n")
    (repo_path / "app.log").write_text("log line\n")

    # File that the drawer points to but no longer exists
    deleted = repo_path / "deleted.py"
    deleted.write_text("# was here\n")
    deleted.unlink()

    # Use tmp_dir for an absolute path; `/tmp/...` literals are not absolute on Windows.
    elsewhere = Path(tmp_dir) / "elsewhere" / "x.md"

    _seed_drawers(palace_path, repo_path, deleted, elsewhere)
    return {"palace_path": palace_path, "repo_path": str(repo_path)}


def _open_drawers(palace_path):
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
    return client, col


def _drawer_ids(col):
    return set(col.get(include=[])["ids"])


class TestSyncPalace:
    def test_dry_run_classifies_correctly(self, synced_world):
        from mempalace.sync import sync_palace

        report = sync_palace(
            palace_path=synced_world["palace_path"],
            project_dirs=[synced_world["repo_path"]],
            dry_run=True,
        )
        assert report["scanned"] == 6
        assert report["gitignored"] == 2  # build/ignored.py, app.log
        assert report["missing"] == 1  # deleted.py
        assert report["no_source"] == 1
        assert report["out_of_scope"] == 1
        assert report["kept"] == 1  # only src/keep.py
        assert report["dry_run"] is True
        assert report["removed_drawers"] == 0

        # Mutation check — collection still has all 6 drawers.
        client, col = _open_drawers(synced_world["palace_path"])
        try:
            assert len(_drawer_ids(col)) == 6
        finally:
            del client

    def test_apply_removes_gitignored_and_missing(self, synced_world):
        from mempalace.sync import sync_palace

        report = sync_palace(
            palace_path=synced_world["palace_path"],
            project_dirs=[synced_world["repo_path"]],
            dry_run=False,
        )
        assert report["dry_run"] is False
        assert report["removed_drawers"] == 3  # 2 gitignored + 1 missing

        client, col = _open_drawers(synced_world["palace_path"])
        try:
            survivors = _drawer_ids(col)
            assert survivors == {
                "drawer_keep",
                "drawer_no_source",
                "drawer_out_of_scope",
            }
        finally:
            del client

    def test_dry_run_does_not_touch_collection(self, synced_world):
        from mempalace.sync import sync_palace

        client, col = _open_drawers(synced_world["palace_path"])
        before = _drawer_ids(col)
        del client

        sync_palace(
            palace_path=synced_world["palace_path"],
            project_dirs=[synced_world["repo_path"]],
            dry_run=True,
        )

        client, col = _open_drawers(synced_world["palace_path"])
        try:
            after = _drawer_ids(col)
        finally:
            del client
        assert before == after

    def test_wing_scope_filters(self, tmp_dir, palace_path):
        """A drawer in another wing must survive a wing-scoped sync."""
        from mempalace.sync import sync_palace

        repo_path = Path(tmp_dir) / "repo"
        (repo_path / "build").mkdir(parents=True)
        (repo_path / ".gitignore").write_text("build/\n")
        (repo_path / "build" / "ignored.py").write_text("# ignored\n")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=["d_demo", "d_other"],
            documents=["x", "y"],
            embeddings=[[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "build",
                    "source_file": str(repo_path / "build" / "ignored.py"),
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                },
                {
                    "wing": "other",
                    "room": "build",
                    "source_file": str(repo_path / "build" / "ignored.py"),
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                },
            ],
        )
        del client

        sync_palace(
            palace_path=palace_path,
            project_dirs=[str(repo_path)],
            wing="demo",
            dry_run=False,
        )

        client, col = _open_drawers(palace_path)
        try:
            assert _drawer_ids(col) == {"d_other"}
        finally:
            del client

    def test_no_source_file_drawers_preserved_on_apply(self, synced_world):
        from mempalace.sync import sync_palace

        sync_palace(
            palace_path=synced_world["palace_path"],
            project_dirs=[synced_world["repo_path"]],
            dry_run=False,
        )
        client, col = _open_drawers(synced_world["palace_path"])
        try:
            assert "drawer_no_source" in _drawer_ids(col)
        finally:
            del client

    def test_out_of_scope_drawers_preserved(self, synced_world):
        from mempalace.sync import sync_palace

        sync_palace(
            palace_path=synced_world["palace_path"],
            project_dirs=[synced_world["repo_path"]],
            dry_run=False,
        )
        client, col = _open_drawers(synced_world["palace_path"])
        try:
            assert "drawer_out_of_scope" in _drawer_ids(col)
        finally:
            del client

    def test_negated_gitignore_rules_respected(self, tmp_dir, palace_path):
        """`!build/keep.py` must un-ignore one specific file under build/."""
        from mempalace.sync import sync_palace

        repo_path = Path(tmp_dir) / "repo"
        (repo_path / "build").mkdir(parents=True)
        (repo_path / ".gitignore").write_text("build/\n!build/keep.py\n")
        (repo_path / "build" / "keep.py").write_text("# survivor\n")
        (repo_path / "build" / "doomed.py").write_text("# doomed\n")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=["d_keep", "d_doom"],
            documents=["x", "y"],
            embeddings=[[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "build",
                    "source_file": str(repo_path / "build" / "keep.py"),
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                },
                {
                    "wing": "demo",
                    "room": "build",
                    "source_file": str(repo_path / "build" / "doomed.py"),
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                },
            ],
        )
        del client

        sync_palace(
            palace_path=palace_path,
            project_dirs=[str(repo_path)],
            dry_run=False,
        )

        client, col = _open_drawers(palace_path)
        try:
            survivors = _drawer_ids(col)
        finally:
            del client
        assert "d_keep" in survivors
        assert "d_doom" not in survivors

    def test_nested_gitignore_layers(self, tmp_dir, palace_path):
        """Subdir .gitignore can deny what root allows."""
        from mempalace.sync import sync_palace

        repo_path = Path(tmp_dir) / "repo"
        (repo_path / "vendor").mkdir(parents=True)
        # Root gitignore is empty.
        (repo_path / ".gitignore").write_text("\n")
        # Subdir gitignore ignores everything under vendor/.
        (repo_path / "vendor" / ".gitignore").write_text("*.py\n")
        (repo_path / "vendor" / "lib.py").write_text("# nested-ignored\n")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=["d_nested"],
            documents=["x"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "vendor",
                    "source_file": str(repo_path / "vendor" / "lib.py"),
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                }
            ],
        )
        del client

        sync_palace(
            palace_path=palace_path,
            project_dirs=[str(repo_path)],
            dry_run=False,
        )

        client, col = _open_drawers(palace_path)
        try:
            assert "d_nested" not in _drawer_ids(col)
        finally:
            del client

    def test_closet_purge_runs_on_apply(self, synced_world):
        """Closets pointing at removed sources must also disappear."""
        from mempalace.sync import sync_palace

        # Seed a closet referencing the to-be-pruned ignored.py source.
        client = chromadb.PersistentClient(path=synced_world["palace_path"])
        closets = client.get_or_create_collection(
            "mempalace_closets", metadata={"hnsw:space": "cosine"}
        )
        ignored_path = str(Path(synced_world["repo_path"]) / "build" / "ignored.py")
        closets.add(
            ids=["closet_ignored_01"],
            documents=["topic line"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "build",
                    "source_file": ignored_path,
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                }
            ],
        )
        del client

        report = sync_palace(
            palace_path=synced_world["palace_path"],
            project_dirs=[synced_world["repo_path"]],
            dry_run=False,
        )
        assert report["removed_closets"] >= 1

        client = chromadb.PersistentClient(path=synced_world["palace_path"])
        closets = client.get_or_create_collection(
            "mempalace_closets", metadata={"hnsw:space": "cosine"}
        )
        try:
            assert closets.get(ids=["closet_ignored_01"])["ids"] == []
        finally:
            del client

    def test_handles_empty_palace(self, palace_path):
        from mempalace.sync import sync_palace

        client = chromadb.PersistentClient(path=palace_path)
        client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
        del client

        report = sync_palace(palace_path=palace_path, dry_run=True)
        assert report["scanned"] == 0
        assert report["removed_drawers"] == 0

    def test_emits_wal_entries_on_apply(self, synced_world):
        from mempalace.sync import sync_palace

        seen = []

        def fake_wal(operation, params, result=None):
            seen.append((operation, params, result))

        sync_palace(
            palace_path=synced_world["palace_path"],
            project_dirs=[synced_world["repo_path"]],
            dry_run=False,
            wal_log=fake_wal,
        )

        ops = [op for op, _, _ in seen]
        assert "sync_prune" in ops
        # F4 — result payload carries the audit trail.
        sync_entry = next(e for e in seen if e[0] == "sync_prune")
        op, params, result = sync_entry
        assert result is not None and "removed_count" in result
        assert result["removed_count"] >= 1
        # Allow-list — params must be exactly the documented audit shape so
        # any future leak (source_file, content, ID lists, etc.) trips a
        # test failure rather than slipping through a deny-list.
        assert set(params.keys()) <= {"first_id"}, (
            f"WAL params drifted from the audit allow-list: {params.keys()}"
        )

    def test_registry_sentinels_preserved_on_apply(self, tmp_dir, palace_path):
        """F2 regression: convo miner `_reg_*` sentinels must survive sync apply.

        Deleting them forces full re-mine + re-embed of the transcript on the
        next miner run, even though the transcript content has not changed.
        """
        from mempalace.sync import sync_palace

        repo_path = Path(tmp_dir) / "repo"
        repo_path.mkdir(parents=True)
        (repo_path / ".gitignore").write_text("transcripts/\n")
        (repo_path / "transcripts").mkdir()
        moved_transcript = repo_path / "transcripts" / "convo.jsonl"
        moved_transcript.write_text("{}\n")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=[
                "_reg_abc123_room_match",
                "_reg_def456_meta_match",
                "_reg_ghi789_id_match",
            ],
            documents=["[registry] x", "[registry] y", "[registry] z"],
            embeddings=[[1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [3.0, 0.0, 0.0]],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "_registry",
                    "source_file": str(moved_transcript),
                    "chunk_index": 0,
                    "added_by": "convo_miner",
                    "filed_at": "2026-05-09T00:00:00",
                },
                {
                    "wing": "demo",
                    "room": "convo",
                    "source_file": str(moved_transcript),
                    "chunk_index": 0,
                    "added_by": "convo_miner",
                    "filed_at": "2026-05-09T00:00:00",
                    "ingest_mode": "registry",
                },
                {
                    "wing": "demo",
                    "room": "convo",
                    "source_file": str(moved_transcript),
                    "chunk_index": 0,
                    "added_by": "convo_miner",
                    "filed_at": "2026-05-09T00:00:00",
                },
            ],
        )
        del client

        # Sentinel transcript is gitignored; without F2 it would also delete
        # the `_reg_*` sentinel rows.
        sync_palace(
            palace_path=palace_path,
            project_dirs=[str(repo_path)],
            dry_run=False,
        )

        client, col = _open_drawers(palace_path)
        try:
            survivors = _drawer_ids(col)
        finally:
            del client
        assert "_reg_abc123_room_match" in survivors  # room=_registry
        assert "_reg_def456_meta_match" in survivors  # ingest_mode=registry
        assert "_reg_ghi789_id_match" in survivors  # id prefix

    def test_auto_detect_picks_deepest_root(self, tmp_dir, palace_path):
        """F3 regression (white-box): when multiple ancestors hold markers
        the DEEPEST one wins. Direct assertion on the helper avoids the
        tautology of round-1's classifier-based test where ancestor walks
        loaded the same matcher chain regardless of which root was picked.
        """
        from mempalace.sync import _auto_detect_project_roots

        outer = Path(tmp_dir) / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        # Both have markers. Deepest wins.
        (outer / ".gitignore").write_text("*.txt\n")
        (inner / ".gitignore").write_text("*.py\n")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=["d_inner"],
            documents=["x"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "src",
                    "source_file": str(inner / "x.py"),
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                }
            ],
        )
        del client

        client, col = _open_drawers(palace_path)
        try:
            roots = _auto_detect_project_roots(col, wing="demo")
        finally:
            del client

        inner_resolved = inner.resolve(strict=False)
        outer_resolved = outer.resolve(strict=False)
        assert inner_resolved in roots, f"expected inner in roots, got {roots}"
        assert outer_resolved not in roots, (
            f"deepest should win exclusively: roots={roots}, outer leaked"
        )

    def test_apply_with_empty_project_dirs_raises(self, palace_path):
        """Round-2 P1: `project_dirs=[]` (empty list) with apply must raise,
        not silently classify everything as out_of_scope."""
        from mempalace.sync import sync_palace

        client = chromadb.PersistentClient(path=palace_path)
        client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
        del client

        with pytest.raises(ValueError, match="empty"):
            sync_palace(
                palace_path=palace_path,
                project_dirs=[],
                wing="demo",
                dry_run=False,
            )

    def test_closet_log_warning_when_collection_unavailable(
        self, monkeypatch, synced_world, caplog
    ):
        """F7 regression: closets-collection-missing logs a warning."""
        import logging

        from mempalace import sync as sync_mod
        from mempalace.sync import sync_palace

        def boom(*args, **kwargs):
            raise RuntimeError("simulated missing closets collection")

        monkeypatch.setattr(sync_mod, "get_closets_collection", boom)

        with caplog.at_level(logging.WARNING, logger="mempalace.sync"):
            sync_palace(
                palace_path=synced_world["palace_path"],
                project_dirs=[synced_world["repo_path"]],
                dry_run=False,
            )
        assert any("Closet purge skipped" in record.getMessage() for record in caplog.records), (
            f"expected closet-skip warning, got: {[r.getMessage() for r in caplog.records]}"
        )

    def test_metadata_cache_cleared_on_exception(self, monkeypatch, config, synced_world, kg):
        """F9 regression: tool_sync's try/finally must clear `_metadata_cache`
        even if sync_palace raises mid-apply.

        Tracks an explicit `called` flag on the explode mock so a refactor
        that bypasses the patched name (and lets the real sync_palace run)
        cannot fake-pass — the assertion below verifies the patched explode
        actually ran before the cache was cleared.
        """
        from mempalace import mcp_server

        # Reconfigure to point at synced_world.
        from mempalace.config import MempalaceConfig
        import json

        cfg_dir = Path(synced_world["palace_path"]).parent / "cfg_for_cache_test"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg_dir / "config.json", "w") as f:
            json.dump({"palace_path": synced_world["palace_path"]}, f)
        monkeypatch.setattr(mcp_server, "_config", MempalaceConfig(config_dir=str(cfg_dir)))
        monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)
        monkeypatch.setattr(mcp_server, "_metadata_cache", ["dirty-cache-marker"])

        called = {"n": 0}

        def explode(*args, **kwargs):
            called["n"] += 1
            raise RuntimeError("simulated mid-apply failure")

        monkeypatch.setattr("mempalace.sync.sync_palace", explode)

        # tool_sync's broad except catches RuntimeError → returns structured error.
        result = mcp_server.tool_sync(
            project_dir=synced_world["repo_path"], wing="demo", apply=True
        )
        assert called["n"] == 1, "explode mock did not actually run; test is a fake-pass"
        assert result.get("success") is False
        assert "simulated" in result.get("error", "")

        assert mcp_server._metadata_cache is None, (
            "F9: cache must be cleared even when sync_palace raises"
        )

    def test_sync_report_keys_stable(self, synced_world):
        """Regression: SyncReport schema must not silently drop a field."""
        from mempalace.sync import sync_palace

        report = sync_palace(
            palace_path=synced_world["palace_path"],
            project_dirs=[synced_world["repo_path"]],
            dry_run=True,
        )
        expected = {
            "scanned",
            "kept",
            "gitignored",
            "missing",
            "unresolved",
            "no_source",
            "out_of_scope",
            "removed_drawers",
            "removed_closets",
            "dry_run",
            "by_source",
            "unresolved_by_source",
        }
        assert set(report.keys()) == expected

    def test_batch_size_boundary(self, tmp_dir, palace_path):
        """`_delete_in_batches` correctness at batch_size smaller than dataset."""
        from mempalace.sync import sync_palace

        repo_path = Path(tmp_dir) / "repo"
        repo_path.mkdir(parents=True)
        (repo_path / ".gitignore").write_text("ignored/\n")
        (repo_path / "ignored").mkdir()
        n = 5
        for i in range(n):
            (repo_path / "ignored" / f"f{i}.py").write_text(f"# {i}\n")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=[f"d_{i}" for i in range(n)],
            documents=[f"x{i}" for i in range(n)],
            embeddings=[[float(i + 1), 0.0, 0.0] for i in range(n)],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "ignored",
                    "source_file": str(repo_path / "ignored" / f"f{i}.py"),
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                }
                for i in range(n)
            ],
        )
        del client

        seen = []

        def fake_wal(operation, params, result=None):
            if operation == "sync_prune":
                seen.append(result["removed_count"])

        report = sync_palace(
            palace_path=palace_path,
            project_dirs=[str(repo_path)],
            wing="demo",
            dry_run=False,
            batch_size=2,
            wal_log=fake_wal,
        )
        assert report["removed_drawers"] == n
        # 5 ids at batch_size=2 → chunks of 2,2,1 → 3 wal entries
        assert seen == [2, 2, 1], f"unexpected chunk sizes: {seen}"

    def test_apply_is_idempotent(self, synced_world):
        """Round-3: a second apply on the same palace must be a no-op."""
        from mempalace.sync import sync_palace

        first = sync_palace(
            palace_path=synced_world["palace_path"],
            project_dirs=[synced_world["repo_path"]],
            dry_run=False,
        )
        assert first["removed_drawers"] >= 1

        second = sync_palace(
            palace_path=synced_world["palace_path"],
            project_dirs=[synced_world["repo_path"]],
            dry_run=False,
        )
        assert second["removed_drawers"] == 0
        assert second["gitignored"] == 0
        assert second["missing"] == 0

    def test_relative_source_file_classified_as_no_source(self, tmp_dir, palace_path):
        """Round-3: a drawer whose source_file metadata is relative is upstream
        corruption (miner writes absolute paths). Sync must NOT guess at
        path resolution; it routes the drawer to `no_source` and leaves it."""
        from mempalace.sync import sync_palace

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=["d_relative"],
            documents=["x"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "src",
                    "source_file": "relative/path.py",  # malformed, not absolute
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                }
            ],
        )
        del client

        repo_path = Path(tmp_dir) / "repo"
        repo_path.mkdir()
        (repo_path / ".gitignore").write_text("*.py\n")

        report = sync_palace(
            palace_path=palace_path,
            project_dirs=[str(repo_path)],
            wing="demo",
            dry_run=False,
        )
        assert report["no_source"] == 1
        assert report["removed_drawers"] == 0

        client, col = _open_drawers(palace_path)
        try:
            assert "d_relative" in _drawer_ids(col)
        finally:
            del client

    def test_overlapping_project_dirs_picks_longest(self, tmp_dir, palace_path):
        """`_resolve_project_root` longest-prefix matching: nested project
        dirs both contain the source; the deeper (longer) one wins."""
        from mempalace.sync import sync_palace

        outer = Path(tmp_dir) / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        # Outer .gitignore would NOT block file. Inner .gitignore blocks it.
        (outer / ".gitignore").write_text("# empty\n")
        (inner / ".gitignore").write_text("x.py\n")
        (inner / "x.py").write_text("# inner-ignored\n")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=["d_x"],
            documents=["x"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "src",
                    "source_file": str(inner / "x.py"),
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                }
            ],
        )
        del client

        # Pass BOTH outer AND inner as project_dirs. inner is the longest
        # prefix, so it should be the chosen root and inner/.gitignore
        # rules apply (file is ignored → drawer removed).
        report = sync_palace(
            palace_path=palace_path,
            project_dirs=[str(outer), str(inner)],
            wing="demo",
            dry_run=False,
        )
        assert report["gitignored"] == 1, f"expected 1 gitignored, got {report}"

    def test_apply_without_scope_raises(self, palace_path):
        """F6: apply=True with both wing=None AND project_dirs=None refuses."""
        from mempalace.sync import sync_palace

        # Empty palace; we never reach delete code, but the guard must fire
        # before any work.
        client = chromadb.PersistentClient(path=palace_path)
        client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
        del client

        with pytest.raises(ValueError, match="explicit wing="):
            sync_palace(palace_path=palace_path, dry_run=False)

        # Dry-run with no scope is still allowed — preview is read-only.
        report = sync_palace(palace_path=palace_path, dry_run=True)
        assert report["dry_run"] is True

    @pytest.mark.skipif(os.name == "nt", reason="fcntl-based contention test is POSIX only")
    def test_mine_already_running_propagates(self, synced_world):
        """F1 + T4: sync acquires `mine_palace_lock` for the whole call.

        Hold the palace lock via raw fcntl on a separate open file
        description; mine_palace_lock opens its own handle and must
        raise MineAlreadyRunning rather than silently running against
        a partial snapshot.
        """
        import fcntl
        import hashlib

        from mempalace.palace import MineAlreadyRunning
        from mempalace.sync import sync_palace

        palace_path = synced_world["palace_path"]
        resolved = os.path.realpath(os.path.expanduser(palace_path))
        palace_key = hashlib.sha256(os.path.normcase(resolved).encode()).hexdigest()[:16]
        lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, f"mine_palace_{palace_key}.lock")
        Path(lock_path).touch()

        with open(lock_path, "r+") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with pytest.raises(MineAlreadyRunning):
                    sync_palace(
                        palace_path=palace_path,
                        project_dirs=[synced_world["repo_path"]],
                        dry_run=True,
                    )
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

        # Lock released — sync now succeeds.
        sync_palace(
            palace_path=palace_path,
            project_dirs=[synced_world["repo_path"]],
            dry_run=True,
        )

    @pytest.mark.skipif(os.name == "nt", reason="os.symlink needs admin on Windows")
    def test_symlinked_project_root_resolves(self, tmp_dir, palace_path):
        """source_file may be written through a symlinked tmp directory
        (real macOS behaviour: /var/folders/... is a symlink to
        /private/var/folders/...). project_dirs goes through .resolve()
        which follows the symlink. Without matching .resolve() on the
        source side, _resolve_project_root would mis-bucket every drawer
        as out_of_scope. This test pins symmetric resolution.
        """
        from mempalace.sync import sync_palace

        real_root = Path(tmp_dir) / "real"
        (real_root / "build").mkdir(parents=True)
        (real_root / ".gitignore").write_text("build/\n")
        (real_root / "build" / "x.py").write_text("# ignored\n")

        link_root = Path(tmp_dir) / "link"
        os.symlink(str(real_root), str(link_root))

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=["d_via_link"],
            documents=["x"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "build",
                    "source_file": str(link_root / "build" / "x.py"),
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                }
            ],
        )
        del client

        report = sync_palace(
            palace_path=palace_path,
            project_dirs=[str(real_root)],
            wing="demo",
            dry_run=True,
        )
        assert report["gitignored"] == 1, (
            f"symmetric resolve broken: drawer mis-bucketed; report={report}"
        )
        assert report["out_of_scope"] == 0

    def test_classification_cache_avoids_redundant_disk_hits(
        self, tmp_dir, palace_path, monkeypatch
    ):
        """Per-file classification cache: N chunks of the same source_file
        cost one _classify_drawer invocation, not N. Verifies the perf
        optimisation actually short-circuits without changing behaviour.
        """
        from mempalace import sync as sync_mod
        from mempalace.sync import sync_palace

        repo_path = Path(tmp_dir) / "repo"
        (repo_path / "build").mkdir(parents=True)
        (repo_path / ".gitignore").write_text("build/\n")
        (repo_path / "build" / "shared.py").write_text("# ignored\n")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=[f"d_chunk_{i}" for i in range(5)],
            documents=[f"chunk{i}" for i in range(5)],
            embeddings=[[float(i + 1), 0.0, 0.0] for i in range(5)],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "build",
                    "source_file": str(repo_path / "build" / "shared.py"),
                    "chunk_index": i,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                }
                for i in range(5)
            ],
        )
        del client

        call_count = {"n": 0}
        real_classify = sync_mod._classify_drawer

        def counting_classify(*args, **kwargs):
            call_count["n"] += 1
            return real_classify(*args, **kwargs)

        monkeypatch.setattr(sync_mod, "_classify_drawer", counting_classify)

        report = sync_palace(
            palace_path=palace_path,
            project_dirs=[str(repo_path)],
            wing="demo",
            dry_run=True,
        )
        assert report["scanned"] == 5
        assert report["gitignored"] == 5
        assert call_count["n"] == 1, (
            f"cache miss: expected 1 _classify_drawer call (4 cache hits), got {call_count['n']}"
        )

    def test_closet_batch_purge_single_call(self, synced_world, monkeypatch):
        """Batched $in closet purge: one delete() call across all removable
        source files, not N. Wraps the real collection so chromadb still
        does the work; only the call count is intercepted.
        """
        from mempalace import sync as sync_mod

        repo_path = Path(synced_world["repo_path"])
        palace_path = synced_world["palace_path"]

        client = chromadb.PersistentClient(path=palace_path)
        closets_col = client.get_or_create_collection(
            "mempalace_closets", metadata={"hnsw:space": "cosine"}
        )
        closets_col.add(
            ids=["c1", "c2", "c3"],
            documents=["c1", "c2", "c3"],
            embeddings=[[1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [3.0, 0.0, 0.0]],
            metadatas=[
                {"source_file": str(repo_path / "build" / "ignored.py")},
                {"source_file": str(repo_path / "app.log")},
                {"source_file": str(repo_path / "deleted.py")},
            ],
        )
        del client

        class CallCountingCol:
            def __init__(self, real):
                self._real = real
                self.delete_calls = 0
                self.get_calls = 0

            def get(self, *args, **kwargs):
                self.get_calls += 1
                return self._real.get(*args, **kwargs)

            def delete(self, *args, **kwargs):
                self.delete_calls += 1
                return self._real.delete(*args, **kwargs)

        captured: dict = {}
        real_get_closets = sync_mod.get_closets_collection

        def wrapped_get_closets(p, create=False):
            real = real_get_closets(p, create=create)
            wrapper = CallCountingCol(real)
            captured["wrapper"] = wrapper
            return wrapper

        monkeypatch.setattr(sync_mod, "get_closets_collection", wrapped_get_closets)

        from mempalace.sync import sync_palace

        report = sync_palace(
            palace_path=palace_path,
            project_dirs=[synced_world["repo_path"]],
            dry_run=False,
        )

        seeded_sources = {
            str(repo_path / "build" / "ignored.py"),
            str(repo_path / "app.log"),
            str(repo_path / "deleted.py"),
        }
        expected = len(seeded_sources & set(report["by_source"].keys()))
        assert report["removed_closets"] == expected, (
            f"removed_closets ({report['removed_closets']}) != |seeded ∩ removable| ({expected})"
        )
        assert "wrapper" in captured, "get_closets_collection patch not invoked"
        assert captured["wrapper"].delete_calls == 1, (
            f"expected one batch delete call, got {captured['wrapper'].delete_calls}"
        )
        assert captured["wrapper"].get_calls == 1, (
            f"expected one batch get call, got {captured['wrapper'].get_calls}"
        )

    def test_registry_check_runs_before_cache_lookup(self, tmp_dir, palace_path):
        """A non-registry drawer with the same source_file must NOT poison
        the bucket of a subsequent _reg_* drawer via the classification
        cache. Order matters for chromadb iteration: seed the regular
        drawer FIRST so it caches `gitignored`, then a registry sentinel
        with the same source_file. Without the registry-bypass at the
        top of the main loop, the cache lookup would route the sentinel
        to gitignored and delete it.
        """
        from mempalace.sync import sync_palace

        repo_path = Path(tmp_dir) / "repo"
        (repo_path / "build").mkdir(parents=True)
        (repo_path / ".gitignore").write_text("build/\n")
        (repo_path / "build" / "shared.py").write_text("# ignored\n")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        shared_source = str(repo_path / "build" / "shared.py")
        col.add(
            ids=["a_regular", "_reg_zzz_sentinel"],
            documents=["regular chunk", "registry sentinel"],
            embeddings=[[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
            metadatas=[
                {
                    "wing": "demo",
                    "room": "build",
                    "source_file": shared_source,
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-05-09T00:00:00",
                },
                {
                    "wing": "demo",
                    "room": "_registry",
                    "source_file": shared_source,
                    "chunk_index": 0,
                    "ingest_mode": "registry",
                    "added_by": "convo_miner",
                    "filed_at": "2026-05-09T00:00:00",
                },
            ],
        )
        del client

        report = sync_palace(
            palace_path=palace_path,
            project_dirs=[str(repo_path)],
            wing="demo",
            dry_run=False,
        )
        assert report["gitignored"] == 1
        assert report["kept"] == 1
        assert report["removed_drawers"] == 1

        client, col = _open_drawers(palace_path)
        try:
            survivors = _drawer_ids(col)
        finally:
            del client
        assert "a_regular" not in survivors
        assert "_reg_zzz_sentinel" in survivors, (
            "registry sentinel was incorrectly pruned via cached non-registry verdict"
        )

    def test_normalize_project_dirs_sort_stable_on_equal_length(self):
        """`_normalize_project_dirs` must sort by `(-len, str)` so equal-length
        roots are alphabetically deterministic; otherwise overlapping nested
        scope choice depends on argv order.
        """
        from mempalace.sync import _normalize_project_dirs

        result = _normalize_project_dirs(["/tmp/zzz", "/tmp/aaa"])
        names = [p.name for p in result]
        assert names == ["aaa", "zzz"], f"equal-length sort not deterministic: got {names}"

        # Different lengths: deepest first.
        deep = _normalize_project_dirs(["/tmp/short", "/tmp/much/deeper/path"])
        assert str(deep[0]).endswith("path")
        assert str(deep[1]).endswith("short")


class TestSyncMcpTool:
    """T2: `mempalace_sync` MCP entry point must keep apply polarity stable."""

    def _patch(self, monkeypatch, config, kg):
        from mempalace import mcp_server

        monkeypatch.setattr(mcp_server, "_config", config)
        monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)

    def test_default_is_dry_run(self, monkeypatch, config, palace_path, kg):
        from mempalace import mcp_server

        self._patch(monkeypatch, config, kg)
        client = chromadb.PersistentClient(path=palace_path)
        client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
        del client

        report = mcp_server.tool_sync(project_dir=palace_path)
        assert report["dry_run"] is True
        # The response is `{"success": True, **report}`, so a bucket that
        # never reaches it is a bucket MCP callers cannot see.
        assert "unresolved" in report

    def test_success_true_on_dry_run(self, monkeypatch, config, palace_path, kg):
        """Round-4: success path returns `success: True` for API symmetry
        with the structured-error branches that all return `success: False`."""
        from mempalace import mcp_server

        self._patch(monkeypatch, config, kg)
        client = chromadb.PersistentClient(path=palace_path)
        client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
        del client

        report = mcp_server.tool_sync(project_dir=palace_path)
        assert report.get("success") is True
        assert report.get("dry_run") is True

    def test_apply_true_is_destructive(self, monkeypatch, config, synced_world, kg):
        from mempalace import mcp_server

        # Rebuild config to point at synced_world's palace.
        from mempalace.config import MempalaceConfig
        import json

        cfg_dir = Path(synced_world["palace_path"]).parent / "cfg_for_mcp_test"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg_dir / "config.json", "w") as f:
            json.dump({"palace_path": synced_world["palace_path"]}, f)
        cfg = MempalaceConfig(config_dir=str(cfg_dir))
        self._patch(monkeypatch, cfg, kg)

        report = mcp_server.tool_sync(
            project_dir=synced_world["repo_path"], apply=True, wing="demo"
        )
        assert report["dry_run"] is False
        assert report["removed_drawers"] >= 1

    def test_no_palace_returns_structured_error(self, monkeypatch, kg):
        """Round-3: tool_sync must keep the {success:False,error:...} contract
        even on the early `_no_palace` short-circuit, not return the bare
        legacy `{error,hint}` dict."""
        from mempalace import mcp_server

        class _EmptyConfig:
            palace_path = ""
            collection_name = "mempalace_drawers"

        monkeypatch.setattr(mcp_server, "_config", _EmptyConfig())
        monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)

        result = mcp_server.tool_sync()
        assert result.get("success") is False
        assert "error" in result

    def test_apply_without_scope_returns_structured_error(
        self, monkeypatch, config, palace_path, kg
    ):
        """Round-2 P0: tool_sync must return {success: False, error: ...}
        rather than letting ValueError propagate to the MCP client."""
        from mempalace import mcp_server

        client = chromadb.PersistentClient(path=palace_path)
        client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
        del client

        self._patch(monkeypatch, config, kg)
        result = mcp_server.tool_sync(apply=True)  # no project_dir, no wing
        assert result.get("success") is False
        assert "wing=" in result.get("error", "") or "project_dirs" in result.get("error", "")

    @pytest.mark.skipif(os.name == "nt", reason="fcntl-based contention test is POSIX only")
    def test_lock_contention_returns_structured_error(self, monkeypatch, config, synced_world, kg):
        """Round-2 P0: tool_sync with apply=True under contention returns
        a structured `{success: False, error: ...}` instead of raising."""
        import fcntl
        import hashlib

        from mempalace import mcp_server
        from mempalace.config import MempalaceConfig
        import json

        # Wire MCP config at synced_world.
        cfg_dir = Path(synced_world["palace_path"]).parent / "cfg_for_lock_test"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        with open(cfg_dir / "config.json", "w") as f:
            json.dump({"palace_path": synced_world["palace_path"]}, f)
        self._patch(monkeypatch, MempalaceConfig(config_dir=str(cfg_dir)), kg)

        # Compute lock path the same way mine_palace_lock does.
        resolved = os.path.realpath(os.path.expanduser(synced_world["palace_path"]))
        palace_key = hashlib.sha256(os.path.normcase(resolved).encode()).hexdigest()[:16]
        lock_dir = os.path.join(os.path.expanduser("~"), ".mempalace", "locks")
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, f"mine_palace_{palace_key}.lock")
        Path(lock_path).touch()

        with open(lock_path, "r+") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                result = mcp_server.tool_sync(
                    project_dir=synced_world["repo_path"], wing="demo", apply=True
                )
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

        assert result.get("success") is False
        assert "another mine" in result.get("error", "").lower()


class TestSyncCli:
    """T1: `cmd_sync` argparse + dispatch wrapper round-trip."""

    def test_dry_run_default_no_mutation(self, monkeypatch, tmp_dir, synced_world, capsys):
        from mempalace import cli

        argv = [
            "mempalace",
            "--palace",
            synced_world["palace_path"],
            "sync",
            synced_world["repo_path"],
        ]
        monkeypatch.setattr("sys.argv", argv)
        cli.main()

        captured = capsys.readouterr().out
        assert "DRY RUN" in captured
        assert "would remove" in captured

        client, col = _open_drawers(synced_world["palace_path"])
        try:
            assert len(_drawer_ids(col)) == 6  # synced_world seeds 6, dry-run touches none
        finally:
            del client

    def test_apply_flag_deletes(self, monkeypatch, tmp_dir, synced_world, capsys):
        from mempalace import cli

        argv = [
            "mempalace",
            "--palace",
            synced_world["palace_path"],
            "sync",
            synced_world["repo_path"],
            "--apply",
            "--wing",
            "demo",
        ]
        monkeypatch.setattr("sys.argv", argv)
        cli.main()

        captured = capsys.readouterr().out
        assert "Removed" in captured
        assert "(removed)" in captured

        client, col = _open_drawers(synced_world["palace_path"])
        try:
            survivors = _drawer_ids(col)
        finally:
            del client
        assert survivors == {
            "drawer_keep",
            "drawer_no_source",
            "drawer_out_of_scope",
        }

    def test_cli_emits_wal_on_apply(self, monkeypatch, synced_world):
        """F8 regression: cmd_sync must wire `_wal_log` so CLI deletes are
        audited. Without this, scripted CLI invocations leave no trail."""
        from mempalace import cli, wal

        seen = []
        original = wal._wal_log

        def recording_wal(operation, params, result=None):
            seen.append((operation, params, result))
            original(operation, params, result)

        monkeypatch.setattr(wal, "_wal_log", recording_wal)

        argv = [
            "mempalace",
            "--palace",
            synced_world["palace_path"],
            "sync",
            synced_world["repo_path"],
            "--apply",
            "--wing",
            "demo",
        ]
        monkeypatch.setattr("sys.argv", argv)
        cli.main()

        ops = [op for op, _, _ in seen]
        assert "sync_prune" in ops, f"CLI --apply did not emit WAL sync_prune entries; seen={ops}"

    def test_apply_without_scope_exits_2(self, monkeypatch, synced_world, capsys):
        """F6 + F8 CLI hardening: --apply with no scope exits non-zero."""
        from mempalace import cli

        argv = [
            "mempalace",
            "--palace",
            synced_world["palace_path"],
            "sync",
            "--apply",
        ]
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
        assert exc_info.value.code == 2


class TestServiceRunSyncReport:
    """Daemon path (service.run_sync) must render the same report shape as the
    direct CLI path, with no KeyError on report['deleted'] (regression: the old
    code read a non-existent 'deleted' key and dropped no_source/out_of_scope/
    by_source and the Re-run/Removed hints).

    sync_palace is mocked so the test exercises only run_sync's report
    formatting — opening the real Chroma collection reinitializes the embedder,
    which disturbs sys.stdout and defeats capsys.
    """

    @pytest.fixture(autouse=True)
    def _cache_mcp_server_import(self):
        """run_sync lazily imports mempalace.mcp_server, whose import initializes
        the embedder and rebinds sys.stdout — defeating capsys for any prints
        after sync_palace returns. Lazy-load it here, scoped to just these report
        tests (not the whole module at collection time), so the import is a cached
        no-op by the time run_sync runs and its report output stays capturable.
        """
        import mempalace.mcp_server  # noqa: F401

        yield

    def _fake_report(self, **overrides):
        report = {
            "scanned": 6,
            "kept": 1,
            "gitignored": 2,
            "missing": 1,
            "unresolved": 1,
            "no_source": 1,
            "out_of_scope": 1,
            "removed_drawers": 0,
            "removed_closets": 0,
            "dry_run": True,
            "by_source": {"src/a.py": 2, "src/b.py": 1},
            "unresolved_by_source": {"src/away.py": 1},
        }
        report.update(overrides)
        return report

    def test_dry_run_renders_full_report(self, monkeypatch, tmp_dir, capsys):
        import mempalace.sync as sync_module
        from mempalace import service

        palace = os.path.join(tmp_dir, "palace")
        os.makedirs(palace)
        # Satisfy run_sync's detect_backend_for_path guard without spinning up
        # the real Chroma/embedder stack (which would disturb sys.stdout).
        make_minimal_chroma_sqlite(palace)
        monkeypatch.setattr(
            sync_module,
            "sync_palace",
            lambda **kw: self._fake_report(dry_run=True),
        )
        result = service.run_sync({"palace_path": palace, "dir": tmp_dir, "dry_run": True})
        assert result["success"] is True
        out = capsys.readouterr().out
        # The fields the stripped daemon report used to drop.
        assert "  Unresolved:     1  (kept)" in out
        assert (
            "  Unresolved drawers are kept: nothing here could show their source file is gone."
        ) in out
        assert "No source:" in out
        assert "Out of scope:" in out
        # by_source top sources block.
        assert "Top sources to remove" in out
        assert "src/a.py  (2)" in out
        # A kept drawer the operator cannot name is not a report.
        assert "    src/away.py  (1)" in out
        # Re-run hint fires when there is something to remove.
        assert "Re-run with --apply" in out
        # The old KeyError line must not be present.
        assert "Deleted:" not in out

    def test_apply_renders_removed_counts(self, monkeypatch, tmp_dir, capsys):
        import mempalace.sync as sync_module
        from mempalace import service

        palace = os.path.join(tmp_dir, "palace")
        os.makedirs(palace)
        make_minimal_chroma_sqlite(palace)
        monkeypatch.setattr(
            sync_module,
            "sync_palace",
            lambda **kw: self._fake_report(
                dry_run=False, removed_drawers=3, removed_closets=2, by_source={"src/a.py": 3}
            ),
        )
        result = service.run_sync({"palace_path": palace, "dir": tmp_dir, "dry_run": False})
        assert result["success"] is True
        out = capsys.readouterr().out
        # Apply mode prints the removed-drawers/closets line, not the Re-run hint.
        assert "Removed 3 drawers, 2 closets" in out
        assert "Top sources removed" in out
        assert "Re-run with --apply" not in out


class TestUnresolvedSources:
    """#2320: a probe that came back empty-handed is not proof of deletion.

    `missing` is a removal bucket, so every state that reaches it has to be
    one where the source file's absence was actually established. One
    `os.stat` cannot establish it on its own: an empty mount point answers
    for its children exactly as a directory whose children were deleted.
    """

    def _seed(self, palace_path, rows, wing="demo"):
        """rows: list of (drawer_id, source_file)."""
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=[r[0] for r in rows],
            documents=[f"doc {i}" for i in range(len(rows))],
            embeddings=[[float(i + 1), 0.0, 0.0] for i in range(len(rows))],
            metadatas=[
                {
                    "wing": wing,
                    "room": "src",
                    "source_file": str(r[1]),
                    "chunk_index": 0,
                    "added_by": "miner",
                    "filed_at": "2026-08-21T00:00:00",
                }
                for r in rows
            ],
        )
        del client

    def _classify(self, tmp_dir, palace_path, source_file, root=None):
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        self._seed(palace_path, [("d_probe", source_file)])
        return sync_palace(
            palace_path=palace_path,
            project_dirs=[str(root or repo)],
            wing="demo",
            dry_run=True,
        )

    def test_deleted_file_beside_a_neighbour_the_palace_can_see_is_missing(
        self, tmp_dir, palace_path
    ):
        """The state that is established: the palace still finds a source
        file of its own in that directory. A deletion leaves the neighbours
        where they were, so this must keep pruning or the feature stops
        doing the job #1252 asked for."""
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        neighbour = repo / "neighbour.py"
        neighbour.write_text("# still here\n")
        gone = repo / "gone.py"
        gone.write_text("# was here\n")
        gone.unlink()

        self._seed(palace_path, [("d_gone", gone), ("d_neighbour", neighbour)])
        report = sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["missing"] == 1, report
        assert report["kept"] == 1, report
        assert report["unresolved"] == 0, report

    def test_a_neighbour_that_goes_away_mid_pass_does_not_corroborate(
        self, monkeypatch, tmp_dir, palace_path
    ):
        """A pass over a large palace runs for minutes, so the volume can go
        away partway through it. A neighbour read while it was still there
        must not corroborate a drawer settled afterwards, or one early
        reading condemns every drawer read after it."""
        from mempalace import sync as sync_mod

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        neighbour = repo / "neighbour.py"
        neighbour.write_text("# on the volume\n")
        gone = repo / "gone.py"
        gone.write_text("# on the volume too\n")
        gone.unlink()
        self._seed(palace_path, [("d_neighbour", neighbour), ("d_gone", gone)])

        real_iter = sync_mod._iter_drawer_metadata

        def iter_then_lose_the_volume(col, wing):
            yield from real_iter(col, wing)
            # Exhausting the generator is the end of the classifying pass.
            neighbour.unlink()

        monkeypatch.setattr(sync_mod, "_iter_drawer_metadata", iter_then_lose_the_volume)
        report = sync_mod.sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report

    def test_a_source_that_comes_back_before_the_settle_is_not_removed(
        self, monkeypatch, tmp_dir, palace_path
    ):
        """The mirror of the test above, and the reason both halves of the
        verdict are read again. A volume can return inside one pass as well
        as leave inside it; a neighbour read at the end must not condemn
        drawers whose own reading was taken while the volume was away."""
        from mempalace import sync as sync_mod

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        neighbour = repo / "neighbour.py"
        neighbour.write_text("# never went anywhere\n")
        away = repo / "away.py"
        self._seed(palace_path, [("d_away", away), ("d_neighbour", neighbour)])

        real_iter = sync_mod._iter_drawer_metadata

        def iter_then_regain_the_volume(col, wing):
            yield from real_iter(col, wing)
            # Exhausting the generator is the end of the classifying pass.
            away.write_text("# it was here all along\n")

        monkeypatch.setattr(sync_mod, "_iter_drawer_metadata", iter_then_regain_the_volume)
        report = sync_mod.sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report

    def test_neighbour_must_be_one_the_palace_knows(self, tmp_dir, palace_path):
        """A file the palace never mined is not evidence. This is the shape
        #2320 reproduces and the one an emptiness test gets wrong: a mount
        point committed to a repository is never empty, because git cannot
        track an empty directory, so `.gitkeep` outlives the unmount and sits
        in the directory looking like a neighbour."""
        repo = Path(tmp_dir) / "repo"
        data = repo / "data"
        data.mkdir(parents=True)
        (data / ".gitkeep").write_text("")
        source = data / "onvolume.py"

        report = self._classify(tmp_dir, palace_path, source, root=repo)
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report

    def test_a_mined_neighbour_corroborates_even_when_it_should_not(self, tmp_dir, palace_path):
        """The limit of this rule, pinned so nobody reads it as watertight.

        A directory holds different contents depending on what is mounted on
        it. A neighbour proves the directory is reachable; it does not prove
        it is the filesystem the missing file was mined from. So a file the
        palace mined while nothing was mounted there still corroborates, and
        the volume's drawers are removed. Separating the two needs the
        identity of the filesystem each source was mined from, which is not
        recorded anywhere; `develop` removes them in this state as well.
        """
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        data = repo / "data"
        data.mkdir(parents=True)
        # Mined while nothing was mounted over `data`, and still on disk.
        underlying = data / "notes_local.md"
        underlying.write_text("# committed to the repository\n")
        # Mined from the volume, which is not mounted right now.
        on_the_volume = data / "capture.csv"

        self._seed(palace_path, [("d_volume", on_the_volume), ("d_underlying", underlying)])
        report = sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["missing"] == 1, report
        assert report["unresolved"] == 0, report

    def test_a_registry_sentinel_cannot_corroborate(self, tmp_dir, palace_path):
        """Registry rows are kept without the file being looked at, so one
        says nothing about what is on disk. Letting a sentinel vouch for its
        directory would corroborate from a path that may never have existed."""
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        gone = repo / "gone.py"
        gone.write_text("# was here\n")
        gone.unlink()
        # On disk, so that only the guard keeps it from vouching.
        transcript = repo / "registry_only.py"
        transcript.write_text("# a transcript the sentinel tracks\n")

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )
        col.add(
            ids=["d_gone", "_reg_sentinel"],
            documents=["a", "b"],
            embeddings=[[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
            metadatas=[
                {"wing": "demo", "source_file": str(gone), "chunk_index": 0},
                {
                    "wing": "demo",
                    "room": "_registry",
                    "source_file": str(transcript),
                    "chunk_index": 0,
                },
            ],
        )
        del client

        report = sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report

    @pytest.mark.skipif(os.name == "nt", reason="os.symlink needs admin on Windows")
    def test_two_spellings_of_one_directory_do_not_corroborate(self, tmp_dir, palace_path):
        """The corroboration key is the metadata string as written, while the
        classification works on the resolved path. A neighbour filed through
        a symlink and a missing file filed through the real path name the
        same directory and still do not meet, which keeps rather than
        removes."""
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        real = repo / "real"
        real.mkdir(parents=True)
        os.symlink(str(real), str(repo / "link"))
        neighbour = real / "neighbour.py"
        neighbour.write_text("# still here\n")
        gone = real / "gone.py"
        gone.write_text("# was here\n")
        gone.unlink()

        # The neighbour is filed under the link, the missing file under the
        # real path. Both are the same directory on disk.
        self._seed(palace_path, [("d_gone", gone), ("d_neighbour", repo / "link" / "neighbour.py")])
        report = sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report

    def test_source_in_an_empty_directory_is_unresolved(self, tmp_dir, palace_path):
        """The same shape with nothing left in the directory at all.

        This pins the cost as much as the win. The state is also reached by
        deleting the last source the palace knows in a directory, and that
        drawer is kept too, because nothing here can tell the two apart."""
        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        source = repo / "onvolume.py"
        source.write_text("# x\n")
        source.unlink()

        report = self._classify(tmp_dir, palace_path, source)
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permission bits"
    )
    def test_directory_that_cannot_be_listed_still_prunes(self, tmp_dir, palace_path):
        """Mode 0o111 lets this process enter a directory but not list it,
        which is what a `0o711` directory looks like to anyone but its
        owner. The neighbour is reached by `os.stat`, which needs only the
        execute bit, so a verdict here does not depend on a permission the
        old code never asked for either."""
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        execonly = repo / "execonly"
        execonly.mkdir(parents=True)
        neighbour = execonly / "neighbour.py"
        neighbour.write_text("# still here\n")
        gone = execonly / "gone.py"
        gone.write_text("# was here\n")
        gone.unlink()
        self._seed(palace_path, [("d_gone", gone), ("d_neighbour", neighbour)])

        os.chmod(execonly, 0o111)
        try:
            report = sync_palace(
                palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
            )
        finally:
            os.chmod(execonly, 0o700)

        assert report["missing"] == 1, report
        assert report["unresolved"] == 0, report

    def test_a_probe_that_failed_is_unresolved_even_beside_a_live_neighbour(
        self, monkeypatch, tmp_dir, palace_path
    ):
        """The leaf probe answering anything other than ENOENT must stop the
        drawer before corroboration is ever consulted, or a directory that
        happens to hold a live neighbour would turn an unreadable path into a
        deletion. `os.stat` is patched for one path so this runs on every
        platform rather than needing permission bits that CI may not honour."""
        import os as os_module

        from mempalace import sync as sync_mod

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        neighbour = repo / "neighbour.py"
        neighbour.write_text("# still here\n")
        target = repo / "unreadable.py"
        self._seed(palace_path, [("d_target", target), ("d_neighbour", neighbour)])

        real_stat = os_module.stat
        # Resolved once, outside the patch: calling `resolve` inside it would
        # stat its way back into this function.
        refused = str(target.resolve())

        def refusing_stat(path, *args, **kwargs):
            if str(path) == refused:
                raise PermissionError(13, "Permission denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(sync_mod.os, "stat", refusing_stat)
        report = sync_mod.sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report
        assert report["kept"] == 1, report

    def test_source_whose_parent_directory_is_gone_is_unresolved(self, tmp_dir, palace_path):
        """A volume that is not mounted leaves the directory it held absent.
        Its children's ENOENT says nothing about whether they were deleted."""
        repo = Path(tmp_dir) / "repo"
        (repo / "onvolume").mkdir(parents=True)
        source = repo / "onvolume" / "f.py"
        source.write_text("# x\n")
        shutil.rmtree(repo / "onvolume")

        report = self._classify(tmp_dir, palace_path, source)
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report

    def test_source_under_a_regular_file_is_unresolved(self, tmp_dir, palace_path):
        """ENOTDIR. The path cannot be walked, which is not the same as the
        leaf being absent."""
        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        blocker = repo / "notadir"
        blocker.write_text("I am a file\n")

        from mempalace.sync import _classify_drawer

        report = self._classify(tmp_dir, palace_path, blocker / "f.py")
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report
        # The report alone cannot tell `unresolved` from `absent` with no
        # neighbour to settle against, so pin the classifier's own answer.
        # `sync_palace` resolves its roots before classifying, and on macOS
        # `/tmp` is a symlink, so an unresolved root matches nothing.
        #
        # POSIX answers ENOTDIR here, which establishes nothing about the
        # leaf. Windows has no errno that says so: this arrives as ENOENT or
        # ENOTDIR depending on the path, which is the whole reason removal
        # cannot rest on the probe. The leaf may therefore read as `absent`
        # there and be settled by corroboration instead, which the report
        # above already pins, so only POSIX pins the bucket itself.
        if os.name != "nt":
            assert (
                _classify_drawer({"source_file": str(blocker / "f.py")}, {}, [repo.resolve()], "d")
                == "unresolved"
            )

    @pytest.mark.skipif(os.name == "nt", reason="os.symlink needs admin on Windows")
    def test_source_behind_a_dangling_symlink_is_unresolved(self, tmp_dir, palace_path):
        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        link = repo / "dangling"
        os.symlink(str(repo / "nowhere"), str(link))

        report = self._classify(tmp_dir, palace_path, link / "f.py")
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report

    @pytest.mark.skipif(os.name == "nt", reason="os.symlink needs admin on Windows")
    def test_symlink_loop_is_unresolved_and_does_not_end_the_run(self, tmp_dir, palace_path):
        """Up to 3.12 `Path.resolve(strict=False)` raises RuntimeError on a
        loop and 3.13 stopped; either way one drawer must not decide the fate
        of the run."""
        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        a, b = repo / "loop_a", repo / "loop_b"
        os.symlink(str(b), str(a))
        os.symlink(str(a), str(b))

        report = self._classify(tmp_dir, palace_path, a / "f.py")
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permission bits"
    )
    def test_unreadable_parent_is_unresolved_and_does_not_end_the_run(self, tmp_dir, palace_path):
        """Up to 3.13 `Path.exists()` raises PermissionError here and 3.14
        answers False, which would delete the drawer. Neither is acceptable."""
        repo = Path(tmp_dir) / "repo"
        locked = repo / "locked"
        locked.mkdir(parents=True)
        source = locked / "f.py"
        source.write_text("# x\n")
        os.chmod(locked, 0o000)
        from mempalace.sync import _classify_drawer

        try:
            report = self._classify(tmp_dir, palace_path, source)
            os.chmod(locked, 0o000)
            # As above: the root has to be resolved, or macOS answers
            # `out_of_scope` before the probe is ever reached.
            bucket = _classify_drawer({"source_file": str(source)}, {}, [repo.resolve()], "d")
        finally:
            os.chmod(locked, 0o700)
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report
        # A probe that could not run is not the leaf being absent, and with
        # no neighbour the report cannot separate the two.
        assert bucket == "unresolved", bucket

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permission bits"
    )
    def test_auto_detected_roots_survive_an_ancestor_it_cannot_walk(self, tmp_dir, palace_path):
        """With no project_dirs, sync walks every source's ancestors looking
        for a project marker. Up to 3.13 that probe raises on a directory the
        process may not enter, which ended the run before a single drawer was
        classified."""
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        locked = repo / "locked"
        locked.mkdir(parents=True)
        (repo / ".gitignore").write_text("build/\n")
        source = locked / "f.py"
        source.write_text("# x\n")
        self._seed(palace_path, [("d_locked", source)])

        os.chmod(locked, 0o000)
        try:
            report = sync_palace(palace_path=palace_path, wing="demo", dry_run=True)
        finally:
            os.chmod(locked, 0o700)

        assert report["scanned"] == 1, report
        assert report["unresolved"] == 1, report

    def test_auto_detected_roots_survive_a_marker_probe_that_raises(
        self, monkeypatch, tmp_dir, palace_path
    ):
        """The same guard as the test above, reached without permission bits
        so it also runs as root and on Windows, where those tests skip."""
        from mempalace import sync as sync_mod

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        (repo / ".gitignore").write_text("build/\n")
        source = repo / "f.py"
        source.write_text("# x\n")
        self._seed(palace_path, [("d_probe", source)])

        real_exists = Path.exists

        def refusing_exists(self, *args, **kwargs):
            if self.name == ".git":
                raise PermissionError(13, "Permission denied")
            return real_exists(self, *args, **kwargs)

        monkeypatch.setattr(Path, "exists", refusing_exists)
        report = sync_mod.sync_palace(palace_path=palace_path, wing="demo", dry_run=True)
        assert report["scanned"] == 1, report
        assert report["kept"] == 1, report

    def test_an_unresolvable_marker_stops_the_climb(self, monkeypatch, tmp_dir, palace_path):
        """A marker found on an ancestor whose path will not resolve must end
        the walk. Climbing past it would register a higher ancestor as the
        root and widen what the run treats as in scope."""
        from mempalace import sync as sync_mod

        outer = Path(tmp_dir) / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        (outer / ".gitignore").write_text("build/\n")
        (inner / ".gitignore").write_text("build/\n")
        source = inner / "f.py"
        source.write_text("# x\n")
        self._seed(palace_path, [("d_probe", source)])

        real_resolve = Path.resolve

        def refusing_resolve(self, *args, **kwargs):
            if self == inner:
                raise RuntimeError(f"Symlink loop from {self!r}")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", refusing_resolve)
        report = sync_mod.sync_palace(palace_path=palace_path, wing="demo", dry_run=True)
        # No root was registered for this source, so it is out of scope and
        # kept, rather than matched against `outer` and judged there.
        assert report["out_of_scope"] == 1, report
        assert report["kept"] == 0, report

    def test_the_report_does_not_depend_on_the_order_drawers_arrive_in(self, monkeypatch, tmp_dir):
        """What settling in two phases buys is that one drawer's verdict does
        not depend on where it fell in the scan. Every permutation of three
        drawers, and a page size small enough that they span two pages, must
        produce the same report, and every drawer must land in exactly one
        bucket."""
        import itertools

        from mempalace import sync as sync_mod

        repo = Path(tmp_dir) / "repo"
        alone = repo / "alone"
        alone.mkdir(parents=True)
        neighbour = repo / "neighbour.py"
        neighbour.write_text("# still here\n")
        beside_it = repo / "beside_it.py"
        beside_it.write_text("# x\n")
        beside_it.unlink()
        on_its_own = alone / "on_its_own.py"
        on_its_own.write_text("# x\n")
        on_its_own.unlink()

        # Two per page, so the neighbour and the drawer it vouches for can
        # land in different pages depending on the order.
        monkeypatch.setattr(sync_mod, "_BATCH", 2)
        buckets = ("kept", "gitignored", "missing", "unresolved", "no_source", "out_of_scope")
        rows = [("d_neighbour", neighbour), ("d_beside", beside_it), ("d_alone", on_its_own)]

        seen = set()
        for order in itertools.permutations(range(3)):
            palace = os.path.join(tmp_dir, "palace_" + "".join(str(i) for i in order))
            self._seed(palace, [rows[i] for i in order])
            report = sync_mod.sync_palace(
                palace_path=palace, project_dirs=[str(repo)], wing="demo", dry_run=True
            )
            assert sum(report[b] for b in buckets) == report["scanned"] == 3, (order, report)
            seen.add(tuple(report[b] for b in buckets))

        assert len(seen) == 1, seen
        assert seen == {(1, 0, 1, 1, 0, 0)}, seen

    def test_unencodable_source_path_is_unresolved(self, tmp_dir):
        """A path the platform cannot encode is not a proven absence either.
        `Path.resolve` raises ValueError on an embedded NUL before any probe
        runs, which used to end the run at that drawer."""
        from mempalace.sync import _classify_drawer

        root = Path(tmp_dir) / "repo"
        root.mkdir(parents=True)

        bucket = _classify_drawer({"source_file": str(root / "nul\x00byte.txt")}, {}, [root])
        assert bucket == "unresolved", bucket

    def test_apply_keeps_unresolved_and_removes_only_the_established_one(
        self, tmp_dir, palace_path
    ):
        """The end the issue is about: --apply must delete the provably
        deleted file's drawer and leave the unreachable one alone."""
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        (repo / "onvolume").mkdir(parents=True)
        unreachable = repo / "onvolume" / "f.py"
        unreachable.write_text("# x\n")
        shutil.rmtree(repo / "onvolume")

        deleted = repo / "gone.py"
        deleted.write_text("# was here\n")
        deleted.unlink()

        kept = repo / "kept.py"
        kept.write_text("# still here\n")

        self._seed(
            palace_path,
            [("d_unreachable", unreachable), ("d_deleted", deleted), ("d_kept", kept)],
        )

        # A closet for each source, so the purge branch actually runs and can
        # be seen to spare the unreachable one.
        client = chromadb.PersistentClient(path=palace_path)
        closets = client.get_or_create_collection(
            "mempalace_closets", metadata={"hnsw:space": "cosine"}
        )
        closets.add(
            ids=["closet_unreachable", "closet_deleted"],
            documents=["c1", "c2"],
            embeddings=[[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
            metadatas=[{"source_file": str(unreachable)}, {"source_file": str(deleted)}],
        )
        del client

        report = sync_palace(
            palace_path=palace_path,
            project_dirs=[str(repo)],
            wing="demo",
            dry_run=False,
        )
        assert report["unresolved"] == 1, report
        assert report["missing"] == 1, report
        assert report["removed_drawers"] == 1, report
        assert report["removed_closets"] == 1, report

        client, col = _open_drawers(palace_path)
        try:
            survivors = _drawer_ids(col)
            closet_ids = set(
                client.get_or_create_collection(
                    "mempalace_closets", metadata={"hnsw:space": "cosine"}
                ).get(include=[])["ids"]
            )
        finally:
            del client
        assert survivors == {"d_unreachable", "d_kept"}, survivors
        assert closet_ids == {"closet_unreachable"}, closet_ids

    def test_unresolved_sources_are_not_listed_as_removable(self, tmp_dir, palace_path):
        """`by_source` drives the "Top sources to remove" block. A source
        nothing will remove must not appear in it."""
        repo = Path(tmp_dir) / "repo"
        (repo / "onvolume").mkdir(parents=True)
        unreachable = repo / "onvolume" / "f.py"
        unreachable.write_text("# x\n")
        shutil.rmtree(repo / "onvolume")

        report = self._classify(tmp_dir, palace_path, unreachable)
        assert report["by_source"] == {}, report

    def test_whole_project_root_gone_removes_nothing(self, tmp_dir, palace_path):
        """The mount-point case at full size: the project root itself is not
        there, so no drawer under it can be proven stale."""
        from mempalace.sync import sync_palace

        mount_point = Path(tmp_dir) / "mnt"
        repo = mount_point / "proj"
        (repo / "src").mkdir(parents=True)
        sources = [repo / "src" / f"f{i}.py" for i in range(3)]
        for s in sources:
            s.write_text("# x\n")
        self._seed(palace_path, [(f"d_{i}", s) for i, s in enumerate(sources)])

        shutil.rmtree(repo)
        mount_point.mkdir(exist_ok=True)

        report = sync_palace(
            palace_path=palace_path,
            project_dirs=[str(repo)],
            wing="demo",
            dry_run=False,
        )
        assert report["unresolved"] == 3, report
        assert report["missing"] == 0, report
        assert report["removed_drawers"] == 0, report

        client, col = _open_drawers(palace_path)
        try:
            survivors = _drawer_ids(col)
        finally:
            del client
        assert survivors == {"d_0", "d_1", "d_2"}, survivors

    def test_cli_prints_the_unresolved_count(self, monkeypatch, tmp_dir, palace_path, capsys):
        """A count the operator never sees is not a report."""
        from mempalace import cli

        repo = Path(tmp_dir) / "repo"
        (repo / "onvolume").mkdir(parents=True)
        unreachable = repo / "onvolume" / "f.py"
        unreachable.write_text("# x\n")
        shutil.rmtree(repo / "onvolume")
        self._seed(palace_path, [("d_unreachable", unreachable)])

        monkeypatch.setattr(
            "sys.argv",
            ["mempalace", "--palace", palace_path, "sync", str(repo), "--wing", "demo"],
        )
        cli.main()

        out = capsys.readouterr().out
        assert "  Unresolved:     1  (kept)" in out, out
        assert (
            "  Unresolved drawers are kept: nothing here could show their source file is gone."
        ) in out, out

    def test_a_directory_spelling_cannot_be_a_witness(self, tmp_dir):
        """A witness has to be a file in the directory it speaks for.

        ``os.path.dirname`` files a source whose last component is empty,
        ``.`` or ``..`` under the very directory it names, or under its
        parent, and a directory outlives the unmount that empties it. Taking
        one for a neighbour would corroborate removing every drawer on the
        volume, which is the failure this whole bucket exists to stop.
        ``tool_add_drawer`` stores its caller's string verbatim, so these
        spellings arrive without anything being corrupt.
        """
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        data = repo / "data"
        data.mkdir(parents=True)
        on_the_volume = [data / "a.py", data / "b.py"]
        for f in on_the_volume:
            f.write_text("# mined off the volume\n")

        for i, spelling in enumerate((str(data) + os.sep, os.path.join(str(data), "."))):
            palace = os.path.join(tmp_dir, f"palace_dirspelling_{i}")
            os.makedirs(palace)
            self._seed(
                palace,
                [("d_dir", spelling)] + [(f"d_{f.name}", f) for f in on_the_volume],
            )
            for f in on_the_volume:
                if f.exists():
                    f.unlink()
            report = sync_palace(
                palace_path=palace, project_dirs=[str(repo)], wing="demo", dry_run=True
            )
            # Name the two drawers rather than count the buckets: whether the
            # odd spelling itself lands in `kept` or in `unresolved` depends
            # on how the platform stats a path with no last component, and
            # the claim here is about the two files on the volume.
            assert report["missing"] == 0, (spelling, report)
            assert set(report["unresolved_by_source"]) >= {str(f) for f in on_the_volume}, (
                spelling,
                report,
            )
            for f in on_the_volume:
                f.write_text("# back for the next spelling\n")

    def test_every_neighbour_the_palace_knows_can_corroborate(
        self, monkeypatch, tmp_dir, palace_path
    ):
        """Corroboration asks the directory, not one remembered drawer.

        Keeping only the first neighbour the pass met would make the verdict
        turn on the order the collection hands drawers over, which it does
        not promise: the same palace and the same disk would prune under one
        order and keep under the other. Here the first neighbour goes away
        mid-pass and the second does not, and the deletion is still
        established."""
        from mempalace import sync as sync_mod

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        first = repo / "first.py"
        first.write_text("# met first\n")
        second = repo / "second.py"
        second.write_text("# met second\n")
        gone = repo / "gone.py"
        gone.write_text("# was here\n")
        gone.unlink()
        rows = [("d_first", first), ("d_second", second), ("d_gone", gone)]
        self._seed(palace_path, rows)

        def iter_in_a_fixed_order(col, wing):
            for drawer_id, source in rows:
                yield (
                    drawer_id,
                    {
                        "wing": "demo",
                        "room": "src",
                        "source_file": str(source),
                        "chunk_index": 0,
                        "added_by": "miner",
                    },
                )
            # Exhausting the generator ends the classifying pass.
            first.unlink()

        monkeypatch.setattr(sync_mod, "_iter_drawer_metadata", iter_in_a_fixed_order)
        report = sync_mod.sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["missing"] == 1, report
        assert report["unresolved"] == 0, report

    @pytest.mark.skipif(os.name == "nt", reason="os.symlink needs admin on Windows")
    def test_a_witness_whose_own_probe_fails_does_not_corroborate(
        self, monkeypatch, tmp_dir, palace_path
    ):
        """A neighbour corroborates by being read as a file, not by failing
        to be read as gone. Those are different answers, and a directory
        that stops answering partway through a pass produces the second."""
        from mempalace import sync as sync_mod

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        target = repo / "target.py"
        target.write_text("# what the neighbour points at\n")
        neighbour = repo / "neighbour.py"
        neighbour.symlink_to(target)
        gone = repo / "gone.py"
        gone.write_text("# was here\n")
        gone.unlink()
        self._seed(palace_path, [("d_neighbour", neighbour), ("d_gone", gone)])

        real_iter = sync_mod._iter_drawer_metadata

        def iter_then_break_the_neighbour(col, wing):
            yield from real_iter(col, wing)
            # The witness now answers ELOOP rather than reporting itself gone.
            neighbour.unlink()
            neighbour.symlink_to(neighbour)

        monkeypatch.setattr(sync_mod, "_iter_drawer_metadata", iter_then_break_the_neighbour)
        report = sync_mod.sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report

    def test_apply_keeps_a_source_whose_probe_could_not_run(self, tmp_dir, palace_path):
        """``unresolved`` is reached from two places and only one of them is
        the settle. A drawer whose probe could not run never enters the
        second list, so the scan loop is the only thing holding it out of
        ``removable_ids``. Pin that on the apply path, beside a drawer that
        really is removable, so the run is deleting while it holds this one
        back."""
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        blocker = repo / "blocker.txt"
        blocker.write_text("a regular file, not a directory\n")
        neighbour = repo / "neighbour.py"
        neighbour.write_text("# still here\n")
        gone = repo / "gone.py"
        gone.write_text("# was here\n")
        gone.unlink()
        self._seed(
            palace_path,
            [
                ("d_unwalkable", blocker / "under_a_regular_file.py"),
                ("d_neighbour", neighbour),
                ("d_gone", gone),
            ],
        )

        report = sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=False
        )
        assert report["unresolved"] == 1, report
        assert report["missing"] == 1, report
        assert report["removed_drawers"] == 1, report

        client, col = _open_drawers(palace_path)
        try:
            assert _drawer_ids(col) == {"d_unwalkable", "d_neighbour"}
        finally:
            del client

    @pytest.mark.skipif(os.name == "nt", reason="os.symlink needs admin on Windows")
    def test_a_source_the_run_never_probed_cannot_corroborate(self, tmp_dir, palace_path):
        """`out_of_scope` is decided before the file is ever looked at, so
        such a drawer says nothing about the directory its metadata files it
        under. A symlink out of the project keeps the link's own spelling in
        `source_file`, so it lands beside sources that are in scope, and its
        target can outlive the directory it appears to sit in."""
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        data = repo / "data"
        data.mkdir(parents=True)
        outside = Path(tmp_dir) / "outside"
        outside.mkdir()
        target = outside / "elsewhere.py"
        target.write_text("# not in the project\n")
        pointer = data / "pointer.py"
        pointer.symlink_to(target)
        gone = data / "gone.py"
        gone.write_text("# was here\n")
        gone.unlink()
        self._seed(palace_path, [("d_pointer", pointer), ("d_gone", gone)])

        report = sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["out_of_scope"] == 1, report
        assert report["unresolved"] == 1, report
        assert report["missing"] == 0, report
        assert report["by_source"] == {}, report

    def test_a_directory_that_lost_every_source_at_once_is_unresolved(self, tmp_dir, palace_path):
        """The cost of the rule, pinned so it is not discovered as a
        surprise. Deleting every file the palace knows in one directory
        leaves nothing to corroborate with, and reads exactly like the
        directory's contents being away, so `develop` prunes those drawers
        and this does not."""
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        module = repo / "module"
        module.mkdir(parents=True)
        sources = [module / f"{name}.py" for name in ("a", "b", "c")]
        for f in sources:
            f.write_text("# mined\n")
        self._seed(palace_path, [(f"d_{f.name}", f) for f in sources])
        for f in sources:
            f.unlink()
        # The directory itself is untouched and still readable.
        assert module.is_dir()

        report = sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["unresolved"] == 3, report
        assert report["missing"] == 0, report

    def test_unresolved_sources_are_named_from_both_paths(self, tmp_dir, palace_path):
        """A count of kept drawers the operator cannot turn into paths is not
        a report. `unresolved` arrives from the classify pass and from the
        settle, and both have to reach `unresolved_by_source`, counted per
        drawer the way `by_source` counts removals."""
        from mempalace.sync import sync_palace

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        blocker = repo / "blocker.txt"
        blocker.write_text("a regular file, not a directory\n")
        walled = blocker / "under_a_regular_file.py"
        alone = repo / "alone"
        alone.mkdir()
        settled = alone / "gone.py"
        settled.write_text("# was here\n")
        settled.unlink()
        self._seed(
            palace_path,
            [
                ("d_walled_0", walled),
                ("d_walled_1", walled),
                ("d_settled", settled),
            ],
        )

        report = sync_palace(
            palace_path=palace_path, project_dirs=[str(repo)], wing="demo", dry_run=True
        )
        assert report["unresolved"] == 3, report
        assert report["unresolved_by_source"] == {str(walled): 2, str(settled): 1}, report
        # The removal side stays empty, so the two lists cannot be confused.
        assert report["by_source"] == {}, report

    def test_cli_names_the_unresolved_sources_and_counts_the_rest(
        self, monkeypatch, tmp_dir, palace_path, capsys
    ):
        """Five paths are printed and the remainder is stated. A list that
        stops at five without saying so reads as the whole set."""
        from mempalace import cli

        repo = Path(tmp_dir) / "repo"
        repo.mkdir(parents=True)
        rows = []
        for i in range(7):
            room = repo / f"room{i}"
            room.mkdir()
            source = room / "gone.py"
            source.write_text("# was here\n")
            source.unlink()
            rows.append((f"d_{i}", source))
        self._seed(palace_path, rows)

        monkeypatch.setattr(
            "sys.argv",
            ["mempalace", "--palace", palace_path, "sync", str(repo), "--wing", "demo"],
        )
        cli.main()

        out = capsys.readouterr().out
        assert "  Unresolved:     7  (kept)" in out, out
        named = [line for line in out.splitlines() if line.startswith("    ") and "gone.py" in line]
        assert len(named) == 5, out
        assert "    and 2 more source file(s)" in out, out
