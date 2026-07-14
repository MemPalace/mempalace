"""Synthetic activation and rollback tests for reviewed migration bundles."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import stat
from contextlib import contextmanager

import pytest

from mempalace.migration_bundle import (
    _require_daemons_stopped,
    _validate_sidecars,
    activate_migrated_palace,
    bundle_permissions,
    drawer_logical_snapshot,
    rollback_activated_palace,
)
from mempalace.reorganize import exact_hash, palace_semantic_snapshot, palace_snapshot


def _seed_palace(path, marker):
    path.mkdir(parents=True)
    with sqlite3.connect(path / "chroma.sqlite3") as connection:
        connection.executescript(
            """
            CREATE TABLE marker(value TEXT NOT NULL);
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
            INSERT INTO collections(id, name, config_json_str, schema_str)
                VALUES (
                    2,
                    'mempalace_closets',
                    '{"a":1,"b":2}',
                    '{"defaults":{"ef":10,"threads":4},"version":1}'
                );
            INSERT INTO segments(id, collection) VALUES (1, 1), (2, 2);
            INSERT INTO embeddings(id, segment_id, embedding_id, created_at)
                VALUES (1, 1, 'drawer', '2026-07-14T00:00:00Z');
            INSERT INTO embeddings(id, segment_id, embedding_id, created_at)
                VALUES (2, 2, 'closet', '2026-07-14T00:00:00Z');
            """
        )
        connection.execute("INSERT INTO marker(value) VALUES (?)", (marker,))
        connection.execute(
            "INSERT INTO embedding_metadata(id, key, string_value) VALUES (1, ?, ?)",
            ("chroma:document", marker),
        )
        connection.execute(
            "INSERT INTO embedding_metadata(id, key, string_value) VALUES (1, 'wing', 'se')"
        )
        connection.execute(
            "INSERT INTO embedding_metadata(id, key, string_value) VALUES (2, ?, ?)",
            ("chroma:document", f"closet {marker}"),
        )
        connection.execute(
            "INSERT INTO embedding_metadata(id, key, string_value) VALUES (2, 'wing', 'se')"
        )


def _marker(path):
    with sqlite3.connect(path / "chroma.sqlite3") as connection:
        return connection.execute("SELECT value FROM marker").fetchone()[0]


def _file_snapshot(path):
    payload = path.read_bytes()
    return {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _activation_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    active = tmp_path / "active" / "palace"
    migrated = tmp_path / "bundle" / "migrated"
    previous = tmp_path / "bundle" / "previous"
    _seed_palace(active, "original")
    _seed_palace(migrated / "palace", "migrated")
    active.parent.mkdir(exist_ok=True)
    (active.parent / "config.json").write_text(
        json.dumps({"palace_path": str(active), "backend": "chroma"}) + "\n"
    )
    (active.parent / "hallways.json").write_text('[{"version":"old"}]\n')
    (active.parent / "tunnels.json").write_text('[{"version":"old"}]\n')
    (migrated / "config.json").write_text(
        json.dumps({"palace_path": str(migrated / "palace"), "backend": "chroma"}) + "\n"
    )
    (migrated / "hallways.json").write_text('[{"version":"new"}]\n')
    (migrated / "tunnels.json").write_text('[{"version":"new"}]\n')

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "palace_path_sha256": exact_hash(str(active.absolute())),
                "sqlite_snapshot": palace_snapshot(active),
                "source_semantic_snapshot": palace_semantic_snapshot(active),
                "counts": {
                    "inventory_total": 3,
                    "verified_duplicate_candidates": 1,
                },
            }
        )
        + "\n"
    )
    os.chmod(manifest, 0o600)
    apply_report = {
        "version": 1,
        "status": "complete",
        "source_palace_sha256": exact_hash(str(active.absolute())),
        "reviewed_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "records_before": 3,
        "records_expected_after": 2,
        "records_deleted_as_verified_duplicates": 1,
        "retained_records_verified": 2,
        "verified_duplicates_absent": 1,
        "sqlite_integrity": "ok",
        "drawer_vector_readiness": {"ready": True},
        "closet_vector_readiness": {"ready": True},
        "derived_sidecars": {
            "hallways.json": _file_snapshot(migrated / "hallways.json"),
            "tunnels.json": _file_snapshot(migrated / "tunnels.json"),
        },
        "migrated_logical_snapshot": drawer_logical_snapshot(migrated / "palace"),
        "migrated_semantic_snapshot": palace_semantic_snapshot(migrated / "palace"),
        "migrated_snapshot": palace_snapshot(migrated / "palace"),
    }
    (migrated / "apply-report.json").write_text(json.dumps(apply_report) + "\n")
    os.chmod(migrated / "apply-report.json", 0o600)
    monkeypatch.setattr(
        "mempalace.migration_bundle._verify_staging_readiness",
        lambda _palace: None,
    )
    return active, migrated, previous, manifest


def test_activation_and_rollback_preserve_both_palaces_and_sidecars(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)

    activated = activate_migrated_palace(
        active_palace=active,
        reviewed_manifest=manifest,
        migrated_root=migrated,
        previous_root=previous,
    )

    assert activated["status"] == "active"
    assert _marker(active) == "migrated"
    assert _marker(previous / "palace") == "original"
    assert (active.parent / "hallways.json").read_text() == '[{"version":"new"}]\n'
    assert (previous / "hallways.json").read_text() == '[{"version":"old"}]\n'
    assert (migrated / "palace").exists() is False
    assert json.loads((previous / "activation-report.json").read_text())["status"] == "active"
    assert bundle_permissions(previous) == []

    rolled_back = rollback_activated_palace(
        active_palace=active,
        migrated_root=migrated,
        previous_root=previous,
    )

    assert rolled_back["status"] == "rolled_back"
    assert _marker(active) == "original"
    assert _marker(migrated / "palace") == "migrated"
    assert (active.parent / "hallways.json").read_text() == '[{"version":"old"}]\n'
    assert (migrated / "hallways.json").read_text() == '[{"version":"new"}]\n'
    assert (previous / "palace").exists() is False
    assert json.loads((previous / "activation-report.json").read_text())["status"] == "rolled_back"
    assert bundle_permissions(previous) == []
    assert bundle_permissions(migrated) == []


def test_activation_refuses_changed_active_snapshot(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    with sqlite3.connect(active / "chroma.sqlite3") as connection:
        connection.execute("UPDATE marker SET value='changed'")

    with pytest.raises(ValueError, match="active palace no longer matches"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )

    assert _marker(active) == "changed"
    assert _marker(migrated / "palace") == "migrated"
    assert previous.exists() is False


def test_activation_allows_semantically_equal_active_chroma_json_reserialization(
    tmp_path, monkeypatch
):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    reviewed = json.loads(manifest.read_text())
    with sqlite3.connect(active / "chroma.sqlite3") as connection:
        connection.execute(
            "UPDATE collections SET schema_str = ? WHERE name = 'mempalace_drawers'",
            ('{ "version": 1, "defaults": { "threads": 4, "ef": 10 } }',),
        )

    assert palace_snapshot(active) != reviewed["sqlite_snapshot"]
    assert palace_semantic_snapshot(active) == reviewed["source_semantic_snapshot"]

    activated = activate_migrated_palace(
        active_palace=active,
        reviewed_manifest=manifest,
        migrated_root=migrated,
        previous_root=previous,
    )

    assert activated["status"] == "active"
    assert _marker(active) == "migrated"
    assert _marker(previous / "palace") == "original"


def test_activation_refuses_running_daemon(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    from mempalace import daemon

    monkeypatch.setattr(daemon, "get_client_if_running", lambda *args, **kwargs: object())

    with pytest.raises(RuntimeError, match="daemon must be stopped"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )

    assert _marker(active) == "original"
    assert _marker(migrated / "palace") == "migrated"
    assert previous.exists() is False


def test_activation_refuses_daemon_targeting_staged_palace(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    from mempalace import daemon

    staged = str(migrated / "palace")
    monkeypatch.setattr(
        daemon,
        "get_client_if_running",
        lambda palace_path, **kwargs: object() if palace_path == staged else None,
    )

    with pytest.raises(RuntimeError, match="daemon must be stopped"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )

    assert _marker(active) == "original"
    assert _marker(migrated / "palace") == "migrated"


def test_activation_binds_completed_report_and_sidecars(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    report_path = migrated / "apply-report.json"
    report = json.loads(report_path.read_text())
    report["reviewed_manifest_sha256"] = "wrong"
    report_path.write_text(json.dumps(report) + "\n")

    with pytest.raises(ValueError, match="different reviewed manifest"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )

    report["reviewed_manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    report_path.write_text(json.dumps(report) + "\n")
    (migrated / "hallways.json").write_text('[{"version":"tampered"}]\n')
    with pytest.raises(ValueError, match="sidecars no longer match"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )


def test_activation_allows_chroma_json_reserialization_when_semantics_match(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    report = json.loads((migrated / "apply-report.json").read_text())
    with sqlite3.connect(migrated / "palace" / "chroma.sqlite3") as connection:
        connection.execute(
            "UPDATE collections SET schema_str = ? WHERE name = 'mempalace_drawers'",
            ('{ "version": 1, "defaults": { "threads": 4, "ef": 10 } }',),
        )
    assert palace_snapshot(migrated / "palace") != report["migrated_snapshot"]
    assert drawer_logical_snapshot(migrated / "palace") == report["migrated_logical_snapshot"]
    assert palace_semantic_snapshot(migrated / "palace") == report["migrated_semantic_snapshot"]

    activated = activate_migrated_palace(
        active_palace=active,
        reviewed_manifest=manifest,
        migrated_root=migrated,
        previous_root=previous,
    )

    assert activated["status"] == "active"
    assert _marker(active) == "migrated"


def test_activation_rejects_closet_content_drift(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    report = json.loads((migrated / "apply-report.json").read_text())
    with sqlite3.connect(migrated / "palace" / "chroma.sqlite3") as connection:
        connection.execute(
            "UPDATE embedding_metadata SET string_value = 'tampered closet' "
            "WHERE id = 2 AND key = 'chroma:document'"
        )

    assert drawer_logical_snapshot(migrated / "palace") == report["migrated_logical_snapshot"]
    assert palace_semantic_snapshot(migrated / "palace") != report["migrated_semantic_snapshot"]

    with pytest.raises(ValueError, match="semantic snapshot"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )


def test_daemon_stop_check_uses_platform_safe_pid_probe(tmp_path, monkeypatch):
    from mempalace import daemon

    palace = tmp_path / "palace"
    palace.mkdir()
    monkeypatch.setenv(daemon.STATE_ROOT_ENV, str(tmp_path / "daemon-state"))
    marker = daemon.pid_path(str(palace))
    marker.parent.mkdir(parents=True)
    marker.write_text("4242")
    monkeypatch.setattr(daemon, "get_client_if_running", lambda *_args, **_kwargs: None)
    probed = []

    def safe_probe(pid):
        probed.append(pid)
        return True

    monkeypatch.setattr(daemon, "_pid_alive", safe_probe)

    with pytest.raises(RuntimeError, match="PID 4242 is still alive"):
        _require_daemons_stopped([palace])

    assert probed == [4242]


def test_sidecar_revalidation_rejects_new_supported_sidecar(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    expected = _validate_sidecars(root, ("hallways.json", "tunnels.json"))
    (root / "hallways.json").write_text("[]\n")

    with pytest.raises(ValueError, match="sidecars changed"):
        _validate_sidecars(
            root,
            ("hallways.json", "tunnels.json"),
            expected=expected,
        )


def test_activation_and_rollback_lock_every_palace_path(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    locked = []

    @contextmanager
    def recording_lock(path, *, blocking=False):
        locked.append(os.path.abspath(path))
        yield

    monkeypatch.setattr("mempalace.migration_bundle.mine_palace_lock", recording_lock)

    activate_migrated_palace(
        active_palace=active,
        reviewed_manifest=manifest,
        migrated_root=migrated,
        previous_root=previous,
    )
    assert set(locked) == {str(active.absolute()), str((migrated / "palace").absolute())}

    locked.clear()
    rollback_activated_palace(
        active_palace=active,
        migrated_root=migrated,
        previous_root=previous,
    )
    assert set(locked) == {
        str(active.absolute()),
        str((migrated / "palace").absolute()),
        str((previous / "palace").absolute()),
    }


def test_rollback_refuses_tampered_sidecar_symlink(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    activate_migrated_palace(
        active_palace=active,
        reviewed_manifest=manifest,
        migrated_root=migrated,
        previous_root=previous,
    )
    target = tmp_path / "unrelated.json"
    target.write_text("do not chmod or promote\n")
    (previous / "hallways.json").unlink()
    (previous / "hallways.json").symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        rollback_activated_palace(
            active_palace=active,
            migrated_root=migrated,
            previous_root=previous,
        )

    assert _marker(active) == "migrated"
    assert _marker(previous / "palace") == "original"


def test_activation_compensates_when_staging_promotion_fails(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    real_replace = os.replace

    def fail_staging_promotion(source, destination):
        if os.fspath(source) == os.fspath(migrated / "palace") and os.fspath(
            destination
        ) == os.fspath(active):
            raise OSError("synthetic promotion failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_staging_promotion)

    with pytest.raises(OSError, match="synthetic promotion failure"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )

    assert _marker(active) == "original"
    assert _marker(migrated / "palace") == "migrated"
    assert (active.parent / "hallways.json").read_text() == '[{"version":"old"}]\n'
    assert (migrated / "hallways.json").read_text() == '[{"version":"new"}]\n'
    assert previous.exists() is False


def test_activation_recovers_after_uncatchable_mid_swap_exit(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    real_replace = os.replace
    failed = False

    def crash_during_staging_promotion(source, destination):
        nonlocal failed
        if (
            not failed
            and os.fspath(source) == os.fspath(migrated / "palace")
            and os.fspath(destination) == os.fspath(active)
        ):
            failed = True
            raise SystemExit("synthetic process death")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", crash_during_staging_promotion)
    with pytest.raises(SystemExit, match="synthetic process death"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )

    journal = previous.parent / f".{previous.name}.activation-journal.json"
    assert journal.is_file()
    monkeypatch.setattr(os, "replace", real_replace)

    recovered = activate_migrated_palace(
        active_palace=active,
        reviewed_manifest=manifest,
        migrated_root=migrated,
        previous_root=previous,
    )

    assert recovered["status"] == "active"
    assert _marker(active) == "migrated"
    assert _marker(previous / "palace") == "original"
    assert journal.exists() is False


def test_rollback_recovers_after_uncatchable_mid_swap_exit(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    activate_migrated_palace(
        active_palace=active,
        reviewed_manifest=manifest,
        migrated_root=migrated,
        previous_root=previous,
    )
    real_replace = os.replace
    failed = False

    def crash_during_previous_promotion(source, destination):
        nonlocal failed
        if (
            not failed
            and os.fspath(source) == os.fspath(previous / "palace")
            and os.fspath(destination) == os.fspath(active)
        ):
            failed = True
            raise SystemExit("synthetic process death")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", crash_during_previous_promotion)
    with pytest.raises(SystemExit, match="synthetic process death"):
        rollback_activated_palace(
            active_palace=active,
            migrated_root=migrated,
            previous_root=previous,
        )

    journal = previous.parent / f".{previous.name}.activation-journal.json"
    assert journal.is_file()
    monkeypatch.setattr(os, "replace", real_replace)

    recovered = rollback_activated_palace(
        active_palace=active,
        migrated_root=migrated,
        previous_root=previous,
    )

    assert recovered["status"] == "rolled_back"
    assert _marker(active) == "original"
    assert _marker(migrated / "palace") == "migrated"
    assert journal.exists() is False


def test_activation_refuses_existing_previous_or_symlinked_staging(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    previous.mkdir()
    with pytest.raises(ValueError, match="previous activation slot already exists"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )

    previous.rmdir()
    staged_palace = migrated / "palace"
    real_palace = migrated / "real-palace"
    staged_palace.rename(real_palace)
    staged_palace.symlink_to(real_palace, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )


def test_activation_preflight_failure_cleans_temp_without_chmodding_parent(tmp_path, monkeypatch):
    active, migrated, previous, manifest = _activation_fixture(tmp_path, monkeypatch)
    os.chmod(previous.parent, 0o755)
    monkeypatch.setattr(
        "mempalace.migration_bundle.shutil.copy2",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic copy failure")),
    )

    with pytest.raises(OSError, match="synthetic copy failure"):
        activate_migrated_palace(
            active_palace=active,
            reviewed_manifest=manifest,
            migrated_root=migrated,
            previous_root=previous,
        )

    assert stat.S_IMODE(previous.parent.stat().st_mode) == 0o755
    assert list(previous.parent.glob(f".{previous.name}.*.tmp")) == []
    assert _marker(active) == "original"
    assert _marker(migrated / "palace") == "migrated"
