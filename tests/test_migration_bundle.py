"""Tests for copy-only, reviewed MemPalace migration bundles."""

from __future__ import annotations

import json
import sqlite3
import stat

import pytest

from mempalace.migration_bundle import (
    _publish_directory_no_replace,
    _read_available_embeddings,
    apply_reviewed_migration,
    apply_actions_to_collection,
    bundle_permissions,
    prepare_migration_copies,
    verify_retained_records,
)
from mempalace.reorganize import (
    InventoryRecord,
    collect_duplicate_evidence,
    inventory_palace,
    palace_snapshot,
    plan_actions,
    write_manifest,
)


class _MemoryCollection:
    def __init__(self, records):
        self.rows = {
            record.drawer_id: {
                "document": record.content,
                "metadata": dict(record.metadata),
            }
            for record in records
        }

    def update(self, *, ids, metadatas):
        for drawer_id, metadata in zip(ids, metadatas):
            if drawer_id in self.rows:
                self.rows[drawer_id]["metadata"] = dict(metadata)

    def delete(self, *, ids):
        for drawer_id in ids:
            self.rows.pop(drawer_id, None)

    def count(self):
        return len(self.rows)

    def get(self, *, ids, include):
        found = [drawer_id for drawer_id in ids if drawer_id in self.rows]
        result = {"ids": found}
        if "documents" in include:
            result["documents"] = [self.rows[drawer_id]["document"] for drawer_id in found]
        if "metadatas" in include:
            result["metadatas"] = [self.rows[drawer_id]["metadata"] for drawer_id in found]
        return result


def test_embedding_reuse_isolates_only_unreadable_vector_ids():
    class PartiallyUnreadable:
        def get(self, *, ids, include):
            assert include == ["embeddings"]
            if "bad" in ids:
                raise RuntimeError("Error finding id")
            return {
                "ids": list(ids),
                "embeddings": [[float(index), 1.0] for index, _ in enumerate(ids)],
            }

    available = _read_available_embeddings(PartiallyUnreadable(), ["good-a", "bad", "good-b"])

    assert set(available) == {"good-a", "good-b"}


def _seed_palace(palace, rows):
    palace.mkdir(parents=True)
    db_path = palace / "chroma.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE collections (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                config_json_str TEXT,
                schema_str TEXT
            );
            CREATE TABLE segments (id INTEGER PRIMARY KEY, collection INTEGER NOT NULL);
            CREATE TABLE embeddings (
                id INTEGER PRIMARY KEY,
                segment_id INTEGER NOT NULL,
                embedding_id TEXT,
                created_at TEXT
            );
            CREATE TABLE embedding_metadata (
                id INTEGER,
                key TEXT,
                string_value TEXT,
                int_value INTEGER,
                float_value REAL,
                bool_value INTEGER
            );
            INSERT INTO collections(id, name, config_json_str, schema_str)
                VALUES (
                    1,
                    'mempalace_drawers',
                    '{"a":1,"b":2}',
                    '{"defaults":{"ef":10,"threads":4},"version":1}'
                );
            INSERT INTO segments(id, collection) VALUES (1, 1);
            """
        )
        for row_id, (drawer_id, content, metadata) in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO embeddings(id, segment_id, embedding_id, created_at) "
                "VALUES (?, 1, ?, '2026-07-14T00:00:00Z')",
                (row_id, drawer_id),
            )
            conn.execute(
                "INSERT INTO embedding_metadata(id, key, string_value) VALUES (?, ?, ?)",
                (row_id, "chroma:document", content),
            )
            for key, value in metadata.items():
                column = "int_value" if isinstance(value, int) else "string_value"
                conn.execute(
                    f"INSERT INTO embedding_metadata(id, key, {column}) VALUES (?, ?, ?)",
                    (row_id, key, value),
                )
    return db_path


def _reviewed_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    active_root = tmp_path / "active"
    palace = active_root / "palace"
    canonical = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    sessions = tmp_path / "sessions"
    for path in (canonical, worktree, sessions):
        path.mkdir()
    _seed_palace(
        palace,
        [
            (
                "canonical",
                "same content",
                {"wing": "se", "source_file": str(canonical / "src/a.ts"), "chunk_index": 0},
            ),
            (
                "worktree",
                "same content",
                {"wing": "se", "source_file": str(worktree / "src/a.ts"), "chunk_index": 0},
            ),
        ],
    )
    (active_root / "config.json").write_text('{"backend": "chroma"}\n')
    (active_root / "hallways.json").write_text("[]\n")
    inventory = inventory_palace(
        palace,
        canonical_root=canonical,
        worktree_roots=[worktree],
        session_roots=[sessions],
    )
    actions = plan_actions(inventory)
    evidence = collect_duplicate_evidence(inventory, actions)
    manifest = tmp_path / "manifest.json"
    write_manifest(
        manifest,
        inventory,
        actions,
        evidence,
        palace_path=palace,
        sqlite_snapshot=palace_snapshot(palace),
    )
    return palace, canonical, worktree, sessions, manifest


def test_prepare_migration_copies_publishes_private_matching_bundles(tmp_path, monkeypatch):
    palace, canonical, worktree, sessions, manifest = _reviewed_fixture(tmp_path, monkeypatch)
    rollback = tmp_path / "out" / "rollback"
    migrated = tmp_path / "out" / "migrated"

    report = prepare_migration_copies(
        source_palace=palace,
        reviewed_manifest=manifest,
        rollback_root=rollback,
        migrated_root=migrated,
        canonical_root=canonical,
        worktree_roots=[worktree],
        session_roots=[sessions],
    )

    assert report["success"] is True
    assert report["inventory_total"] == 2
    assert report["duplicate_candidates"] == 1
    assert palace_snapshot(rollback / "palace") == palace_snapshot(palace)
    assert palace_snapshot(migrated / "palace") == palace_snapshot(palace)
    assert (rollback / "config.json").is_file()
    assert (rollback / "hallways.json").is_file()
    assert bundle_permissions(rollback) == []
    assert bundle_permissions(migrated) == []
    assert stat.S_IMODE((rollback / "palace").stat().st_mode) == 0o700


def test_prepare_allows_semantically_equal_chroma_json_reserialization(tmp_path, monkeypatch):
    palace, canonical, worktree, sessions, manifest = _reviewed_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(palace / "chroma.sqlite3") as conn:
        conn.execute(
            "UPDATE collections SET schema_str = ? WHERE name = 'mempalace_drawers'",
            ('{ "version": 1, "defaults": { "threads": 4, "ef": 10 } }',),
        )
    rollback = tmp_path / "out" / "rollback"
    migrated = tmp_path / "out" / "migrated"

    report = prepare_migration_copies(
        source_palace=palace,
        reviewed_manifest=manifest,
        rollback_root=rollback,
        migrated_root=migrated,
        canonical_root=canonical,
        worktree_roots=[worktree],
        session_roots=[sessions],
    )

    assert report["success"] is True
    assert palace_snapshot(rollback / "palace") == palace_snapshot(palace)


def test_prepare_version1_manifest_requires_exact_physical_snapshot(tmp_path, monkeypatch):
    palace, canonical, worktree, sessions, manifest = _reviewed_fixture(tmp_path, monkeypatch)
    reviewed = json.loads(manifest.read_text())
    reviewed["version"] = 1
    reviewed.pop("source_semantic_snapshot")
    manifest.write_text(json.dumps(reviewed) + "\n")

    prepare_migration_copies(
        source_palace=palace,
        reviewed_manifest=manifest,
        rollback_root=tmp_path / "exact" / "rollback",
        migrated_root=tmp_path / "exact" / "migrated",
        canonical_root=canonical,
        worktree_roots=[worktree],
        session_roots=[sessions],
    )

    with sqlite3.connect(palace / "chroma.sqlite3") as conn:
        conn.execute(
            "UPDATE collections SET schema_str = ? WHERE name = 'mempalace_drawers'",
            ('{ "version": 1, "defaults": { "threads": 4, "ef": 10 } }',),
        )
    with pytest.raises(ValueError, match="manifest snapshot"):
        prepare_migration_copies(
            source_palace=palace,
            reviewed_manifest=manifest,
            rollback_root=tmp_path / "drift" / "rollback",
            migrated_root=tmp_path / "drift" / "migrated",
            canonical_root=canonical,
            worktree_roots=[worktree],
            session_roots=[sessions],
        )


def test_prepare_migration_copies_rejects_snapshot_drift_and_cleans_outputs(tmp_path, monkeypatch):
    palace, canonical, worktree, sessions, manifest = _reviewed_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(palace / "chroma.sqlite3") as conn:
        conn.execute("CREATE TABLE changed_after_review(value TEXT)")
    rollback = tmp_path / "out" / "rollback"
    migrated = tmp_path / "out" / "migrated"

    with pytest.raises(ValueError, match="semantic state no longer matches"):
        prepare_migration_copies(
            source_palace=palace,
            reviewed_manifest=manifest,
            rollback_root=rollback,
            migrated_root=migrated,
            canonical_root=canonical,
            worktree_roots=[worktree],
            session_roots=[sessions],
        )

    assert rollback.exists() is False
    assert migrated.exists() is False


def test_prepare_migration_copies_rejects_existing_or_symlink_destination(tmp_path, monkeypatch):
    palace, canonical, worktree, sessions, manifest = _reviewed_fixture(tmp_path, monkeypatch)
    rollback = tmp_path / "rollback"
    rollback.mkdir()

    with pytest.raises(ValueError, match="already exists"):
        prepare_migration_copies(
            source_palace=palace,
            reviewed_manifest=manifest,
            rollback_root=rollback,
            migrated_root=tmp_path / "migrated",
            canonical_root=canonical,
            worktree_roots=[worktree],
            session_roots=[sessions],
        )


def test_prepare_failure_never_deletes_competing_destination(tmp_path, monkeypatch):
    palace, canonical, worktree, sessions, manifest = _reviewed_fixture(tmp_path, monkeypatch)
    rollback = tmp_path / "out" / "rollback"
    migrated = tmp_path / "out" / "migrated"
    real_publish = _publish_directory_no_replace
    competing_identity = None

    def race_first_publish(source, destination):
        nonlocal competing_identity
        if destination == rollback:
            rollback.mkdir()
            competing_identity = rollback.stat().st_ino
        return real_publish(source, destination)

    monkeypatch.setattr(
        "mempalace.migration_bundle._publish_directory_no_replace",
        race_first_publish,
    )

    with pytest.raises(FileExistsError):
        prepare_migration_copies(
            source_palace=palace,
            reviewed_manifest=manifest,
            rollback_root=rollback,
            migrated_root=migrated,
            canonical_root=canonical,
            worktree_roots=[worktree],
            session_roots=[sessions],
        )

    assert rollback.stat().st_ino == competing_identity
    assert list(rollback.iterdir()) == []
    assert migrated.exists() is False


def test_prepare_second_publish_failure_retains_completed_rollback(tmp_path, monkeypatch):
    palace, canonical, worktree, sessions, manifest = _reviewed_fixture(tmp_path, monkeypatch)
    rollback = tmp_path / "out" / "rollback"
    migrated = tmp_path / "out" / "migrated"
    real_publish = _publish_directory_no_replace
    competing_identity = None

    def race_second_publish(source, destination):
        nonlocal competing_identity
        if destination == migrated:
            migrated.mkdir()
            competing_identity = migrated.stat().st_ino
        return real_publish(source, destination)

    monkeypatch.setattr(
        "mempalace.migration_bundle._publish_directory_no_replace",
        race_second_publish,
    )

    with pytest.raises(FileExistsError):
        prepare_migration_copies(
            source_palace=palace,
            reviewed_manifest=manifest,
            rollback_root=rollback,
            migrated_root=migrated,
            canonical_root=canonical,
            worktree_roots=[worktree],
            session_roots=[sessions],
        )

    assert palace_snapshot(rollback / "palace") == palace_snapshot(palace)
    assert migrated.stat().st_ino == competing_identity
    assert list(migrated.iterdir()) == []


def test_atomic_publish_refuses_existing_empty_directory(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "complete").write_text("published content")
    competing_identity = destination.stat().st_ino

    with pytest.raises(FileExistsError):
        _publish_directory_no_replace(source, destination)

    assert source.is_dir()
    assert destination.stat().st_ino == competing_identity
    assert list(destination.iterdir()) == []


def test_prepare_preserves_existing_parent_permissions(tmp_path, monkeypatch):
    palace, canonical, worktree, sessions, manifest = _reviewed_fixture(tmp_path, monkeypatch)
    output = tmp_path / "shared-output"
    output.mkdir(mode=0o755)
    output.chmod(0o755)

    prepare_migration_copies(
        source_palace=palace,
        reviewed_manifest=manifest,
        rollback_root=output / "rollback",
        migrated_root=output / "migrated",
        canonical_root=canonical,
        worktree_roots=[worktree],
        session_roots=[sessions],
    )

    assert stat.S_IMODE(output.stat().st_mode) == 0o755


def test_apply_actions_deletes_only_evidenced_duplicate_and_is_idempotent():
    canonical = InventoryRecord(
        drawer_id="canonical",
        content="same",
        metadata={"wing": "se", "room": "code", "chunk_index": 0},
        origin="canonical",
        relative_identity="src/a.ts",
        source_path="/repo/src/a.ts",
    )
    duplicate = InventoryRecord(
        drawer_id="duplicate",
        content="same",
        metadata={"wing": "se", "room": "code", "chunk_index": 0},
        origin="worktree",
        relative_identity="src/a.ts",
        source_path="/worktree/src/a.ts",
    )
    unique = InventoryRecord(
        drawer_id="unique",
        content="unique verbatim",
        metadata={"wing": "se", "room": "notes", "chunk_index": 0},
        origin="worktree",
        relative_identity="notes/unique.md",
        source_path="/worktree/notes/unique.md",
    )
    inventory = [canonical, duplicate, unique]
    actions = plan_actions(inventory)
    evidence = collect_duplicate_evidence(inventory, actions)
    collection = _MemoryCollection(inventory)

    report = apply_actions_to_collection(collection, inventory, actions, evidence, batch_size=1)
    second = apply_actions_to_collection(collection, inventory, actions, evidence, batch_size=2)
    verified = verify_retained_records(collection, inventory, actions, evidence)

    assert report["records_deleted_as_verified_duplicates"] == 1
    assert second == report
    assert set(collection.rows) == {"canonical", "unique"}
    assert collection.rows["canonical"]["metadata"]["wing"] == "se-code"
    assert collection.rows["unique"]["metadata"]["wing"] == "se-sessions"
    assert collection.rows["unique"]["document"] == "unique verbatim"
    assert verified == {
        "retained_records_verified": 2,
        "verified_duplicates_absent": 1,
    }


def test_apply_actions_refuses_candidate_without_matching_evidence():
    record = InventoryRecord(
        drawer_id="worktree",
        content="content",
        metadata={"wing": "se", "chunk_index": 0},
        origin="worktree",
        relative_identity="src/a.ts",
        source_path="/worktree/src/a.ts",
    )
    actions = plan_actions([record])
    # Force an unsafe candidate action without producing proof.
    unsafe = [actions[0].__class__(**{**actions[0].__dict__, "action": "duplicate_candidate"})]

    with pytest.raises(ValueError, match="do not reconcile"):
        apply_actions_to_collection(_MemoryCollection([record]), [record], unsafe, [])


def test_apply_reviewed_migration_updates_copy_and_preserves_verbatim(tmp_path, monkeypatch):
    from mempalace.palace import get_backend_for_palace, get_collection

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    active_root = tmp_path / "active"
    palace = active_root / "palace"
    canonical = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    sessions = tmp_path / "sessions"
    for path in (canonical, worktree, sessions):
        path.mkdir(parents=True)
    collection = get_collection(str(palace), create=True, backend="chroma")
    collection.upsert(
        ids=["canonical", "duplicate", "unique"],
        documents=["same content", "same content", "unique verbatim artifact"],
        metadatas=[
            {
                "wing": "se",
                "room": "code",
                "source_file": str(canonical / "src/a.ts"),
                "chunk_index": 0,
            },
            {
                "wing": "se",
                "room": "code",
                "source_file": str(worktree / "src/a.ts"),
                "chunk_index": 0,
            },
            {
                "wing": "se",
                "room": "notes",
                "source_file": str(worktree / "notes/unique.md"),
                "chunk_index": 0,
            },
        ],
    )
    get_backend_for_palace(str(palace), explicit="chroma").close_palace(str(palace))
    active_root.mkdir(exist_ok=True)
    (active_root / "config.json").write_text('{"backend": "chroma"}\n')

    inventory = inventory_palace(
        palace,
        canonical_root=canonical,
        worktree_roots=[worktree],
        session_roots=[sessions],
    )
    actions = plan_actions(inventory)
    evidence = collect_duplicate_evidence(inventory, actions)
    manifest = tmp_path / "manifest.json"
    write_manifest(
        manifest,
        inventory,
        actions,
        evidence,
        palace_path=palace,
        sqlite_snapshot=palace_snapshot(palace),
    )
    rollback = tmp_path / "bundle" / "rollback"
    migrated = tmp_path / "bundle" / "migrated"
    prepare_migration_copies(
        source_palace=palace,
        reviewed_manifest=manifest,
        rollback_root=rollback,
        migrated_root=migrated,
        canonical_root=canonical,
        worktree_roots=[worktree],
        session_roots=[sessions],
    )

    report = apply_reviewed_migration(
        source_palace=palace,
        reviewed_manifest=manifest,
        rollback_root=rollback,
        migrated_root=migrated,
        canonical_root=canonical,
        worktree_roots=[worktree],
        session_roots=[sessions],
        batch_size=1,
    )

    assert report["status"] == "complete"
    assert report["records_before"] == 3
    assert report["records_expected_after"] == 2
    assert report["records_deleted_as_verified_duplicates"] == 1
    assert report["retained_records_verified"] == 2
    assert report["orphan_hnsw_directories_removed"] >= 1
    migrated_collection = get_collection(str(migrated / "palace"), create=False, backend="chroma")
    rows = migrated_collection.get(
        ids=["canonical", "duplicate", "unique"],
        include=["documents", "metadatas"],
    )
    assert set(rows["ids"]) == {"canonical", "unique"}
    by_id = {
        drawer_id: (document, metadata)
        for drawer_id, document, metadata in zip(rows["ids"], rows["documents"], rows["metadatas"])
    }
    assert by_id["canonical"][1]["wing"] == "se-code"
    assert by_id["unique"][0] == "unique verbatim artifact"
    assert by_id["unique"][1]["wing"] == "se-sessions"
    filtered = migrated_collection.query(
        query_texts=["same content"],
        where={"wing": "se-code"},
        n_results=1,
        include=["documents", "metadatas", "distances"],
    )
    assert filtered.ids == [["canonical"]]
    assert (migrated / "apply-report.json").is_file()
    assert bundle_permissions(migrated) == []
    get_backend_for_palace(str(migrated / "palace"), explicit="chroma").close_palace(
        str(migrated / "palace")
    )
